"""Regression tests: agents launched via tmux must start in the intended (absolute) directory.

Covers `dispatch_to_tmux` (new-window / split-window / outside tmux), `dispatch_tiled_panes`,
workdir validation errors, and the absolute-path guarantees for log files.
"""

from __future__ import annotations

import io
import os
import shlex
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any
from unittest import mock

from ai_router import dispatch, flow
from ai_router.errors import AiRouteError
from ai_router.models import DispatchEntry

TMUX_ENV = {"TMUX": "/tmp/fake-tmux,1,0"}
LAUNCH_VERBS = ("new-window", "split-window")


class FakeTmux:
    """Stand-in for `dispatch.run_cmd` that records every call and fakes tmux's output."""

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dict[str, Any]]] = []
        self._pane_seq = 0

    def __call__(self, cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append((list(cmd), kwargs))
        out = ""
        if cmd[:2] == ["tmux", "new-window"] or cmd[:2] == ["tmux", "split-window"]:
            self._pane_seq += 1
            fmt = cmd[cmd.index("-F") + 1]
            out = f"@1 %{self._pane_seq}\n" if "window_id" in fmt else f"%{self._pane_seq}\n"
        return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")

    def launches(self) -> list[tuple[list[str], dict[str, Any]]]:
        return [c for c in self.calls if c[0][1] in LAUNCH_VERBS]

    def cmds(self) -> list[list[str]]:
        return [c[0] for c in self.calls]


def c_flag(cmd: list[str]) -> str:
    return cmd[cmd.index("-c") + 1]


class _ExecCalled(Exception):
    """Raised by the mocked os.execvp (the real one never returns)."""


class _TmpDirCase(unittest.TestCase):
    def setUp(self) -> None:
        # Safety net: no test may ever reach a real tmux server (it would open windows in the
        # developer's live session). Tests that need a recorder patch run_cmd themselves.
        guard = mock.patch.object(
            dispatch, "run_cmd", side_effect=AssertionError("test reached the real run_cmd/tmux")
        )
        guard.start()
        self.addCleanup(guard.stop)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name).resolve()
        self._orig_cwd = Path.cwd()
        self.addCleanup(os.chdir, self._orig_cwd)

    def make_dir(self, rel: str) -> Path:
        path = self.tmp / rel
        path.mkdir(parents=True)
        return path


class TestResolveWorkdir(_TmpDirCase):
    def test_relative_becomes_absolute(self) -> None:
        sub = self.make_dir("sub")
        os.chdir(self.tmp)
        for raw in (Path("sub"), Path("./sub/../sub"), "sub"):
            got = dispatch.resolve_workdir(raw)
            self.assertTrue(got.is_absolute())
            self.assertEqual(got, sub)

    def test_symlink_is_resolved(self) -> None:
        real = self.make_dir("real")
        link = self.tmp / "link"
        link.symlink_to(real)
        self.assertEqual(dispatch.resolve_workdir(link), real)

    def test_missing_dir_raises_clear_error(self) -> None:
        missing = self.tmp / "nope"
        with self.assertRaises(AiRouteError) as ctx:
            dispatch.resolve_workdir(missing)
        self.assertIn("does not exist", str(ctx.exception))
        self.assertIn(str(missing), str(ctx.exception))

    def test_file_is_rejected(self) -> None:
        f = self.tmp / "file.txt"
        f.write_text("x", encoding="utf-8")
        with self.assertRaises(AiRouteError) as ctx:
            dispatch.resolve_workdir(f)
        self.assertIn("not a directory", str(ctx.exception))


class TestWithCd(_TmpDirCase):
    def test_prefix_shape_and_quoting(self) -> None:
        wd = self.make_dir("a b's dir")
        wrapped = dispatch._with_cd("echo hi", wd)
        prefix = f"cd {shlex.quote(str(wd))} && "
        self.assertTrue(wrapped.startswith(prefix), wrapped)
        # shlex round-trip proves the path survives quoting intact
        self.assertEqual(shlex.split(wrapped)[:2], ["cd", str(wd)])

    def test_runs_in_target_dir_regardless_of_starting_cwd(self) -> None:
        wd = self.make_dir("a b's dir")
        other = self.make_dir("other")
        wrapped = dispatch._with_cd("pwd -P", wd)
        res = subprocess.run(["bash", "-c", wrapped], cwd=other, capture_output=True, text=True)
        self.assertEqual(res.returncode, 0)
        self.assertEqual(res.stdout.strip(), str(wd))

    def test_guards_whole_multi_statement_command(self) -> None:
        wd = self.make_dir("gone")
        other = self.make_dir("other")
        wd.rmdir()
        wrapped = dispatch._with_cd("echo first; echo second", wd)
        res = subprocess.run(["bash", "-c", wrapped], cwd=other, capture_output=True, text=True)
        self.assertNotEqual(res.returncode, 0)
        self.assertEqual(res.stdout, "")  # neither statement ran in the wrong directory


class TestDispatchToTmux(_TmpDirCase):
    def setUp(self) -> None:
        super().setUp()
        self.wd = self.make_dir("work/proj")
        self.pf = self.tmp / "prompt.md"
        self.pf.write_text("p", encoding="utf-8")
        self.fake = FakeTmux()

    def dispatch(self, **overrides: Any) -> str:
        kwargs: dict[str, Any] = {
            "shell_cmd": 'claude "$(cat p)"; exec "$SHELL"',
            "workdir": self.wd,
            "win_name": "w",
            "tool": "claude",
            "mode": "interactive",
            "prompt_file": self.pf,
        }
        kwargs.update(overrides)
        buf = io.StringIO()
        with mock.patch.dict(os.environ, TMUX_ENV), \
                mock.patch.object(dispatch, "run_cmd", self.fake), \
                redirect_stdout(buf):
            dispatch.dispatch_to_tmux(**kwargs)
        return buf.getvalue()

    def assert_launch_is_pinned(self, cmd: list[str], kwargs: dict[str, Any], expected: Path) -> None:
        self.assertEqual(c_flag(cmd), str(expected))
        self.assertTrue(Path(c_flag(cmd)).is_absolute())
        self.assertTrue(cmd[-1].startswith(f"cd {shlex.quote(str(expected))} && "), cmd[-1])
        self.assertEqual(kwargs.get("cwd"), expected)

    def test_new_window(self) -> None:
        self.dispatch()
        ((cmd, kwargs),) = self.fake.launches()
        self.assertEqual(cmd[1], "new-window")
        self.assertEqual(cmd[cmd.index("-n") + 1], "w")
        self.assert_launch_is_pinned(cmd, kwargs, self.wd)
        self.assertIn('claude "$(cat p)"; exec "$SHELL"', cmd[-1])

    def test_split_window(self) -> None:
        self.dispatch(split_current=True)
        ((cmd, kwargs),) = self.fake.launches()
        self.assertEqual(cmd[1], "split-window")
        self.assertIn("-h", cmd)
        self.assert_launch_is_pinned(cmd, kwargs, self.wd)

    def test_relative_workdir_is_made_absolute(self) -> None:
        os.chdir(self.tmp)
        for raw in (Path("work/proj"), Path("./work/../work/proj")):
            for split in (False, True):
                self.fake.calls.clear()
                out = self.dispatch(workdir=raw, split_current=split)
                ((cmd, kwargs),) = self.fake.launches()
                self.assert_launch_is_pinned(cmd, kwargs, self.wd)
                self.assertIn(str(self.wd), out)

    def test_missing_dir_errors_before_touching_tmux(self) -> None:
        with self.assertRaises(AiRouteError) as ctx:
            self.dispatch(workdir=self.tmp / "missing")
        self.assertIn("does not exist", str(ctx.exception))
        self.assertEqual(self.fake.calls, [])

    def test_pipe_pane_and_title_preserved(self) -> None:
        log = self.tmp / "logs" / "s1.log"
        self.dispatch(log_path=log, pane_title="claude:opus")
        cmds = self.fake.cmds()
        self.assertIn(["tmux", "pipe-pane", "-o", "-t", "%1", f"cat >> {shlex.quote(str(log))}"], cmds)
        self.assertIn(["tmux", "select-pane", "-t", "%1", "-T", "claude:opus"], cmds)
        # launch first, then pipe-pane, then title (original order)
        verbs = [c[1] for c in cmds]
        self.assertEqual(verbs, ["new-window", "pipe-pane", "select-pane"])

    def test_headless_does_not_pipe_pane(self) -> None:
        self.dispatch(mode="headless", log_path=self.tmp / "s.log")
        self.assertNotIn("pipe-pane", [c[1] for c in self.fake.cmds()])

    def test_relative_log_path_is_absolute_for_pipe_pane(self) -> None:
        os.chdir(self.tmp)
        self.dispatch(log_path=Path("logs/s1.log"))
        pipe = next(c for c in self.fake.cmds() if c[1] == "pipe-pane")
        self.assertEqual(pipe[-1], f"cat >> {shlex.quote(str(self.tmp / 'logs/s1.log'))}")

    def test_agy_paste_fallback_preserved(self) -> None:
        with mock.patch.object(dispatch, "AGY_PASTE_FALLBACK", True), \
                mock.patch.object(dispatch.time, "sleep") as sleep:
            self.dispatch(tool="agy")
        cmds = self.fake.cmds()
        sleep.assert_called_once_with(dispatch.AGY_PASTE_DELAY_SEC)
        self.assertEqual(
            [c for c in cmds if c[1] in ("load-buffer", "paste-buffer", "send-keys")],
            [
                ["tmux", "load-buffer", str(self.pf)],
                ["tmux", "paste-buffer", "-t", "%1"],
                ["tmux", "send-keys", "-t", "%1", "Enter"],
            ],
        )

    def test_agy_paste_not_used_for_other_tools(self) -> None:
        with mock.patch.object(dispatch, "AGY_PASTE_FALLBACK", True), \
                mock.patch.object(dispatch.time, "sleep"):
            self.dispatch(tool="claude")
        self.assertNotIn("load-buffer", [c[1] for c in self.fake.cmds()])


class TestDispatchOutsideTmux(_TmpDirCase):
    def run_outside(self, workdir: Path | str) -> tuple[mock.MagicMock, mock.MagicMock]:
        env = {k: v for k, v in os.environ.items() if k != "TMUX"}
        buf = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(dispatch.os, "chdir") as chdir, \
                mock.patch.object(dispatch.os, "execvp", side_effect=_ExecCalled) as execvp, \
                redirect_stdout(buf):
            with self.assertRaises(_ExecCalled):
                dispatch.dispatch_to_tmux(
                    shell_cmd="agent; exec \"$SHELL\"",
                    workdir=workdir,  # type: ignore[arg-type]
                    win_name="w",
                    tool="claude",
                    mode="interactive",
                    prompt_file=self.tmp / "p.md",
                )
        return chdir, execvp

    def test_chdir_uses_resolved_absolute_path(self) -> None:
        wd = self.make_dir("work/proj")
        os.chdir(self.tmp)
        chdir, execvp = self.run_outside(Path("./work/../work/proj"))
        chdir.assert_called_once_with(wd)
        execvp.assert_called_once_with("bash", ["bash", "-lc", 'agent; exec "$SHELL"'])

    def test_missing_dir_is_a_clear_error_not_a_chdir_crash(self) -> None:
        env = {k: v for k, v in os.environ.items() if k != "TMUX"}
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(dispatch.os, "chdir") as chdir, \
                mock.patch.object(dispatch.os, "execvp", side_effect=_ExecCalled) as execvp:
            with self.assertRaises(AiRouteError) as ctx:
                dispatch.dispatch_to_tmux(
                    shell_cmd="x",
                    workdir=self.tmp / "missing",
                    win_name="w",
                    tool="claude",
                    mode="interactive",
                    prompt_file=self.tmp / "p.md",
                )
        self.assertIn("does not exist", str(ctx.exception))
        chdir.assert_not_called()
        execvp.assert_not_called()


class TestDispatchTiledPanes(_TmpDirCase):
    def setUp(self) -> None:
        super().setUp()
        self.dirs = [self.make_dir(f"wt/{i}") for i in range(3)]
        self.fake = FakeTmux()

    def entry(self, idx: int, workdir: Path, **kw: Any) -> DispatchEntry:
        pf = self.tmp / f"p{idx}.md"
        pf.write_text("p", encoding="utf-8")
        defaults: dict[str, Any] = {
            "shell_cmd": f'agent{idx}; exec "$SHELL"',
            "workdir": workdir,
            "tool": "claude",
            "model": "m",
            "mode": "interactive",
            "prompt_file": pf,
            "title": f"t{idx}",
        }
        defaults.update(kw)
        return DispatchEntry(**defaults)

    def run_tiled(self, entries: list[DispatchEntry]) -> None:
        with mock.patch.dict(os.environ, TMUX_ENV), \
                mock.patch.object(dispatch, "run_cmd", self.fake), \
                redirect_stdout(io.StringIO()):
            dispatch.dispatch_tiled_panes(win_name="win", entries=entries)

    def test_every_pane_is_pinned_to_absolute_workdir(self) -> None:
        os.chdir(self.tmp)
        entries = [
            self.entry(0, self.dirs[0]),
            self.entry(1, Path("wt/1")),  # relative
            self.entry(2, Path("./wt/../wt/2")),  # relative, non-normalized
        ]
        self.run_tiled(entries)
        launches = self.fake.launches()
        self.assertEqual([c[0][1] for c in launches], ["new-window", "split-window", "split-window"])
        for (cmd, kwargs), expected, idx in zip(launches, self.dirs, range(3)):
            self.assertEqual(c_flag(cmd), str(expected))
            self.assertTrue(cmd[-1].startswith(f"cd {shlex.quote(str(expected))} && "), cmd[-1])
            self.assertIn(f'agent{idx}; exec "$SHELL"', cmd[-1])
            self.assertEqual(kwargs.get("cwd"), expected)

    def test_entries_are_not_mutated(self) -> None:
        os.chdir(self.tmp)
        entries = [self.entry(0, Path("wt/0"))]
        self.run_tiled(entries)
        self.assertEqual(entries[0].workdir, Path("wt/0"))
        self.assertEqual(entries[0].shell_cmd, 'agent0; exec "$SHELL"')

    def test_missing_dir_aborts_before_any_window_is_created(self) -> None:
        entries = [self.entry(0, self.dirs[0]), self.entry(1, self.tmp / "missing"), self.entry(2, self.dirs[2])]
        with self.assertRaises(AiRouteError) as ctx:
            self.run_tiled(entries)
        self.assertIn("does not exist", str(ctx.exception))
        self.assertEqual(self.fake.calls, [])

    def test_titles_pipe_pane_layout_and_agy_paste_preserved(self) -> None:
        log = self.tmp / "logs" / "a.log"
        entries = [
            self.entry(0, self.dirs[0], tool="agy", log_path=log),
            self.entry(1, self.dirs[1], mode="headless", log_path=self.tmp / "logs" / "b.log"),
        ]
        with mock.patch.object(dispatch, "AGY_PASTE_FALLBACK", True), \
                mock.patch.object(dispatch.time, "sleep"):
            self.run_tiled(entries)
        cmds = self.fake.cmds()
        self.assertIn(["tmux", "select-pane", "-t", "%1", "-T", "t0"], cmds)
        self.assertIn(["tmux", "select-pane", "-t", "%2", "-T", "t1"], cmds)
        # interactive pane is piped, headless pane is not
        self.assertEqual(
            [c for c in cmds if c[1] == "pipe-pane"],
            [["tmux", "pipe-pane", "-o", "-t", "%1", f"cat >> {shlex.quote(str(log))}"]],
        )
        self.assertIn(["tmux", "select-layout", "-t", "@1", "tiled"], cmds)
        # split targets the window created by the first new-window
        split = next(c for c in cmds if c[1] == "split-window")
        self.assertEqual(split[split.index("-t") + 1], "@1")
        # agy paste applies to the agy pane only
        self.assertEqual(
            [c for c in cmds if c[1] in ("load-buffer", "paste-buffer", "send-keys")],
            [
                ["tmux", "load-buffer", str(entries[0].prompt_file)],
                ["tmux", "paste-buffer", "-t", "%1"],
                ["tmux", "send-keys", "-t", "%1", "Enter"],
            ],
        )


class TestLogPathAbsolute(_TmpDirCase):
    def test_build_agent_shell_cmd_uses_absolute_log_path(self) -> None:
        os.chdir(self.tmp)
        expected = shlex.quote(str(self.tmp / "rel" / "s.log"))
        pf = self.tmp / "p.md"
        for mode in ("headless", "interactive"):
            cmd = dispatch.build_agent_shell_cmd("claude", "opus", pf, mode, log_path=Path("rel/s.log"))
            if mode == "headless":
                self.assertIn(f"tee -a {expected}", cmd)

    def test_absolute_log_path_output_unchanged(self) -> None:
        pf = self.tmp / "p.md"
        log = self.tmp / "s.log"
        cmd = dispatch.build_agent_shell_cmd("claude", "opus", pf, "headless", log_path=log)
        self.assertIn(f"tee -a {shlex.quote(str(log))}", cmd)


class TestLaunchImplementerWorkdir(_TmpDirCase):
    def setUp(self) -> None:
        super().setUp()
        self.wd = self.make_dir("proj")
        env = mock.patch.dict(os.environ, {"AI_ROUTE_LOG_FILE": str(self.tmp / "data" / "history.jsonl")})
        env.start()
        self.addCleanup(env.stop)
        self.dispatched: list[dict[str, Any]] = []

    def tearDown(self) -> None:
        for d in self.dispatched:
            Path(d["prompt_file"]).unlink(missing_ok=True)

    def launch(self, workdir: Path) -> None:
        def fake_dispatch(**kw: Any) -> None:
            self.dispatched.append(kw)

        with mock.patch.object(flow, "dispatch_to_tmux", side_effect=fake_dispatch), \
                redirect_stdout(io.StringIO()):
            flow.launch_implementer(
                tool="claude", model="opus", prompt="do it", workdir=workdir, mode="interactive",
                wait=False, session_id="sid", meta={"session_id": "sid"}, timeout=None,
            )

    def test_relative_workdir_reaches_dispatch_as_absolute(self) -> None:
        os.chdir(self.tmp)
        self.launch(Path("proj"))
        self.assertEqual(self.dispatched[0]["workdir"], self.wd)

    def test_missing_workdir_fails_before_any_side_effects(self) -> None:
        with self.assertRaises(AiRouteError):
            self.launch(self.tmp / "missing")
        self.assertEqual(self.dispatched, [])
        self.assertFalse((self.tmp / "data").exists())  # no session log dir/log created


if __name__ == "__main__":
    unittest.main()
