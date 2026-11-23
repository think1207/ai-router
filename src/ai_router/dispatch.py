"""Agent shell command builders, headless execution (incl. ACP for `agy`), and tmux dispatch."""

from __future__ import annotations

import os
import shlex
import subprocess
import tempfile
import time
from pathlib import Path

from ai_router.acp import RC_COMMAND_NOT_FOUND, RC_TIMEOUT, run_acp_headless_once
from ai_router.config import (
    AGY_PASTE_DELAY_SEC,
    AGY_PASTE_FALLBACK,
    OPENCODE_ATTACH_URL,
    get_active_config,
    get_session_log_dir,
)
from ai_router.errors import AiRouteError
from ai_router.models import DispatchEntry
from ai_router.tools import (
    build_tool_headless_argv,
    build_tool_shell_invocation,
    get_tool_adapter,
)
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
    """
    resolved = Path(path).expanduser().resolve()
    if not resolved.exists():
        raise AiRouteError(f"Working directory does not exist: {resolved}")
    if not resolved.is_dir():
        raise AiRouteError(f"Working directory is not a directory: {resolved}")
    return resolved


def _with_cd(shell_cmd: str, workdir: Path) -> str:
    """Prefix `shell_cmd` with ``cd <workdir> &&`` so it only runs in that directory."""
    return f"cd {shlex.quote(str(workdir))} && {{ {shell_cmd}; }}"


def _absolute(path: Path | str) -> Path:
    """Make `path` absolute (without resolving symlinks) for embedding in a shell command."""
    return Path(os.path.abspath(os.path.expanduser(path)))


EXIT_LINE_PREFIX = "[ai-route] exit="
# Written to a session log before each new agent run on that session
RUN_START_LINE = "[ai-route] run-start"


def _agent_invocation(
    tool: str,
    model: str,
    pf: str,
    mode: str,
    agy_paste_fallback: bool = False,  # kept for signature compatibility; agy always uses ACP
    effort: str = "",
    acp_mode: str = "",
) -> str:
    del agy_paste_fallback
    cfg = get_active_config()
    attach_url = OPENCODE_ATTACH_URL or cfg.opencode_attach_url
    effective_acp_mode = acp_mode or cfg.agy_acp_mode or "auto"
    return build_tool_shell_invocation(
        tool,
        model,
        pf,
        mode,
        effort=effort,
        opencode_attach_url=attach_url,
        agy_acp_mode=effective_acp_mode,
        copilot_permission_flag=cfg.copilot_permission_flag,
    )


def build_agent_shell_cmd(
    tool: str,
    model: str,
    prompt_file: Path,
    mode: str,
    *,
    agy_paste_fallback: bool = AGY_PASTE_FALLBACK,
    log_path: Path | None = None,
    effort: str = "",
    acp_mode: str = "",
) -> str:
    """Build the shell command string executed inside a tmux pane or current shell.

    `agy` always runs via ACP mode (`agy acp --stdio`) through the built-in ACP bridge.
    With ``log_path``, the agent's exit code is echoed as ``[ai-route] exit=N`` and, in
    headless mode, all output is also tee'd into the log so `ai-route status` can inspect
    it later.
    """
    pf = shlex.quote(str(prompt_file))
    invocation = _agent_invocation(
        tool, model, pf, mode, agy_paste_fallback, effort=effort, acp_mode=acp_mode
    )
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
    read_only: bool = False,
    acp_mode: str = "",
) -> tuple[int, str, int]:
    """Run a single agent non-interactively and return `(returncode, output, latency_ms)`.

    `agy` always executes via ACP mode (`agy acp --stdio`), defaulting to `auto` mode
    (or `plan` mode when `read_only=True`, such as planner/review/router stages).

    Launch failures never raise: a missing executable (or other ``OSError``) is reported
    as ``RC_COMMAND_NOT_FOUND`` (127) and a timeout as ``RC_TIMEOUT`` (124), with the
    reason included in ``output``.
    """
    cfg = get_active_config()
    adapter = get_tool_adapter(tool)
    if adapter is not None and adapter.uses_acp:
        effective_acp_mode = acp_mode or cfg.agy_acp_mode or "auto"
        return run_acp_headless_once(
            adapter.acp_cmd(),
            model,
            prompt,
            cwd,
            timeout=timeout,
            effort=effort,
            acp_mode=effective_acp_mode,
            read_only=read_only,
        )

    attach_url = OPENCODE_ATTACH_URL or cfg.opencode_attach_url
    cmd = build_tool_headless_argv(
        tool,
        model,
        prompt,
        effort=effort,
        opencode_attach_url=attach_url,
        copilot_permission_flag=cfg.copilot_permission_flag,
    )

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
    """Launch an agent inside a new tmux window/pane, or exec directly if outside tmux."""
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
    """Open a new tmux window with multiple tiled panes running agents in parallel."""
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
