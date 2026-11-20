"""Handlers for `status` (after-the-fact blocked detection) and `escalate`."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from ai_router.blocking import blocked_signature, detect_blocked, parse_session_log
from ai_router.config import STAGE_DEFAULTS, get_agent_timeout
from ai_router.errors import AiRouteError
from ai_router.flow import classify_status, escalate_session, launch_implementer, record_blocked
from ai_router.models import BlockSignal
from ai_router.sessions import append_history, resolve_session, summarize_output
from ai_router.storage import load_jsonl, load_session_meta, save_session_meta
from ai_router.twostage import build_resume_prompt, format_plan_sections, resolve_spec, select_candidate
from ai_router.utils import now_iso

# Read at most this many trailing bytes of a session log (TUI logs can grow large)
LOG_READ_MAX_BYTES = 256 * 1024


def _read_log_tail(path: Path) -> str:
    with path.open("rb") as f:
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(0, size - LOG_READ_MAX_BYTES))
        return f.read().decode("utf-8", errors="replace")


def refresh_session_from_log(workdir: Path, meta: dict[str, Any]) -> BlockSignal | None:
    """Re-inspect a session's output log and update `meta` (status/blocked) in place.

    Only re-detects when the log grew since the last check; a newly seen block is recorded
    as a `blocked` event (source="status"). Returns the current block signal, if any.
    """
    log_raw = str(meta.get("log_path") or "")
    log_path = Path(log_raw) if log_raw else None
    if log_path is None or not log_path.is_file():
        blocked = meta.get("blocked")
        return BlockSignal(**blocked) if isinstance(blocked, dict) and blocked else None

    size = log_path.stat().st_size
    checked = meta.get("log_checked") if isinstance(meta.get("log_checked"), dict) else {}
    if checked.get("size") == size:
        blocked = meta.get("blocked")
        return BlockSignal(**blocked) if isinstance(blocked, dict) and blocked else None

    rc, output = parse_session_log(_read_log_tail(log_path))
    signal = detect_blocked(rc, output)
    if signal is not None or rc is not None:
        status = classify_status(rc if rc is not None else 0, signal)
    else:
        status = "running"
    signature = blocked_signature(signal)

    if signal is not None and signature != checked.get("signature"):
        record_blocked(
            session_id=str(meta.get("session_id", "")),
            workdir=workdir,
            tool=str(meta.get("current_tool", "")),
            model=str(meta.get("current_model", "")),
            signal=signal,
            source="status",
            stage=str(meta.get("stage", "")),
        )
        append_history(meta, "blocked", reason=signal.reason, question=signal.question)

    meta["status"] = status
    meta["blocked"] = signal.to_dict() if signal else None
    meta["last_output_summary"] = summarize_output(output)
    meta["log_checked"] = {"size": size, "signature": signature}
    meta["updated_at"] = now_iso()
    save_session_meta(workdir, meta)
    return signal


def _status_targets(args: argparse.Namespace, cwd: Path) -> list[tuple[Path, dict[str, Any]]]:
    if args.group:
        event = None
        for rec in load_jsonl(warn_malformed=False):
            if rec.get("event") == "multi" and rec.get("group_id") == args.group:
                event = rec
        if event is None:
            raise AiRouteError(f"No `multi` run found with group ID '{args.group}'.")
        targets = []
        for m in event.get("members", []) or []:
            wd = Path(str(m.get("workdir", "")))
            meta = load_session_meta(wd) if wd.is_dir() else None
            if meta:
                targets.append((wd, meta))
        return targets

    if args.from_session:
        return [resolve_session(args.from_session, cwd)]

    local = load_session_meta(cwd)
    if local:
        return [(cwd, local)]

    # No session here: show the most recent sessions that still exist
    seen: dict[str, Path] = {}
    for rec in load_jsonl(warn_malformed=False):
        if rec.get("event") in {"route", "handoff"} and rec.get("workdir"):
            seen.pop(str(rec["workdir"]), None)
            seen[str(rec["workdir"])] = Path(str(rec["workdir"]))
    targets = []
    for wd in reversed(list(seen.values())):
        meta = load_session_meta(wd) if wd.is_dir() else None
        if meta:
            targets.append((wd, meta))
        if len(targets) >= args.recent:
            break
    return targets


def cmd_status(args: argparse.Namespace) -> None:
    """Show session status, detecting blocked agents from their captured output logs."""
    targets = _status_targets(args, Path.cwd())
    if not targets:
        print("No ai-route sessions found.")
        return

    rows: list[dict[str, Any]] = []
    for workdir, meta in targets:
        signal = refresh_session_from_log(workdir, meta)
        rows.append(
            {
                "session_id": meta.get("session_id", ""),
                "tool": meta.get("current_tool", ""),
                "model": meta.get("current_model", ""),
                "status": meta.get("status", "unknown"),
                "workdir": str(workdir),
                "log_path": meta.get("log_path", ""),
                "blocked": signal.to_dict() if signal else None,
                "decision": bool(meta.get("decision")),
            }
        )

    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return

    for r in rows:
        print(f"{r['session_id']:<10} {r['tool']}:{r['model']:<32} {r['status']:<13} {r['workdir']}")
        blocked = r["blocked"]
        if blocked:
            print(f"  [{blocked['reason']}/{blocked['kind']}] {blocked.get('question') or '(no question text)'}")
            if blocked.get("options"):
                print(f"    options       : {' / '.join(blocked['options'])}")
            if blocked.get("recommendation"):
                print(f"    recommendation: {blocked['recommendation']}")
            if blocked["reason"] == "blocked":
                sid = r["session_id"]
                print(f"    next: ai-route escalate --from-session {sid} --then-implement")
                print(f"          ai-route handoff --from-session {sid}")


def _escalation_target(args: argparse.Namespace) -> tuple[str, str]:
    if args.model and not args.tool:
        raise AiRouteError("--model requires --tool.")
    if args.tool and args.model:
        return args.tool, args.model
    if args.tool:
        return select_candidate("high", args.tool)
    return resolve_spec(STAGE_DEFAULTS["escalate"], "high")


def cmd_escalate(args: argparse.Namespace) -> None:
    """Escalate a (blocked) session to a stronger model for a decision; optionally resume."""
    if args.wait and args.mode != "headless":
        raise AiRouteError("--wait requires --mode headless.")
    workdir, meta = resolve_session(args.from_session, Path.cwd())
    refresh_session_from_log(workdir, meta)
    tool, model = _escalation_target(args)
    impl_tool = str(meta.get("current_tool") or "")
    impl_model = str(meta.get("current_model") or "")
    timeout = get_agent_timeout(args.agent_timeout)

    plan = escalate_session(
        workdir=workdir, meta=meta, tool=tool, model=model, note=args.note or "",
        timeout=timeout, trigger="manual",
    )
    if plan is None:
        raise AiRouteError(f"Escalation to {tool}:{model} failed.")

    print("\n=== Decision ===")
    print(format_plan_sections(plan))

    if not args.then_implement:
        sid = meta.get("session_id", "")
        print(f"\n[saved] decision stored in session {sid}. Resume: ai-route escalate --from-session {sid} --then-implement")
        return

    if not impl_tool or not impl_model:
        raise AiRouteError("Session has no current tool/model to resume with.")
    base = str(meta.get("refined_prompt") or meta.get("task") or "")
    prompt = build_resume_prompt(base, plan)
    meta["status"] = "running"
    save_session_meta(workdir, meta)
    launch_implementer(
        tool=impl_tool,
        model=impl_model,
        prompt=prompt,
        workdir=workdir,
        mode=args.mode,
        wait=args.wait,
        session_id=str(meta.get("session_id", "")),
        meta=meta,
        timeout=timeout,
        auto_escalate=args.auto_escalate,
        stage="implementer",
        win_name=f"{impl_tool}-resume",
        pane_title=f"resume:{impl_tool}:{impl_model[:14]}",
    )
