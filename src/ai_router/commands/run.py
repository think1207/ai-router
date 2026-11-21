"""Handlers for `run` and `handoff` subcommands."""

from __future__ import annotations

import argparse
import uuid
from pathlib import Path

from ai_router.config import ROUTER_MODEL, get_active_config, get_router_timeout
from ai_router.dispatch import build_agent_shell_cmd, dispatch_to_tmux, write_prompt_file
from ai_router.errors import AiRouteError
from ai_router.files import (
    format_files_for_agent,
    format_files_for_router,
    load_task_text,
    resolve_files,
)
from ai_router.models import RouteDecision
from ai_router.router import route_task
from ai_router.storage import append_jsonl, load_session_meta, save_session_meta
from ai_router.utils import (
    get_git_context,
    get_git_root,
    append_plan_first,
    get_repo_and_branch,
    now_iso,
    plan_first_enabled,
    run_cmd,
    sanitize_slug,
)
from ai_router.worktree import create_worktree


def cmd_run(args: argparse.Namespace) -> None:
    """Classify a task and dispatch it to the selected coding agent."""
    if bool(args.tool) != bool(args.model):
        raise AiRouteError("Both --tool and --model must be provided together to skip routing.")

    repo_root = get_git_root()
    repo_ctx = get_git_context(repo_root) if repo_root else "(not a git repository)"
    task_text = load_task_text(args.task, args.task_file)
    resolved_files = resolve_files(args.files or [], repo_root)
    files_ctx = format_files_for_router(resolved_files)
    use_plan_first = plan_first_enabled(args.no_plan_first, args.mode)

    cfg = get_active_config()
    if args.tool and args.model:
        if (args.tool, args.model) not in cfg.allowed_pairs:
            raise AiRouteError(
                f"Disallowed (tool, model)=({args.tool}, {args.model}). "
                f"Allowed pairs: {', '.join(f'{t}:{m}' for t, m in sorted(cfg.allowed_pairs))}"
            )
        decision = RouteDecision(
            difficulty="manual",
            tool=args.tool,
            model=args.model,
            branch_slug=sanitize_slug(args.slug or "manual-task"),
            reason="Explicitly specified by user",
            refined_prompt=append_plan_first(task_text, use_plan_first),
        )
    else:
        decision = route_task(
            task_text,
            repo_ctx,
            files_ctx,
            plan_first=use_plan_first,
            timeout=get_router_timeout(args.router_timeout),
            config=cfg,
        )

    agent_files_block = format_files_for_agent(resolved_files, using_worktree=bool(args.worktree))
    if agent_files_block:
        decision.refined_prompt = f"{agent_files_block}\n\n{decision.refined_prompt}"

    print("========================================")
    print(f" Difficulty : {decision.difficulty}")
    print(f" Selected   : {decision.tool} ({decision.model})")
    print(f" Reason     : {decision.reason}")
    if resolved_files:
        print(f" Files      : {', '.join(rf.display_path for rf in resolved_files)}")
    print("========================================")

    if args.dry_run:
        print("\n[Dry Run] Refined Prompt:\n" + decision.refined_prompt)
        return

    workdir = Path.cwd()
    if args.worktree:
        if not repo_root:
            raise AiRouteError("--worktree can only be used inside a git repository.")
        workdir, branch = create_worktree(repo_root, decision.branch_slug, base_ref=args.base)
        print(f" Worktree   : {workdir} (branch: {branch})")

    session_id = uuid.uuid4().hex[:8]
    repo_name, branch_name = get_repo_and_branch(workdir)

    save_session_meta(
        workdir,
        {
            "session_id": session_id,
            "current_tool": decision.tool,
            "current_model": decision.model,
            "handoff_count": 0,
            "started_at": now_iso(),
        },
    )

    append_jsonl(
        {
            "ts": now_iso(),
            "event": "route",
            "session_id": session_id,
            "repo": repo_name,
            "branch": branch_name,
            "workdir": str(workdir),
            "tool": decision.tool,
            "model": decision.model,
            "difficulty": decision.difficulty,
            "mode": args.mode,
            "worktree": bool(args.worktree),
            "router_model": None if (args.tool and args.model) else ROUTER_MODEL,
            "router_latency_ms": decision.router_latency_ms,
            "is_fallback": decision.is_fallback,
            "files": [rf.display_path for rf in resolved_files],
            "task": task_text,
            "reason": decision.reason,
        }
    )

    prompt_file = write_prompt_file(decision.refined_prompt)
    shell_cmd = build_agent_shell_cmd(decision.tool, decision.model, prompt_file, args.mode)
    win_name = f"{decision.tool}-{decision.branch_slug[:14]}"

    dispatch_to_tmux(
        shell_cmd=shell_cmd,
        workdir=workdir,
        win_name=win_name,
        tool=decision.tool,
        mode=args.mode,
        prompt_file=prompt_file,
        split_current=args.split,
        pane_title=f"{decision.tool}:{decision.model[:18]}",
    )


def cmd_handoff(args: argparse.Namespace) -> None:
    """Hand off the current worktree diff to another model in a split tmux pane."""
    cwd = Path.cwd()
    repo_root = get_git_root(cwd)
    cfg = get_active_config()
    if (args.tool, args.model) not in cfg.allowed_pairs:
        raise AiRouteError(
            f"Disallowed (tool, model)=({args.tool}, {args.model}). "
            f"Allowed pairs: {', '.join(f'{t}:{m}' for t, m in sorted(cfg.allowed_pairs))}"
        )

    resolved_files = resolve_files(args.files or [], repo_root)
    agent_files_block = format_files_for_agent(resolved_files, using_worktree=False)

    diff_stat = run_cmd(["git", "diff", "--stat"], cwd=cwd, check=False).stdout.strip()
    status = run_cmd(["git", "status", "--short"], cwd=cwd, check=False).stdout.strip()

    handoff_prompt = (
        f"現在作業中のworktreeを引き継ぎます。\n\n"
        f"{agent_files_block + chr(10) + chr(10) if agent_files_block else ''}"
        f"## 現在の変更状況\n```\n{status or '(no changes)'}\n{diff_stat}\n```\n\n"
        f"## 引き継ぎ指示\n{args.note}\n\n"
        f"現在の差分を確認したうえで、方針の確認と続きの実装を進めてください。"
    )

    meta = load_session_meta(cwd) or {}
    session_id = str(meta.get("session_id") or uuid.uuid4().hex[:8])
    from_tool = str(meta.get("current_tool", "unknown"))
    from_model = str(meta.get("current_model", "unknown"))
    handoff_seq = int(meta.get("handoff_count", 0)) + 1
    repo_name, branch_name = get_repo_and_branch(cwd)

    save_session_meta(
        cwd,
        {
            "session_id": session_id,
            "current_tool": args.tool,
            "current_model": args.model,
            "handoff_count": handoff_seq,
            "started_at": meta.get("started_at", now_iso()),
        },
    )

    append_jsonl(
        {
            "ts": now_iso(),
            "event": "handoff",
            "session_id": session_id,
            "repo": repo_name,
            "branch": branch_name,
            "workdir": str(cwd),
            "from_tool": from_tool,
            "from_model": from_model,
            "tool": args.tool,
            "model": args.model,
            "handoff_seq": handoff_seq,
            "files": [rf.display_path for rf in resolved_files],
            "note": args.note,
        }
    )

    prompt_file = write_prompt_file(handoff_prompt)
    shell_cmd = build_agent_shell_cmd(args.tool, args.model, prompt_file, "interactive")
    dispatch_to_tmux(
        shell_cmd=shell_cmd,
        workdir=cwd,
        win_name=f"handoff-{args.tool}",
        tool=args.tool,
        mode="interactive",
        prompt_file=prompt_file,
        split_current=True,
        pane_title=f"ho:{args.tool}:{args.model[:16]}",
    )
