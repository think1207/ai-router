"""Handlers for `run` (including `--wait` and `--two-stage`) and `handoff` subcommands."""

from __future__ import annotations

import argparse
import sys
import uuid
from pathlib import Path
from typing import Any

from ai_router.commands.session import refresh_session_from_log
from ai_router.config import (
    DIFFICULTY_TO_DEFAULT_EFFORT,
    ROUTER_MODEL,
    get_active_config,
    get_agent_timeout,
    get_router_timeout,
    resolve_implementer_spec,
)
from ai_router.credits import (
    get_exhausted_tools,
    select_alternative_candidate,
)
from ai_router.dispatch import resolve_workdir
from ai_router.errors import AiRouteError
from ai_router.files import (
    format_files_for_agent,
    format_files_for_router,
    load_task_text,
    resolve_files,
)
from ai_router.models import RouteDecision
from ai_router.router import route_task
from ai_router.security import sanitize_terminal_text
from ai_router.flow import append_headless_instruction, launch_implementer, run_and_record
from ai_router.sessions import build_session_meta, resolve_session
from ai_router.storage import append_jsonl, load_session_meta, save_session_meta
from ai_router.twostage import (
    build_implementation_prompt,
    build_planner_prompt,
    extract_plan,
    format_plan_sections,
    resolve_implementer,
    resolve_planner,
)
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
    if getattr(args, "auto", False):
        args.mode = "headless"
        args.wait = True
    if getattr(args, "wait", False) and args.mode != "headless":
        raise AiRouteError("--wait requires --mode headless.")
    if getattr(args, "two_stage", False):
        _cmd_run_two_stage(args)
        return
    if bool(args.tool) != bool(args.model):
        raise AiRouteError("Both --tool and --model must be provided together to skip routing.")

    repo_root = get_git_root()
    repo_ctx = get_git_context(repo_root) if repo_root else "(not a git repository)"
    task_text = load_task_text(args.task, args.task_file)
    resolved_files = resolve_files(args.files or [], repo_root)
    files_ctx = format_files_for_router(resolved_files)
    use_plan_first = plan_first_enabled(args.no_plan_first, args.mode)

    cfg = get_active_config()
    cli_effort = str(getattr(args, "effort", "") or "").strip().lower()
    if args.tool and args.model:
        if (args.tool, args.model) not in cfg.allowed_pairs:
            raise AiRouteError(
                f"Disallowed (tool, model)=({args.tool}, {args.model}). "
                f"Allowed pairs: {', '.join(f'{t}:{m}' for t, m in sorted(cfg.allowed_pairs))}"
            )
        decision = RouteDecision(
            difficulty="manual",
            effort=cli_effort or cfg.defaults.effort,
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
        if cli_effort:
            decision.effort = cli_effort

    agent_files_block = format_files_for_agent(resolved_files, using_worktree=bool(args.worktree))
    if agent_files_block:
        decision.refined_prompt = f"{agent_files_block}\n\n{decision.refined_prompt}"

    print("========================================")
    print(f" Difficulty : {decision.difficulty}")
    if decision.effort:
        print(f" Effort     : {decision.effort}")
    print(f" Selected   : {decision.tool} ({decision.model})")
    print(f" Reason     : {sanitize_terminal_text(decision.reason)}")
    if resolved_files:
        print(f" Files      : {', '.join(rf.display_path for rf in resolved_files)}")
    print("========================================")

    if args.dry_run:
        shown = decision.refined_prompt
        if getattr(args, "wait", False):
            shown = append_headless_instruction(shown)
        print("\n[Dry Run] Refined Prompt:\n" + shown)
        return

    workdir = _prepare_workdir(args, repo_root, decision.branch_slug)
    _start_session(
        args,
        decision,
        task_text=task_text,
        resolved_files=resolved_files,
        workdir=workdir,
        router_model=None if (args.tool and args.model) else ROUTER_MODEL,
    )


def _prepare_workdir(args: argparse.Namespace, repo_root: Path | None, branch_slug: str) -> Path:
    workdir = Path.cwd()
    if args.worktree:
        if not repo_root:
            raise AiRouteError("--worktree can only be used inside a git repository.")
        workdir, branch = create_worktree(repo_root, branch_slug, base_ref=args.base)
        print(f" Worktree   : {workdir} (branch: {branch})")
    return workdir


def _start_session(
    args: argparse.Namespace,
    decision: RouteDecision,
    *,
    task_text: str,
    resolved_files: list,
    workdir: Path,
    router_model: str | None,
    session_id: str | None = None,
    extra_meta: dict[str, Any] | None = None,
) -> None:
    """Save session metadata, record the `route` event, and launch the agent."""
    session_id = session_id or uuid.uuid4().hex[:8]
    repo_name, branch_name = get_repo_and_branch(workdir)
    wait = bool(getattr(args, "wait", False))

    meta = build_session_meta(
        session_id=session_id,
        tool=decision.tool,
        model=decision.model,
        workdir=workdir,
        handoff_count=0,
        difficulty=decision.difficulty,
        refined_prompt=decision.refined_prompt,
        task=task_text,
        status="running",
        event="route",
        stage=decision.stage,
        parent_session=decision.parent_session,
        mode=args.mode,
        effort=decision.effort,
        **(extra_meta or {}),
    )
    save_session_meta(workdir, meta)

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
            "effort": decision.effort,
            "mode": args.mode,
            "worktree": bool(args.worktree),
            "router_model": router_model,
            "router_latency_ms": decision.router_latency_ms,
            "is_fallback": decision.is_fallback,
            "files": [rf.display_path for rf in resolved_files],
            "task": task_text,
            "reason": decision.reason,
            "stage": decision.stage,
            "parent_session": decision.parent_session,
            "wait": wait,
        }
    )

    launch_implementer(
        tool=decision.tool,
        model=decision.model,
        prompt=decision.refined_prompt,
        workdir=workdir,
        mode=args.mode,
        wait=wait,
        session_id=session_id,
        meta=meta,
        timeout=get_agent_timeout(getattr(args, "agent_timeout", None)),
        auto_escalate=bool(getattr(args, "auto_escalate", True)),
        auto_failover=bool(getattr(args, "auto_failover", True)),
        stage=decision.stage,
        split=bool(getattr(args, "split", False)),
        win_name=f"{decision.tool}-{decision.branch_slug[:14]}",
        pane_title=f"{decision.tool}:{decision.model[:18]}",
        effort=decision.effort,
        difficulty=decision.difficulty if decision.difficulty in {"low", "medium", "high"} else "medium",
    )


def _cmd_run_two_stage(args: argparse.Namespace) -> None:
    """`run --two-stage`: planner (decisions only) -> implementer, bypassing the router."""
    if args.tool or args.model:
        raise AiRouteError("--two-stage cannot be combined with --tool/--model (use --planner/--implementer).")

    repo_root = get_git_root()
    repo_ctx = get_git_context(repo_root) if repo_root else "(not a git repository)"
    task_text = load_task_text(args.task, args.task_file)
    resolved_files = resolve_files(args.files or [], repo_root)
    planner_tool, planner_model = resolve_planner(args.planner, args.plan_preset)
    planner_files = format_files_for_agent(resolved_files, using_worktree=False)
    planner_prompt = build_planner_prompt(task_text, planner_files, repo_ctx)
    impl_spec = resolve_implementer_spec(args.implementer, args.plan_preset)

    print("========================================")
    print(" Two-stage  : planner -> implementer")
    print(f" Planner    : {planner_tool} ({planner_model})")
    print(f" Implementer: {impl_spec or '(auto by planner difficulty)'}")
    if args.plan_preset:
        print(f" Preset     : {args.plan_preset}")
    print("========================================")

    if args.dry_run:
        print("\n[Dry Run] Planner Prompt:\n" + planner_prompt)
        return

    timeout = get_agent_timeout(args.agent_timeout)
    auto_failover = bool(getattr(args, "auto_failover", True))
    planner_session = uuid.uuid4().hex[:8]
    planner_cwd = repo_root or Path.cwd()
    tried_planner_tools: set[str] = set()
    while True:
        result, _ = run_and_record(
            tool=planner_tool, model=planner_model, prompt=planner_prompt, workdir=planner_cwd,
            session_id=planner_session, meta=None, timeout=timeout, stage="planner", save_meta=False,
        )
        if result.status == "credit_exhausted" and auto_failover:
            tried_planner_tools.add(planner_tool)
            excluded = tried_planner_tools | set(get_exhausted_tools().keys())
            alt = select_alternative_candidate("high", excluded_tools=excluded)
            if alt is not None:
                alt_tool, alt_model, _ = alt
                print(
                    f"[failover] Planner {planner_tool}:{planner_model} credit exhausted "
                    f"({sanitize_terminal_text(result.credit_exhausted_reason)}); retrying planner with {alt_tool}:{alt_model}.",
                    file=sys.stderr,
                )
                planner_tool, planner_model = alt_tool, alt_model
                continue
        break
    repo_name, branch_name = get_repo_and_branch(planner_cwd)
    two_stage_event: dict[str, Any] = {
        "ts": now_iso(),
        "event": "two_stage",
        "source": "run",
        "parent_session": planner_session,
        "repo": repo_name,
        "branch": branch_name,
        "planner_tool": planner_tool,
        "planner_model": planner_model,
        "planner_latency_ms": result.latency_ms,
        "preset": args.plan_preset or "",
        "task": task_text,
    }
    # The planner emits JSON (whose open questions may look like questions), so only a
    # non-zero exit counts as a planner failure.
    if result.rc != 0:
        append_jsonl({**two_stage_event, "status": "planner_failed", "rc": result.rc})
        print("\n" + result.output)
        raise AiRouteError(f"Planner {planner_tool}:{planner_model} failed ({result.status}, rc={result.rc}).")

    plan, valid = extract_plan(result.output)
    if not valid:
        print("[warn] Planner output was not a valid plan JSON; using safe defaults with raw notes.", file=sys.stderr)
    impl_tool, impl_model = resolve_implementer(plan["implementer_difficulty"], args.implementer, args.plan_preset)
    exhausted_now = set(get_exhausted_tools().keys())
    if impl_tool in exhausted_now and auto_failover and not args.implementer:
        alt_impl = select_alternative_candidate(plan["implementer_difficulty"], excluded_tools=exhausted_now)
        if alt_impl is not None:
            impl_tool, impl_model, _ = alt_impl
    cli_effort = str(getattr(args, "effort", "") or "").strip().lower()
    impl_effort = cli_effort or str(
        plan.get("implementer_effort")
        or DIFFICULTY_TO_DEFAULT_EFFORT.get(plan["implementer_difficulty"], "")
    )
    print("\n" + format_plan_sections(plan))
    effort_note = f", effort={impl_effort}" if impl_effort else ""
    print(f"\n Implementer: {impl_tool} ({impl_model}) [difficulty={plan['implementer_difficulty']}{effort_note}]")

    workdir = _prepare_workdir(args, repo_root, sanitize_slug(args.slug or "two-stage"))
    agent_files = format_files_for_agent(resolved_files, using_worktree=bool(args.worktree))
    impl_prompt = build_implementation_prompt(plan, task_text, agent_files)
    impl_prompt = append_plan_first(impl_prompt, plan_first_enabled(args.no_plan_first, args.mode))
    decision = RouteDecision(
        difficulty=plan["implementer_difficulty"],
        effort=impl_effort,
        tool=impl_tool,
        model=impl_model,
        branch_slug=sanitize_slug(args.slug or "two-stage"),
        reason=f"Two-stage: planned by {planner_tool}:{planner_model}",
        refined_prompt=impl_prompt,
        stage="implementer",
        parent_session=planner_session,
    )
    impl_session = uuid.uuid4().hex[:8]
    append_jsonl(
        {
            **two_stage_event,
            "status": "planned",
            "session_id": impl_session,
            "workdir": str(workdir),
            "plan_valid": valid,
            "implementer_tool": impl_tool,
            "implementer_model": impl_model,
            "implementer_difficulty": plan["implementer_difficulty"],
        }
    )
    _start_session(
        args,
        decision,
        task_text=task_text,
        resolved_files=resolved_files,
        workdir=workdir,
        router_model=None,
        session_id=impl_session,
        extra_meta={
            "decision": {**plan, "tool": planner_tool, "model": planner_model, "valid": valid, "ts": now_iso()}
        },
    )


def _build_session_handoff_prompt(meta: dict[str, Any], note: str, files_block: str, status: str, diff_stat: str) -> str:
    sections = ["現在作業中のworktreeを引き継ぎます。前任エージェントの作業内容と判断待ちの点を確認してください。\n"]
    if files_block:
        sections.append(f"{files_block}\n")
    if meta.get("task"):
        sections.append(f"## 元のタスク\n{meta['task']}\n")
    if meta.get("refined_prompt"):
        sections.append(f"## 前任への指示（refined_prompt）\n{meta['refined_prompt']}\n")
    blocked = meta.get("blocked") or {}
    if blocked:
        lines = [f"- 質問: {blocked.get('question') or '(明示なし)'}"]
        if blocked.get("options"):
            lines.append(f"- 選択肢: {' / '.join(blocked['options'])}")
        if blocked.get("recommendation"):
            lines.append(f"- 前任の推奨: {blocked['recommendation']}")
        lines.append(f"- 状態: {blocked.get('reason', '?')}（検出: {blocked.get('kind', '?')}）")
        sections.append("## 前任が判断待ちになっていた点\n" + "\n".join(lines) + "\n")
    decision = meta.get("decision")
    if isinstance(decision, dict) and decision:
        sections.append(f"# 確定済みの判断\n\n{format_plan_sections(decision)}\n")
    if meta.get("last_output_summary"):
        sections.append(f"## 前任の出力（末尾）\n```\n{meta['last_output_summary']}\n```\n")
    sections.append(f"## 現在の変更状況\n```\n{status or '(no changes)'}\n{diff_stat}\n```\n")
    instruction = note or "判断待ちの点があれば妥当な判断を行い、その根拠を示したうえで作業を引き継いでください。"
    sections.append(f"## 引き継ぎ指示\n{instruction}\n")
    sections.append("現在の差分を確認したうえで、方針の確認と続きの実装を進めてください。")
    return "\n".join(sections)


def cmd_handoff(args: argparse.Namespace) -> None:
    """Hand off the current worktree diff to another model in a split tmux pane."""
    from_session = getattr(args, "from_session", None)
    if not from_session and not args.note:
        raise AiRouteError("A handoff note is required unless --from-session is given.")
    if from_session:
        cwd, meta = resolve_session(from_session, Path.cwd())
        refresh_session_from_log(cwd, meta)
    else:
        cwd = Path.cwd()
        meta = load_session_meta(cwd) or {}
    # Normalize once so the recorded `workdir` matches the directory the new pane starts in
    cwd = resolve_workdir(cwd)
    repo_root = get_git_root(cwd)
    resolved_files = resolve_files(args.files or [], repo_root)
    agent_files_block = format_files_for_agent(resolved_files, using_worktree=False)

    diff_stat = run_cmd(["git", "diff", "--stat"], cwd=cwd, check=False).stdout.strip()
    status = run_cmd(["git", "status", "--short"], cwd=cwd, check=False).stdout.strip()

    if from_session:
        handoff_prompt = _build_session_handoff_prompt(meta, args.note or "", agent_files_block, status, diff_stat)
    else:
        handoff_prompt = (
            f"現在作業中のworktreeを引き継ぎます。\n\n"
            f"{agent_files_block + chr(10) + chr(10) if agent_files_block else ''}"
            f"## 現在の変更状況\n```\n{status or '(no changes)'}\n{diff_stat}\n```\n\n"
            f"## 引き継ぎ指示\n{args.note}\n\n"
            f"現在の差分を確認したうえで、方針の確認と続きの実装を進めてください。"
        )

    session_id = str(meta.get("session_id") or uuid.uuid4().hex[:8])
    from_tool = str(meta.get("current_tool", "unknown"))
    from_model = str(meta.get("current_model", "unknown"))
    handoff_seq = int(meta.get("handoff_count", 0)) + 1
    repo_name, branch_name = get_repo_and_branch(cwd)

    new_meta = build_session_meta(
        session_id=session_id,
        tool=args.tool,
        model=args.model,
        workdir=cwd,
        previous=meta,
        handoff_count=handoff_seq,
        status="running",
        event="handoff",
    )
    save_session_meta(cwd, new_meta)

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
            "note": args.note or "",
            "from_session": bool(from_session),
            "blocked_question": (meta.get("blocked") or {}).get("question", "") if from_session else "",
        }
    )

    launch_implementer(
        tool=args.tool,
        model=args.model,
        prompt=handoff_prompt,
        workdir=cwd,
        mode="interactive",
        wait=False,
        session_id=session_id,
        meta=new_meta,
        timeout=None,
        split=True,
        win_name=f"handoff-{args.tool}",
        pane_title=f"ho:{args.tool}:{args.model[:16]}",
    )
