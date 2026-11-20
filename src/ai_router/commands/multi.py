"""Handlers for `multi`, `compare`, and `pick` subcommands."""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

from ai_router.config import get_log_file, get_test_cmd_timeout
from ai_router.dispatch import (
    build_agent_shell_cmd,
    dispatch_tiled_panes,
    dispatch_to_tmux,
    write_prompt_file,
)
from ai_router.errors import AiRouteError
from ai_router.files import format_files_for_agent, load_task_text, resolve_files
from ai_router.models import DispatchEntry
from ai_router.storage import append_jsonl, load_jsonl, save_session_meta
from ai_router.utils import (
    append_plan_first,
    get_git_root,
    get_main_worktree_head,
    get_repo_and_branch,
    now_iso,
    parse_model_specs,
    plan_first_enabled,
    resolve_commit,
    run_cmd,
    sanitize_slug,
)
from ai_router.worktree import create_worktree


def cmd_multi(args: argparse.Namespace) -> None:
    """Launch multiple models in isolated git worktrees to solve the same task in parallel."""
    if not os.environ.get("TMUX"):
        raise AiRouteError("`multi` requires running inside a tmux session to spawn parallel panes/windows.")

    repo_root = get_git_root()
    if not repo_root:
        raise AiRouteError("`multi` can only be used inside a git repository (worktree isolation is required).")

    task_text = load_task_text(args.task, args.task_file)
    resolved_files = resolve_files(args.files or [], repo_root)
    specs = parse_model_specs(args.models, args.preset)

    # Pin the base to an absolute commit before creating anything. A relative ref such as
    # `HEAD` would later be re-evaluated inside each worktree (where it means the agent's own
    # HEAD), which made `compare` report zero commits.
    base_sha = resolve_commit(args.base, repo_root)
    if not base_sha:
        raise AiRouteError(f"Invalid base ref '{args.base}': it does not resolve to a commit.")

    group_id = uuid.uuid4().hex[:6]
    base_slug = sanitize_slug(args.slug or f"multi-{group_id}")

    agent_files_block = format_files_for_agent(resolved_files, using_worktree=True)
    base_prompt = f"{agent_files_block}\n\n{task_text}" if agent_files_block else task_text
    base_prompt = append_plan_first(base_prompt, plan_first_enabled(args.no_plan_first, args.mode))

    print(f"=== Multi-Model Parallel Solve (group={group_id}, base={args.base} @ {base_sha[:12]}) ===")

    entries: list[DispatchEntry] = []
    created_members: list[dict[str, Any]] = []

    for idx, (tool, model) in enumerate(specs, 1):
        model_tag = sanitize_slug(f"{tool}-{model}")[:16]
        slug = f"m{group_id}-{idx}-{model_tag}"
        wt_path, branch_name = create_worktree(repo_root, slug, base_ref=base_sha)

        session_id = f"{group_id}-{idx}"
        save_session_meta(
            wt_path,
            {
                "session_id": session_id,
                "group_id": group_id,
                "current_tool": tool,
                "current_model": model,
                "handoff_count": 0,
                "started_at": now_iso(),
            },
        )

        pf = write_prompt_file(base_prompt)
        shell_cmd = build_agent_shell_cmd(tool, model, pf, args.mode)

        entries.append(
            DispatchEntry(
                shell_cmd=shell_cmd,
                workdir=wt_path,
                tool=tool,
                model=model,
                mode=args.mode,
                prompt_file=pf,
                title=f"{idx}:{tool}:{model[:14]}",
                win_name=f"m{group_id}-{tool[:4]}{idx}",
            )
        )
        created_members.append(
            {
                "index": idx,
                "tool": tool,
                "model": model,
                "branch": branch_name,
                "workdir": str(wt_path),
            }
        )
        print(f"  [{idx}] {tool}:{model:<28} -> {wt_path} ({branch_name})")

    repo_name, _ = get_repo_and_branch(repo_root)
    append_jsonl(
        {
            "ts": now_iso(),
            "event": "multi",
            "group_id": group_id,
            "repo": repo_name,
            "base_ref": base_sha,
            "base_input": args.base,
            "mode": args.mode,
            "layout": args.layout,
            "members": created_members,
            "files": [rf.display_path for rf in resolved_files],
            "task": task_text,
        }
    )

    if args.layout == "tiled":
        dispatch_tiled_panes(win_name=f"multi-{base_slug[:12]}", entries=entries)
    else:
        for entry in entries:
            dispatch_to_tmux(
                shell_cmd=entry.shell_cmd,
                workdir=entry.workdir,
                win_name=entry.win_name,
                tool=entry.tool,
                mode=entry.mode,
                prompt_file=entry.prompt_file,
                split_current=False,
                pane_title=entry.title,
            )

    print(f"\nCompare command: ai-route compare {group_id}")
    print(f"Pick command   : ai-route pick {group_id}")


def _find_multi_event(group_id: str, *, log_file: Path | None = None) -> dict[str, Any] | None:
    """Find the matching `multi` event by group_id, or the latest `multi` event if empty."""
    target_log = log_file or get_log_file()
    if not target_log.is_file():
        return None

    target_event: dict[str, Any] | None = None
    for rec in load_jsonl(target_log, warn_malformed=False):
        if rec.get("event") == "multi":
            if not group_id or rec.get("group_id") == group_id:
                target_event = rec
    return target_event


_FULL_OR_ABBREV_SHA = re.compile(r"^[0-9a-f]{7,64}$")


def _resolve_group_base(event: dict[str, Any], git_cwd: Path | None) -> tuple[str, str]:
    """Return `(base_ref, source)` to diff a `multi` group's members against.

    New logs record the base as a commit SHA, which is used as-is. Legacy logs recorded a
    relative ref (typically "HEAD"); for those the fork point is recovered as:
      * 2+ surviving member branches: `git merge-base --octopus <branches...>`
      * 1 surviving member branch: merge-base of it and the main worktree's HEAD
    If neither works, a warning is printed and the recorded ref is returned unchanged.
    """
    recorded = str(event.get("base_ref") or "HEAD")
    if git_cwd is not None:
        if _FULL_OR_ABBREV_SHA.match(recorded):
            sha = resolve_commit(recorded, git_cwd)
            if sha:
                return sha, "recorded"

        branches = [
            str(m.get("branch", ""))
            for m in event.get("members", [])
            if _branch_exists(git_cwd, str(m.get("branch", "")))
        ]
        merge_base_cmd: list[str] | None = None
        source = ""
        if len(branches) >= 2:
            merge_base_cmd = ["git", "merge-base", "--octopus", *branches]
            source = f"merge-base of {len(branches)} member branches"
        elif len(branches) == 1:
            main_head = get_main_worktree_head(git_cwd)
            if main_head:
                merge_base_cmd = ["git", "merge-base", branches[0], main_head]
                source = "merge-base of member branch and main worktree HEAD"
        if merge_base_cmd:
            res = run_cmd(merge_base_cmd, cwd=git_cwd, check=False)
            sha = res.stdout.strip()
            if res.returncode == 0 and sha:
                return sha, source

    print(
        f"[warn] Could not determine the fork point for base_ref '{recorded}'; "
        f"commit counts and diffs below may be inaccurate.",
        file=sys.stderr,
    )
    return recorded, "unresolved"


def cmd_compare(args: argparse.Namespace) -> None:
    """Compare diffs, commits, and optional test results across a `multi` group's worktrees."""
    log_file = get_log_file()
    if not log_file.is_file():
        raise AiRouteError(f"Log file not found: {log_file}")

    target_event = _find_multi_event(args.group_id, log_file=log_file)
    if not target_event:
        raise AiRouteError(f"Matching `multi` group not found: {args.group_id or '(latest)'}")

    gid = target_event["group_id"]
    # Prefer a surviving member worktree: it is guaranteed to belong to the group's repository,
    # whereas the current directory may be another repository (or none at all).
    git_cwd = next(
        (Path(m["workdir"]) for m in target_event.get("members", []) if Path(m["workdir"]).exists()),
        None,
    ) or get_git_root()
    base_ref, base_source = _resolve_group_base(target_event, git_cwd)
    task_headline = str(target_event.get("task", "")).splitlines()[0][:80] if target_event.get("task") else ""
    test_timeout = get_test_cmd_timeout(args.timeout) if args.test_cmd else None
    print(f"=== Multi-Model Comparison (group={gid}, base={base_ref[:12]} [{base_source}]) ===")
    print(f"Task: {task_headline}\n")

    for member in target_event.get("members", []):
        wt = Path(member["workdir"])
        print("-" * 72)
        print(f"[{member['index']}] {member['tool']}:{member['model']}  (branch: {member['branch']})")
        if not wt.exists():
            print("  (worktree already removed)")
            continue

        status = run_cmd(["git", "status", "--short"], cwd=wt, check=False).stdout.strip()
        commits = run_cmd(["git", "log", "--oneline", f"{base_ref}..HEAD"], cwd=wt, check=False).stdout.strip()
        diff_stat = run_cmd(["git", "diff", "--stat", base_ref], cwd=wt, check=False).stdout.strip()

        print(f"  Path   : {wt}")
        print(f"  Commits: {len(commits.splitlines()) if commits else 0}")
        if commits:
            for c in commits.splitlines()[:5]:
                print(f"    {c}")
        if status:
            print("  Uncommitted:")
            for s in status.splitlines()[:10]:
                print(f"    {s}")
        if diff_stat:
            print("  Diff stat vs base:")
            for d in diff_stat.splitlines():
                print(f"    {d}")

        if args.test_cmd:
            print(f"  Running test: {args.test_cmd}")
            print(f"  Test result : {_run_test_cmd(args.test_cmd, wt, test_timeout)}")

    print("-" * 72)
    print(f"Pick command: ai-route pick {gid} <index>")


def _run_test_cmd(test_cmd: str, wt: Path, timeout: float | None) -> str:
    """Run a verification command in a worktree and return a one-line verdict.

    Timeouts and launch failures are reported as verdicts so `compare` continues with
    the next member. Limitation: on timeout only the direct `bash` child is killed, so
    processes it spawned (e.g. the test runner of a compound command) may keep running.
    """
    try:
        t_res = run_cmd(["bash", "-lc", test_cmd], cwd=wt, check=False, timeout=timeout)
    except subprocess.TimeoutExpired:
        return f"TIMEOUT (>{timeout:g}s)"
    except OSError as e:
        return f"ERROR (could not run: {e})"
    return "PASS" if t_res.returncode == 0 else f"FAIL (rc={t_res.returncode})"


def _branch_exists(repo_root: Path, branch_name: str) -> bool:
    """Return True if a local git branch exists in the repository."""
    if not branch_name:
        return False
    res = run_cmd(
        ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch_name}"],
        cwd=repo_root,
        check=False,
    )
    return res.returncode == 0


def _resolve_pick_event_and_choice(
    args: argparse.Namespace,
    log_file: Path,
) -> tuple[dict[str, Any], int | None]:
    """Resolve the target `multi` event and optional member index from CLI arguments."""
    if args.choice is not None and args.choice_opt is not None and args.choice != args.choice_opt:
        raise AiRouteError("Conflicting member indices provided via positional argument and --choice.")

    explicit_choice = args.choice if args.choice is not None else args.choice_opt
    target = (args.target or "").strip()

    if target and explicit_choice is None:
        matched = _find_multi_event(target, log_file=log_file)
        if matched is not None:
            return matched, None
        if target.isdigit():
            latest = _find_multi_event("", log_file=log_file)
            if latest is None:
                raise AiRouteError("No `multi` group found in log.")
            return latest, int(target)
        raise AiRouteError(f"Matching `multi` group not found: {target}")

    event = _find_multi_event(target, log_file=log_file)
    if event is None:
        raise AiRouteError(f"Matching `multi` group not found: {target or '(latest)'}")
    return event, explicit_choice


def _prompt_pick_member(members: list[dict[str, Any]], base_ref: str, repo_root: Path) -> int:
    """Display candidate branches from a `multi` group and interactively prompt for a selection."""
    print("Available candidates:")
    valid_indices: list[int] = []
    for member in members:
        idx = int(member["index"])
        valid_indices.append(idx)
        branch = str(member.get("branch", ""))
        wt = Path(member["workdir"])
        commits = ""
        if _branch_exists(repo_root, branch):
            commits = run_cmd(
                ["git", "log", "--oneline", f"{base_ref}..{branch}"],
                cwd=repo_root,
                check=False,
            ).stdout.strip()
        commit_cnt = len(commits.splitlines()) if commits else 0
        dirty_tag = ""
        if wt.exists():
            wt_status = run_cmd(["git", "status", "--porcelain"], cwd=wt, check=False).stdout.strip()
            if wt_status:
                dirty_tag = " [uncommitted changes]"
        else:
            dirty_tag = " [worktree removed]"
        print(
            f"  [{idx}] {member['tool']}:{member['model']} "
            f"(branch: {branch}, commits: {commit_cnt}){dirty_tag}"
        )

    valid_str = "/".join(str(i) for i in valid_indices)
    while True:
        try:
            raw = input(f"\nSelect member to merge [{valid_str}] (or 'q' to cancel): ").strip()
        except EOFError:
            raise AiRouteError("Aborted (no input received).")
        if raw.lower() in {"q", "quit", "exit", ""}:
            print("Cancelled.")
            sys.exit(0)
        if raw.isdigit() and int(raw) in valid_indices:
            return int(raw)
        print(f"[warn] Invalid selection '{raw}'. Enter one of: {valid_str} (or 'q' to cancel).")


def cmd_pick(args: argparse.Namespace) -> None:
    """Merge the chosen branch from a `multi` group and remove worktrees and `ai/` branches."""
    repo_root = get_git_root()
    if not repo_root:
        raise AiRouteError("Please run this command inside a git repository.")

    log_file = get_log_file()
    if not log_file.is_file():
        raise AiRouteError(f"Log file not found: {log_file}")

    target_event, chosen_index = _resolve_pick_event_and_choice(args, log_file)
    members: list[dict[str, Any]] = list(target_event.get("members", []))
    if not members:
        raise AiRouteError("Selected `multi` group has no recorded members.")

    gid = str(target_event["group_id"])
    repo_name, current_branch = get_repo_and_branch(repo_root)

    print(f"=== Multi-Model Pick (group={gid}, target branch={current_branch}) ===")

    if chosen_index is None:
        base_ref, _ = _resolve_group_base(target_event, repo_root)
        chosen_index = _prompt_pick_member(members, base_ref, repo_root)

    member_by_index = {int(m["index"]): m for m in members}
    if chosen_index not in member_by_index:
        avail = ", ".join(str(i) for i in sorted(member_by_index))
        raise AiRouteError(f"Invalid member index: {chosen_index} (available: {avail})")

    winner = member_by_index[chosen_index]
    winner_branch = str(winner.get("branch", ""))
    winner_wt = Path(winner["workdir"]).resolve()

    if not _branch_exists(repo_root, winner_branch):
        raise AiRouteError(f"Winning branch does not exist: {winner_branch}")

    if current_branch == winner_branch:
        raise AiRouteError(
            f"Already checked out on the winning branch ({winner_branch}). "
            "Switch to your target branch (e.g. main) before running `pick`."
        )

    # Ensure we are not standing inside a worktree scheduled for removal
    cwd_resolved = Path.cwd().resolve()
    for member in members:
        if int(member["index"]) == chosen_index and args.keep_winner:
            continue
        wt_path = Path(member["workdir"]).resolve()
        if cwd_resolved == wt_path or wt_path in cwd_resolved.parents:
            raise AiRouteError(
                f"Current directory is inside worktree to be removed ({wt_path}). "
                "Run `pick` from the main repository instead."
            )

    # Guard against merging into a dirty repository
    main_status = run_cmd(["git", "status", "--porcelain"], cwd=repo_root, check=False).stdout.strip()
    if main_status and not args.dry_run:
        raise AiRouteError(
            "Target repository has uncommitted changes. "
            "Commit or stash them before running `pick`."
        )

    # Guard against losing uncommitted changes in the winning worktree
    if winner_wt.exists():
        winner_status = run_cmd(
            ["git", "status", "--porcelain"], cwd=winner_wt, check=False
        ).stdout.strip()
        if winner_status and not args.force:
            raise AiRouteError(
                f"Winning worktree has uncommitted changes: {winner_wt}\n"
                "Commit them in that worktree first so they are included in the merge, "
                "or pass --force to merge only committed changes."
            )

    ahead_commits = run_cmd(
        ["git", "rev-list", "--count", f"HEAD..{winner_branch}"],
        cwd=repo_root,
        check=False,
    ).stdout.strip()
    if ahead_commits == "0":
        print(
            f"[warn] Winning branch '{winner_branch}' has 0 commits ahead of HEAD.",
            file=sys.stderr,
        )

    merge_strategy = "squash" if args.squash else ("no-ff" if args.no_ff else "merge")
    merge_cmd = ["git", "merge"]
    if args.squash:
        merge_cmd.append("--squash")
    elif args.no_ff:
        merge_cmd.append("--no-ff")
    merge_cmd.append(winner_branch)

    print(
        f" Winner    : [{chosen_index}] {winner['tool']}:{winner['model']} "
        f"(branch: {winner_branch})"
    )
    print(f" Merge cmd : {' '.join(merge_cmd)}")

    cleanup_targets = [
        m for m in members if not (int(m["index"]) == chosen_index and args.keep_winner)
    ]

    if args.dry_run:
        print("\n[Dry Run] Planned cleanup:")
        for member in cleanup_targets:
            print(f"  - worktree: {member['workdir']}")
            print(f"    branch  : {member['branch']}")
        return

    merge_res = run_cmd(merge_cmd, cwd=repo_root, check=False, capture=False)
    if merge_res.returncode != 0:
        raise AiRouteError(
            f"Merge failed (rc={merge_res.returncode}, likely due to conflicts). "
            "No worktrees or branches were deleted. Resolve conflicts or run `git merge --abort`."
        )

    if args.squash:
        print("\n[info] Squash-merged changes are staged. Run `git commit` to finalize the commit.")

    removed_worktrees: list[str] = []
    deleted_branches: list[str] = []
    skipped_items: list[str] = []

    print("\nCleaning up worktrees and `ai/` branches...")
    for member in cleanup_targets:
        idx = int(member["index"])
        is_winner = idx == chosen_index
        wt_path = Path(member["workdir"]).expanduser().resolve()
        branch_name = str(member.get("branch", ""))

        wt_removed_ok = True
        if wt_path.exists():
            rm_cmd = ["git", "worktree", "remove"]
            if args.force:
                rm_cmd.append("--force")
            rm_cmd.append(str(wt_path))
            rm_res = run_cmd(rm_cmd, cwd=repo_root, check=False)
            if rm_res.returncode == 0:
                removed_worktrees.append(str(wt_path))
                print(f"  [removed worktree] {wt_path}")
            else:
                wt_removed_ok = False
                skipped_items.append(str(wt_path))
                detail = (rm_res.stderr or rm_res.stdout).strip()
                print(
                    f"  [warn] Skipped worktree {wt_path}: {detail or 'removal failed'} "
                    f"(use `ai-route clean {wt_path} -b -f`)",
                    file=sys.stderr,
                )
        else:
            run_cmd(["git", "worktree", "prune"], cwd=repo_root, check=False)

        if wt_removed_ok and branch_name.startswith("ai/") and _branch_exists(repo_root, branch_name):
            # Non-winning branches are unmerged by design and require -D;
            # squash merges also leave the winning branch unmerged in git ancestry.
            del_flag = "-d" if (is_winner and not args.squash and not args.force) else "-D"
            br_res = run_cmd(["git", "branch", del_flag, branch_name], cwd=repo_root, check=False)
            if br_res.returncode == 0:
                deleted_branches.append(branch_name)
                print(f"  [deleted branch]   {branch_name}")
            else:
                skipped_items.append(branch_name)
                detail = (br_res.stderr or br_res.stdout).strip()
                print(
                    f"  [warn] Failed to delete branch {branch_name}: {detail}",
                    file=sys.stderr,
                )

    append_jsonl(
        {
            "ts": now_iso(),
            "event": "pick",
            "group_id": gid,
            "repo": repo_name,
            "target_branch": current_branch,
            "winner_index": chosen_index,
            "winner_tool": winner["tool"],
            "winner_model": winner["model"],
            "winner_branch": winner_branch,
            "merge_strategy": merge_strategy,
            "kept_winner": bool(args.keep_winner),
            "removed_worktrees": removed_worktrees,
            "deleted_branches": deleted_branches,
        },
        log_file=log_file,
    )

    print(
        f"\nDone: merged {winner_branch} into {current_branch} "
        f"(removed {len(removed_worktrees)} worktree(s), deleted {len(deleted_branches)} branch(es))."
    )
    if skipped_items:
        print(
            f"[warn] {len(skipped_items)} item(s) were skipped; review warnings above.",
            file=sys.stderr,
        )
