"""Rich session metadata: snapshot, worktree diff, output summary, and lookup by session ID.

Session metadata is stored per worktree (see `storage.save_session_meta`). The legacy keys
(`session_id`, `current_tool`, `current_model`, `handoff_count`, `started_at`) are always
kept so older `handoff` flows keep working.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from ai_router.config import OUTPUT_SUMMARY_CHARS, SESSION_DIFF_MAX_CHARS, SESSION_HISTORY_MAX
from ai_router.errors import AiRouteError
from ai_router.models import BlockSignal
from ai_router.security import redact_secrets
from ai_router.storage import load_jsonl, load_session_meta, save_session_meta
from ai_router.utils import now_iso, run_cmd


def truncate_text(text: str, max_chars: int, *, keep: str = "head") -> str:
    """Truncate text to `max_chars`, keeping the head (or tail) and noting the omission."""
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    omitted = len(text) - max_chars
    if keep == "tail":
        return f"... ({omitted} chars truncated)\n{text[-max_chars:]}"
    return f"{text[:max_chars]}\n... ({omitted} chars truncated)"


def summarize_output(output: str, max_chars: int = OUTPUT_SUMMARY_CHARS) -> str:
    """Keep the tail of an agent's output (with secrets redacted)."""
    redacted = redact_secrets((output or "").strip())
    return truncate_text(redacted, max_chars, keep="tail")


def capture_worktree_diff(cwd: Path, max_chars: int = SESSION_DIFF_MAX_CHARS) -> dict[str, Any]:
    """Return `{"diff_stat", "diff", "truncated"}` for uncommitted changes vs HEAD.

    Never raises: outside a git repository (or without git) it returns empty values.
    Untracked files are listed in `diff_stat` via `git status --short`.
    """
    empty: dict[str, Any] = {"diff_stat": "", "diff": "", "truncated": False}
    try:
        inside = run_cmd(["git", "rev-parse", "--is-inside-work-tree"], cwd=cwd, check=False)
        if inside.returncode != 0 or inside.stdout.strip() != "true":
            return empty
        has_head = run_cmd(["git", "rev-parse", "--verify", "--quiet", "HEAD"], cwd=cwd, check=False)
        diff_args = ["HEAD"] if has_head.returncode == 0 else ["--cached"]
        stat = run_cmd(["git", "diff", "--stat", *diff_args], cwd=cwd, check=False).stdout.strip()
        status = run_cmd(["git", "status", "--short"], cwd=cwd, check=False).stdout.strip()
        diff = run_cmd(["git", "diff", *diff_args], cwd=cwd, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return empty

    untracked = "\n".join(ln for ln in status.splitlines() if ln.startswith("??"))
    diff_stat = "\n".join(x for x in (stat, untracked) if x)
    redacted_diff = redact_secrets(diff)
    truncated = max_chars > 0 and len(redacted_diff) > max_chars
    return {
        "diff_stat": diff_stat,
        "diff": truncate_text(redacted_diff, max_chars),
        "truncated": truncated,
    }


def append_history(meta: dict[str, Any], event: str, **fields: Any) -> None:
    """Append a compact entry to `meta["history"]` (bounded by SESSION_HISTORY_MAX)."""
    history = meta.get("history")
    if not isinstance(history, list):
        history = []
    history.append({"ts": now_iso(), "event": event, **fields})
    meta["history"] = history[-SESSION_HISTORY_MAX:]


def build_session_meta(
    *,
    session_id: str,
    tool: str,
    model: str,
    workdir: Path,
    previous: dict[str, Any] | None = None,
    handoff_count: int | None = None,
    difficulty: str | None = None,
    refined_prompt: str | None = None,
    task: str | None = None,
    status: str = "running",
    output: str | None = None,
    blocked: BlockSignal | dict[str, Any] | None = None,
    include_diff: bool = False,
    diff_max_chars: int = SESSION_DIFF_MAX_CHARS,
    event: str = "",
    **extra: Any,
) -> dict[str, Any]:
    """Build session metadata, merging over `previous` so unknown keys survive.

    `None` arguments keep the previous value. `blocked` is cleared unless given (a new
    run supersedes the old block) — pass `blocked=...` to set it. When `event` is given an
    entry is appended to `history`.
    """
    prev = dict(previous or {})
    meta: dict[str, Any] = dict(prev)
    meta.update(
        {
            "session_id": session_id,
            "current_tool": tool,
            "current_model": model,
            "handoff_count": int(handoff_count if handoff_count is not None else prev.get("handoff_count", 0)),
            "started_at": prev.get("started_at") or now_iso(),
            "updated_at": now_iso(),
            "workdir": str(workdir),
            "status": status,
        }
    )
    for key, value in (("difficulty", difficulty), ("refined_prompt", refined_prompt), ("task", task)):
        if value is not None:
            meta[key] = value
        else:
            meta.setdefault(key, "")
    if output is not None:
        meta["last_output_summary"] = summarize_output(output)
    else:
        meta.setdefault("last_output_summary", "")
    if isinstance(blocked, BlockSignal):
        meta["blocked"] = blocked.to_dict()
    else:
        meta["blocked"] = blocked or None
    if include_diff:
        snap = capture_worktree_diff(workdir, diff_max_chars)
        meta["diff_stat"] = snap["diff_stat"]
        meta["diff"] = snap["diff"]
        meta["diff_truncated"] = snap["truncated"]
    else:
        meta.setdefault("diff_stat", "")
        meta.setdefault("diff", "")
    meta.update(extra)
    if not isinstance(meta.get("history"), list):
        meta["history"] = []
    if event:
        entry: dict[str, Any] = {"tool": tool, "model": model, "status": status}
        if meta["blocked"]:
            entry["blocked_reason"] = meta["blocked"].get("reason")
        append_history(meta, event, **entry)
    return meta


def find_session_workdir(session_id: str, *, log_file: Path | None = None) -> Path | None:
    """Find the workdir of a session from history.jsonl (latest matching event wins)."""
    found: Path | None = None
    for rec in load_jsonl(log_file, warn_malformed=False):
        if rec.get("session_id") == session_id and rec.get("workdir"):
            found = Path(str(rec["workdir"]))
        elif rec.get("event") == "multi":
            for m in rec.get("members", []) or []:
                if f"{rec.get('group_id')}-{m.get('index')}" == session_id and m.get("workdir"):
                    found = Path(str(m["workdir"]))
    return found


def resolve_session(from_session: str | None, cwd: Path) -> tuple[Path, dict[str, Any]]:
    """Return `(workdir, meta)` for a session ID, or for the current worktree when empty.

    Raises AiRouteError when the session or its metadata cannot be found.
    """
    if from_session and from_session not in {".", "current"}:
        local = load_session_meta(cwd)
        if local and local.get("session_id") == from_session:
            return cwd, local
        workdir = find_session_workdir(from_session)
        if workdir is None:
            raise AiRouteError(f"Session '{from_session}' not found in history.")
        if not workdir.is_dir():
            raise AiRouteError(f"Workdir of session '{from_session}' no longer exists: {workdir}")
        meta = load_session_meta(workdir)
        if not meta:
            raise AiRouteError(f"No session metadata found in {workdir}.")
        return workdir, meta

    meta = load_session_meta(cwd)
    if not meta:
        raise AiRouteError("No ai-route session metadata in the current worktree (use --from-session ID).")
    return cwd, meta


def update_session(workdir: Path, meta: dict[str, Any]) -> None:
    """Persist metadata (thin wrapper kept for symmetry with `resolve_session`)."""
    save_session_meta(workdir, meta)
