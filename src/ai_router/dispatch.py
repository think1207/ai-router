"""Agent shell command builders, headless execution, and tmux dispatch helpers."""

from __future__ import annotations

import os
import shlex
import subprocess
import tempfile
import time
from pathlib import Path

from ai_router.config import AGY_PASTE_DELAY_SEC, AGY_PASTE_FALLBACK, OPENCODE_ATTACH_URL
from ai_router.errors import AiRouteError
from ai_router.models import DispatchEntry
from ai_router.utils import run_cmd


def write_prompt_file(prompt: str) -> Path:
    """Write a prompt to a temporary markdown file and return its path."""
    fd, path_str = tempfile.mkstemp(prefix="ai-route-prompt-", suffix=".md")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(prompt)
    return Path(path_str)


def build_agent_shell_cmd(
    tool: str,
    model: str,
    prompt_file: Path,
    mode: str,
    *,
    agy_paste_fallback: bool = AGY_PASTE_FALLBACK,
) -> str:
    """Build the shell command string executed inside a tmux pane or current shell."""
    pf = shlex.quote(str(prompt_file))
    m = shlex.quote(model)

    if tool == "claude":
        if mode == "headless":
            return f'claude --model {m} -p "$(cat {pf})"; rm -f {pf}; exec "$SHELL"'
        return f'claude --model {m} "$(cat {pf})"; rm -f {pf}; exec "$SHELL"'

    if tool == "opencode":
        attach_flag = f"--attach {shlex.quote(OPENCODE_ATTACH_URL)} " if OPENCODE_ATTACH_URL else ""
        if mode == "headless":
            return f'opencode run {attach_flag}-m {m} "$(cat {pf})"; rm -f {pf}; exec "$SHELL"'
        return f'opencode -m {m} --prompt "$(cat {pf})"; rm -f {pf}; exec "$SHELL"'

    if tool == "agy":
        if mode == "headless":
            return f'agy --model {m} -p "$(cat {pf})"; rm -f {pf}; exec "$SHELL"'
        if agy_paste_fallback:
            return f'agy --model {m}; rm -f {pf}; exec "$SHELL"'
        return f'agy --model {m} --prompt-interactive "$(cat {pf})"; rm -f {pf}; exec "$SHELL"'

    raise ValueError(f"Unsupported tool: {tool}")


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
) -> tuple[int, str, int]:
    """Run a single agent non-interactively and return `(returncode, output, latency_ms)`.

    Launch failures never raise: a missing executable (or other ``OSError``) is reported
    as ``RC_COMMAND_NOT_FOUND`` (127) and a timeout as ``RC_TIMEOUT`` (124), with the
    reason included in ``output``.
    """
    if tool == "claude":
        cmd = ["claude", "--model", model, "-p", prompt]
    elif tool == "agy":
        cmd = ["agy", "--model", model, "-p", prompt]
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
) -> None:
    """Launch an agent inside a new tmux window/pane, or exec directly if outside tmux."""
    if not os.environ.get("TMUX"):
        print("[info] Running outside tmux; launching agent in the current shell.")
        os.chdir(workdir)
        os.execvp("bash", ["bash", "-lc", shell_cmd])

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
            shell_cmd,
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
            shell_cmd,
        ]

    pane_id = run_cmd(tmux_cmd).stdout.strip()
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

    pane_ids: list[tuple[str, DispatchEntry]] = []
    win_target = ""

    for idx, entry in enumerate(entries):
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
                str(entry.workdir),
                entry.shell_cmd,
            ]
            out = run_cmd(cmd).stdout.strip().split()
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
                str(entry.workdir),
                entry.shell_cmd,
            ]
            pane_id = run_cmd(cmd).stdout.strip()
            run_cmd(["tmux", "select-layout", "-t", win_target, "tiled"], check=False)

        run_cmd(["tmux", "select-pane", "-t", pane_id, "-T", entry.title], check=False)
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
