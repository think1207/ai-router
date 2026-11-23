"""Tests for `agy` ACP mode, copilot/codex adapters, and usability features A–G."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any
from unittest import mock

from ai_router import acp, cli, config, credits, dispatch, files, router, tools
from ai_router.errors import AiRouteError


FAKE_ACP_SERVER_SCRIPT = textwrap.dedent(
    """\
    import json, sys

    recorded = []
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        msg = json.loads(raw)
        req_id = msg.get("id")
        method = msg.get("method")
        params = msg.get("params") or {}
        if method is None:
            # response to our server->client request_permission
            recorded.append({"perm_response": msg.get("result")})
            continue
        recorded.append({"method": method, "params": params})

        if method == "initialize":
            resp = {"jsonrpc": "2.0", "id": req_id, "result": {"protocolVersion": 1, "agentInfo": {"name": "FakeACP"}}}
        elif method == "session/new":
            resp = {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "sessionId": "acp-sess-1",
                    "models": {
                        "availableModels": [
                            {"modelId": "gemini-4-neon-high"},
                            {"modelId": "gemini-4-neon-xhigh"},
                            {"modelId": "gemini-3.8-flash-medium"},
                        ]
                    },
                    "modes": {
                        "currentModeId": "default",
                        "availableModes": [{"id": "default"}, {"id": "auto"}, {"id": "plan"}],
                    },
                    "configOptions": [
                        {
                            "id": "thought_level",
                            "category": "thought_level",
                            "options": [{"value": "low"}, {"value": "medium"}, {"value": "high"}, {"value": "xhigh"}],
                        }
                    ],
                },
            }
        elif method in ("session/set_mode", "session/set_model", "session/set_config_option"):
            resp = {"jsonrpc": "2.0", "id": req_id, "result": {}}
        elif method == "session/prompt":
            prompt_text = "".join(b.get("text", "") for b in params.get("prompt", []) if isinstance(b, dict))
            if "TRIGGER_CREDIT_ERROR" in prompt_text:
                resp = {"jsonrpc": "2.0", "id": req_id, "error": {"code": 429, "message": "credit balance is too low"}}
            else:
                # Ask for a permission first to verify auto vs read_only handling
                perm_req = {
                    "jsonrpc": "2.0",
                    "id": 999,
                    "method": "session/request_permission",
                    "params": {
                        "toolCall": {"title": "Edit app.py"},
                        "options": [
                            {"optionId": "opt-allow", "kind": "allow_once", "name": "Allow"},
                            {"optionId": "opt-reject", "kind": "reject_once", "name": "Reject"},
                        ],
                    },
                }
                sys.stdout.write(json.dumps(perm_req) + "\\n")
                sys.stdout.flush()
                perm_reply = json.loads(sys.stdin.readline())
                recorded.append({"perm_outcome": perm_reply.get("result")})

                # Stream two chunks back
                for chunk in ("ACP_OK: ", json.dumps(recorded)):
                    upd = {
                        "jsonrpc": "2.0",
                        "method": "session/update",
                        "params": {
                            "sessionId": "acp-sess-1",
                            "update": {
                                "sessionUpdate": "agent_message_chunk",
                                "content": {"type": "text", "text": chunk},
                            },
                        },
                    }
                    sys.stdout.write(json.dumps(upd) + "\\n")
                    sys.stdout.flush()
                resp = {"jsonrpc": "2.0", "id": req_id, "result": {"stopReason": "end_turn"}}
        else:
            resp = {"jsonrpc": "2.0", "id": req_id, "result": {}}

        sys.stdout.write(json.dumps(resp) + "\\n")
        sys.stdout.flush()
    """
)


def _git(args: list[str], cwd: Path) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def _init_repo(tmp: Path) -> Path:
    repo = tmp / "repo"
    repo.mkdir()
    _git(["init", "-b", "main"], repo)
    _git(["config", "user.email", "test@example.com"], repo)
    _git(["config", "user.name", "Test User"], repo)
    (repo / "app.txt").write_text("base\n", encoding="utf-8")
    _git(["add", "app.txt"], repo)
    _git(["commit", "-m", "initial"], repo)
    return repo


# ---------------------------------------------------------------------------
# 1. ACP Client & `agy` ACP-only execution
# ---------------------------------------------------------------------------


class TestAgyACPMode(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.server_script = self.tmp / "fake_acp.py"
        self.server_script.write_text(FAKE_ACP_SERVER_SCRIPT, encoding="utf-8")
        self.fake_cmd = [sys.executable, str(self.server_script)]
        self.addCleanup(self._tmp.cleanup)

    def test_acp_headless_auto_mode_configures_model_effort_and_allows_permissions(self) -> None:
        rc, output, _lat = acp.run_acp_headless_once(
            self.fake_cmd,
            "gemini-4-neon-high",
            "Implement feature",
            self.tmp,
            effort="xhigh",
            acp_mode="auto",
            read_only=False,
        )
        self.assertEqual(rc, 0)
        self.assertTrue(output.startswith("ACP_OK: "))
        recorded = json.loads(output.removeprefix("ACP_OK: "))
        methods = [r.get("method") for r in recorded if "method" in r]
        self.assertIn("initialize", methods)
        self.assertIn("session/new", methods)
        self.assertIn("session/set_mode", methods)
        self.assertIn("session/set_model", methods)
        self.assertIn("session/set_config_option", methods)

        set_mode = next(r["params"] for r in recorded if r.get("method") == "session/set_mode")
        self.assertEqual(set_mode["modeId"], "auto")
        # Effort xhigh resolved compound modelId gemini-4-neon-xhigh
        set_model = next(r["params"] for r in recorded if r.get("method") == "session/set_model")
        self.assertEqual(set_model["modelId"], "gemini-4-neon-xhigh")
        set_cfg = next(r["params"] for r in recorded if r.get("method") == "session/set_config_option")
        self.assertEqual(set_cfg["configId"], "thought_level")
        self.assertEqual(set_cfg["value"], "xhigh")
        # Permission auto-allowed in auto mode
        perm = next(r["perm_outcome"] for r in recorded if "perm_outcome" in r)
        self.assertEqual(perm["outcome"]["optionId"], "opt-allow")

    def test_acp_headless_read_only_uses_plan_mode_and_rejects_permissions(self) -> None:
        rc, output, _ = acp.run_acp_headless_once(
            self.fake_cmd,
            "gemini-3.8-flash-medium",
            "Review code",
            self.tmp,
            acp_mode="auto",
            read_only=True,
        )
        self.assertEqual(rc, 0)
        recorded = json.loads(output.removeprefix("ACP_OK: "))
        set_mode = next(r["params"] for r in recorded if r.get("method") == "session/set_mode")
        self.assertEqual(set_mode["modeId"], "plan")
        perm = next(r["perm_outcome"] for r in recorded if "perm_outcome" in r)
        self.assertEqual(perm["outcome"]["optionId"], "opt-reject")

    def test_acp_error_integrates_with_credit_exhausted_detection(self) -> None:
        rc, output, _ = acp.run_acp_headless_once(
            self.fake_cmd,
            "gemini-4-neon-high",
            "TRIGGER_CREDIT_ERROR",
            self.tmp,
        )
        self.assertEqual(rc, 1)
        self.assertIn("credit balance is too low", output)
        self.assertIsNotNone(credits.detect_credit_exhausted(rc, output))

    def test_dispatch_always_uses_acp_for_agy(self) -> None:
        pf = Path("/tmp/prompt.md")
        cmd_i = dispatch.build_agent_shell_cmd("agy", "gemini-4-neon-high", pf, "interactive", effort="high")
        self.assertIn("_acp-session", cmd_i)
        self.assertIn("--tool agy", cmd_i)
        self.assertIn("--acp-mode auto", cmd_i)
        self.assertIn("--effort high", cmd_i)

        cmd_h = dispatch.build_agent_shell_cmd("agy", "gemini-4-neon-high", pf, "headless")
        self.assertIn("_acp-session", cmd_h)
        self.assertIn("--mode headless", cmd_h)

        # Headless dispatch routes `agy` to run_acp_headless_once (never direct `agy --model`)
        with mock.patch.object(dispatch, "run_acp_headless_once", return_value=(0, "ok", 5)) as m_acp:
            rc, out, _ = dispatch.run_agent_headless_once("agy", "gemini-4-neon-high", "hello", self.tmp, effort="max")
        self.assertEqual((rc, out), (0, "ok"))
        m_acp.assert_called_once()
        self.assertEqual(m_acp.call_args.args[0], ["agy", "acp", "--stdio"])
        self.assertEqual(m_acp.call_args.kwargs["acp_mode"], "auto")


# ---------------------------------------------------------------------------
# 2. Copilot & Codex Adapters
# ---------------------------------------------------------------------------


class TestCopilotAndCodexAdapters(unittest.TestCase):
    def test_copilot_shell_and_headless_commands(self) -> None:
        pf = Path("/tmp/p.md")
        cmd_i = dispatch.build_agent_shell_cmd("copilot", "gpt-5.6-sol", pf, "interactive", effort="high")
        self.assertIn("copilot --model gpt-5.6-sol --reasoning-effort high -i", cmd_i)

        cmd_h = dispatch.build_agent_shell_cmd("copilot", "gpt-5.6-sol", pf, "headless", effort="max")
        self.assertIn("copilot --model gpt-5.6-sol --reasoning-effort max --allow-all-tools -s -p", cmd_h)

        argv = tools.build_tool_headless_argv("copilot", "gpt-5.6-sol", "Fix", effort="medium")
        self.assertEqual(
            argv,
            ["copilot", "--model", "gpt-5.6-sol", "--reasoning-effort", "medium", "--allow-all-tools", "-s", "-p", "Fix"],
        )

    def test_codex_shell_and_headless_commands_with_effort_clamping(self) -> None:
        pf = Path("/tmp/p.md")
        cmd_i = dispatch.build_agent_shell_cmd("codex", "gpt-6.1-sol", pf, "interactive", effort="high")
        self.assertIn("codex -m gpt-6.1-sol -c model_reasoning_effort=high", cmd_i)

        # `max` is clamped to `xhigh` for codex
        cmd_h = dispatch.build_agent_shell_cmd("codex", "gpt-6.1-sol", pf, "headless", effort="max")
        self.assertIn("codex exec -m gpt-6.1-sol -c model_reasoning_effort=xhigh", cmd_h)

        argv = tools.build_tool_headless_argv("codex", "gpt-6.1-sol", "Refactor", effort="max")
        self.assertEqual(argv, ["codex", "exec", "-m", "gpt-6.1-sol", "-c", "model_reasoning_effort=xhigh", "Refactor"])


# ---------------------------------------------------------------------------
# 3. Usability Features A–G
# ---------------------------------------------------------------------------


class TestUsabilityFeaturesAG(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name).resolve()
        self.log = self.tmp / "data" / "history.jsonl"
        self._env = mock.patch.dict(os.environ, {"AI_ROUTE_LOG_FILE": str(self.log)})
        self._env.start()
        self.addCleanup(self._env.stop)
        self.addCleanup(self._tmp.cleanup)

    def test_a_auto_detect_excludes_uninstalled_tools_in_routing(self) -> None:
        def fake_which(name: str) -> str | None:
            return f"/usr/bin/{name}" if name in {"opencode", "copilot"} else None

        seen_prompts: list[str] = []

        def fake_run(cmd: list[str], **_kw: Any) -> subprocess.CompletedProcess[str]:
            seen_prompts.append(cmd[-1])
            return subprocess.CompletedProcess(
                cmd,
                0,
                stdout=json.dumps(
                    {
                        "difficulty": "medium",
                        "effort": "medium",
                        "tool": "copilot",
                        "model": "gpt-5.6-sol",
                        "branch_slug": "cop-task",
                        "reason": "copilot installed",
                        "refined_prompt": "Do it",
                    }
                ),
                stderr="",
            )

        with mock.patch("shutil.which", side_effect=fake_which), mock.patch.object(
            router, "run_cmd", side_effect=fake_run
        ):
            decision = router.route_task("Build feature", "main", "(none)", plan_first=False)

        self.assertEqual(decision.tool, "copilot")
        self.assertIn('"copilot"', seen_prompts[0])
        self.assertNotIn('"codex"', seen_prompts[0])
        self.assertNotIn('"claude"', seen_prompts[0])

    def test_b_multi_service_router_works_without_opencode(self) -> None:
        def fake_which(name: str) -> str | None:
            return f"/usr/bin/{name}" if name in {"copilot", "claude"} else None

        cfg = config.load_config_from_dict({})
        with mock.patch("shutil.which", side_effect=fake_which):
            choice = router.resolve_router_service(cfg)
        self.assertIsNotNone(choice)
        assert choice is not None
        self.assertEqual(choice[0], "copilot")

        fake_json = json.dumps(
            {
                "difficulty": "high",
                "effort": "high",
                "tool": "claude",
                "model": "opus",
                "branch_slug": "arch",
                "reason": "hard",
                "refined_prompt": "Solve",
            }
        )
        with mock.patch("shutil.which", side_effect=fake_which), mock.patch.object(
            router, "run_agent_headless_once", return_value=(0, fake_json, 20)
        ) as m_hl:
            decision = router.route_task("Hard task", "main", "(none)", plan_first=False, config=cfg)
        m_hl.assert_called_once()
        self.assertEqual(m_hl.call_args.args[0], "copilot")
        self.assertTrue(m_hl.call_args.kwargs["read_only"])
        self.assertEqual(decision.tool, "claude")
        self.assertEqual(decision.effort, "high")

    def test_c_config_defaults_section_reflected_in_cli_parser(self) -> None:
        cfg = config.load_config_from_dict(
            {
                "defaults": {
                    "worktree": True,
                    "auto": True,
                    "split": True,
                    "plan_first": False,
                    "preset": "strong",
                    "effort": "high",
                    "acp_mode": "accept-edits",
                }
            }
        )
        self.assertEqual(cfg.agy_acp_mode, "accept-edits")
        parser = cli.build_parser(cfg)
        args = parser.parse_args(["run", "Fix bug"])
        self.assertTrue(args.worktree)
        self.assertTrue(args.auto)
        self.assertTrue(args.split)
        self.assertTrue(args.no_plan_first)

        # CLI --no-worktree overrides defaults.worktree
        args_no_wt = parser.parse_args(["run", "--no-worktree", "Fix bug"])
        self.assertFalse(args_no_wt.worktree)

        rev_args = parser.parse_args(["review"])
        self.assertEqual(rev_args.preset, "strong")

    def test_d_piped_stdin_auto_detected_without_task_file_flag(self) -> None:
        fake_stdin = io.StringIO("Fix null pointer in parser.py\n")
        with mock.patch.object(sys, "stdin", fake_stdin):
            text = files.load_task_text(None, None)
        self.assertEqual(text, "Fix null pointer in parser.py")

    def test_e_init_generates_valid_config_and_doctor_runs(self) -> None:
        out_path = self.tmp / "generated.toml"
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.main(["init", "--output", str(out_path)])
        self.assertTrue(out_path.is_file())
        loaded = config.load_config(out_path)
        self.assertEqual(loaded.agy_acp_mode, "auto")
        self.assertTrue(loaded.candidates)

        doc_buf = io.StringIO()
        with redirect_stdout(doc_buf):
            cli.main(["doctor"])
        self.assertIn("ai-route Environment Diagnostics", doc_buf.getvalue())
        self.assertIn("[ACP]", doc_buf.getvalue())

        chk_buf = io.StringIO()
        with redirect_stdout(chk_buf):
            cli.main(["status", "--check"])
        self.assertIn("ai-route Environment Diagnostics", chk_buf.getvalue())

    def test_f_done_merges_and_cleans_up_worktree(self) -> None:
        repo = _init_repo(self.tmp)
        wt = self.tmp / "repo-worktrees" / "feat-1"
        wt.parent.mkdir(parents=True, exist_ok=True)
        _git(["worktree", "add", "-b", "ai/feat-1", str(wt), "HEAD"], repo)
        (wt / "new.txt").write_text("hello\n", encoding="utf-8")
        _git(["add", "new.txt"], wt)
        _git(["commit", "-m", "add new.txt"], wt)

        orig = Path.cwd()
        try:
            os.chdir(wt)
            # Dry-run PR preview
            buf_pr = io.StringIO()
            with redirect_stdout(buf_pr):
                cli.main(["done", "--pr", "--dry-run"])
            self.assertIn("git push -u origin ai/feat-1", buf_pr.getvalue())
            self.assertIn("gh pr create", buf_pr.getvalue())

            # Actual merge + test + cleanup
            os.chdir(repo)
            buf_merge = io.StringIO()
            with redirect_stdout(buf_merge):
                cli.main(["done", str(wt), "--test-cmd", "true"])
            self.assertTrue((repo / "new.txt").is_file())
            self.assertFalse(wt.exists())
            branches = _git(["branch", "--list", "ai/feat-1"], repo)
            self.assertEqual(branches, "")
        finally:
            os.chdir(orig)

    def test_g_shell_completions_for_bash_zsh_fish(self) -> None:
        for shell_name, marker in (
            ("bash", "_ai_route_complete"),
            ("zsh", "#compdef ai-route"),
            ("fish", "complete -c ai-route"),
        ):
            buf = io.StringIO()
            with redirect_stdout(buf):
                cli.main(["--completions", shell_name])
            script = buf.getvalue()
            self.assertIn(marker, script)
            self.assertIn("resume", script)
            self.assertIn("done", script)
            self.assertIn("copilot", script)
