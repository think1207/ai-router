"""Tests for rich session metadata and worktree diff capture."""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ai_router import sessions, storage
from ai_router.errors import AiRouteError
from ai_router.models import BlockSignal

LEGACY_KEYS = ("session_id", "current_tool", "current_model", "handoff_count", "started_at")


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


class TestSessionMeta(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name).resolve()
        self.repo = _init_repo(self.tmp)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_roundtrip_keeps_legacy_and_new_keys(self) -> None:
        sig = BlockSignal(kind="marker", reason="blocked", question="Q?")
        meta = sessions.build_session_meta(
            session_id="abc12345",
            tool="claude",
            model="sonnet",
            workdir=self.repo,
            difficulty="medium",
            refined_prompt="do it",
            task="task",
            status="blocked",
            output="hello",
            blocked=sig,
            event="agent_result",
            group_id="g1",
        )
        storage.save_session_meta(self.repo, meta)
        loaded = storage.load_session_meta(self.repo)
        assert loaded is not None
        for key in LEGACY_KEYS:
            self.assertIn(key, loaded)
        for key in ("workdir", "difficulty", "refined_prompt", "task", "status",
                    "last_output_summary", "blocked", "diff_stat", "diff", "history"):
            self.assertIn(key, loaded)
        self.assertEqual(loaded["blocked"]["question"], "Q?")
        self.assertEqual(loaded["group_id"], "g1")
        self.assertEqual(loaded["history"][-1]["event"], "agent_result")
        self.assertEqual(loaded["history"][-1]["blocked_reason"], "blocked")

    def test_merge_preserves_previous_values(self) -> None:
        first = sessions.build_session_meta(
            session_id="s1", tool="claude", model="opus", workdir=self.repo,
            task="T", refined_prompt="P", event="route", decision={"x": 1},
        )
        second = sessions.build_session_meta(
            session_id="s1", tool="agy", model="g", workdir=self.repo, previous=first,
            handoff_count=1, event="handoff",
        )
        self.assertEqual(second["task"], "T")
        self.assertEqual(second["refined_prompt"], "P")
        self.assertEqual(second["decision"], {"x": 1})
        self.assertEqual(second["started_at"], first["started_at"])
        self.assertEqual(second["handoff_count"], 1)
        self.assertIsNone(second["blocked"])
        self.assertEqual([h["event"] for h in second["history"]], ["route", "handoff"])

    def test_capture_diff_and_truncation(self) -> None:
        (self.repo / "app.txt").write_text("base\n" + "changed line\n" * 200, encoding="utf-8")
        (self.repo / "new.txt").write_text("untracked\n", encoding="utf-8")
        snap = sessions.capture_worktree_diff(self.repo, max_chars=100000)
        self.assertIn("app.txt", snap["diff_stat"])
        self.assertIn("?? new.txt", snap["diff_stat"])
        self.assertIn("+changed line", snap["diff"])
        self.assertFalse(snap["truncated"])

        small = sessions.capture_worktree_diff(self.repo, max_chars=50)
        self.assertTrue(small["truncated"])
        self.assertTrue(small["diff"].startswith(snap["diff"][:50]))
        self.assertIn("chars truncated", small["diff"])

        meta = sessions.build_session_meta(
            session_id="s", tool="claude", model="m", workdir=self.repo,
            include_diff=True, diff_max_chars=50,
        )
        self.assertTrue(meta["diff_truncated"])

    def test_capture_diff_outside_git_is_safe(self) -> None:
        plain = self.tmp / "plain"
        plain.mkdir()
        self.assertEqual(
            sessions.capture_worktree_diff(plain),
            {"diff_stat": "", "diff": "", "truncated": False},
        )
        with mock.patch.object(sessions, "run_cmd", side_effect=FileNotFoundError("git")):
            self.assertEqual(sessions.capture_worktree_diff(self.repo)["diff"], "")

    def test_history_is_bounded(self) -> None:
        meta: dict = {}
        for i in range(sessions.SESSION_HISTORY_MAX + 5):
            sessions.append_history(meta, "e", n=i)
        self.assertEqual(len(meta["history"]), sessions.SESSION_HISTORY_MAX)
        self.assertEqual(meta["history"][-1]["n"], sessions.SESSION_HISTORY_MAX + 4)

    def test_summarize_output_keeps_tail(self) -> None:
        out = sessions.summarize_output("a" * 50 + "TAIL", max_chars=10)
        self.assertTrue(out.endswith("TAIL"))
        self.assertIn("truncated", out)

    def test_resolve_session_by_id_and_cwd(self) -> None:
        log = self.tmp / "h.jsonl"
        meta = sessions.build_session_meta(session_id="sid1", tool="claude", model="m", workdir=self.repo)
        storage.save_session_meta(self.repo, meta)
        storage.append_jsonl({"event": "route", "session_id": "sid1", "workdir": str(self.repo)}, log_file=log)
        other = self.tmp / "other"
        other.mkdir()
        with mock.patch.dict(os.environ, {"AI_ROUTE_LOG_FILE": str(log)}):
            wd, loaded = sessions.resolve_session("sid1", other)
            self.assertEqual(wd, self.repo)
            self.assertEqual(loaded["session_id"], "sid1")
            wd2, _ = sessions.resolve_session(None, self.repo)
            self.assertEqual(wd2, self.repo)
            with self.assertRaises(AiRouteError):
                sessions.resolve_session("missing", other)
            with self.assertRaises(AiRouteError):
                sessions.resolve_session(None, other)

    def test_find_session_workdir_for_multi_member(self) -> None:
        log = self.tmp / "h.jsonl"
        storage.append_jsonl(
            {"event": "multi", "group_id": "g9", "members": [{"index": 2, "workdir": "/x/y"}]},
            log_file=log,
        )
        self.assertEqual(sessions.find_session_workdir("g9-2", log_file=log), Path("/x/y"))


if __name__ == "__main__":
    unittest.main()
