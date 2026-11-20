"""Unit tests covering CLI parsing, config, utils, files, router, dispatch, and storage."""

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
from ai_router.commands import multi, run, stats


class TestConfig(unittest.TestCase):
    def test_agy_models_updated_to_two_tiers(self) -> None:
        agy_models = {c["model"] for c in config.CANDIDATES if c["tool"] == "agy"}
        self.assertEqual(agy_models, {"gemini-3.8-flash-medium", "gemini-4-neon-high"})

    def test_presets_reference_supported_tools(self) -> None:
        for preset_name, pairs in config.MODEL_PRESETS.items():
            self.assertGreaterEqual(len(pairs), 2, msg=f"preset {preset_name} should have >=2 models")
            for tool, model in pairs:
                self.assertIn(tool, config.SUPPORTED_TOOLS)
                self.assertTrue(model)


class TestUtils(unittest.TestCase):
    def test_sanitize_slug(self) -> None:
        self.assertEqual(utils.sanitize_slug("Fix Parser / Edge Case!!"), "fix-parser-edge-case")
        self.assertEqual(utils.sanitize_slug("___"), "ai-task")
        self.assertLessEqual(len(utils.sanitize_slug("a" * 50)), 32)

    def test_extract_first_json_object(self) -> None:
        raw = 'Here is the result:\n```json\n{"tool": "agy", "model": "gemini-4-neon-high"}\n```'
        parsed = utils.extract_first_json_object(raw)
        self.assertEqual(parsed, {"tool": "agy", "model": "gemini-4-neon-high"})
        self.assertIsNone(utils.extract_first_json_object("no json here"))

    def test_parse_model_specs_preset_and_explicit(self) -> None:
        self.assertEqual(utils.parse_model_specs("", "balanced"), config.MODEL_PRESETS["balanced"])
        specs = utils.parse_model_specs("claude:opus,agy:gemini-4-neon-high", "balanced")
        self.assertEqual(specs, [("claude", "opus"), ("agy", "gemini-4-neon-high")])

    def test_parse_model_specs_invalid_tool_exits(self) -> None:
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                utils.parse_model_specs("unknown:model", "balanced")


class TestFiles(unittest.TestCase):
    def test_resolve_files_and_formatting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sample = root / "sample.py"
            sample.write_text("print('hello')\n" * 5, encoding="utf-8")

            resolved = files.resolve_files([str(sample)], repo_root=None)
            self.assertEqual(len(resolved), 1)
            self.assertEqual(resolved[0].line_count, 5)
            self.assertFalse(resolved[0].in_repo)

            router_ctx = files.format_files_for_router(resolved)
            self.assertIn("sample.py", router_ctx)
            self.assertIn("print('hello')", router_ctx)

            agent_ctx = files.format_files_for_agent(resolved, using_worktree=True)
            self.assertIn("参照専用ファイル", agent_ctx)

    def test_load_task_text_combines_file_and_arg(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tf = Path(tmp) / "task.md"
            tf.write_text("File details", encoding="utf-8")
            combined = files.load_task_text("Extra note", str(tf))
            self.assertEqual(combined, "File details\n\nExtra note")


class TestDispatch(unittest.TestCase):
    def test_build_agent_shell_cmd_for_all_tools(self) -> None:
        pf = Path("/tmp/test prompt.md")

        cmd_claude_i = dispatch.build_agent_shell_cmd("claude", "sonnet", pf, "interactive")
        self.assertIn("claude --model sonnet", cmd_claude_i)
        self.assertIn("'/tmp/test prompt.md'", cmd_claude_i)

        cmd_claude_h = dispatch.build_agent_shell_cmd("claude", "opus", pf, "headless")
        self.assertIn("claude --model opus -p", cmd_claude_h)

        cmd_agy_i = dispatch.build_agent_shell_cmd(
            "agy", "gemini-3.8-flash-medium", pf, "interactive", agy_paste_fallback=False
        )
        self.assertIn("agy --model gemini-3.8-flash-medium --prompt-interactive", cmd_agy_i)

        cmd_agy_paste = dispatch.build_agent_shell_cmd(
            "agy", "gemini-3.8-flash-medium", pf, "interactive", agy_paste_fallback=True
        )
        self.assertNotIn("--prompt-interactive", cmd_agy_paste)
        self.assertIn("agy --model gemini-3.8-flash-medium;", cmd_agy_paste)

        cmd_agy_h = dispatch.build_agent_shell_cmd("agy", "gemini-4-neon-high", pf, "headless")
        self.assertIn("agy --model gemini-4-neon-high -p", cmd_agy_h)

        cmd_oc = dispatch.build_agent_shell_cmd(
            "opencode", "opencode/deepseek-v4.1-flash", pf, "interactive"
        )
        self.assertIn("opencode -m opencode/deepseek-v4.1-flash --prompt", cmd_oc)


class TestRouter(unittest.TestCase):
    def test_route_task_valid_json(self) -> None:
        fake_stdout = (
            '{"difficulty": "high", "tool": "agy", "model": "gemini-4-neon-high", '
            '"branch_slug": "Refactor Auth", "reason": "大規模変更のため", '
            '"refined_prompt": "認証モジュールをリファクタリングしてください。"}'
        )
        fake_proc = subprocess.CompletedProcess(args=[], returncode=0, stdout=fake_stdout, stderr="")
        with mock.patch.object(router, "run_cmd", return_value=fake_proc):
            decision = router.route_task("Refactor auth", "Git branch: main", "(none)", plan_first=True)

        self.assertFalse(decision.is_fallback)
        self.assertEqual(decision.tool, "agy")
        self.assertEqual(decision.model, "gemini-4-neon-high")
        self.assertEqual(decision.branch_slug, "refactor-auth")
        self.assertIn("合意", decision.refined_prompt)

    def test_route_task_disallowed_model_falls_back(self) -> None:
        fake_stdout = (
            '{"difficulty": "medium", "tool": "agy", "model": "Gemini 3.7 Flash (Low)", '
            '"branch_slug": "legacy", "reason": "old", "refined_prompt": "test"}'
        )
        fake_proc = subprocess.CompletedProcess(args=[], returncode=0, stdout=fake_stdout, stderr="")
        with redirect_stderr(io.StringIO()):
            with mock.patch.object(router, "run_cmd", return_value=fake_proc):
                decision = router.route_task("task", "ctx", "files", plan_first=False)

        self.assertTrue(decision.is_fallback)
        self.assertEqual(decision.tool, config.FALLBACK_DECISION["tool"])
        self.assertEqual(decision.model, config.FALLBACK_DECISION["model"])


class TestCLIAndStats(unittest.TestCase):
    def test_cli_parses_all_subcommands(self) -> None:
        parser = cli.build_parser()
        cases = [
            ["run", "--dry-run", "--tool", "agy", "--model", "gemini-4-neon-high", "hello"],
            ["handoff", "next step", "--tool", "claude", "--model", "opus"],
            ["review", "--staged", "--preset", "strong", "--roles", "--synthesize"],
            ["multi", "--preset", "fast", "solve this"],
            ["compare", "abc123", "--test-cmd", "pytest"],
            ["pick"],
            ["pick", "2"],
            ["pick", "abc123", "2", "--squash", "--force"],
            ["pick", "abc123", "--choice", "1", "--no-ff", "--keep-winner", "--dry-run"],
            ["list"],
            ["clean", "/tmp/wt", "--delete-branch", "--force"],
            ["stats", "--days", "7", "--recent", "5"],
        ]
        for argv in cases:
            args = parser.parse_args(argv)
            self.assertTrue(callable(args.func))

    def test_run_dry_run_manual_model(self) -> None:
        parser = cli.build_parser()
        args = parser.parse_args(
            ["run", "--dry-run", "--tool", "agy", "--model", "gemini-3.8-flash-medium", "Fix bug"]
        )
        buf = io.StringIO()
        with redirect_stdout(buf):
            run.cmd_run(args)
        out = buf.getvalue()
        self.assertIn("Selected   : agy (gemini-3.8-flash-medium)", out)
        self.assertIn("[Dry Run] Refined Prompt:", out)

    def test_run_rejects_partial_tool_or_model(self) -> None:
        parser = cli.build_parser()
        args = parser.parse_args(["run", "--tool", "agy", "Fix bug"])
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                run.cmd_run(args)

    def test_storage_and_stats_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "history.jsonl"
            ts = utils.now_iso()
            storage.append_jsonl(
                {
                    "ts": ts,
                    "event": "route",
                    "session_id": "sess0001",
                    "repo": "demo-repo",
                    "tool": "agy",
                    "model": "gemini-3.8-flash-medium",
                    "difficulty": "medium",
                    "router_latency_ms": 420,
                    "is_fallback": False,
                    "task": "Implement feature X",
                },
                log_file=log_path,
            )
            storage.append_jsonl(
                {
                    "ts": ts,
                    "event": "handoff",
                    "session_id": "sess0001",
                    "repo": "demo-repo",
                    "from_tool": "agy",
                    "from_model": "gemini-3.8-flash-medium",
                    "tool": "claude",
                    "model": "opus",
                },
                log_file=log_path,
            )

            parser = cli.build_parser()
            args = parser.parse_args(["stats", "--days", "30", "--repo", "demo-repo"])
            buf = io.StringIO()
            with mock.patch.dict(os.environ, {"AI_ROUTE_LOG_FILE": str(log_path)}):
                with redirect_stdout(buf):
                    stats.cmd_stats(args)
            out = buf.getvalue()
            self.assertIn("Total routed tasks : 1", out)
            self.assertIn("Total handoffs     : 1", out)
            self.assertIn("agy (gemini-3.8-flash-medium) -> claude (opus)", out)

    def test_compare_missing_log_exits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing_log = Path(tmp) / "missing.jsonl"
            parser = cli.build_parser()
            args = parser.parse_args(["compare"])
            with mock.patch.dict(os.environ, {"AI_ROUTE_LOG_FILE": str(missing_log)}):
                with redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit):
                        multi.cmd_compare(args)


class TestPickIntegration(unittest.TestCase):
    """End-to-end tests for `ai-route pick` using real temporary git repositories and worktrees."""

    def _init_repo_with_multi_worktrees(
        self, tmp_dir: Path, *, conflict_on_main: bool = False, dirty_winner: bool = False
    ) -> tuple[Path, Path, Path, Path, str, str]:
        repo = tmp_dir / "repo"
        repo.mkdir()
        log_path = tmp_dir / "history.jsonl"

        def git(args: list[str], cwd: Path = repo) -> None:
            subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)

        git(["init", "-b", "main"])
        git(["config", "user.email", "test@example.com"])
        git(["config", "user.name", "Test User"])
        (repo / "app.txt").write_text("base\n", encoding="utf-8")
        git(["add", "app.txt"])
        git(["commit", "-m", "initial"])

        wt1 = tmp_dir / "repo-worktrees" / "mabc123-1-claude-opus"
        wt2 = tmp_dir / "repo-worktrees" / "mabc123-2-agy-neon"
        wt1.parent.mkdir(parents=True, exist_ok=True)
        br1 = "ai/mabc123-1-claude-opus-0101-000001"
        br2 = "ai/mabc123-2-agy-neon-0101-000002"

        git(["worktree", "add", "-b", br1, str(wt1), "HEAD"])
        git(["worktree", "add", "-b", br2, str(wt2), "HEAD"])

        (wt1 / "app.txt").write_text("base\nwinner-1\n", encoding="utf-8")
        git(["add", "app.txt"], cwd=wt1)
        git(["commit", "-m", "solution 1"], cwd=wt1)
        if dirty_winner:
            (wt1 / "uncommitted.txt").write_text("wip\n", encoding="utf-8")

        (wt2 / "app.txt").write_text("base\nwinner-2\n", encoding="utf-8")
        git(["add", "app.txt"], cwd=wt2)
        git(["commit", "-m", "solution 2"], cwd=wt2)

        if conflict_on_main:
            (repo / "app.txt").write_text("base\nmain-change\n", encoding="utf-8")
            git(["add", "app.txt"])
            git(["commit", "-m", "diverging main change"])

        storage.append_jsonl(
            {
                "ts": utils.now_iso(),
                "event": "multi",
                "group_id": "abc123",
                "repo": "repo",
                "base_ref": "HEAD",
                "task": "Implement feature",
                "members": [
                    {
                        "index": 1,
                        "tool": "claude",
                        "model": "opus",
                        "branch": br1,
                        "workdir": str(wt1),
                    },
                    {
                        "index": 2,
                        "tool": "agy",
                        "model": "gemini-4-neon-high",
                        "branch": br2,
                        "workdir": str(wt2),
                    },
                ],
            },
            log_file=log_path,
        )
        return repo, wt1, wt2, log_path, br1, br2

    def test_pick_merges_winner_and_cleans_all_worktrees_and_branches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo, wt1, wt2, log_path, br1, br2 = self._init_repo_with_multi_worktrees(Path(tmp))
            parser = cli.build_parser()
            args = parser.parse_args(["pick", "2"])

            orig_cwd = Path.cwd()
            buf = io.StringIO()
            try:
                os.chdir(repo)
                with mock.patch.dict(os.environ, {"AI_ROUTE_LOG_FILE": str(log_path)}):
                    with redirect_stdout(buf):
                        multi.cmd_pick(args)
            finally:
                os.chdir(orig_cwd)

            self.assertEqual((repo / "app.txt").read_text(encoding="utf-8"), "base\nwinner-2\n")
            self.assertFalse(wt1.exists())
            self.assertFalse(wt2.exists())
            self.assertFalse(multi._branch_exists(repo, br1))
            self.assertFalse(multi._branch_exists(repo, br2))

            events = storage.load_jsonl(log_path)
            pick_events = [e for e in events if e.get("event") == "pick"]
            self.assertEqual(len(pick_events), 1)
            self.assertEqual(pick_events[0]["winner_model"], "gemini-4-neon-high")

            # Verify stats displays Multi Pick Winners
            stats_args = parser.parse_args(["stats", "--days", "30", "--repo", "repo"])
            stats_buf = io.StringIO()
            with mock.patch.dict(os.environ, {"AI_ROUTE_LOG_FILE": str(log_path)}):
                with redirect_stdout(stats_buf):
                    stats.cmd_stats(stats_args)
            self.assertIn("Multi Pick Winners", stats_buf.getvalue())
            self.assertIn("agy (gemini-4-neon-high)", stats_buf.getvalue())

    def test_pick_keep_winner_preserves_winner_and_cleans_loser(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo, wt1, wt2, log_path, br1, br2 = self._init_repo_with_multi_worktrees(Path(tmp))
            parser = cli.build_parser()
            args = parser.parse_args(["pick", "abc123", "1", "--keep-winner"])

            orig_cwd = Path.cwd()
            try:
                os.chdir(repo)
                with mock.patch.dict(os.environ, {"AI_ROUTE_LOG_FILE": str(log_path)}):
                    with redirect_stdout(io.StringIO()):
                        multi.cmd_pick(args)
            finally:
                os.chdir(orig_cwd)

            self.assertEqual((repo / "app.txt").read_text(encoding="utf-8"), "base\nwinner-1\n")
            self.assertTrue(wt1.exists())
            self.assertTrue(multi._branch_exists(repo, br1))
            self.assertFalse(wt2.exists())
            self.assertFalse(multi._branch_exists(repo, br2))

    def test_pick_dry_run_does_not_modify_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo, wt1, wt2, log_path, br1, br2 = self._init_repo_with_multi_worktrees(Path(tmp))
            parser = cli.build_parser()
            args = parser.parse_args(["pick", "abc123", "1", "--dry-run"])

            orig_cwd = Path.cwd()
            buf = io.StringIO()
            try:
                os.chdir(repo)
                with mock.patch.dict(os.environ, {"AI_ROUTE_LOG_FILE": str(log_path)}):
                    with redirect_stdout(buf):
                        multi.cmd_pick(args)
            finally:
                os.chdir(orig_cwd)

            self.assertIn("[Dry Run] Planned cleanup:", buf.getvalue())
            self.assertEqual((repo / "app.txt").read_text(encoding="utf-8"), "base\n")
            self.assertTrue(wt1.exists())
            self.assertTrue(wt2.exists())
            self.assertTrue(multi._branch_exists(repo, br1))
            self.assertTrue(multi._branch_exists(repo, br2))

    def test_pick_refuses_dirty_winner_without_force(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo, wt1, wt2, log_path, br1, br2 = self._init_repo_with_multi_worktrees(
                Path(tmp), dirty_winner=True
            )
            parser = cli.build_parser()
            args = parser.parse_args(["pick", "abc123", "1"])

            orig_cwd = Path.cwd()
            err = io.StringIO()
            try:
                os.chdir(repo)
                with mock.patch.dict(os.environ, {"AI_ROUTE_LOG_FILE": str(log_path)}):
                    with redirect_stdout(io.StringIO()), redirect_stderr(err):
                        with self.assertRaises(SystemExit) as ctx:
                            multi.cmd_pick(args)
            finally:
                os.chdir(orig_cwd)

            self.assertEqual(ctx.exception.code, 1)
            self.assertIn("uncommitted changes", err.getvalue())
            self.assertTrue(wt1.exists())
            self.assertTrue(wt2.exists())
            self.assertTrue(multi._branch_exists(repo, br1))
            self.assertTrue(multi._branch_exists(repo, br2))

    def test_pick_merge_conflict_keeps_all_worktrees_intact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo, wt1, wt2, log_path, br1, br2 = self._init_repo_with_multi_worktrees(
                Path(tmp), conflict_on_main=True
            )
            parser = cli.build_parser()
            args = parser.parse_args(["pick", "abc123", "1"])

            orig_cwd = Path.cwd()
            err = io.StringIO()
            try:
                os.chdir(repo)
                with mock.patch.dict(os.environ, {"AI_ROUTE_LOG_FILE": str(log_path)}):
                    with redirect_stdout(io.StringIO()), redirect_stderr(err):
                        with self.assertRaises(SystemExit) as ctx:
                            multi.cmd_pick(args)
            finally:
                os.chdir(orig_cwd)

            self.assertNotEqual(ctx.exception.code, 0)
            self.assertIn("No worktrees or branches were deleted", err.getvalue())
            self.assertTrue(wt1.exists())
            self.assertTrue(wt2.exists())
            self.assertTrue(multi._branch_exists(repo, br1))
            self.assertTrue(multi._branch_exists(repo, br2))

    def test_pick_interactive_selection_and_squash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo, wt1, wt2, log_path, br1, br2 = self._init_repo_with_multi_worktrees(Path(tmp))
            parser = cli.build_parser()
            args = parser.parse_args(["pick", "--squash"])

            orig_cwd = Path.cwd()
            buf = io.StringIO()
            try:
                os.chdir(repo)
                with mock.patch.dict(os.environ, {"AI_ROUTE_LOG_FILE": str(log_path)}):
                    with mock.patch("builtins.input", return_value="1"):
                        with redirect_stdout(buf):
                            multi.cmd_pick(args)
            finally:
                os.chdir(orig_cwd)

            self.assertEqual((repo / "app.txt").read_text(encoding="utf-8"), "base\nwinner-1\n")
            self.assertIn("Squash-merged changes are staged", buf.getvalue())
            self.assertFalse(wt1.exists())
            self.assertFalse(wt2.exists())
            self.assertFalse(multi._branch_exists(repo, br1))
            self.assertFalse(multi._branch_exists(repo, br2))


if __name__ == "__main__":
    unittest.main()
