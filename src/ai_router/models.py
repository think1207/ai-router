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
    stage: str = "single"  # "single" | "planner" | "implementer"
    parent_session: str = ""


@dataclasses.dataclass
class BlockSignal:
    """Evidence that an agent stopped waiting for a human decision (or failed to run).

    ``kind`` is how it was detected: ``marker`` (explicit BLOCKED_MARKER), ``pattern``
    (question-like output tail), or ``exit`` (return code). ``reason`` is one of
    ``blocked`` | ``timeout`` | ``launch_failed``.
    """

    kind: str
    reason: str
    question: str = ""
    excerpt: str = ""
    options: list[str] = dataclasses.field(default_factory=list)
    recommendation: str = ""

    def to_dict(self) -> dict[str, object]:
        return dataclasses.asdict(self)


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
    log_path: Path | None = None
