"""Core data models shared across ai-router modules."""

from __future__ import annotations

import dataclasses
from pathlib import Path


@dataclasses.dataclass
class RouteDecision:
    """Result of routing a task to a specific coding agent and model."""

    difficulty: str
    tool: str
    model: str
    branch_slug: str
    reason: str
    refined_prompt: str
    router_latency_ms: int = 0
    is_fallback: bool = False


@dataclasses.dataclass
class ResolvedFile:
    """Normalized file reference along with a truncated preview for the router."""

    display_path: str  # Path passed to agents (repo-relative when inside repo)
    abs_path: Path
    in_repo: bool
    is_tracked: bool
    line_count: int | None
    preview: str  # Truncated excerpt for the router prompt


@dataclasses.dataclass
class DispatchEntry:
    """Descriptor for launching a single agent pane/window in tmux."""

    shell_cmd: str
    workdir: Path
    tool: str
    model: str
    mode: str
    prompt_file: Path
    title: str
    win_name: str = ""
