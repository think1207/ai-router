"""Handlers for `list` and `clean` git worktree management subcommands."""

from __future__ import annotations

import argparse
from pathlib import Path

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
