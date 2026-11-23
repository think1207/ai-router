"""Handlers for `status` (blocked & credit detection), `escalate`, and unified `resume`."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from ai_router.blocking import blocked_signature, detect_blocked, parse_session_log
from ai_router.config import STAGE_DEFAULTS, get_active_config, get_agent_timeout
from ai_router.credits import (
    clear_tool_exhausted,
    detect_credit_exhausted,
    format_remaining_cooldown,
    get_exhausted_tools,
    mark_tool_exhausted,
    select_alternative_candidate,
)
from ai_router.dispatch import resolve_workdir
from ai_router.errors import AiRouteError
from ai_router.flow import (
    classify_status,
    escalate_session,
    launch_implementer,
    record_blocked,
    record_credit_exhausted,
)
from ai_router.models import BlockSignal
from ai_router.sessions import append_history, build_session_meta, resolve_session, summarize_output
from ai_router.storage import append_jsonl, load_jsonl, load_session_meta, save_session_meta
from ai_router.twostage import build_resume_prompt, format_plan_sections, resolve_spec, select_candidate
from ai_router.utils import get_repo_and_branch, now_iso, run_cmd

# Read at most this many trailing bytes of a session log (TUI logs can grow large)
LOG_READ_MAX_BYTES = 256 * 1024


def _read_log_tail(path: Path) -> str:
    with path.open("rb") as f:
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(0, size - LOG_READ_MAX_BYTES))
        return f.read().decode("utf-8", errors="replace")


def refresh_session_from_log(workdir: Path, meta: dict[str, Any]) -> BlockSignal | None:
    """Re-inspect a session's output log and update `meta` (status/blocked/credit_exhausted) in place.

    Only re-detects when the log grew since the last check; newly seen blocks or credit
    exhaustion are recorded in history.jsonl (source="status"). Returns the current block signal, if any.
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
    credit_reason = detect_credit_exhausted(rc, output)
    signal = None if credit_reason else detect_blocked(rc, output)
    if credit_reason or signal is not None or rc is not None:
        status = classify_status(rc if rc is not None else 0, signal, credit_reason=credit_reason)
    else:
        status = "running"

    signature = f"credit|{credit_reason}" if credit_reason else blocked_signature(signal)
    tool = str(meta.get("current_tool", ""))
    model = str(meta.get("current_model", ""))

    if credit_reason and signature != checked.get("signature"):
        if tool:
            mark_tool_exhausted(tool, credit_reason, model=model)
        record_credit_exhausted(
            session_id=str(meta.get("session_id", "")),
            workdir=workdir,
            tool=tool,
            model=model,
            reason=credit_reason,
            source="status",
            stage=str(meta.get("stage", "")),
        )
        append_history(meta, "credit_exhausted", tool=tool, model=model, reason=credit_reason)
    elif signal is not None and signature != checked.get("signature"):
        record_blocked(
            session_id=str(meta.get("session_id", "")),
            workdir=workdir,
            tool=tool,
            model=model,
            signal=signal,
            source="status",
            stage=str(meta.get("stage", "")),
        )
        append_history(meta, "blocked", reason=signal.reason, question=signal.question)

    meta["status"] = status
    meta["blocked"] = signal.to_dict() if signal else None
    meta["credit_exhausted"] = (
        {"tool": tool, "model": model, "reason": credit_reason} if credit_reason else None
    )
    meta["last_output_summary"] = summarize_output(output)
    meta["log_checked"] = {"size": size, "signature": signature}
    meta["updated_at"] = now_iso()
    save_session_meta(workdir, meta)
    return signal


def _recent_sessions(limit: int) -> list[tuple[Path, dict[str, Any]]]:
    seen: dict[str, Path] = {}
    for rec in load_jsonl(warn_malformed=False):
        if rec.get("event") in {"route", "handoff", "failover"} and rec.get("workdir"):
            seen.pop(str(rec["workdir"]), None)
            seen[str(rec["workdir"])] = Path(str(rec["workdir"]))
    targets: list[tuple[Path, dict[str, Any]]] = []
    for wd in reversed(list(seen.values())):
        meta = load_session_meta(wd) if wd.is_dir() else None
        if meta:
            targets.append((wd, meta))
        if len(targets) >= limit:
            break
    return targets


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

    return _recent_sessions(args.recent)


def cmd_status(args: argparse.Namespace) -> None:
    """Show session status, credit cooldown state, and detect blocked/exhausted agents."""
    if getattr(args, "check", False):
        from ai_router.commands.setup import run_diagnostics

        run_diagnostics()
        return

    reset_target = getattr(args, "reset_credits", None)
    if reset_target is not None:
        tool_to_clear = None if reset_target == "__all__" else reset_target
        cleared = clear_tool_exhausted(tool_to_clear)
        if cleared:
            print(f"[credits] Cleared credit-exhausted status for: {', '.join(cleared)}")
        else:
            label = "any service" if tool_to_clear is None else f"'{tool_to_clear}'"
            print(f"[credits] No active credit-exhausted status found for {label}.")
        return

    mark_tool = getattr(args, "mark_exhausted", None)
    if mark_tool:
        cfg = get_active_config()
        if mark_tool not in cfg.supported_tools:
            raise AiRouteError(
                f"Unsupported tool '{mark_tool}' (supported: {', '.join(cfg.supported_tools)})"
            )
        entry = mark_tool_exhausted(mark_tool, "manually marked credit-exhausted")
        rem = format_remaining_cooldown(entry["until"])
        print(f"[credits] Marked '{mark_tool}' as credit-exhausted (cooldown: {rem}).")
        return

    exhausted = get_exhausted_tools()
    targets = _status_targets(args, Path.cwd())

    if not targets:
        if args.json:
            print(json.dumps([], ensure_ascii=False, indent=2))
            return
        if exhausted:
            _print_exhausted_summary(exhausted)
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
                "effort": meta.get("effort", ""),
                "status": meta.get("status", "unknown"),
                "workdir": str(workdir),
                "log_path": meta.get("log_path", ""),
                "blocked": signal.to_dict() if signal else None,
                "credit_exhausted": meta.get("credit_exhausted"),
                "decision": bool(meta.get("decision")),
            }
        )

    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return

    # Re-read exhausted in case refresh_session_from_log just discovered one
    exhausted = get_exhausted_tools()
    if exhausted:
        _print_exhausted_summary(exhausted)

    for r in rows:
        effort_part = f" [{r['effort']}]" if r.get("effort") else ""
        print(f"{r['session_id']:<10} {r['tool']}:{r['model']:<32}{effort_part} {r['status']:<16} {r['workdir']}")
        sid = r["session_id"]
        credit_info = r.get("credit_exhausted")
        if r["status"] == "credit_exhausted" and isinstance(credit_info, dict):
            print(f"  [credit_exhausted] {credit_info.get('reason') or 'credit/quota limit reached'}")
            print(f"    next: ai-route resume {sid}")
        blocked = r["blocked"]
        if blocked:
            print(f"  [{blocked['reason']}/{blocked['kind']}] {blocked.get('question') or '(no question text)'}")
            if blocked.get("options"):
                print(f"    options       : {' / '.join(blocked['options'])}")
            if blocked.get("recommendation"):
                print(f"    recommendation: {blocked['recommendation']}")
            if blocked["reason"] == "blocked":
                print(f"    next: ai-route resume {sid}")
                print(f"          ai-route escalate --from-session {sid} --then-implement")
                print(f"          ai-route handoff --from-session {sid}")


def _print_exhausted_summary(exhausted: dict[str, dict[str, Any]]) -> None:
    parts = []
    for tool, info in sorted(exhausted.items()):
        rem = format_remaining_cooldown(str(info.get("until") or ""))
        reason = str(info.get("reason") or "exhausted")
        parts.append(f"{tool} ({rem} left: {reason})")
    print("[credits] Exhausted services: " + "; ".join(parts))
    print("          (Clear with: ai-route status --reset-credits)")


def _escalation_target(args: argparse.Namespace) -> tuple[str, str]:
    if args.model and not args.tool:
        raise AiRouteError("--model requires --tool.")
    if args.tool and args.model:
        return args.tool, args.model
    if args.tool:
        return select_candidate("high", args.tool)
    tool, model = resolve_spec(STAGE_DEFAULTS["escalate"], "high")
    exhausted = set(get_exhausted_tools().keys())
    if tool in exhausted:
        alt = select_alternative_candidate("high", excluded_tools=exhausted)
        if alt is not None:
            return alt[0], alt[1]
    return tool, model


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
        print(f"\n[saved] decision stored in session {sid}. Resume: ai-route resume {sid}")
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
        effort=str(meta.get("effort") or ""),
        difficulty=str(meta.get("difficulty") or "medium"),
    )


def _resolve_resume_session(session_ref: str | None, cwd: Path) -> tuple[Path, dict[str, Any]]:
    if session_ref:
        return resolve_session(session_ref, cwd)
    local = load_session_meta(cwd)
    if local:
        return cwd, local
    recent = _recent_sessions(1)
    if recent:
        return recent[0]
    raise AiRouteError("No ai-route session found in the current worktree or history.")


def _build_continue_prompt(meta: dict[str, Any], cwd: Path, note: str, reason_header: str) -> str:
    diff_stat = run_cmd(["git", "diff", "--stat"], cwd=cwd, check=False).stdout.strip()
    status_out = run_cmd(["git", "status", "--short"], cwd=cwd, check=False).stdout.strip()
    sections = [f"{reason_header}\n"]
    if meta.get("task"):
        sections.append(f"## 元のタスク\n{meta['task']}\n")
    if meta.get("refined_prompt"):
        sections.append(f"## これまでの指示\n{meta['refined_prompt']}\n")
    decision = meta.get("decision")
    if isinstance(decision, dict) and decision:
        sections.append(f"# 確定済みの判断\n\n{format_plan_sections(decision)}\n")
    if meta.get("last_output_summary"):
        sections.append(f"## 前任の出力（末尾）\n```\n{meta['last_output_summary']}\n```\n")
    sections.append(f"## 現在の変更状況\n```\n{status_out or '(no changes)'}\n{diff_stat}\n```\n")
    if note:
        sections.append(f"## 追加の指示\n{note}\n")
    sections.append("現在の差分を確認したうえで、続きの作業を完了させてください。")
    return "\n".join(sections)


def cmd_resume(args: argparse.Namespace) -> None:
    """Unified smart command to inspect and continue a session (handles credit failover, blocked escalation, or handoff)."""
    if getattr(args, "auto", False):
        args.mode = "headless"
        args.wait = True
    if args.wait and args.mode != "headless":
        raise AiRouteError("--wait requires --mode headless.")
    if args.model and not args.tool:
        raise AiRouteError("--model requires --tool.")

    session_ref = args.session_id or args.from_session
    workdir, meta = _resolve_resume_session(session_ref, Path.cwd())
    workdir = resolve_workdir(workdir)
    refresh_session_from_log(workdir, meta)

    sid = str(meta.get("session_id", ""))
    cur_tool = str(meta.get("current_tool") or "")
    cur_model = str(meta.get("current_model") or "")
    difficulty = str(meta.get("difficulty") or "medium")
    if difficulty not in {"low", "medium", "high"}:
        difficulty = "medium"
    effort = str(getattr(args, "effort", "") or meta.get("effort") or "")
    status = str(meta.get("status") or "unknown")
    note = str(args.note or "").strip()
    timeout = get_agent_timeout(args.agent_timeout)
    auto_failover = bool(getattr(args, "auto_failover", True))

    if status == "completed" and not note and not args.tool:
        print(f"[completed] Session {sid} ({cur_tool}:{cur_model}) is already completed in {workdir}.")
        if meta.get("diff_stat"):
            print(meta["diff_stat"])
        return

    if status == "running" and not note and not args.tool:
        print(f"[running] Session {sid} ({cur_tool}:{cur_model}) is still running in {workdir}.")
        if meta.get("log_path"):
            print(f"  log: {meta['log_path']}")
        print(f"  Tip: pass a note or --tool to hand off immediately: ai-route resume {sid} \"...\"")
        return

    exhausted = set(get_exhausted_tools().keys())
    target_tool, target_model = cur_tool, cur_model
    is_service_switch = False

    if args.tool:
        if args.model:
            target_tool, target_model = args.tool, args.model
        else:
            target_tool, target_model = select_candidate(difficulty, args.tool)
        is_service_switch = (target_tool, target_model) != (cur_tool, cur_model)
    elif status == "credit_exhausted" or (cur_tool in exhausted and auto_failover):
        excluded = exhausted | ({cur_tool} if cur_tool else set())
        alt = select_alternative_candidate(difficulty, excluded_tools=excluded)
        if alt is None:
            raise AiRouteError(
                f"Credit exhausted on {cur_tool}:{cur_model} and no alternative service is available.\n"
                "  Clear cooldown with: ai-route status --reset-credits"
            )
        target_tool, target_model, alt_effort = alt
        effort = effort or alt_effort
        is_service_switch = True
        print(f"[failover] Switching session {sid} from {cur_tool}:{cur_model} -> {target_tool}:{target_model}")

    # Determine the resume prompt based on session state
    if status == "blocked":
        esc_tool, esc_model = resolve_spec(STAGE_DEFAULTS["escalate"], "high")
        if esc_tool in exhausted:
            alt_esc = select_alternative_candidate("high", excluded_tools=exhausted)
            if alt_esc is not None:
                esc_tool, esc_model, _ = alt_esc
        plan = escalate_session(
            workdir=workdir, meta=meta, tool=esc_tool, model=esc_model,
            note=note, timeout=timeout, trigger="resume", auto_failover=auto_failover,
        )
        if plan is None:
            raise AiRouteError(f"Escalation to {esc_tool}:{esc_model} failed.")
        print("\n=== Decision ===")
        print(format_plan_sections(plan))
        base = str(meta.get("refined_prompt") or meta.get("task") or "")
        prompt = build_resume_prompt(base, plan)
    elif status == "decided" and isinstance(meta.get("decision"), dict) and meta["decision"]:
        base = str(meta.get("refined_prompt") or meta.get("task") or "")
        prompt = build_resume_prompt(base, meta["decision"])
        if note:
            prompt = f"{prompt}\n\n## 追加の指示\n{note}"
    elif status == "credit_exhausted":
        prompt = _build_continue_prompt(
            meta, workdir, note,
            f"前任エージェント（{cur_tool}:{cur_model}）のクレジットが切れたため、別サービスへ作業を引き継ぎます。",
        )
    else:
        prompt = _build_continue_prompt(
            meta, workdir, note,
            "現在作業中のセッションを引き継ぎ・再開します。",
        )

    if not target_tool or not target_model:
        alt = select_alternative_candidate(difficulty, excluded_tools=exhausted)
        if alt is None:
            raise AiRouteError("No available tool/model to resume session with.")
        target_tool, target_model, alt_effort = alt
        effort = effort or alt_effort

    handoff_seq = int(meta.get("handoff_count", 0)) + (1 if is_service_switch else 0)
    repo_name, branch_name = get_repo_and_branch(workdir)
    new_meta = build_session_meta(
        session_id=sid,
        tool=target_tool,
        model=target_model,
        workdir=workdir,
        previous=meta,
        handoff_count=handoff_seq,
        status="running",
        event="handoff" if is_service_switch else "resume",
        effort=effort,
        credit_exhausted=None,
    )
    save_session_meta(workdir, new_meta)

    if is_service_switch:
        append_jsonl(
            {
                "ts": now_iso(),
                "event": "failover" if status == "credit_exhausted" else "handoff",
                "session_id": sid,
                "repo": repo_name,
                "branch": branch_name,
                "workdir": str(workdir),
                "from_tool": cur_tool or "unknown",
                "from_model": cur_model or "unknown",
                "tool": target_tool,
                "model": target_model,
                "handoff_seq": handoff_seq,
                "note": note,
                "from_session": True,
                "reason": (meta.get("credit_exhausted") or {}).get("reason", "") if status == "credit_exhausted" else "",
            }
        )

    launch_implementer(
        tool=target_tool,
        model=target_model,
        prompt=prompt,
        workdir=workdir,
        mode=args.mode,
        wait=args.wait,
        session_id=sid,
        meta=new_meta,
        timeout=timeout,
        auto_escalate=args.auto_escalate,
        auto_failover=auto_failover,
        stage="implementer",
        split=bool(getattr(args, "split", False)),
        win_name=f"{target_tool}-resume",
        pane_title=f"resume:{target_tool}:{target_model[:14]}",
        effort=effort,
        difficulty=difficulty,
    )
