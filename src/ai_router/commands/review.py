"""Handler for the `review` subcommand (parallel multi-model code review)."""

from __future__ import annotations

import argparse
import concurrent.futures
import sys
import uuid
from pathlib import Path
from typing import Any

from ai_router.config import REVIEW_ROLES, ROUTER_MODEL, get_log_file, get_review_timeout
from ai_router.dispatch import (
    RC_COMMAND_NOT_FOUND,
    RC_TIMEOUT,
    build_agent_shell_cmd,
    dispatch_tiled_panes,
    run_agent_headless_once,
    write_prompt_file,
)
from ai_router.errors import AiRouteError
from ai_router.files import resolve_files
from ai_router.models import DispatchEntry
from ai_router.storage import append_jsonl
from ai_router.utils import (
    get_git_root,
    get_repo_and_branch,
    now_iso,
    parse_model_specs,
    parse_tool_model_spec,
    run_cmd,
)


def collect_review_target(args: argparse.Namespace, cwd: Path, repo_root: Path | None) -> str:
    """Gather specified files and/or git diff output as the review target payload."""
    sections: list[str] = []

    if args.files:
        resolved = resolve_files(args.files, repo_root)
        for rf in resolved:
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
                diff_cmd = ["git", "diff", f"{args.base}...HEAD"]
                label = f"git diff {args.base}...HEAD"
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
                run_agent_headless_once, tool, model, build_review_prompt(idx), cwd, timeout=timeout
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
            print(f"  [synthesizing with {syn_tool}:{syn_model}...]")
            try:
                syn_rc, syn_out, syn_lat = run_agent_headless_once(
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
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(full_report, encoding="utf-8")

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


def _describe_failure(rc: int) -> str:
    """Return a short failure label for a headless agent return code ("" on success)."""
    if rc == 0:
        return ""
    if rc == RC_TIMEOUT:
        return "TIMEOUT"
    if rc == RC_COMMAND_NOT_FOUND:
        return "command not found"
    return f"rc={rc}"
