"""Tests for effort estimation, credit-exhaustion failover, and CLI usability improvements."""

from __future__ import annotations

import datetime
import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any
from unittest import mock

from ai_router import cli, config, credits, dispatch, flow, router, sessions, storage, twostage
from ai_router.commands import session as session_cmd
from ai_router.errors import AiRouteError, ConfigError


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


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
# 1. Effort estimation & propagation
# ---------------------------------------------------------------------------


class TestEffortEstimation(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.log = Path(self._tmp.name) / "history.jsonl"
        self._env = mock.patch.dict(os.environ, {"AI_ROUTE_LOG_FILE": str(self.log)})
        self._env.start()
        self.addCleanup(self._env.stop)
        self.addCleanup(self._tmp.cleanup)

    def test_router_prompt_requests_effort_and_dynamic_tools(self) -> None:
        prompt = router.build_router_prompt("Refactor parser", "main", "(none)")
        self.assertIn('"effort"', prompt)
        self.assertIn("xhigh", prompt)
        self.assertIn("max", prompt)

        only_agy = [c for c in config.DEFAULT_CANDIDATES if c["tool"] == "agy"]
        prompt_agy = router.build_router_prompt("Task", "main", "(none)", candidates=only_agy)
        self.assertIn('"tool": "agy"', prompt_agy)
        self.assertNotIn('"claude"', prompt_agy)

    def test_route_task_parses_effort_and_falls_back_on_invalid(self) -> None:
        raw = json.dumps(
            {
                "difficulty": "high",
                "effort": "xhigh",
                "tool": "claude",
                "model": "opus",
                "branch_slug": "deep-arch",
                "reason": "complex architecture",
                "refined_prompt": "Design carefully.",
            }
        )
        proc = subprocess.CompletedProcess(args=[], returncode=0, stdout=raw, stderr="")
        with mock.patch.object(router, "run_cmd", return_value=proc):
            decision = router.route_task("Design arch", "main", "(none)", plan_first=False)
        self.assertEqual(decision.difficulty, "high")
        self.assertEqual(decision.effort, "xhigh")
        self.assertEqual(decision.tool, "claude")
        self.assertEqual(decision.model, "opus")

        # Invalid/missing effort defaults to difficulty level
        raw_bad = json.dumps(
            {
                "difficulty": "low",
                "effort": "turbo",
                "tool": "opencode",
                "model": "opencode-go/deepseek-v4.1-flash",
                "branch_slug": "typo",
                "reason": "typo",
                "refined_prompt": "Fix typo.",
            }
        )
        proc_bad = subprocess.CompletedProcess(args=[], returncode=0, stdout=raw_bad, stderr="")
        with mock.patch.object(router, "run_cmd", return_value=proc_bad):
            decision_bad = router.route_task("Fix typo", "main", "(none)", plan_first=False)
        self.assertEqual(decision_bad.effort, "low")

    def test_config_candidate_effort_validation(self) -> None:
        cfg = config.load_config_from_dict(
            {
                "candidates": [
                    {
                        "tool": "claude",
                        "model": "opus",
                        "difficulty": "high",
                        "effort": "xhigh",
                        "description": "Deep reasoning",
                    }
                ]
            }
        )
        opus = next(c for c in cfg.candidates if c["tool"] == "claude" and c["model"] == "opus")
        self.assertEqual(opus["effort"], "xhigh")

        with self.assertRaises(ConfigError):
            config.load_config_from_dict(
                {
                    "candidates": [
                        {
                            "tool": "claude",
                            "model": "opus",
                            "difficulty": "high",
                            "effort": "ultra",
                            "description": "Invalid effort",
                        }
                    ]
                }
            )

    def test_dispatch_passes_effort_to_claude_and_agy_not_opencode(self) -> None:
        pf = Path("/tmp/prompt.md")
        self.assertIn(
            "claude --model opus --effort xhigh",
            dispatch.build_agent_shell_cmd("claude", "opus", pf, "interactive", effort="xhigh"),
        )
        self.assertIn(
            "claude --model opus --effort high -p",
            dispatch.build_agent_shell_cmd("claude", "opus", pf, "headless", effort="high"),
        )
        agy_i = dispatch.build_agent_shell_cmd("agy", "gemini-4-neon-high", pf, "interactive", effort="max")
        self.assertIn("_acp-session --tool agy --model gemini-4-neon-high", agy_i)
        self.assertIn("--effort xhigh", agy_i)
        agy_h = dispatch.build_agent_shell_cmd("agy", "gemini-4-neon-high", pf, "headless", effort="medium")
        self.assertIn("_acp-session --tool agy --model gemini-4-neon-high --mode headless", agy_h)
        self.assertIn("--effort medium", agy_h)
        oc_cmd = dispatch.build_agent_shell_cmd(
            "opencode", "opencode-go/deepseek-v4.1-flash", pf, "headless", effort="high"
        )
        self.assertNotIn("--effort", oc_cmd)

        # Headless command array (non-ACP tools via run_cmd, agy via ACP runner)
        seen_cmds: list[list[str]] = []

        def fake_run(cmd: list[str], **_kw: Any) -> subprocess.CompletedProcess[str]:
            seen_cmds.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

        with mock.patch.object(dispatch, "run_cmd", side_effect=fake_run), mock.patch.object(
            dispatch, "run_acp_headless_once", return_value=(0, "ok", 5)
        ) as m_acp:
            dispatch.run_agent_headless_once("claude", "sonnet", "hi", Path.cwd(), effort="high")
            dispatch.run_agent_headless_once("agy", "gemini-4-neon-high", "hi", Path.cwd(), effort="xhigh")
            dispatch.run_agent_headless_once("opencode", "opencode-go/deepseek-v4.1-flash", "hi", Path.cwd(), effort="high")

        self.assertEqual(seen_cmds[0], ["claude", "--model", "sonnet", "--effort", "high", "-p", "hi"])
        m_acp.assert_called_once()
        self.assertEqual(m_acp.call_args.kwargs["effort"], "xhigh")
        self.assertNotIn("--effort", seen_cmds[1])

    def test_twostage_plan_extracts_implementer_effort(self) -> None:
        plan, valid = twostage.extract_plan(
            json.dumps(
                {
                    "decisions": ["Use cache"],
                    "constraints": [],
                    "open_questions": [],
                    "implementation_steps": ["Add cache.py"],
                    "implementer_difficulty": "medium",
                    "implementer_effort": "high",
                }
            )
        )
        self.assertTrue(valid)
        self.assertEqual(plan["implementer_effort"], "high")


# ---------------------------------------------------------------------------
# 2. Credit exhaustion detection, cooldown, and cross-service failover
# ---------------------------------------------------------------------------


class TestCreditExhaustionAndFailover(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name).resolve()
        self.repo = _init_repo(self.tmp)
        self.log = self.tmp / "data" / "history.jsonl"
        self._orig_cwd = Path.cwd()
        os.chdir(self.repo)
        self._env = mock.patch.dict(os.environ, {"AI_ROUTE_LOG_FILE": str(self.log)})
        self._env.start()
        self.addCleanup(self._env.stop)
        self.addCleanup(self._cleanup)

    def _cleanup(self) -> None:
        os.chdir(self._orig_cwd)
        self._tmp.cleanup()

    def test_detect_credit_exhausted_patterns(self) -> None:
        self.assertIsNotNone(credits.detect_credit_exhausted(1, "Error: Your credit balance is too low."))
        self.assertIsNotNone(credits.detect_credit_exhausted(1, "429 Too Many Requests: insufficient_quota"))
        self.assertIsNotNone(credits.detect_credit_exhausted(1, "You have hit your usage limit for today"))
        self.assertIsNotNone(credits.detect_credit_exhausted(1, "エラー: クレジット残高が不足しています"))
        # Clean exit (rc=0) never falsely flags normal discussion of rate limits
        self.assertIsNone(credits.detect_credit_exhausted(0, "Added retry logic for 429 credit balance errors."))
        # Regular syntax/test error is not credit exhaustion
        self.assertIsNone(credits.detect_credit_exhausted(1, "AssertionError: expected 1 == 2"))

    def test_provider_state_persistence_and_expiry(self) -> None:
        credits.mark_tool_exhausted("claude", "credit balance is too low", model="opus", cooldown_sec=600)
        active = credits.get_exhausted_tools()
        self.assertIn("claude", active)
        self.assertEqual(active["claude"]["reason"], "credit balance is too low")

        # Expired cooldown is automatically ignored
        future = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=700)
        self.assertEqual(credits.get_exhausted_tools(now=future), {})

        cleared = credits.clear_tool_exhausted("claude")
        self.assertEqual(cleared, ["claude"])
        self.assertEqual(credits.get_exhausted_tools(), {})

    def test_router_excludes_exhausted_service_from_candidates(self) -> None:
        credits.mark_tool_exhausted("claude", "out of credits", cooldown_sec=600)
        seen_prompts: list[str] = []

        def fake_run(cmd: list[str], **_kw: Any) -> subprocess.CompletedProcess[str]:
            seen_prompts.append(cmd[-1])
            return subprocess.CompletedProcess(
                cmd,
                0,
                stdout=json.dumps(
                    {
                        "difficulty": "high",
                        "effort": "high",
                        "tool": "agy",
                        "model": "gemini-4-neon-high",
                        "branch_slug": "alt-service",
                        "reason": "claude unavailable",
                        "refined_prompt": "Solve with agy.",
                    }
                ),
                stderr="",
            )

        with mock.patch.object(router, "run_cmd", side_effect=fake_run), redirect_stderr(io.StringIO()):
            decision = router.route_task("Big refactor", "main", "(none)", plan_first=False)

        self.assertEqual(decision.tool, "agy")
        self.assertEqual(decision.model, "gemini-4-neon-high")
        self.assertNotIn('"claude"', seen_prompts[0])

    def test_router_opencode_credit_exhausted_marks_and_falls_back(self) -> None:
        err_proc = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="Error: insufficient_quota on opencode"
        )
        with mock.patch.object(router, "run_cmd", return_value=err_proc), redirect_stderr(io.StringIO()):
            decision = router.route_task("Fix bug", "main", "(none)", plan_first=False)

        self.assertTrue(decision.is_fallback)
        self.assertNotEqual(decision.tool, "opencode")
        self.assertIn("opencode", credits.get_exhausted_tools())

        # Subsequent call skips invoking opencode immediately while in cooldown
        with mock.patch.object(router, "run_cmd") as m_run, redirect_stderr(io.StringIO()):
            decision2 = router.route_task("Fix bug 2", "main", "(none)", plan_first=False)
        m_run.assert_not_called()
        self.assertNotEqual(decision2.tool, "opencode")

    def test_auto_run_fails_over_to_another_service_on_credit_exhausted(self) -> None:
        calls: list[dict[str, Any]] = []

        def fake_headless(tool: str, model: str, prompt: str, cwd: Path, **kw: Any) -> tuple[int, str, int]:
            calls.append({"tool": tool, "model": model, "effort": kw.get("effort", "")})
            if tool == "claude":
                return 1, "Error: Your credit balance is too low to access the Anthropic API.", 10
            return 0, "Completed successfully on alternative service!", 15

        router_proc = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps(
                {
                    "difficulty": "high",
                    "effort": "high",
                    "tool": "claude",
                    "model": "opus",
                    "branch_slug": "credit-test",
                    "reason": "complex",
                    "refined_prompt": "Implement feature.",
                }
            ),
            stderr="",
        )

        out = io.StringIO()
        err = io.StringIO()
        with mock.patch.object(router, "run_cmd", return_value=router_proc), \
                mock.patch.object(flow, "run_agent_headless_once", side_effect=fake_headless), \
                redirect_stdout(out), redirect_stderr(err):
            cli.main(["--auto", "Implement feature"])

        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["tool"], "claude")
        self.assertEqual(calls[0]["effort"], "high")
        self.assertNotEqual(calls[1]["tool"], "claude")
        self.assertEqual(calls[1]["tool"], "agy")
        self.assertEqual(calls[1]["model"], "gemini-4-neon-high")
        self.assertEqual(calls[1]["effort"], "high")
        self.assertIn("Completed successfully on alternative service!", out.getvalue())
        self.assertIn("claude", credits.get_exhausted_tools())

        events = storage.load_jsonl(self.log)
        self.assertTrue(any(e.get("event") == "credit_exhausted" and e.get("tool") == "claude" for e in events))
        self.assertTrue(any(e.get("event") == "failover" and e.get("tool") == "agy" for e in events))

    def test_status_detects_credit_exhausted_from_log_and_resets(self) -> None:
        log_path = dispatch.begin_session_log("cred1234", "claude", "sonnet")
        with log_path.open("a", encoding="utf-8") as f:
            f.write("API Error: credit balance is too low\n[ai-route] exit=1\n")

        meta = sessions.build_session_meta(
            session_id="cred1234",
            tool="claude",
            model="sonnet",
            workdir=self.repo,
            status="running",
            task="Fix parser",
            log_path=str(log_path),
        )
        storage.save_session_meta(self.repo, meta)

        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.main(["status"])
        text = buf.getvalue()
        self.assertIn("credit_exhausted", text)
        self.assertIn("ai-route resume cred1234", text)
        self.assertIn("claude", credits.get_exhausted_tools())

        # Reset credits via status --reset-credits
        buf2 = io.StringIO()
        with redirect_stdout(buf2):
            cli.main(["status", "--reset-credits", "claude"])
        self.assertIn("Cleared credit-exhausted status for: claude", buf2.getvalue())
        self.assertEqual(credits.get_exhausted_tools(), {})


# ---------------------------------------------------------------------------
# 3. Usability: default command (`run` omitted), shortcuts, and unified `resume`
# ---------------------------------------------------------------------------


class TestUsabilityAndResume(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name).resolve()
        self.repo = _init_repo(self.tmp)
        self.log = self.tmp / "data" / "history.jsonl"
        self._orig_cwd = Path.cwd()
        os.chdir(self.repo)
        self._env = mock.patch.dict(os.environ, {"AI_ROUTE_LOG_FILE": str(self.log)})
        self._env.start()
        self.addCleanup(self._env.stop)
        self.addCleanup(self._cleanup)

    def _cleanup(self) -> None:
        os.chdir(self._orig_cwd)
        self._tmp.cleanup()

    def test_normalize_argv_omits_run_cleanly(self) -> None:
        self.assertEqual(cli.normalize_argv(["Fix bug"]), ["run", "Fix bug"])
        self.assertEqual(cli.normalize_argv(["-w", "Add feature"]), ["run", "-w", "Add feature"])
        self.assertEqual(
            cli.normalize_argv(["--config", "/tmp/c.toml", "--auto", "Task"]),
            ["--config", "/tmp/c.toml", "run", "--auto", "Task"],
        )
        # Known commands and global flags are untouched
        self.assertEqual(cli.normalize_argv(["review", "--staged"]), ["review", "--staged"])
        self.assertEqual(cli.normalize_argv(["resume"]), ["resume"])
        self.assertEqual(cli.normalize_argv(["continue", "abc"]), ["continue", "abc"])
        self.assertEqual(cli.normalize_argv(["next"]), ["next"])
        self.assertEqual(cli.normalize_argv(["--help"]), ["--help"])
        self.assertEqual(cli.normalize_argv(["--version"]), ["--version"])

    def test_direct_invocation_without_run_and_effort_override(self) -> None:
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            cli.main(
                ["--dry-run", "--tool", "claude", "--model", "opus", "--effort", "xhigh", "Design cache"]
            )
        rendered = out.getvalue()
        self.assertIn("Selected   : claude (opus)", rendered)
        self.assertIn("Effort     : xhigh", rendered)

    def test_plan_short_flag_maps_to_two_stage(self) -> None:
        parser = cli.build_parser()
        args = parser.parse_args(cli.normalize_argv(["-p", "Refactor auth"]))
        self.assertTrue(args.two_stage)
        self.assertEqual(args.task, "Refactor auth")

    def test_resume_auto_failover_on_credit_exhausted_session(self) -> None:
        log_path = dispatch.begin_session_log("s_cred", "claude", "sonnet")
        with log_path.open("a", encoding="utf-8") as f:
            f.write("Error: out of credits\n[ai-route] exit=1\n")

        meta = sessions.build_session_meta(
            session_id="s_cred",
            tool="claude",
            model="sonnet",
            workdir=self.repo,
            difficulty="medium",
            effort="medium",
            status="running",
            task="Implement endpoint",
            refined_prompt="Implement /api/items",
            log_path=str(log_path),
        )
        storage.save_session_meta(self.repo, meta)

        dispatched: list[dict[str, Any]] = []

        def fake_dispatch(**kw: Any) -> None:
            kw["prompt_text"] = Path(kw["prompt_file"]).read_text(encoding="utf-8")
            Path(kw["prompt_file"]).unlink(missing_ok=True)
            dispatched.append(kw)

        out = io.StringIO()
        with mock.patch.object(flow, "dispatch_to_tmux", side_effect=fake_dispatch), \
                redirect_stdout(out), redirect_stderr(io.StringIO()):
            cli.main(["resume"])  # No session ID needed!

        self.assertEqual(len(dispatched), 1)
        self.assertEqual(dispatched[0]["tool"], "agy")
        self.assertIn("クレジットが切れた", dispatched[0]["prompt_text"])
        self.assertIn("_acp-session --tool agy --model gemini-3.8-flash-medium", dispatched[0]["shell_cmd"])
        self.assertIn("--effort medium", dispatched[0]["shell_cmd"])

    def test_resume_auto_escalates_blocked_session_in_one_command(self) -> None:
        meta = sessions.build_session_meta(
            session_id="s_blk",
            tool="agy",
            model="gemini-3.8-flash-medium",
            workdir=self.repo,
            difficulty="medium",
            effort="medium",
            status="blocked",
            task="Choose DB",
            refined_prompt="Implement storage",
            blocked={
                "kind": "marker",
                "reason": "blocked",
                "question": "Postgres or SQLite?",
                "options": ["Postgres", "SQLite"],
                "recommendation": "SQLite",
                "excerpt": "",
            },
        )
        storage.save_session_meta(self.repo, meta)

        plan_json = json.dumps(
            {
                "decisions": ["Use SQLite"],
                "constraints": ["No extra deps"],
                "open_questions": [],
                "implementation_steps": ["Create db.py"],
                "implementer_difficulty": "medium",
                "implementer_effort": "medium",
            }
        )
        calls: list[tuple[str, str]] = []

        def fake_headless(tool: str, model: str, prompt: str, cwd: Path, **kw: Any) -> tuple[int, str, int]:
            calls.append((tool, model))
            if len(calls) == 1:
                return 0, plan_json, 10
            return 0, "Implemented with SQLite!", 12

        out = io.StringIO()
        with mock.patch.object(flow, "run_agent_headless_once", side_effect=fake_headless), \
                redirect_stdout(out), redirect_stderr(io.StringIO()):
            cli.main(["resume", "--auto"])

        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0], ("claude", "opus"))  # escalated automatically
        self.assertEqual(calls[1], ("agy", "gemini-3.8-flash-medium"))  # resumed implementer
        self.assertIn("Implemented with SQLite!", out.getvalue())
