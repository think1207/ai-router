"""JSONL history logging and per-worktree session metadata persistence."""

from __future__ import annotations

import fcntl
import json
import sys
from pathlib import Path
from typing import Any

from ai_router.config import get_log_file
from ai_router.security import ensure_private_dir, ensure_private_file
from ai_router.utils import get_git_dir


def append_jsonl(record: dict[str, Any], *, log_file: Path | None = None) -> None:
    """Append a single JSON record to the history log file with an exclusive file lock."""
    target = log_file or get_log_file()
    ensure_private_dir(target.parent)
    line = json.dumps(record, ensure_ascii=False) + "\n"
    with target.open("a", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            f.write(line)
            f.flush()
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    ensure_private_file(target)


def load_jsonl(log_file: Path | None = None, *, warn_malformed: bool = True) -> list[dict[str, Any]]:
    """Read all valid JSON objects from the history log file."""
    target = log_file or get_log_file()
    if not target.is_file():
        return []

    records: list[dict[str, Any]] = []
    with target.open("r", encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                rec = json.loads(stripped)
            except json.JSONDecodeError:
                if warn_malformed:
                    print(f"[warn] Skipping malformed JSON at line {lineno}", file=sys.stderr)
                continue
            if isinstance(rec, dict):
                records.append(rec)
    return records


def session_meta_path(workdir: Path) -> Path | None:
    """Return the path to the session metadata file inside the worktree's git dir."""
    git_dir = get_git_dir(workdir)
    if not git_dir:
        return None
    return git_dir / "ai-route-session.json"


def save_session_meta(workdir: Path, meta: dict[str, Any]) -> None:
    """Persist current session metadata for subsequent `handoff` commands."""
    path = session_meta_path(workdir)
    if not path:
        return
    path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    ensure_private_file(path)


def load_session_meta(workdir: Path) -> dict[str, Any] | None:
    """Load saved session metadata for the current git worktree, if available."""
    path = session_meta_path(workdir)
    if not path or not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        return None
