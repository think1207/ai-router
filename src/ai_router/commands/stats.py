"""Handler for the `stats` subcommand (JSONL routing and handoff analytics)."""

from __future__ import annotations

import argparse
import collections
import datetime
from pathlib import Path
from typing import Any

from ai_router.config import get_log_file
from ai_router.storage import load_jsonl
from ai_router.utils import get_repo_and_branch


def cmd_stats(args: argparse.Namespace) -> None:
    """Aggregate and display routing, handoff, review, and multi-solve statistics."""
    log_file = get_log_file()
    if not log_file.is_file():
        print(f"No log file found yet: {log_file}")
        return

    cutoff: datetime.datetime | None = None
    if args.days > 0:
        cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=args.days)

    repo_filter = args.repo
    if args.here:
        repo_filter, _ = get_repo_and_branch(Path.cwd())

    events: list[dict[str, Any]] = []
    for rec in load_jsonl(log_file, warn_malformed=True):
        if repo_filter and rec.get("repo") != repo_filter:
            continue
        if cutoff:
            try:
                ev_dt = datetime.datetime.fromisoformat(rec["ts"]).astimezone(datetime.timezone.utc)
                if ev_dt < cutoff:
                    continue
            except (KeyError, ValueError):
                pass
        events.append(rec)

    routes = [e for e in events if e.get("event") == "route"]
    handoffs = [e for e in events if e.get("event") == "handoff"]
    reviews = [e for e in events if e.get("event") == "review"]
    multis = [e for e in events if e.get("event") == "multi"]
    picks = [e for e in events if e.get("event") == "pick"]
    blocks = [e for e in events if e.get("event") == "blocked"]
    escalations = [e for e in events if e.get("event") == "escalated"]
    two_stages = [e for e in events if e.get("event") == "two_stage"]
    agent_results = [e for e in events if e.get("event") == "agent_result"]
    credit_exhausted = [e for e in events if e.get("event") == "credit_exhausted"]
    failovers = [e for e in events if e.get("event") == "failover"]

    if not any((routes, handoffs, reviews, multis, picks, blocks, escalations, two_stages, agent_results, credit_exhausted, failovers)):
        print("No log entries match the specified filters.")
        return

    handoffs_by_session: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for h in handoffs:
        handoffs_by_session[str(h.get("session_id", ""))].append(h)

    model_counts: collections.Counter[str] = collections.Counter()
    model_handoff_sessions: collections.Counter[str] = collections.Counter()
    diff_counts: collections.Counter[str] = collections.Counter()
    effort_counts: collections.Counter[str] = collections.Counter()
    latencies: list[int] = []
    fallback_count = 0

    for r in routes:
        key = f'{r.get("tool")} ({r.get("model")})'
        model_counts[key] += 1
        diff_counts[str(r.get("difficulty", "unknown"))] += 1
        if r.get("effort"):
            effort_counts[str(r.get("effort"))] += 1
        if r.get("is_fallback"):
            fallback_count += 1
        if isinstance(r.get("router_latency_ms"), int) and r["router_latency_ms"] > 0:
            latencies.append(r["router_latency_ms"])
        if handoffs_by_session.get(str(r.get("session_id", ""))):
            model_handoff_sessions[key] += 1

    transitions: collections.Counter[str] = collections.Counter()
    for h in handoffs:
        src = f'{h.get("from_tool", "?")} ({h.get("from_model", "?")})'
        dst = f'{h.get("tool", "?")} ({h.get("model", "?")})'
        transitions[f"{src} -> {dst}"] += 1

    total_routes = len(routes)
    sessions_with_ho = sum(
        1 for r in routes if handoffs_by_session.get(str(r.get("session_id", "")))
    )
    ho_rate = (sessions_with_ho / total_routes * 100) if total_routes else 0.0
    avg_lat = (sum(latencies) / len(latencies)) if latencies else 0.0

    scope = f"last {args.days} days" if args.days > 0 else "all time"
    if repo_filter:
        scope += f" / repo={repo_filter}"

    print(f"=== ai-route Statistics ({scope}) ===")
    print(f"Total routed tasks : {total_routes}")
    print(
        f"Total handoffs     : {len(handoffs)} "
        f"(sessions with handoff: {sessions_with_ho}/{total_routes} = {ho_rate:.1f}%)"
    )
    print(f"Multi-model runs   : review {len(reviews)} / multi {len(multis)} / pick {len(picks)}")
    print(f"Avg router latency : {avg_lat:.0f} ms / fallbacks: {fallback_count}")
    block_summary = summarize_blocking(blocks, escalations, two_stages, agent_results)
    if any(block_summary[k] for k in ("blocked", "escalated", "two_stage", "agent_results")):
        print(
            f"Blocking / stages  : blocked {block_summary['blocked']} "
            f"(sessions {block_summary['blocked_sessions']}) / "
            f"escalated {block_summary['escalated']} (auto {block_summary['escalated_auto']}) / "
            f"two-stage {block_summary['two_stage']}"
        )
    if credit_exhausted or failovers:
        print(f"Credit failovers   : exhausted {len(credit_exhausted)} / failovers {len(failovers)}")

    if total_routes > 0:
        print("\n--- Initial Routing Breakdown by Model ---")
        print(f"{'Model':<42} {'Count':>5} {'Share':>7} {'Handoffs':>12}")
        for model_key, cnt in model_counts.most_common():
            share = cnt / total_routes * 100
            ho_s = model_handoff_sessions[model_key]
            ho_pct = (ho_s / cnt * 100) if cnt else 0.0
            print(f"{model_key:<42} {cnt:>5} {share:>6.1f}% {ho_s:>4} ({ho_pct:>4.0f}%)")

        print("\n--- Breakdown by Difficulty ---")
        for d in ("low", "medium", "high", "manual"):
            if diff_counts[d]:
                print(f"  {d:<8}: {diff_counts[d]}")

        if effort_counts:
            print("\n--- Breakdown by Effort ---")
            for ef in ("low", "medium", "high", "xhigh", "max"):
                if effort_counts[ef]:
                    print(f"  {ef:<8}: {effort_counts[ef]}")

    if transitions:
        print("\n--- Handoff Transition Patterns ---")
        for trans, cnt in transitions.most_common():
            print(f"  {cnt:>3}x  {trans}")

    if block_summary["blocked_reasons"] or block_summary["result_statuses"]:
        print("\n--- Blocking & Escalation ---")
        if block_summary["result_statuses"]:
            parts = ", ".join(f"{k} {v}" for k, v in block_summary["result_statuses"].most_common())
            print(f"  agent results : {parts}")
        if block_summary["blocked_reasons"]:
            parts = ", ".join(f"{k} {v}" for k, v in block_summary["blocked_reasons"].most_common())
            print(f"  blocked by    : {parts}")
        for model_key, cnt in block_summary["blocked_models"].most_common(5):
            print(f"  {cnt:>3}x blocked  {model_key}")
        for model_key, cnt in block_summary["escalation_targets"].most_common(5):
            print(f"  {cnt:>3}x escalated -> {model_key}")

    if picks:
        pick_wins: collections.Counter[str] = collections.Counter()
        for p in picks:
            pick_wins[f'{p.get("winner_tool", "?")} ({p.get("winner_model", "?")})'] += 1
        print("\n--- Multi Pick Winners ---")
        for model_key, cnt in pick_wins.most_common():
            print(f"  {cnt:>3}x  {model_key}")

    if args.recent > 0 and routes:
        print(f"\n--- Most Recent {min(args.recent, len(routes))} Sessions ---")
        for r in routes[-args.recent :][::-1]:
            sid = str(r.get("session_id", ""))
            ho_list = handoffs_by_session.get(sid, [])
            ho_tag = f"HOx{len(ho_list)}" if ho_list else "HOx0"
            ts_short = str(r.get("ts", ""))[:16].replace("T", " ")
            task_short = str(r.get("task", "")).replace("\n", " ")[:48]
            print(f"  [{ts_short}] {sid} | {r.get('tool')}:{r.get('model')} | {ho_tag} | {task_short}")


def summarize_blocking(
    blocks: list[dict[str, Any]],
    escalations: list[dict[str, Any]],
    two_stages: list[dict[str, Any]],
    agent_results: list[dict[str, Any]],
) -> dict[str, Any]:
    """Aggregate blocked / escalated / two-stage counts from history events."""
    blocked_reasons: collections.Counter[str] = collections.Counter()
    blocked_models: collections.Counter[str] = collections.Counter()
    for b in blocks:
        blocked_reasons[str(b.get("reason", "unknown"))] += 1
        blocked_models[f'{b.get("tool")} ({b.get("model")})'] += 1
    escalation_targets: collections.Counter[str] = collections.Counter(
        f'{e.get("tool")} ({e.get("model")})' for e in escalations
    )
    result_statuses: collections.Counter[str] = collections.Counter(
        str(r.get("status", "unknown")) for r in agent_results
    )
    # A two-stage run logs a planner-failure or a "planned" record; count each run once.
    two_stage_runs = {
        str(t.get("parent_session") or t.get("session_id") or id(t)) for t in two_stages
    }
    return {
        "blocked": len(blocks),
        "blocked_sessions": len({str(b.get("session_id", "")) for b in blocks}),
        "blocked_reasons": blocked_reasons,
        "blocked_models": blocked_models,
        "escalated": len(escalations),
        "escalated_auto": sum(1 for e in escalations if e.get("trigger") == "auto"),
        "escalation_targets": escalation_targets,
        "two_stage": len(two_stage_runs),
        "agent_results": len(agent_results),
        "result_statuses": result_statuses,
    }
