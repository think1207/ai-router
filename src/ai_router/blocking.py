"""Detection of agents that stopped waiting for a human decision.

Everything here is a pure function over `(returncode, output)` so it can be applied
equally to synchronous headless runs and to logs captured from tmux panes.
"""

from __future__ import annotations

import re

from ai_router.config import BLOCKED_MARKER, BLOCKED_TAIL_LINES, get_blocked_patterns
from ai_router.dispatch import RC_COMMAND_NOT_FOUND, RC_TIMEOUT, RUN_START_LINE
from ai_router.models import BlockSignal

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-_]")
_EXIT_LINE_RE = re.compile(r"^\[ai-route\] exit=(-?\d+)\s*$", re.MULTILINE)
_FIELD_RE = re.compile(
    r"^\s*[-*]?\s*(質問|選択肢|推奨|question|options?|recommendation)\s*[:：]\s*(.*)$",
    re.IGNORECASE,
)
_FIELD_KEYS = {
    "質問": "question",
    "question": "question",
    "選択肢": "options",
    "option": "options",
    "options": "options",
    "推奨": "recommendation",
    "recommendation": "recommendation",
}
_BULLET_RE = re.compile(r"^\s*(?:[-*]|\d+[.)]|[A-Za-z][.)])\s+")
EXCERPT_MAX_CHARS = 1200


def strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences and carriage returns (e.g. from tmux pipe-pane logs)."""
    return _ANSI_RE.sub("", text).replace("\r", "")


def parse_session_log(text: str) -> tuple[int | None, str]:
    """Split a captured session log into `(returncode, output)` for the latest run.

    Only the text after the last RUN_START_LINE is considered. The returncode comes from
    the last ``[ai-route] exit=N`` line (None while the agent is still running).
    """
    clean = strip_ansi(text)
    start = clean.rfind(RUN_START_LINE)
    if start != -1:
        newline = clean.find("\n", start)
        clean = clean[newline + 1 :] if newline != -1 else ""
    rc: int | None = None
    for m in _EXIT_LINE_RE.finditer(clean):
        rc = int(m.group(1))
    output = _EXIT_LINE_RE.sub("", clean).strip()
    return rc, output


def _tail_lines(output: str, n: int = BLOCKED_TAIL_LINES) -> list[str]:
    lines = [ln.rstrip() for ln in output.splitlines() if ln.strip()]
    return lines[-n:]


def _excerpt(lines: list[str]) -> str:
    text = "\n".join(lines)
    return text[-EXCERPT_MAX_CHARS:]


def _split_options(raw: str) -> list[str]:
    parts = re.split(r"\s*(?:/|／|、|,|\|)\s*", raw.strip())
    return [p for p in (x.strip() for x in parts) if p]


def _parse_marker(output: str) -> BlockSignal | None:
    """Parse the last real BLOCKED_MARKER block (ignoring echoed template placeholders)."""
    idx = output.rfind(BLOCKED_MARKER)
    while idx != -1:
        after = output[idx + len(BLOCKED_MARKER) :]
        fields: dict[str, list[str]] = {}
        current: str | None = None
        for line in after.splitlines():
            m = _FIELD_RE.match(line)
            if m:
                current = _FIELD_KEYS[m.group(1).lower()]
                fields.setdefault(current, [])
                if m.group(2).strip():
                    fields[current].append(m.group(2).strip())
            elif current and line.strip():
                if current == "options" and _BULLET_RE.match(line):
                    fields[current].append(line.strip())
                elif current != "options":
                    fields[current].append(line.strip())
            elif not line.strip():
                current = None if current == "options" else current

        question = " ".join(fields.get("question", [])).strip()
        if question.startswith("<") and question.endswith(">"):
            # Echoed instruction template, not a real block; try an earlier marker.
            idx = output.rfind(BLOCKED_MARKER, 0, idx)
            continue

        options: list[str] = []
        for raw in fields.get("options", []):
            bullet = _BULLET_RE.match(raw)
            options.extend([raw[bullet.end() :].strip()] if bullet else _split_options(raw))
        before_lines = _tail_lines(output[:idx], 5)
        return BlockSignal(
            kind="marker",
            reason="blocked",
            question=question,
            excerpt=_excerpt(before_lines + [BLOCKED_MARKER] + after.strip().splitlines()),
            options=options,
            recommendation=" ".join(fields.get("recommendation", [])).strip(),
        )
    return None


def _find_question(lines: list[str], *, allow_question_mark: bool) -> str:
    """Return the last tail line that looks like a question to the user ("" if none)."""
    patterns = get_blocked_patterns()
    for line in reversed(lines):
        if any(p.search(line) for p in patterns):
            return line.strip()
    if allow_question_mark and lines and re.search(r"[?？]\s*$", lines[-1]):
        return lines[-1].strip()
    return ""


def detect_blocked(rc: int | None, output: str) -> BlockSignal | None:
    """Detect whether an agent run ended waiting for a decision (or could not run).

    Priority:
      1. Explicit BLOCKED_MARKER in the output (kind="marker", reason="blocked").
      2. Return code: RC_TIMEOUT -> "timeout", RC_COMMAND_NOT_FOUND -> "launch_failed",
         other non-zero -> "blocked" only when the output tail is a question.
      3. Question patterns in the output tail (kind="pattern", reason="blocked").

    ``rc=None`` means "unknown / still running" and skips the return-code step.
    Returns None when nothing indicates a block.
    """
    output = strip_ansi(output or "")

    marker = _parse_marker(output)
    if marker:
        return marker

    lines = _tail_lines(output)
    if rc is not None and rc != 0:
        if rc == RC_TIMEOUT:
            return BlockSignal(kind="exit", reason="timeout", excerpt=_excerpt(lines))
        if rc == RC_COMMAND_NOT_FOUND:
            return BlockSignal(kind="exit", reason="launch_failed", excerpt=_excerpt(lines))
        question = _find_question(lines, allow_question_mark=True)
        if question:
            return BlockSignal(kind="exit", reason="blocked", question=question, excerpt=_excerpt(lines))
        return None

    question = _find_question(lines, allow_question_mark=False)
    if question:
        return BlockSignal(kind="pattern", reason="blocked", question=question, excerpt=_excerpt(lines))
    return None


def blocked_signature(signal: BlockSignal | None) -> str:
    """Stable identity of a block, used to avoid recording the same block twice."""
    if signal is None:
        return ""
    return f"{signal.kind}|{signal.reason}|{signal.question}"


def is_decision_block(signal: BlockSignal | None) -> bool:
    """True when the signal means 'needs a decision' (eligible for escalation)."""
    return bool(signal) and signal.reason == "blocked"  # type: ignore[union-attr]
