"""Handler for the `review` subcommand (parallel multi-model code review)."""

from __future__ import annotations

import argparse
import concurrent.futures
import sys
import uuid
from pathlib import Path
from typing import Any

from ai_router.config import (
    REVIEW_ROLES,
    ROUTER_MODEL,
    get_agent_timeout,
    get_log_file,
    get_review_timeout,
)
from ai_router.dispatch import (
    RC_COMMAND_NOT_FOUND,
    RC_TIMEOUT,
    build_agent_shell_cmd,
    dispatch_tiled_panes,
    run_agent_headless_once,
    write_prompt_file,
)
from ai_router.errors import AiRouteError
from ai_router.files import format_files_for_agent, resolve_files
from ai_router.flow import launch_implementer
from ai_router.models import DispatchEntry
from ai_router.security import (
    MAX_READ_FILE_BYTES,
    ensure_private_dir,
    ensure_private_file,
    validate_git_ref,
)
from ai_router.sessions import build_session_meta
from ai_router.storage import append_jsonl, save_session_meta
from ai_router.twostage import PLAN_JSON_SCHEMA, build_implementation_prompt, extract_plan, resolve_implementer
from ai_router.utils import (
    append_plan_first,
    plan_first_enabled,
    get_git_root,
    get_repo_and_branch,
    now_iso,
    parse_model_specs,
    parse_tool_model_spec,
    run_cmd,
)


_REAL_RUN_HEADLESS = run_agent_headless_once


def _run_review_model(
    tool: str, model: str, prompt: str, cwd: Path, *, timeout: float | None
) -> tuple[int, str, int]:
    """Run a review/synthesis model in read-only (ACP `plan`) mode when using the real runner."""
    if run_agent_headless_once is _REAL_RUN_HEADLESS:
        return run_agent_headless_once(tool, model, prompt, cwd, timeout=timeout, read_only=True)
    return run_agent_headless_once(tool, model, prompt, cwd, timeout=timeout)


def collect_review_target(args: argparse.Namespace, cwd: Path, repo_root: Path | None) -> str:
    """Gather specified files and/or git diff output as the review target payload."""
    sections: list[str] = []

    if args.files:
        resolved = resolve_files(args.files, repo_root)
        for rf in resolved:
            try:
                file_size = rf.abs_path.stat().st_size
            except OSError:
                file_size = 0
            if file_size > MAX_READ_FILE_BYTES:
                content = f"(file too large: {file_size} bytes > {MAX_READ_FILE_BYTES} bytes; skipped)"
            else:
                try:
                    content = rf.abs_path.read_text(encoding="utf-8")
                except UnicodeDecodeError:
                    content = "(binary file)"
            sections.append(f"### File: {rf.display_path}\n```\n{content}\n```")

    if not args.files or args.include_diff:
        if repo_root:
            if args.staged:
                diff_cmd = ["git", "diff", "--cached"]
                label = "git diff --cached"
            elif args.base:
                safe_base = validate_git_ref(args.base, "base ref")
                diff_cmd = ["git", "diff", f"{safe_base}...HEAD"]
                label = f"git diff {safe_base}...HEAD"
            else:
                diff_cmd = ["git", "diff", "HEAD"]
                label = "git diff HEAD"
            diff_out = run_cmd(diff_cmd, cwd=cwd, check=False).stdout.strip()
            if diff_out:
                sections.append(f"### {label}\n```diff\n{diff_out}\n```")

    if not sections:
        raise AiRouteError("No diff or files found to review (specify -f, --staged, or --base).")

    return "\n\n".join(sections)


def cmd_review(args: argparse.Namespace) -> None:
    """Run parallel code reviews across multiple models (headless or tiled interactive)."""
    then_implement = getattr(args, "then_implement", None)
    if then_implement is not None:
        if args.mode != "headless":
            raise AiRouteError("--then-implement requires headless reviews (--mode headless).")
        if args.wait and args.impl_mode != "headless":
            raise AiRouteError("--wait requires --impl-mode headless.")
        args.synthesize = True
    cwd = Path.cwd()
    repo_root = get_git_root(cwd)
    specs = parse_model_specs(args.models, args.preset)
    target_text = collect_review_target(args, cwd, repo_root)
    focus = args.focus or "バグ、設計上の懸念、エッジケース、パフォーマンス、保守性"

    group_id = uuid.uuid4().hex[:8]
    repo_name, branch_name = get_repo_and_branch(cwd)

    def build_review_prompt(idx: int) -> str:
        role_line = ""
        if args.roles:
            role_line = f"\n- **あなたの担当観点:** {REVIEW_ROLES[idx % len(REVIEW_ROLES)]}"
        return (
            "あなたは厳格なシニアソフトウェアエンジニアです。以下の変更内容／コードをレビューしてください。\n"
            "ファイルの直接編集は行わず、指摘と改善案のみを出力してください。\n\n"
            f"- **重点確認テーマ:** {focus}{role_line}\n"
            "- **出力形式:** 重要度別（Critical / Warning / Suggestion）に、該当ファイル・箇所・理由・修正案を簡潔に記載すること。\n\n"
            f"## レビュー対象\n{target_text}\n"
        )

    if args.mode == "interactive":
        entries: list[DispatchEntry] = []
        for idx, (tool, model) in enumerate(specs):
            pf = write_prompt_file(build_review_prompt(idx))
            shell_cmd = build_agent_shell_cmd(tool, model, pf, "interactive")
            entries.append(
                DispatchEntry(
                    shell_cmd=shell_cmd,
                    workdir=cwd,
                    tool=tool,
                    model=model,
                    mode="interactive",
                    prompt_file=pf,
                    title=f"rev:{tool}:{model[:16]}",
                )
            )

        append_jsonl(
            {
                "ts": now_iso(),
                "event": "review",
                "group_id": group_id,
                "repo": repo_name,
                "branch": branch_name,
                "mode": "interactive",
                "models": [f"{t}:{m}" for t, m in specs],
                "files": args.files or [],
                "focus": focus,
            }
        )
        dispatch_tiled_panes(win_name=f"rev-{group_id[:6]}", entries=entries)
        return

    # Validate the synthesizer spec before spending time on the reviews themselves
    syn_tool, syn_model = "opencode", ROUTER_MODEL
    if args.synthesize and args.synthesizer:
        syn_tool, syn_model = parse_tool_model_spec(args.synthesizer)

    timeout = get_review_timeout(args.timeout)
    timeout_desc = f"{timeout:g}s" if timeout else "none"
    print(f"=== Multi-Model Review (group={group_id}, models={len(specs)}, timeout={timeout_desc}) ===")
    results: list[dict[str, Any]] = []

    # Each model is isolated: a crash, timeout, or missing CLI in one model is recorded
    # as a failed result and never discards the reviews of the other models.
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(specs)) as ex:
        fut_map = {
            ex.submit(
                _run_review_model, tool, model, build_review_prompt(idx), cwd, timeout=timeout
            ): (idx, tool, model)
            for idx, (tool, model) in enumerate(specs)
        }
        for fut in concurrent.futures.as_completed(fut_map):
            idx, tool, model = fut_map[fut]
            try:
                rc, out, lat = fut.result()
            except Exception as e:  # noqa: BLE001 - keep other models' results no matter what
                rc, out, lat = 1, f"[error] {type(e).__name__}: {e}", 0
            failure = _describe_failure(rc)
            results.append(
                {
                    "idx": idx,
                    "tool": tool,
                    "model": model,
                    "rc": rc,
                    "output": out,
                    "latency_ms": lat,
                    "failure": failure,
                }
            )
            label = f"[FAILED: {failure}]" if failure else "[done]"
            print(f"  {label} {tool}:{model} ({lat} ms, rc={rc})")

    results.sort(key=lambda r: r["idx"])
    succeeded = [r for r in results if not r["failure"]]
    failed = [r for r in results if r["failure"]]

    report_parts = [f"# Multi-Model Code Review (group: {group_id})\n"]
    if failed:
        names = ", ".join(f"{r['tool']}:{r['model']} ({r['failure']})" for r in failed)
        report_parts.append(f"> [warn] {len(failed)}/{len(results)} model(s) failed: {names}\n")
    for r in results:
        if r["failure"]:
            report_parts.append(
                f"## {r['tool']} ({r['model']}) [{r['latency_ms']} ms] - FAILED ({r['failure']})\n\n"
                f"{r['output']}\n"
            )
        else:
            report_parts.append(f"## {r['tool']} ({r['model']}) [{r['latency_ms']} ms]\n\n{r['output']}\n")

    synthesized = False
    syn_out = ""
    if args.synthesize:
        if not succeeded:
            print("  [warn] Skipping synthesis: no successful reviews to synthesize.", file=sys.stderr)
            report_parts.insert(1, "## Synthesized Summary - SKIPPED (no successful reviews)\n\n---\n")
        else:
            syn_prompt = (
                "以下は同一コードに対して複数のAIモデルが独立して行ったコードレビュー結果です。\n"
                "これらを統合し、次の3セクションで簡潔な最終レビューサマリを日本語で作成してください：\n"
                "1. **複数モデルが一致して指摘した重要課題（優先対応）**\n"
                "2. **特定モデルのみが指摘した注目点（見落とし防止）**\n"
                "3. **モデル間で見解が割れた論点・設計トレードオフ**\n\n"
                + "\n---\n".join(
                    f"## {r['tool']} ({r['model']})\n\n{r['output']}\n" for r in succeeded
                )
            )
            if then_implement is not None:
                syn_prompt += (
                    "\n---\n最後に、統合結果に基づく修正計画を次のスキーマのJSONオブジェクトとして出力してください。"
                    "ファイルの編集は行わないでください。\n"
                    f"{PLAN_JSON_SCHEMA}\n"
                )
            print(f"  [synthesizing with {syn_tool}:{syn_model}...]")
            try:
                syn_rc, syn_out, syn_lat = _run_review_model(
                    syn_tool, syn_model, syn_prompt, cwd, timeout=timeout
                )
            except Exception as e:  # noqa: BLE001 - individual reviews must still be saved
                syn_rc, syn_out, syn_lat = 1, f"[error] {type(e).__name__}: {e}", 0
            syn_failure = _describe_failure(syn_rc)
            if syn_failure:
                print(
                    f"  [warn] Synthesis failed ({syn_failure}); saving individual reviews only.",
                    file=sys.stderr,
                )
                report_parts.insert(
                    1,
                    f"## Synthesized Summary ({syn_tool}:{syn_model}) - FAILED ({syn_failure})\n\n"
                    f"> [warn] Synthesis failed; see individual reviews below.\n\n{syn_out}\n\n---\n",
                )
            else:
                synthesized = True
                report_parts.insert(
                    1,
                    f"## Synthesized Summary ({syn_tool}:{syn_model}, {syn_lat} ms)\n\n{syn_out}\n\n---\n",
                )

    full_report = "\n".join(report_parts)

    out_path = (
        Path(args.output)
        if args.output
        else (get_log_file().parent / "reviews" / f"review-{group_id}.md")
    )
    ensure_private_dir(out_path.parent)
    out_path.write_text(full_report, encoding="utf-8")
    ensure_private_file(out_path)

    append_jsonl(
        {
            "ts": now_iso(),
            "event": "review",
            "group_id": group_id,
            "repo": repo_name,
            "branch": branch_name,
            "mode": "headless",
            "models": [f"{t}:{m}" for t, m in specs],
            "failed_models": [f"{r['tool']}:{r['model']}" for r in failed],
            "synthesized": synthesized,
            "then_implement": then_implement,
            "timeout_sec": timeout,
            "report_path": str(out_path),
            "files": args.files or [],
            "focus": focus,
        }
    )

    print("\n" + full_report)
    print(f"\n[saved] {out_path}")

    if not succeeded:
        raise AiRouteError(f"All {len(results)} review model(s) failed; report saved to {out_path}")

    if then_implement is not None:
        if not synthesized:
            raise AiRouteError("Synthesis failed; skipping --then-implement (report saved).")
        _implement_from_review(
            args,
            cwd=cwd,
            group_id=group_id,
            focus=focus,
            synthesized_output=syn_out,
            planner=(syn_tool, syn_model),
            report_path=out_path,
            impl_spec=then_implement,
        )


def _implement_from_review(
    args: argparse.Namespace,
    *,
    cwd: Path,
    group_id: str,
    focus: str,
    synthesized_output: str,
    planner: tuple[str, str],
    report_path: Path,
    impl_spec: str,
) -> None:
    """Launch an implementer using the synthesized review as the planner output."""
    plan, valid = extract_plan(synthesized_output)
    if not valid:
        print("[warn] Synthesis did not contain a valid plan JSON; passing it as raw notes.", file=sys.stderr)
    impl_tool, impl_model = resolve_implementer(plan["implementer_difficulty"], impl_spec or None, None)
    task = (
        f"マルチモデルコードレビュー（group: {group_id}, 重点: {focus}）で指摘された問題を修正してください。"
        f"レビュー全文: {report_path}"
    )
    files_block = format_files_for_agent(resolve_files(args.files or [], get_git_root(cwd)), using_worktree=False)
    prompt = build_implementation_prompt(plan, task, files_block)
    prompt = append_plan_first(prompt, plan_first_enabled(False, args.impl_mode))

    session_id = uuid.uuid4().hex[:8]
    repo_name, branch_name = get_repo_and_branch(cwd)
    print(f"\n=== Implementing review result with {impl_tool}:{impl_model} (session={session_id}) ===")
    meta = build_session_meta(
        session_id=session_id,
        tool=impl_tool,
        model=impl_model,
        workdir=cwd,
        handoff_count=0,
        difficulty=plan["implementer_difficulty"],
        refined_prompt=prompt,
        task=task,
        status="running",
        event="route",
        stage="implementer",
        parent_session=group_id,
        mode=args.impl_mode,
        decision={**plan, "tool": planner[0], "model": planner[1], "valid": valid, "ts": now_iso()},
    )
    save_session_meta(cwd, meta)
    common = {"repo": repo_name, "branch": branch_name, "workdir": str(cwd), "task": task}
    append_jsonl(
        {
            "ts": now_iso(),
            "event": "two_stage",
            "source": "review",
            "status": "planned",
            "group_id": group_id,
            "parent_session": group_id,
            "session_id": session_id,
            "planner_tool": planner[0],
            "planner_model": planner[1],
            "plan_valid": valid,
            "implementer_tool": impl_tool,
            "implementer_model": impl_model,
            "implementer_difficulty": plan["implementer_difficulty"],
            **common,
        }
    )
    append_jsonl(
        {
            "ts": now_iso(),
            "event": "route",
            "session_id": session_id,
            "tool": impl_tool,
            "model": impl_model,
            "difficulty": plan["implementer_difficulty"],
            "mode": args.impl_mode,
            "worktree": False,
            "router_model": None,
            "router_latency_ms": 0,
            "is_fallback": False,
            "files": args.files or [],
            "reason": f"review --then-implement (group {group_id})",
            "stage": "implementer",
            "parent_session": group_id,
            "wait": bool(args.wait),
            **common,
        }
    )
    launch_implementer(
        tool=impl_tool,
        model=impl_model,
        prompt=prompt,
        workdir=cwd,
        mode=args.impl_mode,
        wait=bool(args.wait),
        session_id=session_id,
        meta=meta,
        timeout=get_agent_timeout(args.agent_timeout),
        auto_escalate=bool(args.auto_escalate),
        stage="implementer",
        win_name=f"fix-{group_id[:6]}",
        pane_title=f"fix:{impl_tool}:{impl_model[:14]}",
        effort=str(plan.get("implementer_effort") or ""),
        difficulty=plan["implementer_difficulty"],
    )


def _describe_failure(rc: int) -> str:
    """Return a short failure label for a headless agent return code ("" on success)."""
    if rc == 0:
        return ""
    if rc == RC_TIMEOUT:
        return "TIMEOUT"
    if rc == RC_COMMAND_NOT_FOUND:
        return "command not found"
    return f"rc={rc}"
