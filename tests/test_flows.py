"""CLI and end-to-end flow tests for run --wait, status, escalate, handoff --from-session,
run --two-stage, review --then-implement, and blocking statistics."""

from __future__ import annotations

import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any, Callable
from unittest import mock

from ai_router import cli, config, flow, storage, utils
from ai_router.commands import review, session, stats
from ai_router.errors import AiRouteError

MARKER = config.BLOCKED_MARKER
BLOCKED_OUT = f"途中まで実装しました。\n{MARKER}\n質問: DBはどちらにしますか\n選択肢: Postgres / SQLite\n推奨: SQLite\n"
PLAN = {
    "decisions": ["SQLiteを採用する"],
    "constraints": ["既存スキーマを変更しない"],
    "open_questions": [],
    "implementation_steps": ["db.pyを追加"],
    "implementer_difficulty": "high",
}
PLAN_JSON = json.dumps(PLAN, ensure_ascii=False)


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


class _FlowHarness(unittest.TestCase):
    """Temp repo as cwd, isolated history log, agents and tmux mocked."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name).resolve()
        self.repo = _init_repo(self.tmp)
        self.log = self.tmp / "data" / "history.jsonl"
        self._orig_cwd = Path.cwd()
        os.chdir(self.repo)
        env = mock.patch.dict(os.environ, {"AI_ROUTE_LOG_FILE": str(self.log)})
        env.start()
        for name in (config.PLANNER_ENV, config.IMPLEMENTER_ENV, config.AGENT_TIMEOUT_ENV):
            os.environ.pop(name, None)
        self.calls: list[dict[str, Any]] = []
        self.dispatched: list[dict[str, Any]] = []
        self.addCleanup(env.stop)

    def tearDown(self) -> None:
        os.chdir(self._orig_cwd)
        for d in self.dispatched:
            Path(d["prompt_file"]).unlink(missing_ok=True)
        self._tmp.cleanup()

    def agent(self, outputs: list[tuple[int, str]]) -> Callable[..., tuple[int, str, int]]:
        queue = list(outputs)

        def fake(tool: str, model: str, prompt: str, cwd: Path, *, timeout: float | None = None):
            self.calls.append({"tool": tool, "model": model, "prompt": prompt, "cwd": cwd, "timeout": timeout})
            rc, out = queue.pop(0)
            return rc, out, 7

        return fake

    def run_cli(
        self, argv: list[str], outputs: list[tuple[int, str]] | None = None
    ) -> tuple[str, BaseException | None]:
        args = cli.build_parser().parse_args(argv)
        buf = io.StringIO()
        raised: BaseException | None = None
        fake = self.agent(outputs or [])

        def fake_dispatch(**kw: Any) -> None:
            kw["prompt_text"] = Path(kw["prompt_file"]).read_text(encoding="utf-8")
            self.dispatched.append(kw)

        with mock.patch.object(flow, "run_agent_headless_once", side_effect=fake), \
                mock.patch.object(review, "run_agent_headless_once", side_effect=fake), \
                mock.patch.object(flow, "dispatch_to_tmux", side_effect=fake_dispatch), \
                redirect_stdout(buf), redirect_stderr(io.StringIO()):
            try:
                args.func(args)
            except AiRouteError as e:
                raised = e
        return buf.getvalue(), raised

    def events(self, name: str | None = None) -> list[dict[str, Any]]:
        recs = storage.load_jsonl(self.log, warn_malformed=False)
        return [r for r in recs if name is None or r.get("event") == name]

    def meta(self) -> dict[str, Any]:
        m = storage.load_session_meta(self.repo)
        assert m is not None
        return m


class TestParser(unittest.TestCase):
    def test_new_commands_and_flags_parse(self) -> None:
        parser = cli.build_parser()
        cases = [
            ["run", "--mode", "headless", "--wait", "--no-auto-escalate", "--agent-timeout", "5", "x"],
            ["run", "--two-stage", "--plan-preset", "cheap", "--planner", "claude:opus", "--implementer", "agy", "x"],
            ["handoff", "--from-session", "abc"],
            ["handoff", "--from-session"],
            ["handoff", "legacy note", "--tool", "agy", "--model", "m"],
            ["escalate", "--from-session", "abc", "--tool", "claude", "--model", "opus",
             "--then-implement", "--note", "n", "--mode", "headless", "--wait"],
            ["review", "--then-implement=claude:opus", "--impl-mode", "headless", "--wait"],
            ["review", "--then-implement"],
            ["status", "--group", "g1", "--json"],
        ]
        for argv in cases:
            args = parser.parse_args(argv)
            self.assertTrue(callable(args.func), msg=argv)

    def test_defaults(self) -> None:
        parser = cli.build_parser()
        args = parser.parse_args(["run", "x"])
        self.assertTrue(args.auto_escalate)
        self.assertFalse(args.wait)
        self.assertFalse(args.two_stage)
        self.assertFalse(parser.parse_args(["run", "--no-auto-escalate", "x"]).auto_escalate)
        self.assertEqual(parser.parse_args(["handoff", "--from-session"]).from_session, ".")
        self.assertIsNone(parser.parse_args(["handoff", "n"]).from_session)
        self.assertEqual(parser.parse_args(["review", "--then-implement"]).then_implement, "")
        self.assertIsNone(parser.parse_args(["review"]).then_implement)
        self.assertEqual(parser.parse_args(["review", "--then-implement=agy"]).then_implement, "agy")

    def test_wait_requires_headless_via_main(self) -> None:
        err = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            with self.assertRaises(SystemExit) as ctx:
                cli.main(["run", "--wait", "--tool", "claude", "--model", "opus", "x"])
        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("--wait requires --mode headless", err.getvalue())


RUN_WAIT = ["run", "--mode", "headless", "--wait", "--tool", "claude", "--model", "sonnet", "--agent-timeout", "42"]


class TestRunWait(_FlowHarness):
    def test_completed_run_records_agent_result(self) -> None:
        out, raised = self.run_cli([*RUN_WAIT, "Fix bug"], [(0, "All done.")])
        self.assertIsNone(raised)
        self.assertIn("All done.", out)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0]["timeout"], 42.0)
        self.assertTrue(self.calls[0]["prompt"].rstrip().endswith(config.HEADLESS_BLOCKED_INSTRUCTION))
        self.assertNotIn(config.PLAN_FIRST_INSTRUCTION, self.calls[0]["prompt"])

        route = self.events("route")[-1]
        self.assertTrue(route["wait"])
        self.assertEqual(route["stage"], "single")
        result = self.events("agent_result")[-1]
        self.assertEqual((result["rc"], result["status"], result["latency_ms"]), (0, "completed", 7))
        self.assertEqual(self.events("blocked"), [])

        meta = self.meta()
        self.assertEqual(meta["status"], "completed")
        self.assertEqual(meta["task"], "Fix bug")
        self.assertIn("All done.", meta["last_output_summary"])
        self.assertIn("exit=0", Path(meta["log_path"]).read_text(encoding="utf-8"))
        for key in ("session_id", "current_tool", "current_model", "handoff_count", "started_at"):
            self.assertIn(key, meta)

    def test_default_timeout_comes_from_env(self) -> None:
        argv = ["run", "--mode", "headless", "--wait", "--tool", "claude", "--model", "sonnet", "x"]
        with mock.patch.dict(os.environ, {config.AGENT_TIMEOUT_ENV: "11"}):
            self.run_cli(argv, [(0, "ok")])
        self.assertEqual(self.calls[0]["timeout"], 11.0)

    def test_blocked_auto_escalates_and_resumes_once(self) -> None:
        out, raised = self.run_cli([*RUN_WAIT, "Add DB"], [(0, BLOCKED_OUT), (0, PLAN_JSON), (0, "Implemented.")])
        self.assertIsNone(raised)
        self.assertEqual([(c["tool"], c["model"]) for c in self.calls],
                         [("claude", "sonnet"), ("claude", "opus"), ("claude", "sonnet")])
        self.assertIn("DBはどちらにしますか", self.calls[1]["prompt"])
        resume = self.calls[2]["prompt"]
        self.assertIn("## 決定事項\n- SQLiteを採用する", resume)
        self.assertEqual(resume.count(config.BLOCKED_MARKER), 1)  # instruction appended once
        self.assertTrue(resume.rstrip().endswith(config.HEADLESS_BLOCKED_INSTRUCTION))

        blocked = self.events("blocked")
        self.assertEqual(len(blocked), 1)
        self.assertEqual((blocked[0]["kind"], blocked[0]["question"]), ("marker", "DBはどちらにしますか"))
        esc = self.events("escalated")
        self.assertEqual(len(esc), 1)
        self.assertEqual((esc[0]["trigger"], esc[0]["plan_valid"]), ("auto", True))
        self.assertEqual([r["status"] for r in self.events("agent_result")], ["blocked", "completed"])
        meta = self.meta()
        self.assertEqual(meta["decision"]["decisions"], ["SQLiteを採用する"])
        self.assertEqual(meta["status"], "completed")
        self.assertIn("escalated", [h["event"] for h in meta["history"]])

    def test_auto_escalation_is_bounded(self) -> None:
        _, raised = self.run_cli([*RUN_WAIT, "x"], [(0, BLOCKED_OUT), (0, PLAN_JSON), (0, BLOCKED_OUT)])
        self.assertIsInstance(raised, AiRouteError)
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(len(self.events("escalated")), config.AUTO_ESCALATE_MAX_ROUNDS)

    def test_blocked_without_auto_escalate_raises_with_hint(self) -> None:
        _, raised = self.run_cli([*RUN_WAIT, "--no-auto-escalate", "x"], [(0, BLOCKED_OUT)])
        self.assertIsInstance(raised, AiRouteError)
        self.assertIn("ai-route escalate --from-session", str(raised))
        self.assertEqual(self.events("escalated"), [])
        meta = self.meta()
        self.assertEqual(meta["status"], "blocked")
        self.assertEqual(meta["blocked"]["options"], ["Postgres", "SQLite"])

    def test_timeout_is_not_escalated(self) -> None:
        _, raised = self.run_cli([*RUN_WAIT, "x"], [(124, "[error] Timed out")])
        self.assertIsInstance(raised, AiRouteError)
        self.assertIn("timeout", str(raised))
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.events("blocked")[-1]["reason"], "timeout")
        self.assertEqual(self.events("escalated"), [])

    def test_dry_run_shows_instruction_without_running(self) -> None:
        out, raised = self.run_cli([*RUN_WAIT, "--dry-run", "x"])
        self.assertIsNone(raised)
        self.assertIn(config.BLOCKED_MARKER, out)
        self.assertEqual(self.calls, [])


class TestTmuxLogAndStatus(_FlowHarness):
    def test_headless_tmux_is_teed_and_status_detects_block(self) -> None:
        _, raised = self.run_cli(["run", "--mode", "headless", "--tool", "claude", "--model", "sonnet", "x"])
        self.assertIsNone(raised)
        d = self.dispatched[0]
        log_path = Path(d["log_path"])
        self.assertIn("tee -a", d["shell_cmd"])
        self.assertIn(str(log_path), d["shell_cmd"])
        self.assertEqual(self.meta()["log_path"], str(log_path))
        self.assertNotIn(MARKER, d["prompt_text"])  # instruction only for --wait

        with log_path.open("a", encoding="utf-8") as f:
            f.write("\x1b[1mworking\x1b[0m\nShould I drop the legacy table?\n[ai-route] exit=0\n")
        sid = self.meta()["session_id"]
        for _ in range(2):
            out, raised = self.run_cli(["status"])
            self.assertIsNone(raised)
        self.assertIn("blocked", out)
        self.assertIn(f"ai-route escalate --from-session {sid}", out)
        blocked = self.events("blocked")
        self.assertEqual(len(blocked), 1)  # not recorded twice
        self.assertEqual(blocked[0]["source"], "status")
        self.assertEqual(self.meta()["status"], "blocked")

        out, _ = self.run_cli(["status", "--from-session", sid, "--json"])
        rows = json.loads(out)
        self.assertEqual(rows[0]["blocked"]["kind"], "pattern")

    def test_interactive_uses_pipe_pane_log_and_running_status(self) -> None:
        self.run_cli(["run", "--tool", "claude", "--model", "sonnet", "x"])
        d = self.dispatched[0]
        self.assertNotIn("tee", d["shell_cmd"])
        self.assertIn("[ai-route] exit=$?", d["shell_cmd"])
        self.assertEqual(d["mode"], "interactive")
        self.assertIsNotNone(d["log_path"])
        out, _ = self.run_cli(["status"])
        self.assertIn("running", out)

    def test_status_for_multi_group(self) -> None:
        wt = self.tmp / "member"
        _git(["worktree", "add", "-b", "ai/m1", str(wt), "HEAD"], self.repo)
        log_path = self.tmp / "m.log"
        log_path.write_text(f"{MARKER}\n質問: 続行しますか\n[ai-route] exit=0\n", encoding="utf-8")
        storage.save_session_meta(wt, {"session_id": "g1-1", "current_tool": "agy", "current_model": "m",
                                       "log_path": str(log_path)})
        storage.append_jsonl({"event": "multi", "group_id": "g1",
                              "members": [{"index": 1, "workdir": str(wt)}]}, log_file=self.log)
        out, raised = self.run_cli(["status", "--group", "g1"])
        self.assertIsNone(raised)
        self.assertIn("続行しますか", out)
        _, raised = self.run_cli(["status", "--group", "nope"])
        self.assertIsInstance(raised, AiRouteError)


class TestEscalateAndHandoff(_FlowHarness):
    def _blocked_session(self) -> str:
        self.run_cli([*RUN_WAIT, "--no-auto-escalate", "Add DB"], [(0, BLOCKED_OUT)])
        self.calls.clear()
        return str(self.meta()["session_id"])

    def test_escalate_stores_decision(self) -> None:
        sid = self._blocked_session()
        out, raised = self.run_cli(["escalate", "--from-session", sid, "--note", "軽量優先"], [(0, PLAN_JSON)])
        self.assertIsNone(raised)
        self.assertEqual((self.calls[0]["tool"], self.calls[0]["model"]), ("claude", "opus"))
        self.assertIn("軽量優先", self.calls[0]["prompt"])
        self.assertIn("編集", self.calls[0]["prompt"])
        self.assertIn("## 決定事項", out)
        meta = self.meta()
        self.assertEqual(meta["status"], "decided")
        self.assertTrue(meta["decision"]["valid"])
        esc = self.events("escalated")[-1]
        self.assertEqual((esc["trigger"], esc["session_id"], esc["question"]),
                         ("manual", sid, "DBはどちらにしますか"))

    def test_escalate_tool_only_uses_high_candidate_and_then_implements(self) -> None:
        sid = self._blocked_session()
        _, raised = self.run_cli(
            ["escalate", "--from-session", sid, "--tool", "agy", "--then-implement", "--mode", "headless", "--wait"],
            [(0, PLAN_JSON), (0, "done")],
        )
        self.assertIsNone(raised)
        self.assertEqual((self.calls[0]["tool"], self.calls[0]["model"]), ("agy", "gemini-4-neon-high"))
        self.assertEqual((self.calls[1]["tool"], self.calls[1]["model"]), ("claude", "sonnet"))
        self.assertIn("SQLiteを採用する", self.calls[1]["prompt"])
        self.assertEqual(self.meta()["status"], "completed")

    def test_escalate_then_implement_interactive_dispatches(self) -> None:
        self._blocked_session()
        _, raised = self.run_cli(["escalate", "--then-implement"], [(0, PLAN_JSON)])
        self.assertIsNone(raised)
        self.assertEqual(self.dispatched[0]["mode"], "interactive")
        self.assertIn("SQLiteを採用する", self.dispatched[0]["prompt_text"])

    def test_escalate_failure_raises(self) -> None:
        self._blocked_session()
        _, raised = self.run_cli(["escalate"], [(1, "boom")])
        self.assertIsInstance(raised, AiRouteError)
        self.assertEqual(self.events("escalated")[-1]["status"], "failed")
        self.assertNotIn("decision", self.meta())

    def test_escalate_requires_session(self) -> None:
        _, raised = self.run_cli(["escalate", "--model", "opus"])
        self.assertIsInstance(raised, AiRouteError)

    def test_handoff_from_session_includes_context(self) -> None:
        sid = self._blocked_session()
        _, raised = self.run_cli(["handoff", "--from-session", sid, "--tool", "agy", "--model", "g"])
        self.assertIsNone(raised)
        prompt = self.dispatched[0]["prompt_text"]
        for text in ("Add DB", "DBはどちらにしますか", "Postgres / SQLite", "途中まで実装しました", "## 引き継ぎ指示"):
            self.assertIn(text, prompt)
        self.assertTrue(self.dispatched[0]["split_current"])
        ho = self.events("handoff")[-1]
        self.assertTrue(ho["from_session"])
        self.assertEqual((ho["from_tool"], ho["tool"], ho["handoff_seq"]), ("claude", "agy", 1))
        meta = self.meta()
        self.assertEqual((meta["current_tool"], meta["handoff_count"], meta["session_id"]), ("agy", 1, sid))
        self.assertEqual(meta["task"], "Add DB")

    def test_handoff_dispatches_to_absolute_workdir(self) -> None:
        sid = self._blocked_session()
        for argv in (
            ["handoff", "--from-session", sid, "--tool", "agy", "--model", "g"],
            ["handoff", "続きをお願いします"],
        ):
            self.dispatched.clear()
            _, raised = self.run_cli(argv)
            self.assertIsNone(raised)
            workdir = self.dispatched[0]["workdir"]
            self.assertTrue(workdir.is_absolute())
            self.assertEqual(workdir, self.repo)
            self.assertEqual(self.events("handoff")[-1]["workdir"], str(self.repo))

    def test_legacy_handoff_still_works_and_requires_note(self) -> None:
        _, raised = self.run_cli(["handoff", "続きをお願いします"])
        self.assertIsNone(raised)
        prompt = self.dispatched[0]["prompt_text"]
        self.assertTrue(prompt.startswith("現在作業中のworktreeを引き継ぎます。"))
        self.assertIn("## 引き継ぎ指示\n続きをお願いします", prompt)
        _, raised = self.run_cli(["handoff"])
        self.assertIsInstance(raised, AiRouteError)


class TestTwoStage(_FlowHarness):
    def test_planner_then_implementer_by_difficulty(self) -> None:
        argv = ["run", "--two-stage", "--mode", "headless", "--wait", "Add DB"]
        out, raised = self.run_cli(argv, [(0, "計画:\n" + PLAN_JSON), (0, "Implemented.")])
        self.assertIsNone(raised)
        planner, impl = self.calls
        self.assertEqual((planner["tool"], planner["model"]), ("claude", "opus"))
        self.assertIn('"implementer_difficulty"', planner["prompt"])
        self.assertEqual((impl["tool"], impl["model"]), ("agy", "gemini-4-neon-high"))
        self.assertIn("## 決定事項\n- SQLiteを採用する", impl["prompt"])
        self.assertIn("## タスク\nAdd DB", impl["prompt"])

        ts = self.events("two_stage")[-1]
        self.assertEqual((ts["status"], ts["plan_valid"], ts["implementer_model"]), ("planned", True, "gemini-4-neon-high"))
        route = self.events("route")[-1]
        self.assertEqual(route["stage"], "implementer")
        self.assertEqual(route["parent_session"], ts["parent_session"])
        self.assertEqual(route["router_model"], None)
        stages = [r["stage"] for r in self.events("agent_result")]
        self.assertEqual(stages, ["planner", "implementer"])
        meta = self.meta()
        self.assertEqual(meta["stage"], "implementer")
        self.assertEqual(meta["decision"]["tool"], "claude")

    def test_preset_and_invalid_plan_fallback(self) -> None:
        argv = ["run", "--two-stage", "--plan-preset", "cheap", "--mode", "headless", "--wait", "x"]
        _, raised = self.run_cli(argv, [(0, "ただの文章"), (0, "done")])
        self.assertIsNone(raised)
        self.assertEqual(self.calls[0]["model"], "sonnet")
        self.assertEqual(self.calls[1]["tool"], "opencode")
        self.assertIn("ただの文章", self.calls[1]["prompt"])
        self.assertFalse(self.events("two_stage")[-1]["plan_valid"])

    def test_interactive_implementer_dispatches_with_plan_first(self) -> None:
        _, raised = self.run_cli(["run", "--two-stage", "--implementer", "claude", "x"], [(0, PLAN_JSON)])
        self.assertIsNone(raised)
        d = self.dispatched[0]
        self.assertEqual((d["tool"], d["mode"]), ("claude", "interactive"))
        self.assertTrue(d["prompt_text"].rstrip().endswith(config.PLAN_FIRST_INSTRUCTION))

    def test_planner_failure_aborts(self) -> None:
        _, raised = self.run_cli(["run", "--two-stage", "x"], [(127, "[error] not found")])
        self.assertIsInstance(raised, AiRouteError)
        self.assertEqual(self.events("two_stage")[-1]["status"], "planner_failed")
        self.assertEqual(self.events("route"), [])
        self.assertEqual(self.dispatched, [])

    def test_dry_run_and_conflicting_flags(self) -> None:
        out, raised = self.run_cli(["run", "--two-stage", "--dry-run", "--planner", "agy:p", "x"])
        self.assertIsNone(raised)
        self.assertIn("Planner    : agy (p)", out)
        self.assertEqual(self.calls, [])
        _, raised = self.run_cli(["run", "--two-stage", "--tool", "claude", "--model", "opus", "x"])
        self.assertIsInstance(raised, AiRouteError)


class TestReviewThenImplement(_FlowHarness):
    def _review(self, extra: list[str], outputs: list[tuple[int, str]]) -> tuple[str, BaseException | None]:
        target = self.repo / "code.py"
        target.write_text("print('x')\n", encoding="utf-8")
        argv = ["review", "-f", str(target), "--models", "claude:sonnet,agy:gemini-3.8-flash-medium",
                "-o", str(self.tmp / "r.md"), *extra]
        return self.run_cli(argv, outputs)

    def test_synthesis_plan_is_passed_to_implementer(self) -> None:
        synth = "## 統合\n重要: X\n" + PLAN_JSON
        _, raised = self._review(["--then-implement=claude:opus"], [(0, "r1"), (0, "r2"), (0, synth)])
        self.assertIsNone(raised)
        self.assertIn('"implementation_steps"', self.calls[2]["prompt"])  # plan schema requested
        d = self.dispatched[0]
        self.assertEqual((d["tool"], d["mode"]), ("claude", "interactive"))
        self.assertIn("## 決定事項\n- SQLiteを採用する", d["prompt_text"])
        rev = self.events("review")[-1]
        self.assertTrue(rev["synthesized"])
        self.assertEqual(rev["then_implement"], "claude:opus")
        ts = self.events("two_stage")[-1]
        self.assertEqual((ts["source"], ts["group_id"]), ("review", rev["group_id"]))
        self.assertEqual(self.events("route")[-1]["parent_session"], rev["group_id"])

    def test_headless_wait_implementer_by_difficulty(self) -> None:
        _, raised = self._review(
            ["--then-implement", "--impl-mode", "headless", "--wait"],
            [(0, "r1"), (0, "r2"), (0, PLAN_JSON), (0, "fixed")],
        )
        self.assertIsNone(raised)
        self.assertEqual((self.calls[3]["tool"], self.calls[3]["model"]), ("agy", "gemini-4-neon-high"))
        self.assertEqual(self.events("agent_result")[-1]["status"], "completed")

    def test_synthesis_failure_skips_implementation(self) -> None:
        _, raised = self._review(["--then-implement"], [(0, "r1"), (0, "r2"), (1, "boom")])
        self.assertIsInstance(raised, AiRouteError)
        self.assertEqual(self.dispatched, [])
        self.assertTrue((self.tmp / "r.md").is_file())

    def test_requires_headless_reviews(self) -> None:
        _, raised = self._review(["--then-implement", "--mode", "interactive"], [])
        self.assertIsInstance(raised, AiRouteError)


class TestStatsBlocking(unittest.TestCase):
    def test_counts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "h.jsonl"
            ts = utils.now_iso()
            for rec in (
                {"event": "route", "session_id": "s1", "tool": "claude", "model": "sonnet"},
                {"event": "agent_result", "session_id": "s1", "status": "blocked"},
                {"event": "blocked", "session_id": "s1", "reason": "blocked", "tool": "claude", "model": "sonnet"},
                {"event": "blocked", "session_id": "s1", "reason": "timeout", "tool": "claude", "model": "sonnet"},
                {"event": "escalated", "session_id": "s1", "trigger": "auto", "tool": "claude", "model": "opus"},
                {"event": "agent_result", "session_id": "s1", "status": "completed"},
                {"event": "two_stage", "parent_session": "p1", "status": "planned"},
            ):
                storage.append_jsonl({"ts": ts, "repo": "r", **rec}, log_file=log)
            args = cli.build_parser().parse_args(["stats"])
            buf = io.StringIO()
            with mock.patch.dict(os.environ, {"AI_ROUTE_LOG_FILE": str(log)}), redirect_stdout(buf):
                stats.cmd_stats(args)
            out = buf.getvalue()
            self.assertIn("blocked 2 (sessions 1) / escalated 1 (auto 1) / two-stage 1", out)
            self.assertIn("agent results : blocked 1, completed 1", out)
            self.assertIn("blocked by    : blocked 1, timeout 1", out)
            self.assertIn("escalated -> claude (opus)", out)

    def test_summary_counts_only_new_events(self) -> None:
        summary = stats.summarize_blocking([], [], [], [])
        self.assertEqual((summary["blocked"], summary["escalated"], summary["two_stage"]), (0, 0, 0))

    def test_stats_with_only_new_events_is_not_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "h.jsonl"
            storage.append_jsonl({"ts": utils.now_iso(), "event": "blocked", "reason": "blocked"}, log_file=log)
            buf = io.StringIO()
            with mock.patch.dict(os.environ, {"AI_ROUTE_LOG_FILE": str(log)}), redirect_stdout(buf):
                stats.cmd_stats(cli.build_parser().parse_args(["stats"]))
            self.assertNotIn("No log entries", buf.getvalue())
            self.assertIn("blocked 1", buf.getvalue())


class TestRefreshSessionFromLog(_FlowHarness):
    def test_new_run_separator_resets_detection(self) -> None:
        log = self.tmp / "s.log"
        log.write_text(f"{MARKER}\n質問: 旧質問\n[ai-route] exit=0\n[ai-route] run-start t x:y\nworking...\n",
                       encoding="utf-8")
        meta = {"session_id": "s", "log_path": str(log)}
        self.assertIsNone(session.refresh_session_from_log(self.repo, meta))
        self.assertEqual(meta["status"], "running")


if __name__ == "__main__":
    unittest.main()
