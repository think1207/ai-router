"""Agent shell command builders, headless execution, and tmux dispatch helpers."""

from __future__ import annotations

import os
import shlex
import subprocess
import tempfile
import time
from pathlib import Path

from ai_router.config import (
    AGY_PASTE_DELAY_SEC,
    AGY_PASTE_FALLBACK,
    OPENCODE_ATTACH_URL,
    get_session_log_dir,
)
from ai_router.errors import AiRouteError
from ai_router.models import DispatchEntry
from ai_router.utils import run_cmd


def write_prompt_file(prompt: str) -> Path:
    """Write a prompt to a temporary markdown file and return its path."""
    fd, path_str = tempfile.mkstemp(prefix="ai-route-prompt-", suffix=".md")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(prompt)
    return Path(path_str)


def resolve_workdir(path: Path | str) -> Path:
    """Return `path` as an absolute, symlink-free directory path, or raise `AiRouteError`.

    Every launch path (tmux ``-c``, the tmux client's own cwd, ``cd`` in the pane's shell,
    ``os.chdir`` outside tmux) goes through this so they all agree on the same directory.
    A missing directory is rejected up front: tmux would otherwise silently fall back to
    ``$HOME``, and ``subprocess`` would report a misleading "tmux: not found".
    """
    resolved = Path(path).expanduser().resolve()
    if not resolved.exists():
        raise AiRouteError(f"Working directory does not exist: {resolved}")
    if not resolved.is_dir():
        raise AiRouteError(f"Working directory is not a directory: {resolved}")
    return resolved


def _with_cd(shell_cmd: str, workdir: Path) -> str:
    """Prefix `shell_cmd` with ``cd <workdir> &&`` so it only runs in that directory.

    The command is brace-grouped because it is a ``;``-separated sequence: without the group
    only its first statement would be guarded and the rest (including the final
    ``exec "$SHELL"``) would still run in the wrong directory if ``cd`` failed.
    """
    return f"cd {shlex.quote(str(workdir))} && {{ {shell_cmd}; }}"


def _absolute(path: Path | str) -> Path:
    """Make `path` absolute (without resolving symlinks) for embedding in a shell command.

    Such commands run in another process/cwd (the pane after ``cd``, or tmux's pipe-pane
    job), so a relative path would point somewhere else than where it was computed.
    """
    return Path(os.path.abspath(os.path.expanduser(path)))


EXIT_LINE_PREFIX = "[ai-route] exit="
# Written to a session log before each new agent run on that session
RUN_START_LINE = "[ai-route] run-start"


def _agent_invocation(
    tool: str,
    model: str,
    pf: str,
    mode: str,
    agy_paste_fallback: bool,
    effort: str = "",
) -> str:
    m = shlex.quote(model)
    effort_flag = f"--effort {shlex.quote(effort)} " if effort else ""
    if tool == "claude":
        if mode == "headless":
            return f'claude --model {m} {effort_flag}-p "$(cat {pf})"'
        return f'claude --model {m} {effort_flag}"$(cat {pf})"'

    if tool == "opencode":
        attach_flag = f"--attach {shlex.quote(OPENCODE_ATTACH_URL)} " if OPENCODE_ATTACH_URL else ""
        if mode == "headless":
            return f'opencode run {attach_flag}-m {m} "$(cat {pf})"'
        return f'opencode -m {m} --prompt "$(cat {pf})"'

    if tool == "agy":
        if mode == "headless":
            return f'agy --model {m} {effort_flag}-p "$(cat {pf})"'
        if agy_paste_fallback:
            return f"agy --model {m}" + (f" --effort {shlex.quote(effort)}" if effort else "")
        return f'agy --model {m} {effort_flag}--prompt-interactive "$(cat {pf})"'

    raise ValueError(f"Unsupported tool: {tool}")


def build_agent_shell_cmd(
    tool: str,
    model: str,
    prompt_file: Path,
    mode: str,
    *,
    agy_paste_fallback: bool = AGY_PASTE_FALLBACK,
    log_path: Path | None = None,
    effort: str = "",
) -> str:
    """Build the shell command string executed inside a tmux pane or current shell.

    With ``log_path``, the agent's exit code is echoed as ``[ai-route] exit=N`` and, in
    headless mode, all output is also tee'd into the log so `ai-route status` can inspect
    it later. Interactive output is captured separately via ``tmux pipe-pane`` (piping a
    TUI would break its TTY). Without ``log_path`` the command is unchanged.
    """
    pf = shlex.quote(str(prompt_file))
    invocation = _agent_invocation(tool, model, pf, mode, agy_paste_fallback, effort=effort)
    tail = f'rm -f {pf}; exec "$SHELL"'
    if log_path is None:
        return f"{invocation}; {tail}"

    lf = shlex.quote(str(_absolute(log_path)))
    exit_echo = f'echo "{EXIT_LINE_PREFIX}$?"'
    if mode == "headless":
        return f"{{ {invocation}; {exit_echo}; }} 2>&1 | tee -a {lf}; {tail}"
    return f"{invocation}; {exit_echo}; {tail}"


def session_log_path(session_id: str) -> Path:
    """Return (and create the parent of) the output log path for a session."""
    path = get_session_log_dir() / f"{session_id}.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def begin_session_log(session_id: str, tool: str, model: str) -> Path:
    """Return the session log path after appending a RUN_START_LINE header for a new run."""
    path = session_log_path(session_id)
    with path.open("a", encoding="utf-8") as f:
        f.write(f"{RUN_START_LINE} {time.strftime('%Y-%m-%dT%H:%M:%S')} {tool}:{model}\n")
    return path


def _pipe_pane_to_log(pane_id: str, log_path: Path) -> None:
    """Mirror an interactive pane's output into `log_path` (best effort)."""
    run_cmd(
        ["tmux", "pipe-pane", "-o", "-t", pane_id, f"cat >> {shlex.quote(str(_absolute(log_path)))}"],
        check=False,
    )


# Shell-style return codes reported when an agent could not run to completion
RC_COMMAND_NOT_FOUND = 127
RC_TIMEOUT = 124


def _to_text(data: str | bytes | None) -> str:
    if data is None:
        return ""
    if isinstance(data, bytes):
        return data.decode("utf-8", errors="replace")
    return data


def run_agent_headless_once(
    tool: str,
    model: str,
    prompt: str,
    cwd: Path,
    *,
    timeout: float | None = None,
    effort: str = "",
) -> tuple[int, str, int]:
    """Run a single agent non-interactively and return `(returncode, output, latency_ms)`.

    Launch failures never raise: a missing executable (or other ``OSError``) is reported
    as ``RC_COMMAND_NOT_FOUND`` (127) and a timeout as ``RC_TIMEOUT`` (124), with the
    reason included in ``output``.
    """
    if tool == "claude":
        cmd = ["claude", "--model", model]
        if effort:
            cmd.extend(["--effort", effort])
        cmd.extend(["-p", prompt])
    elif tool == "agy":
        cmd = ["agy", "--model", model]
        if effort:
            cmd.extend(["--effort", effort])
        cmd.extend(["-p", prompt])
    elif tool == "opencode":
        cmd = ["opencode", "run"]
        if OPENCODE_ATTACH_URL:
            cmd.extend(["--attach", OPENCODE_ATTACH_URL])
        cmd.extend(["-m", model, prompt])
    else:
        raise ValueError(f"Unsupported tool: {tool}")

    t0 = time.monotonic()
    try:
        res = run_cmd(cmd, cwd=cwd, check=False, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        latency_ms = int((time.monotonic() - t0) * 1000)
        partial = _to_text(e.stdout).strip()
        reason = f"[error] Timed out after {timeout:g}s: {tool}:{model}"
        return RC_TIMEOUT, f"{reason}\n[partial output]\n{partial}" if partial else reason, latency_ms
    except OSError as e:
        latency_ms = int((time.monotonic() - t0) * 1000)
        return RC_COMMAND_NOT_FOUND, f"[error] Failed to launch '{cmd[0]}': {e}", latency_ms

    latency_ms = int((time.monotonic() - t0) * 1000)
    output = (res.stdout or "").strip()
    if res.returncode != 0 and res.stderr:
        output = f"{output}\n[stderr]\n{res.stderr.strip()}".strip()
    return res.returncode, output, latency_ms


def _paste_prompt_to_tmux_pane(pane_id: str, prompt_file: Path) -> None:
    """Fallback helper that pastes the prompt file into a tmux pane via tmux buffers."""
    run_cmd(["tmux", "load-buffer", str(prompt_file)])
    run_cmd(["tmux", "paste-buffer", "-t", pane_id])
    run_cmd(["tmux", "send-keys", "-t", pane_id, "Enter"])


def dispatch_to_tmux(
    *,
    shell_cmd: str,
    workdir: Path,
    win_name: str,
    tool: str,
    mode: str,
    prompt_file: Path,
    split_current: bool = False,
    pane_title: str = "",
    log_path: Path | None = None,
) -> None:
    """Launch an agent inside a new tmux window/pane, or exec directly if outside tmux.

    The agent always starts in `workdir` (resolved to an absolute path, which must exist):
    tmux gets it via ``-c``, the tmux client runs with it as its cwd, and the pane's shell
    command begins with ``cd <workdir> &&`` as a fallback.
    """
    workdir = resolve_workdir(workdir)

    if not os.environ.get("TMUX"):
        print("[info] Running outside tmux; launching agent in the current shell.")
        os.chdir(workdir)
        os.execvp("bash", ["bash", "-lc", shell_cmd])

    launch_cmd = _with_cd(shell_cmd, workdir)
    if split_current:
        tmux_cmd = [
            "tmux",
            "split-window",
            "-h",
            "-P",
            "-F",
            "#{pane_id}",
            "-c",
            str(workdir),
            launch_cmd,
        ]
    else:
        tmux_cmd = [
            "tmux",
            "new-window",
            "-P",
            "-F",
            "#{pane_id}",
            "-n",
            win_name,
            "-c",
            str(workdir),
            launch_cmd,
        ]

    pane_id = run_cmd(tmux_cmd, cwd=workdir).stdout.strip()
    if log_path is not None and mode == "interactive":
        _pipe_pane_to_log(pane_id, log_path)
    if pane_title:
        run_cmd(["tmux", "select-pane", "-t", pane_id, "-T", pane_title], check=False)

    if AGY_PASTE_FALLBACK and tool == "agy" and mode == "interactive":
        time.sleep(AGY_PASTE_DELAY_SEC)
        _paste_prompt_to_tmux_pane(pane_id, prompt_file)

    target_desc = "split pane" if split_current else f"window '{win_name}'"
    print(f"Dispatched to tmux {target_desc} ({pane_id}) in {workdir}")


def dispatch_tiled_panes(*, win_name: str, entries: list[DispatchEntry]) -> None:
    """Open a new tmux window with multiple tiled panes running agents in parallel.

    Every pane gets the same cwd guarantees as `dispatch_to_tmux`. All entries' workdirs are
    validated before the first window is created, so a bad entry never leaves a half-built
    window behind.
    """
    if not os.environ.get("TMUX"):
        raise AiRouteError("Parallel interactive mode requires running inside a tmux session.")

    workdirs = [resolve_workdir(entry.workdir) for entry in entries]

    pane_ids: list[tuple[str, DispatchEntry]] = []
    win_target = ""

    for idx, (entry, workdir) in enumerate(zip(entries, workdirs)):
        launch_cmd = _with_cd(entry.shell_cmd, workdir)
        if idx == 0:
            cmd = [
                "tmux",
                "new-window",
                "-P",
                "-F",
                "#{window_id} #{pane_id}",
                "-n",
                win_name,
                "-c",
                str(workdir),
                launch_cmd,
            ]
            out = run_cmd(cmd, cwd=workdir).stdout.strip().split()
            win_target, pane_id = out[0], out[1]
        else:
            cmd = [
                "tmux",
                "split-window",
                "-t",
                win_target,
                "-P",
                "-F",
                "#{pane_id}",
                "-c",
                str(workdir),
                launch_cmd,
            ]
            pane_id = run_cmd(cmd, cwd=workdir).stdout.strip()
            run_cmd(["tmux", "select-layout", "-t", win_target, "tiled"], check=False)

        run_cmd(["tmux", "select-pane", "-t", pane_id, "-T", entry.title], check=False)
        if entry.log_path is not None and entry.mode == "interactive":
            _pipe_pane_to_log(pane_id, entry.log_path)
        pane_ids.append((pane_id, entry))

    run_cmd(["tmux", "select-layout", "-t", win_target, "tiled"], check=False)

    if AGY_PASTE_FALLBACK:
        agy_panes = [
            (pid, entry)
            for pid, entry in pane_ids
            if entry.tool == "agy" and entry.mode == "interactive"
        ]
        if agy_panes:
            time.sleep(AGY_PASTE_DELAY_SEC)
            for pid, entry in agy_panes:
                _paste_prompt_to_tmux_pane(pid, entry.prompt_file)

    print(f"Dispatched {len(pane_ids)} panes in tmux window '{win_name}' ({win_target})")
