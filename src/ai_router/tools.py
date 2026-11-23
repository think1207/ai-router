"""Tool adapter registry and environment auto-detection for supported AI coding CLIs.

Supports:
- `claude`   : Claude Code CLI (`--model`, `--effort`)
- `agy`      : Antigravity CLI — always executed via ACP mode (`agy acp --stdio`)
- `opencode` : OpenCode CLI (`-m`, `opencode run`)
- `copilot`  : GitHub Copilot CLI (`--model`, `--reasoning-effort`, `-i` / `-s -p`)
- `codex`    : OpenAI Codex CLI (`-m`, `-c model_reasoning_effort=...`, `codex exec`)
"""

from __future__ import annotations

import dataclasses
import shlex
import shutil
import sys
from pathlib import Path
from typing import Sequence


@dataclasses.dataclass(frozen=True)
class ToolAdapter:
    """Descriptor for invoking a coding agent CLI."""

    name: str
    binary: str
    uses_acp: bool = False
    acp_args: tuple[str, ...] = ()
    default_router_model: str = ""

    def acp_cmd(self) -> list[str]:
        return [self.binary, *self.acp_args]


BUILTIN_TOOL_ADAPTERS: dict[str, ToolAdapter] = {
    "opencode": ToolAdapter(
        name="opencode",
        binary="opencode",
        uses_acp=False,
        default_router_model="opencode-go/deepseek-v4.1-flash",
    ),
    "agy": ToolAdapter(
        name="agy",
        binary="agy",
        uses_acp=True,
        acp_args=("acp", "--stdio"),
        default_router_model="gemini-3.8-flash-medium",
    ),
    "claude": ToolAdapter(
        name="claude",
        binary="claude",
        uses_acp=False,
        default_router_model="sonnet",
    ),
    "copilot": ToolAdapter(
        name="copilot",
        binary="copilot",
        uses_acp=False,
        acp_args=("--acp",),
        default_router_model="gpt-5.6-sol",
    ),
    "codex": ToolAdapter(
        name="codex",
        binary="codex",
        uses_acp=False,
        default_router_model="gpt-6.1-sol",
    ),
}


def get_tool_adapter(tool: str) -> ToolAdapter | None:
    """Return the built-in ToolAdapter for `tool`, or None if unknown."""
    return BUILTIN_TOOL_ADAPTERS.get(tool)


def is_tool_installed(tool: str) -> bool:
    """Return True if the CLI executable for `tool` is found on PATH."""
    adapter = get_tool_adapter(tool)
    binary = adapter.binary if adapter else tool
    return shutil.which(binary) is not None


def get_installed_tools(tools: Sequence[str]) -> tuple[str, ...]:
    """Return the subset of `tools` whose CLI binaries are currently installed on PATH."""
    return tuple(t for t in tools if is_tool_installed(t))


def clamp_effort_for_tool(tool: str, effort: str) -> str:
    """Normalize an effort level to one supported by `tool`."""
    eff = (effort or "").strip().lower()
    if not eff:
        return ""
    if tool == "codex":
        return "xhigh" if eff == "max" else eff
    if tool == "agy":
        return "xhigh" if eff == "max" else eff
    if tool == "opencode":
        return ""
    return eff


def build_tool_shell_invocation(
    tool: str,
    model: str,
    prompt_file_quoted: str,
    mode: str,
    *,
    effort: str = "",
    opencode_attach_url: str = "",
    agy_acp_mode: str = "auto",
    copilot_permission_flag: str = "--allow-all-tools",
) -> str:
    """Build the shell command (without trailing cleanup/exec) to run `tool` on `prompt_file_quoted`."""
    m = shlex.quote(model)
    eff = clamp_effort_for_tool(tool, effort)

    if tool == "claude":
        effort_flag = f"--effort {shlex.quote(eff)} " if eff else ""
        if mode == "headless":
            return f'claude --model {m} {effort_flag}-p "$(cat {prompt_file_quoted})"'
        return f'claude --model {m} {effort_flag}"$(cat {prompt_file_quoted})"'

    if tool == "opencode":
        attach_flag = f"--attach {shlex.quote(opencode_attach_url)} " if opencode_attach_url else ""
        if mode == "headless":
            return f'opencode run {attach_flag}-m {m} "$(cat {prompt_file_quoted})"'
        return f'opencode -m {m} --prompt "$(cat {prompt_file_quoted})"'

    if tool == "agy":
        # Always run `agy` via ACP mode (`agy acp --stdio`) through our built-in ACP bridge
        py = shlex.quote(sys.executable)
        parts = [
            py,
            "-m",
            "ai_router",
            "_acp-session",
            "--tool",
            "agy",
            "--model",
            m,
            "--mode",
            shlex.quote(mode),
            "--acp-mode",
            shlex.quote(agy_acp_mode or "auto"),
            "--prompt-file",
            prompt_file_quoted,
        ]
        if eff:
            parts.extend(["--effort", shlex.quote(eff)])
        return " ".join(parts)

    if tool == "copilot":
        effort_flag = f"--reasoning-effort {shlex.quote(eff)} " if eff else ""
        if mode == "headless":
            perm_flag = f"{copilot_permission_flag} " if copilot_permission_flag else ""
            return f'copilot --model {m} {effort_flag}{perm_flag}-s -p "$(cat {prompt_file_quoted})"'
        return f'copilot --model {m} {effort_flag}-i "$(cat {prompt_file_quoted})"'

    if tool == "codex":
        effort_flag = f'-c {shlex.quote(f"model_reasoning_effort={eff}")} ' if eff else ""
        if mode == "headless":
            return f'codex exec -m {m} {effort_flag}"$(cat {prompt_file_quoted})"'
        return f'codex -m {m} {effort_flag}"$(cat {prompt_file_quoted})"'

    raise ValueError(f"Unsupported tool: {tool}")


def build_tool_headless_argv(
    tool: str,
    model: str,
    prompt: str,
    *,
    effort: str = "",
    opencode_attach_url: str = "",
    copilot_permission_flag: str = "--allow-all-tools",
) -> list[str]:
    """Build the subprocess argv list for non-ACP headless execution."""
    eff = clamp_effort_for_tool(tool, effort)

    if tool == "claude":
        cmd = ["claude", "--model", model]
        if eff:
            cmd.extend(["--effort", eff])
        cmd.extend(["-p", prompt])
        return cmd

    if tool == "opencode":
        cmd = ["opencode", "run"]
        if opencode_attach_url:
            cmd.extend(["--attach", opencode_attach_url])
        cmd.extend(["-m", model, prompt])
        return cmd

    if tool == "copilot":
        cmd = ["copilot", "--model", model]
        if eff:
            cmd.extend(["--reasoning-effort", eff])
        if copilot_permission_flag:
            cmd.extend(shlex.split(copilot_permission_flag))
        cmd.extend(["-s", "-p", prompt])
        return cmd

    if tool == "codex":
        cmd = ["codex", "exec", "-m", model]
        if eff:
            cmd.extend(["-c", f"model_reasoning_effort={eff}"])
        cmd.append(prompt)
        return cmd

    raise ValueError(f"Unsupported non-ACP tool: {tool}")
