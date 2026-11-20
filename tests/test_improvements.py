"""Tests for the B1-B5 improvements (error handling, timeouts, base ref resolution, plan-first)."""

from __future__ import annotations

import io
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from ai_router import cli, config, dispatch, files, router, storage, utils
from ai_router.commands import multi, review, run
from ai_router.errors import AiRouteError


def _git(args: list[str], cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


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


def _add_member(repo: Path, idx: int, *, commits: int = 1) -> tuple[Path, str]:
    """Create an `ai/` worktree off HEAD with `commits` commits, like `multi` would."""
    wt = repo.parent / "repo-worktrees" / f"mg-{idx}"
    wt.parent.mkdir(parents=True, exist_ok=True)
    branch = f"ai/mg-{idx}"
    _git(["worktree", "add", "-b", branch, str(wt), "HEAD"], repo)
    for n in range(commits):
        (wt / f"m{idx}-{n}.txt").write_text(f"member {idx} change {n}\n", encoding="utf-8")
        _git(["add", "."], wt)
        _git(["commit", "-m", f"member {idx} commit {n}"], wt)
    return wt, branch


def _write_multi_event(
    log_path: Path, base_ref: str, members: list[tuple[Path, str]], group_id: str = "g1"
) -> None:
    storage.append_jsonl(
        {
            "ts": utils.now_iso(),
            "event": "multi",
            "group_id": group_id,
            "repo": "repo",
            "base_ref": base_ref,
            "task": "demo task",
            "members": [
                {"index": i, "tool": "claude", "model": f"m{i}", "branch": br, "workdir": str(wt)}
                for i, (wt, br) in enumerate(members, 1)
            ],
        },
        log_file=log_path,
    )


def _run_main(argv: list[str]) -> tuple[int | str | None, str]:
    """Run cli.main() and return (exit_code, stderr). exit_code is None on success."""
    err = io.StringIO()
    code: int | str | None = None
    with redirect_stdout(io.StringIO()), redirect_stderr(err):
        try:
            cli.main(argv)
        except SystemExit as e:
            code = e.code
    return code, err.getvalue()


# ---------------------------------------------------------------------------
# B2 / B3: centralized error handling and OSError fallbacks
# ---------------------------------------------------------------------------


class TestMainErrorHandling(unittest.TestCase):
    def _main_with_list_raising(self, exc: BaseException) -> tuple[int | str | None, str]:
        with mock.patch.object(cli, "cmd_list", side_effect=exc):
            return _run_main(["list"])

    def test_ai_route_error_is_reported_without_traceback(self) -> None:
        code, err = _run_main(["review", "--models", "bogus:model", "-f", __file__])
        self.assertEqual(code, 1)
        self.assertIn("[error] Unsupported tool 'bogus'", err)
        self.assertNotIn("Traceback", err)

    def test_called_process_error(self) -> None:
        exc = subprocess.CalledProcessError(128, ["git", "worktree", "add"], stderr="fatal: bad ref\n")
        code, err = self._main_with_list_raising(exc)
        self.assertEqual(code, 1)
        self.assertIn("[error] Command failed (rc=128): git worktree add", err)
        self.assertIn("fatal: bad ref", err)

    def test_timeout_expired(self) -> None:
        code, err = self._main_with_list_raising(subprocess.TimeoutExpired(["bash", "-lc", "x"], 2.5))
        self.assertEqual(code, 1)
        self.assertIn("[error] Command timed out after 2.5s", err)

    def test_file_not_found(self) -> None:
        exc = FileNotFoundError(2, "No such file or directory", "tmux")
        code, err = self._main_with_list_raising(exc)
        self.assertEqual(code, 1)
        self.assertIn("[error] File or command not found: tmux", err)

    def test_os_error(self) -> None:
        code, err = self._main_with_list_raising(PermissionError(13, "Permission denied"))
        self.assertEqual(code, 1)
        self.assertIn("[error] OS error", err)

    def test_keyboard_interrupt(self) -> None:
        code, err = self._main_with_list_raising(KeyboardInterrupt())
        self.assertEqual(code, 1)
        self.assertIn("[error] Interrupted by user.", err)

    def test_success_returns_normally(self) -> None:
        with mock.patch.object(cli, "cmd_list", return_value=None):
            code, err = _run_main(["list"])
        self.assertIsNone(code)
        self.assertEqual(err, "")


class TestCommandErrorsEndToEnd(unittest.TestCase):
    def _pick_prompt(self, input_mock: mock.Mock) -> tuple[int | str | None, str, Path]:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp).resolve()
            repo = _init_repo(tmp_path)
            m1, m2 = _add_member(repo, 1), _add_member(repo, 2)
            log_path = tmp_path / "h.jsonl"
            _write_multi_event(log_path, _git(["rev-parse", "HEAD"], repo), [m1, m2])
            orig = Path.cwd()
            try:
                os.chdir(repo)
                with mock.patch.dict(os.environ, {"AI_ROUTE_LOG_FILE": str(log_path)}):
                    with mock.patch("builtins.input", input_mock):
                        code, err = _run_main(["pick"])
            finally:
                os.chdir(orig)
            self.assertTrue(m1[0].exists() and m2[0].exists())
            return code, err, repo

    def test_pick_cancel_with_q_exits_zero(self) -> None:
        code, err, _ = self._pick_prompt(mock.Mock(return_value="q"))
        self.assertEqual(code, 0)
        self.assertNotIn("[error]", err)

    def test_pick_eof_is_an_error(self) -> None:
        code, err, _ = self._pick_prompt(mock.Mock(side_effect=EOFError))
        self.assertEqual(code, 1)
        self.assertIn("[error] Aborted (no input received).", err)

    def test_clean_git_failure_is_reported_cleanly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp).resolve()
            repo = _init_repo(tmp_path)
            orig = Path.cwd()
            try:
                os.chdir(repo)
                code, err = _run_main(["clean", str(tmp_path / "not-a-worktree")])
            finally:
                os.chdir(orig)
            self.assertEqual(code, 1)
            self.assertIn("[error] Command failed (rc=", err)
            self.assertIn("git worktree remove", err)
            self.assertNotIn("Traceback", err)


class TestLibraryRaisesAiRouteError(unittest.TestCase):
    def test_parse_tool_model_spec_errors(self) -> None:
        for bad in ("no-colon", "claude:", "nope:model"):
            with self.assertRaises(AiRouteError):
                utils.parse_tool_model_spec(bad)

    def test_parse_model_specs_empty_list(self) -> None:
        with self.assertRaises(AiRouteError):
            utils.parse_model_specs(" , ,", "balanced")

    def test_resolve_files_missing(self) -> None:
        with self.assertRaises(AiRouteError):
            files.resolve_files(["/definitely/not/here.txt"], repo_root=None)

    def test_load_task_text_errors(self) -> None:
        with self.assertRaises(AiRouteError):
            files.load_task_text(None, "/definitely/not/here.md")
        with self.assertRaises(AiRouteError):
            files.load_task_text(None, None)

    def test_dispatch_tiled_panes_requires_tmux(self) -> None:
        env = {k: v for k, v in os.environ.items() if k != "TMUX"}
        with mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaises(AiRouteError):
                dispatch.dispatch_tiled_panes(win_name="w", entries=[])


class TestRouterFallbackOnLaunchFailure(unittest.TestCase):
    def test_missing_opencode_falls_back(self) -> None:
        with tempfile.TemporaryDirectory() as empty_dir:
            with mock.patch.dict(os.environ, {"PATH": empty_dir}):
                err = io.StringIO()
                with redirect_stderr(err):
                    decision = router.route_task("task", "ctx", "files", plan_first=False)
        self.assertTrue(decision.is_fallback)
        self.assertEqual(decision.tool, config.FALLBACK_DECISION["tool"])
        self.assertEqual(decision.refined_prompt, "task")
        self.assertIn("[warn] Router command could not be executed", err.getvalue())

    def test_router_timeout_falls_back(self) -> None:
        exc = subprocess.TimeoutExpired(["opencode"], 1.5)
        err = io.StringIO()
        with mock.patch.object(router, "run_cmd", side_effect=exc) as m, redirect_stderr(err):
            decision = router.route_task("task", "ctx", "files", plan_first=False, timeout=1.5)
        self.assertTrue(decision.is_fallback)
        self.assertEqual(m.call_args.kwargs["timeout"], 1.5)
        self.assertIn("[warn] Router timed out after 1.5s", err.getvalue())


class TestHeadlessAgentFailures(unittest.TestCase):
    def test_missing_command_returns_127(self) -> None:
        with tempfile.TemporaryDirectory() as empty_dir:
            with mock.patch.dict(os.environ, {"PATH": empty_dir}):
                rc, out, _ = dispatch.run_agent_headless_once("claude", "opus", "hi", Path(empty_dir))
        self.assertEqual(rc, dispatch.RC_COMMAND_NOT_FOUND)
        self.assertIn("Failed to launch 'claude'", out)

    def test_timeout_returns_124_with_partial_output(self) -> None:
        exc = subprocess.TimeoutExpired(["claude"], 3.0, output="half a review")
        with mock.patch.object(dispatch, "run_cmd", side_effect=exc) as m:
            rc, out, _ = dispatch.run_agent_headless_once("claude", "opus", "hi", Path("."), timeout=3.0)
        self.assertEqual(rc, dispatch.RC_TIMEOUT)
        self.assertEqual(m.call_args.kwargs["timeout"], 3.0)
        self.assertIn("Timed out after 3s", out)
        self.assertIn("half a review", out)


# ---------------------------------------------------------------------------
# B4: timeouts and per-model failure isolation
# ---------------------------------------------------------------------------


class TestTimeoutConfig(unittest.TestCase):
    def test_defaults(self) -> None:
        env = {k: v for k, v in os.environ.items() if not k.endswith("_TIMEOUT")}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(config.get_router_timeout(), 60.0)
            self.assertEqual(config.get_review_timeout(), 300.0)
            self.assertEqual(config.get_test_cmd_timeout(), 300.0)

    def test_env_override_and_disable(self) -> None:
        with mock.patch.dict(os.environ, {"AI_ROUTE_ROUTER_TIMEOUT": "12.5", "AI_ROUTE_REVIEW_TIMEOUT": "0"}):
            self.assertEqual(config.get_router_timeout(), 12.5)
            self.assertIsNone(config.get_review_timeout())

    def test_cli_value_takes_precedence(self) -> None:
        with mock.patch.dict(os.environ, {"AI_ROUTE_TEST_CMD_TIMEOUT": "99"}):
            self.assertEqual(config.get_test_cmd_timeout(5.0), 5.0)
            self.assertIsNone(config.get_test_cmd_timeout(-1.0))

    def test_invalid_env_warns_and_uses_default(self) -> None:
        err = io.StringIO()
        with mock.patch.dict(os.environ, {"AI_ROUTE_ROUTER_TIMEOUT": "abc"}), redirect_stderr(err):
            self.assertEqual(config.get_router_timeout(), 60.0)
        self.assertIn("Ignoring invalid AI_ROUTE_ROUTER_TIMEOUT", err.getvalue())

    def test_cli_parses_timeout_options(self) -> None:
        parser = cli.build_parser()
        self.assertEqual(parser.parse_args(["run", "--router-timeout", "5", "t"]).router_timeout, 5.0)
        self.assertEqual(parser.parse_args(["review", "--timeout", "30"]).timeout, 30.0)
        self.assertEqual(parser.parse_args(["compare", "--timeout", "0"]).timeout, 0.0)
        self.assertIsNone(parser.parse_args(["compare"]).timeout)


class TestRunCmdTimeout(unittest.TestCase):
    def test_run_cmd_raises_timeout_expired(self) -> None:
        with self.assertRaises(subprocess.TimeoutExpired):
            utils.run_cmd(["sleep", "5"], check=False, timeout=0.2)

    def test_run_passes_router_timeout(self) -> None:
        args = cli.build_parser().parse_args(["run", "--dry-run", "--router-timeout", "7", "Fix bug"])
        fake = router.RouteDecision("low", "claude", "sonnet", "x", "r", "p")
        with mock.patch.object(run, "route_task", return_value=fake) as m:
            with redirect_stdout(io.StringIO()):
                run.cmd_run(args)
        self.assertEqual(m.call_args.kwargs["timeout"], 7.0)


class TestReviewFailureIsolation(unittest.TestCase):
    def _run_review(
        self, tmp: Path, extra: list[str], fake_results: dict[str, object]
    ) -> tuple[str, str, Path, BaseException | None]:
        target = tmp / "code.py"
        target.write_text("print('x')\n", encoding="utf-8")
        out_path = tmp / "report.md"
        argv = [
            "review", "-f", str(target), "--models", "claude:sonnet,agy:gemini-3.8-flash-medium",
            "-o", str(out_path), "--timeout", "9", *extra,
        ]
        args = cli.build_parser().parse_args(argv)
        calls: list[dict[str, object]] = []

        def fake_run(tool: str, model: str, prompt: str, cwd: Path, *, timeout: float | None = None):
            calls.append({"model": model, "timeout": timeout})
            result = fake_results[model]
            if isinstance(result, BaseException):
                raise result
            return result

        out, err = io.StringIO(), io.StringIO()
        raised: BaseException | None = None
        with mock.patch.dict(os.environ, {"AI_ROUTE_LOG_FILE": str(tmp / "h.jsonl")}):
            with mock.patch.object(review, "run_agent_headless_once", side_effect=fake_run):
                with redirect_stdout(out), redirect_stderr(err):
                    try:
                        review.cmd_review(args)
                    except AiRouteError as e:
                        raised = e
        self.assertTrue(all(c["timeout"] == 9.0 for c in calls))
        return out.getvalue(), err.getvalue(), out_path, raised

    def test_one_model_failing_keeps_other_results(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out, _, report, raised = self._run_review(
                Path(tmp),
                [],
                {
                    "sonnet": (0, "LGTM from sonnet", 10),
                    "gemini-3.8-flash-medium": RuntimeError("boom"),
                },
            )
            self.assertIsNone(raised)
            text = report.read_text(encoding="utf-8")
            self.assertIn("LGTM from sonnet", text)
            self.assertIn("FAILED (rc=1)", text)
            self.assertIn("RuntimeError: boom", text)
            self.assertIn("[FAILED: rc=1] agy:gemini-3.8-flash-medium", out)
            events = storage.load_jsonl(Path(tmp) / "h.jsonl")
            self.assertEqual(events[-1]["failed_models"], ["agy:gemini-3.8-flash-medium"])

    def test_timeout_and_missing_command_are_labelled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, _, report, raised = self._run_review(
                Path(tmp),
                [],
                {
                    "sonnet": (dispatch.RC_TIMEOUT, "[error] Timed out", 9000),
                    "gemini-3.8-flash-medium": (dispatch.RC_COMMAND_NOT_FOUND, "[error] no agy", 1),
                },
            )
            text = report.read_text(encoding="utf-8")
            self.assertIn("FAILED (TIMEOUT)", text)
            self.assertIn("FAILED (command not found)", text)
            # All models failed: report is still saved, but the command reports failure.
            self.assertIsInstance(raised, AiRouteError)
            self.assertIn("All 2 review model(s) failed", str(raised))

    def test_synthesis_failure_keeps_individual_reviews(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, err, report, raised = self._run_review(
                Path(tmp),
                ["--synthesize", "--synthesizer", "claude:opus"],
                {
                    "sonnet": (0, "review A", 10),
                    "gemini-3.8-flash-medium": (0, "review B", 10),
                    "opus": (dispatch.RC_TIMEOUT, "[error] Timed out", 9000),
                },
            )
            self.assertIsNone(raised)
            text = report.read_text(encoding="utf-8")
            self.assertIn("review A", text)
            self.assertIn("review B", text)
            self.assertIn("Synthesized Summary (claude:opus) - FAILED (TIMEOUT)", text)
            self.assertIn("[warn] Synthesis failed", err)

    def test_invalid_synthesizer_fails_before_running_reviews(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "code.py"
            target.write_text("x\n", encoding="utf-8")
            args = cli.build_parser().parse_args(
                ["review", "-f", str(target), "--synthesize", "--synthesizer", "bad"]
            )
            with mock.patch.object(review, "run_agent_headless_once") as m:
                with self.assertRaises(AiRouteError):
                    review.cmd_review(args)
            m.assert_not_called()


class TestCompareTestCmdTimeout(unittest.TestCase):
    def test_timeout_is_reported_and_next_member_still_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp).resolve()
            repo = _init_repo(tmp_path)
            m1 = _add_member(repo, 1)
            m2 = _add_member(repo, 2)
            base = _git(["rev-parse", "HEAD"], repo)
            (m1[0] / "slow").write_text("", encoding="utf-8")  # marker: member 1 hangs
            log_path = tmp_path / "h.jsonl"
            _write_multi_event(log_path, base, [m1, m2])

            args = cli.build_parser().parse_args(
                ["compare", "g1", "--test-cmd", "if [ -f slow ]; then sleep 5; fi", "--timeout", "0.5"]
            )
            buf = io.StringIO()
            with mock.patch.dict(os.environ, {"AI_ROUTE_LOG_FILE": str(log_path)}), redirect_stdout(buf):
                multi.cmd_compare(args)
            out = buf.getvalue()
            # cwd is this project's repo (not the group's): base must still resolve via the members.
            self.assertIn(f"base={base[:12]} [recorded]", out)
            self.assertIn("Test result : TIMEOUT (>0.5s)", out)
            self.assertIn("Test result : PASS", out)
            self.assertLess(out.index("TIMEOUT"), out.index("PASS"))


# ---------------------------------------------------------------------------
# B1: absolute base commit for `multi` and recovery of legacy logs
# ---------------------------------------------------------------------------


def _compare_output(log_path: Path, cwd: Path, group_id: str = "g1") -> tuple[str, str]:
    args = cli.build_parser().parse_args(["compare", group_id])
    out, err = io.StringIO(), io.StringIO()
    orig = Path.cwd()
    try:
        os.chdir(cwd)
        with mock.patch.dict(os.environ, {"AI_ROUTE_LOG_FILE": str(log_path)}):
            with redirect_stdout(out), redirect_stderr(err):
                multi.cmd_compare(args)
    finally:
        os.chdir(orig)
    return out.getvalue(), err.getvalue()


def _member_blocks(out: str) -> dict[int, str]:
    blocks: dict[int, str] = {}
    for chunk in out.split("-" * 72):
        chunk = chunk.strip()
        if chunk.startswith("[") and "]" in chunk:
            blocks[int(chunk[1 : chunk.index("]")])] = chunk
    return blocks


class TestResolveCommit(unittest.TestCase):
    def test_resolve_commit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = _init_repo(Path(tmp).resolve())
            head = _git(["rev-parse", "HEAD"], repo)
            self.assertEqual(utils.resolve_commit("HEAD", repo), head)
            self.assertEqual(utils.resolve_commit("main", repo), head)
            self.assertEqual(utils.resolve_commit(head[:8], repo), head)
            self.assertIsNone(utils.resolve_commit("no-such-ref", repo))
            self.assertIsNone(utils.resolve_commit("", repo))
            self.assertEqual(utils.get_main_worktree_head(repo), head)


class TestCompareBaseRef(unittest.TestCase):
    def test_recorded_sha_counts_commits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp).resolve()
            repo = _init_repo(tmp_path)
            base = _git(["rev-parse", "HEAD"], repo)
            m1, m2 = _add_member(repo, 1, commits=2), _add_member(repo, 2, commits=1)
            log_path = tmp_path / "h.jsonl"
            _write_multi_event(log_path, base, [m1, m2])

            out, err = _compare_output(log_path, repo)
            self.assertIn(f"base={base[:12]} [recorded]", out)
            blocks = _member_blocks(out)
            self.assertIn("Commits: 2", blocks[1])
            self.assertIn("Commits: 1", blocks[2])
            self.assertIn("m1-0.txt", blocks[1])
            self.assertEqual(err, "")

    def test_legacy_head_with_two_members_uses_octopus_merge_base(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp).resolve()
            repo = _init_repo(tmp_path)
            fork = _git(["rev-parse", "HEAD"], repo)
            m1, m2 = _add_member(repo, 1, commits=2), _add_member(repo, 2, commits=1)
            # main moves on after the fork; the fork point must still be used
            (repo / "later.txt").write_text("later\n", encoding="utf-8")
            _git(["add", "."], repo)
            _git(["commit", "-m", "later main work"], repo)
            log_path = tmp_path / "h.jsonl"
            _write_multi_event(log_path, "HEAD", [m1, m2])

            out, _ = _compare_output(log_path, repo)
            self.assertIn(f"base={fork[:12]} [merge-base of 2 member branches]", out)
            blocks = _member_blocks(out)
            self.assertIn("Commits: 2", blocks[1])
            self.assertIn("Commits: 1", blocks[2])
            self.assertNotIn("later.txt", out)

    def test_legacy_head_single_member_reproduction(self) -> None:
        """The B1 reproduction from docs/improvement-proposals.md: previously `Commits: 0`."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp).resolve()
            repo = _init_repo(tmp_path)
            fork = _git(["rev-parse", "HEAD"], repo)
            m1 = _add_member(repo, 1, commits=1)
            log_path = tmp_path / "h.jsonl"
            _write_multi_event(log_path, "HEAD", [m1])

            # Run from inside the member worktree to make sure the main worktree HEAD is used.
            out, _ = _compare_output(log_path, m1[0])
            self.assertIn(f"base={fork[:12]} [merge-base of member branch and main worktree HEAD]", out)
            self.assertIn("Commits: 1", _member_blocks(out)[1])
            self.assertIn("Diff stat vs base:", out)

    def test_unresolvable_legacy_base_warns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp).resolve()
            repo = _init_repo(tmp_path)
            log_path = tmp_path / "h.jsonl"
            gone = (tmp_path / "gone", "ai/deleted-branch")
            _write_multi_event(log_path, "HEAD", [gone])

            out, err = _compare_output(log_path, repo)
            self.assertIn("base=HEAD [unresolved]", out)
            self.assertIn("[warn] Could not determine the fork point", err)
            self.assertIn("(worktree already removed)", out)

    def test_pick_prompt_counts_commits_against_recovered_base(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp).resolve()
            repo = _init_repo(tmp_path)
            m1, m2 = _add_member(repo, 1, commits=2), _add_member(repo, 2, commits=1)
            log_path = tmp_path / "h.jsonl"
            _write_multi_event(log_path, "HEAD", [m1, m2])

            args = cli.build_parser().parse_args(["pick", "g1", "--dry-run"])
            buf = io.StringIO()
            orig = Path.cwd()
            try:
                os.chdir(repo)
                with mock.patch.dict(os.environ, {"AI_ROUTE_LOG_FILE": str(log_path)}):
                    with mock.patch("builtins.input", return_value="1"), redirect_stdout(buf):
                        multi.cmd_pick(args)
            finally:
                os.chdir(orig)
            self.assertIn("(branch: ai/mg-1, commits: 2)", buf.getvalue())
            self.assertIn("(branch: ai/mg-2, commits: 1)", buf.getvalue())


class _MultiHarness:
    """Runs `cmd_multi` in a temp repo with tmux dispatch mocked out."""

    def _run_multi(
        self, repo: Path, log_path: Path, argv: list[str]
    ) -> tuple[list[object], BaseException | None]:
        args = cli.build_parser().parse_args(["multi", "--models", "claude:opus,agy:gemini-4-neon-high", *argv])
        captured: list[object] = []
        raised: BaseException | None = None
        orig = Path.cwd()
        try:
            os.chdir(repo)
            env = {"AI_ROUTE_LOG_FILE": str(log_path), "TMUX": "/tmp/fake-tmux,1,0"}
            with mock.patch.dict(os.environ, env):
                with mock.patch.object(
                    multi, "dispatch_tiled_panes", side_effect=lambda **kw: captured.extend(kw["entries"])
                ):
                    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                        try:
                            multi.cmd_multi(args)
                        except AiRouteError as e:
                            raised = e
        finally:
            os.chdir(orig)
        for entry in captured:
            entry.prompt_file.unlink(missing_ok=True)  # type: ignore[attr-defined]
        return captured, raised


class TestMultiRecordsBaseSha(_MultiHarness, unittest.TestCase):
    def test_base_is_resolved_to_sha_and_used_for_worktrees(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp).resolve()
            repo = _init_repo(tmp_path)
            first = _git(["rev-parse", "HEAD"], repo)
            (repo / "app.txt").write_text("second\n", encoding="utf-8")
            _git(["commit", "-am", "second"], repo)
            log_path = tmp_path / "h.jsonl"

            entries, raised = self._run_multi(repo, log_path, ["--base", "HEAD~1", "task"])
            self.assertIsNone(raised)
            event = storage.load_jsonl(log_path)[-1]
            self.assertEqual(event["base_ref"], first)
            self.assertEqual(event["base_input"], "HEAD~1")
            self.assertEqual(len(entries), 2)
            for member in event["members"]:
                self.assertEqual(_git(["rev-parse", "HEAD"], Path(member["workdir"])), first)

    def test_invalid_base_aborts_before_creating_worktrees(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp).resolve()
            repo = _init_repo(tmp_path)
            log_path = tmp_path / "h.jsonl"
            entries, raised = self._run_multi(repo, log_path, ["--base", "no-such-ref", "task"])
            self.assertIsInstance(raised, AiRouteError)
            self.assertIn("Invalid base ref 'no-such-ref'", str(raised))
            self.assertEqual(entries, [])
            self.assertFalse((tmp_path / "repo-worktrees").exists())
            self.assertFalse(log_path.exists())
            self.assertEqual(_git(["branch", "--list", "ai/*"], repo), "")


# ---------------------------------------------------------------------------
# B5: plan-first instruction only in interactive mode, appended deterministically
# ---------------------------------------------------------------------------

PLAN = config.PLAN_FIRST_INSTRUCTION


def _router_proc(difficulty: str, refined: str) -> subprocess.CompletedProcess[str]:
    import json

    payload = {
        "difficulty": difficulty,
        "tool": "claude",
        "model": "sonnet",
        "branch_slug": "x",
        "reason": "r",
        "refined_prompt": refined,
    }
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=json.dumps(payload), stderr="")


class TestPlanFirstHelpers(unittest.TestCase):
    def test_plan_first_enabled(self) -> None:
        self.assertTrue(utils.plan_first_enabled(False, "interactive"))
        self.assertFalse(utils.plan_first_enabled(False, "headless"))
        self.assertFalse(utils.plan_first_enabled(True, "interactive"))

    def test_append_plan_first(self) -> None:
        self.assertEqual(utils.append_plan_first("do it\n", True), f"do it\n\n{PLAN}")
        self.assertEqual(utils.append_plan_first("do it", False), "do it")


class TestRouterPlanFirst(unittest.TestCase):
    def test_router_prompt_no_longer_asks_model_for_instruction(self) -> None:
        prompt = router.build_router_prompt("task", "ctx", "files")
        self.assertNotIn(PLAN, prompt)
        self.assertIn("システム側で付与します", prompt)

    def test_appended_for_every_difficulty(self) -> None:
        for difficulty in ("low", "medium", "high"):
            with mock.patch.object(router, "run_cmd", return_value=_router_proc(difficulty, "refined")):
                d = router.route_task("task", "ctx", "files", plan_first=True)
            self.assertEqual(d.refined_prompt, f"refined\n\n{PLAN}", msg=difficulty)

    def test_not_appended_when_disabled_even_if_model_mentions_agreement(self) -> None:
        with mock.patch.object(router, "run_cmd", return_value=_router_proc("high", "合意して進めて")):
            d = router.route_task("task", "ctx", "files", plan_first=False)
        self.assertEqual(d.refined_prompt, "合意して進めて")

    def test_appended_exactly_once_even_if_model_mentions_agreement(self) -> None:
        # The old `"合意" not in refined` heuristic would have skipped the instruction here.
        with mock.patch.object(router, "run_cmd", return_value=_router_proc("high", "合意事項: API互換")):
            d = router.route_task("task", "ctx", "files", plan_first=True)
        self.assertTrue(d.refined_prompt.endswith(PLAN))
        self.assertEqual(d.refined_prompt.count(PLAN), 1)

    def test_appended_on_fallback_paths(self) -> None:
        cases = [
            FileNotFoundError(2, "No such file", "opencode"),
            subprocess.TimeoutExpired(["opencode"], 1.0),
            subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="boom"),
            subprocess.CompletedProcess(args=[], returncode=0, stdout="not json", stderr=""),
        ]
        for case in cases:
            kwargs = {"side_effect": case} if isinstance(case, BaseException) else {"return_value": case}
            with mock.patch.object(router, "run_cmd", **kwargs), redirect_stderr(io.StringIO()):
                d = router.route_task("task", "ctx", "files", plan_first=True, timeout=1.0)
            self.assertTrue(d.is_fallback)
            self.assertEqual(d.refined_prompt, f"task\n\n{PLAN}", msg=repr(case))


class TestRunPlanFirst(unittest.TestCase):
    def _dry_run(self, argv: list[str]) -> str:
        args = cli.build_parser().parse_args(["run", "--dry-run", *argv])
        buf = io.StringIO()
        with redirect_stdout(buf):
            run.cmd_run(args)
        return buf.getvalue()

    def test_manual_interactive_appends(self) -> None:
        out = self._dry_run(["--tool", "claude", "--model", "opus", "Fix bug"])
        self.assertTrue(out.rstrip().endswith(PLAN))

    def test_manual_headless_does_not_append(self) -> None:
        out = self._dry_run(["--mode", "headless", "--tool", "claude", "--model", "opus", "Fix bug"])
        self.assertNotIn(PLAN, out)

    def test_no_plan_first_flag(self) -> None:
        out = self._dry_run(["--no-plan-first", "--tool", "claude", "--model", "opus", "Fix bug"])
        self.assertNotIn(PLAN, out)

    def test_routed_headless_disables_plan_first(self) -> None:
        for mode, expected in (("headless", False), ("interactive", True)):
            fake = router.RouteDecision("low", "claude", "sonnet", "x", "r", "p")
            with mock.patch.object(run, "route_task", return_value=fake) as m:
                self._dry_run(["--mode", mode, "Fix bug"])
            self.assertIs(m.call_args.kwargs["plan_first"], expected, msg=mode)

    def test_instruction_stays_last_with_files_block(self) -> None:
        out = self._dry_run(["--tool", "claude", "--model", "opus", "-f", __file__, "Fix bug"])
        self.assertIn("## 対象・参照ファイル", out)
        self.assertTrue(out.rstrip().endswith(PLAN))


class TestMultiPlanFirst(_MultiHarness, unittest.TestCase):
    def _prompts(self, extra: list[str]) -> list[str]:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp).resolve()
            repo = _init_repo(tmp_path)
            prompts: list[str] = []
            orig_unlink = Path.unlink

            def read_then_unlink(path: Path, missing_ok: bool = False) -> None:
                if path.name.startswith("ai-route-prompt-"):
                    prompts.append(path.read_text(encoding="utf-8"))
                orig_unlink(path, missing_ok=missing_ok)

            with mock.patch.object(Path, "unlink", read_then_unlink):
                _, raised = self._run_multi(repo, tmp_path / "h.jsonl", [*extra, "Solve it"])
            self.assertIsNone(raised)
            return prompts

    def test_interactive_appends_once_per_member(self) -> None:
        prompts = self._prompts([])
        self.assertEqual(len(prompts), 2)
        for prompt in prompts:
            self.assertEqual(prompt, f"Solve it\n\n{PLAN}")

    def test_headless_and_flag_do_not_append(self) -> None:
        for extra in (["--mode", "headless"], ["--no-plan-first"]):
            for prompt in self._prompts(extra):
                self.assertEqual(prompt, "Solve it", msg=str(extra))


if __name__ == "__main__":
    unittest.main()
