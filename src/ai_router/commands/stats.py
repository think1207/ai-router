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

    if not routes and not handoffs and not reviews and not multis and not picks:
        print("No log entries match the specified filters.")
        return

    handoffs_by_session: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for h in handoffs:
        handoffs_by_session[str(h.get("session_id", ""))].append(h)

    model_counts: collections.Counter[str] = collections.Counter()
    model_handoff_sessions: collections.Counter[str] = collections.Counter()
    diff_counts: collections.Counter[str] = collections.Counter()
    latencies: list[int] = []
    fallback_count = 0

    for r in routes:
        key = f'{r.get("tool")} ({r.get("model")})'
        model_counts[key] += 1
        diff_counts[str(r.get("difficulty", "unknown"))] += 1
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

    if transitions:
        print("\n--- Handoff Transition Patterns ---")
        for trans, cnt in transitions.most_common():
            print(f"  {cnt:>3}x  {trans}")

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
