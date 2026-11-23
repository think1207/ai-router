"""Handlers for `list`, `clean`, and `done` git worktree management subcommands."""

from __future__ import annotations

import argparse
import shlex
import subprocess
from pathlib import Path

from ai_router.config import get_test_cmd_timeout
from ai_router.errors import AiRouteError
from ai_router.utils import get_git_root, run_cmd


def cmd_list(_: argparse.Namespace) -> None:
    """List active git worktrees for the current repository."""
    repo_root = get_git_root()
    if not repo_root:
        raise AiRouteError("Please run this command inside a git repository.")
    res = run_cmd(["git", "worktree", "list"], cwd=repo_root)
    print(res.stdout, end="")


def cmd_clean(args: argparse.Namespace) -> None:
    """Remove a completed git worktree and optionally delete its `ai/` branch."""
    repo_root = get_git_root()
    if not repo_root:
        raise AiRouteError("Please run this command inside a git repository.")

    target = Path(args.path).expanduser().resolve()
    branch_name = ""
    if target.exists():
        branch_res = run_cmd(["git", "branch", "--show-current"], cwd=target, check=False)
        if branch_res.returncode == 0:
            branch_name = branch_res.stdout.strip()

    rm_cmd = ["git", "worktree", "remove"]
    if args.force:
        rm_cmd.append("--force")
    rm_cmd.append(str(target))
    run_cmd(rm_cmd, cwd=repo_root, capture=False)
    print(f"Removed worktree: {target}")

    if args.delete_branch and branch_name and branch_name.startswith("ai/"):
        del_flag = "-D" if args.force else "-d"
        run_cmd(["git", "branch", del_flag, branch_name], cwd=repo_root, capture=False)
        print(f"Deleted branch: {branch_name}")


def _parse_worktree_list(repo_root: Path) -> list[dict[str, str]]:
    res = run_cmd(["git", "worktree", "list", "--porcelain"], cwd=repo_root)
    entries: list[dict[str, str]] = []
    cur: dict[str, str] = {}
    for line in res.stdout.splitlines():
        if not line.strip():
            if cur:
                entries.append(cur)
                cur = {}
            continue
        if line.startswith("worktree "):
            cur["path"] = str(Path(line.split(" ", 1)[1].strip()).resolve())
        elif line.startswith("branch "):
            ref = line.split(" ", 1)[1].strip()
            cur["branch"] = ref.removeprefix("refs/heads/")
    if cur:
        entries.append(cur)
    return entries


def cmd_done(args: argparse.Namespace) -> None:
    """Finish a worktree task in one command: run tests, merge or open a PR, and clean up."""
    cwd = Path(args.path).expanduser().resolve() if args.path else Path.cwd().resolve()
    repo_root = get_git_root(cwd)
    if not repo_root:
        raise AiRouteError("`done` must be run inside a git repository/worktree.")

    worktrees = _parse_worktree_list(repo_root)
    if not worktrees:
        raise AiRouteError("Unable to inspect git worktrees.")
    main_wt = Path(worktrees[0]["path"])
    main_branch = worktrees[0].get("branch", "")

    # Identify the target worktree (current worktree, or the single non-main worktree if run from main)
    target_wt = repo_root.resolve()
    if target_wt == main_wt and not args.path:
        ai_worktrees = [w for w in worktrees[1:] if w.get("branch", "").startswith("ai/")]
        if len(ai_worktrees) == 1:
            target_wt = Path(ai_worktrees[0]["path"])
        elif len(ai_worktrees) > 1:
            choices = "\n".join(f"  - {w['path']} ({w.get('branch', '?')})" for w in ai_worktrees)
            raise AiRouteError(
                f"Multiple `ai/` worktrees exist; pass one via `ai-route done <path>`:\n{choices}"
            )
        else:
            raise AiRouteError(
                "You are on the main worktree and no active `ai/` worktree was found. "
                "Run `ai-route done` inside a task worktree or pass its path."
            )

    branch = run_cmd(["git", "branch", "--show-current"], cwd=target_wt, check=False).stdout.strip()
    if not branch:
        raise AiRouteError(f"Worktree {target_wt} is on a detached HEAD; cannot finish automatically.")
    if target_wt == main_wt and not args.pr:
        raise AiRouteError("Cannot merge the main worktree into itself (use --pr to create a pull request).")

    status = run_cmd(["git", "status", "--short"], cwd=target_wt, check=False).stdout.strip()
    if status and not args.force:
        raise AiRouteError(
            f"Uncommitted changes exist in {target_wt}:\n{status}\n"
            "  Commit them first, or pass --force to proceed."
        )

    diff_stat = ""
    if main_branch and main_branch != branch:
        diff_stat = run_cmd(["git", "diff", "--stat", f"{main_branch}...{branch}"], cwd=target_wt, check=False).stdout.strip()

    print(f"=== Finishing Worktree ===")
    print(f" Worktree : {target_wt}")
    print(f" Branch   : {branch}" + (f" -> {main_branch}" if main_branch and not args.pr else ""))
    if diff_stat:
        print(f" Diff     :\n{diff_stat}")

    # Optional verification command before merging/pushing
    if args.test_cmd:
        timeout = get_test_cmd_timeout(args.timeout)
        print(f"[test] Running: {args.test_cmd}")
        if not args.dry_run:
            try:
                res = subprocess.run(
                    args.test_cmd,
                    shell=True,
                    cwd=str(target_wt),
                    timeout=timeout,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                raise AiRouteError(f"Test command timed out after {timeout:g}s: {args.test_cmd}")
            if res.returncode != 0:
                raise AiRouteError(f"Test command failed (rc={res.returncode}): {args.test_cmd}")

    if args.pr:
        push_cmd = ["git", "push", "-u", "origin", branch]
        pr_cmd = ["gh", "pr", "create"]
        if args.title:
            pr_cmd.extend(["--title", args.title, "--body", ""])
        else:
            pr_cmd.append("--fill")
        if args.draft:
            pr_cmd.append("--draft")
        if args.dry_run:
            print(f"[dry-run] {shlex.join(push_cmd)}")
            print(f"[dry-run] {shlex.join(pr_cmd)}")
            return
        run_cmd(push_cmd, cwd=target_wt, capture=False)
        run_cmd(pr_cmd, cwd=target_wt, capture=False)
        return

    # Default action: merge into main worktree and clean up worktree + branch
    merge_cmd = ["git", "merge"]
    if args.squash:
        merge_cmd.append("--squash")
    elif args.no_ff:
        merge_cmd.append("--no-ff")
    merge_cmd.append(branch)

    rm_cmd = ["git", "worktree", "remove"]
    if args.force:
        rm_cmd.append("--force")
    rm_cmd.append(str(target_wt))

    del_branch_cmd = ["git", "branch", "-D" if (args.force or args.squash) else "-d", branch]

    if args.dry_run:
        print(f"[dry-run] (in {main_wt}) {shlex.join(merge_cmd)}")
        if not args.keep:
            print(f"[dry-run] (in {main_wt}) {shlex.join(rm_cmd)}")
            print(f"[dry-run] (in {main_wt}) {shlex.join(del_branch_cmd)}")
        return

    run_cmd(merge_cmd, cwd=main_wt, capture=False)
    if args.squash:
        print(f"[info] Squash-merged '{branch}' into {main_wt}. Review staged changes and run `git commit`.")
    else:
        print(f"Merged '{branch}' into {main_wt} ({main_branch}).")

    if not args.keep:
        run_cmd(rm_cmd, cwd=main_wt, capture=False)
        print(f"Removed worktree: {target_wt}")
        run_cmd(del_branch_cmd, cwd=main_wt, check=False, capture=False)
        print(f"Deleted branch: {branch}")
