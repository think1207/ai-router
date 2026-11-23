"""Credit / quota exhaustion detection, cooldown tracking, and cross-service failover.

When a provider/tool runs out of credits or hits a hard quota/rate limit:
1. `detect_credit_exhausted(rc, output)` identifies the failure from CLI output.
2. `mark_tool_exhausted(tool, reason)` persists a cooldown entry in `provider_state.json`.
3. `get_exhausted_tools()` returns currently exhausted tools so the router and
   `select_alternative_candidate()` automatically route to another service.
"""

from __future__ import annotations

import datetime
import fcntl
import json
import re
from pathlib import Path
from typing import Any

from ai_router.blocking import strip_ansi
from ai_router.config import (
    DIFFICULTY_TO_DEFAULT_EFFORT,
    AppConfig,
    get_active_config,
    get_credit_cooldown_sec,
    get_credit_patterns,
    get_provider_state_file,
    get_strong_credit_patterns,
)
from ai_router.utils import now_iso

_DIFFICULTY_ORDER: tuple[str, ...] = ("low", "medium", "high")


def _now_utc() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _match_patterns(text: str, patterns: list[re.Pattern[str]]) -> str | None:
    """Return the first matching line (trimmed) if any pattern hits `text`, else None."""
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        for pat in patterns:
            if pat.search(stripped):
                return stripped[:200]
    for pat in patterns:
        m = pat.search(text)
        if m:
            return m.group(0).strip()[:200]
    return None


def detect_credit_exhausted(rc: int | None, output: str) -> str | None:
    """Detect whether an agent/router invocation failed due to exhausted credits or quota.

    Returns a short human-readable reason string when detected, or ``None``.
    - When ``rc == 0`` (clean success), returns ``None`` to avoid false positives when
      an agent merely discusses rate limits or billing errors in normal output.
    - When ``rc != 0``, checks all configured credit/quota/rate-limit patterns.
    - When ``rc is None`` (e.g. still-running / unexited tmux log), checks only the
      unambiguous strong credit-exhaustion patterns.
    """
    if rc == 0:
        return None
    clean = strip_ansi(output or "")
    if not clean.strip():
        return None
    if rc is None:
        return _match_patterns(clean, get_strong_credit_patterns())
    return _match_patterns(clean, get_credit_patterns())


def _empty_state() -> dict[str, Any]:
    return {"exhausted": {}}


def load_provider_state(path: Path | None = None) -> dict[str, Any]:
    """Read provider state JSON from disk (returns empty state if missing or malformed)."""
    target = path or get_provider_state_file()
    if not target.is_file():
        return _empty_state()
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("exhausted"), dict):
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return _empty_state()


def save_provider_state(state: dict[str, Any], path: Path | None = None) -> None:
    """Atomically/safely persist provider state JSON with an exclusive file lock."""
    target = path or get_provider_state_file()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(state, ensure_ascii=False, indent=2) + "\n"
    with target.open("a+", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            f.seek(0)
            f.truncate()
            f.write(payload)
            f.flush()
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def _parse_iso(ts: str) -> datetime.datetime | None:
    try:
        dt = datetime.datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt.astimezone(datetime.timezone.utc)
    except (TypeError, ValueError):
        return None


def get_exhausted_tools(
    *,
    now: datetime.datetime | None = None,
    path: Path | None = None,
) -> dict[str, dict[str, Any]]:
    """Return currently credit-exhausted tools (filtering out expired cooldowns)."""
    current = (now or _now_utc()).astimezone(datetime.timezone.utc)
    state = load_provider_state(path)
    exhausted = state.get("exhausted", {})
    active: dict[str, dict[str, Any]] = {}
    for tool, entry in exhausted.items():
        if not isinstance(entry, dict):
            continue
        until_str = str(entry.get("until") or "")
        if not until_str:
            active[tool] = entry
            continue
        until_dt = _parse_iso(until_str)
        if until_dt is None or until_dt > current:
            active[tool] = entry
    return active


def mark_tool_exhausted(
    tool: str,
    reason: str,
    *,
    model: str = "",
    cooldown_sec: float | None = None,
    path: Path | None = None,
) -> dict[str, Any]:
    """Record `tool` as credit-exhausted until `now + cooldown_sec`."""
    cd = get_credit_cooldown_sec() if cooldown_sec is None else cooldown_sec
    now_dt = datetime.datetime.now(datetime.timezone.utc).astimezone()
    until_dt = now_dt + datetime.timedelta(seconds=max(0.0, cd))
    entry = {
        "since": now_dt.isoformat(timespec="seconds"),
        "until": until_dt.isoformat(timespec="seconds"),
        "reason": reason,
        "model": model,
    }
    state = load_provider_state(path)
    state.setdefault("exhausted", {})[tool] = entry
    save_provider_state(state, path)
    return entry


def clear_tool_exhausted(tool: str | None = None, *, path: Path | None = None) -> list[str]:
    """Clear credit-exhausted status for a specific tool or all tools; return cleared names."""
    state = load_provider_state(path)
    exhausted = state.get("exhausted", {})
    if tool is None:
        cleared = sorted(exhausted.keys())
        state["exhausted"] = {}
    else:
        cleared = [tool] if tool in exhausted else []
        exhausted.pop(tool, None)
    if cleared:
        save_provider_state(state, path)
    return cleared


def format_remaining_cooldown(until_iso: str, *, now: datetime.datetime | None = None) -> str:
    """Format remaining cooldown time (e.g. '42m', '1h 15m', 'expired')."""
    until_dt = _parse_iso(until_iso)
    if until_dt is None:
        return "unknown"
    current = (now or _now_utc()).astimezone(datetime.timezone.utc)
    secs = int((until_dt - current).total_seconds())
    if secs <= 0:
        return "expired"
    mins, rem_s = divmod(secs, 60)
    hours, rem_m = divmod(mins, 60)
    if hours > 0:
        return f"{hours}h {rem_m}m"
    if mins > 0:
        return f"{mins}m"
    return f"{rem_s}s"


def select_alternative_candidate(
    difficulty: str,
    *,
    excluded_tools: set[str] | None = None,
    config: AppConfig | None = None,
) -> tuple[str, str, str] | None:
    """Select the best available `(tool, model, default_effort)` excluding `excluded_tools`.

    Prefers exact difficulty match; otherwise nearest difficulty with a preference for the
    stronger tier on ties (matching `twostage.select_candidate`). Returns ``None`` when no
    candidate remains outside `excluded_tools`.
    """
    cfg = config or get_active_config()
    excluded = excluded_tools or set()
    pool = [c for c in cfg.candidates if c["tool"] not in excluded]
    if not pool:
        return None

    target = _DIFFICULTY_ORDER.index(difficulty) if difficulty in _DIFFICULTY_ORDER else 1

    def rank(c: dict[str, str]) -> tuple[int, int]:
        level = _DIFFICULTY_ORDER.index(c["difficulty"]) if c["difficulty"] in _DIFFICULTY_ORDER else 1
        return abs(level - target), 0 if level >= target else 1

    best = min(pool, key=rank)
    default_effort = best.get("effort") or DIFFICULTY_TO_DEFAULT_EFFORT.get(best["difficulty"], "medium")
    return best["tool"], best["model"], default_effort
