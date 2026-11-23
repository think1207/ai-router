"""Git worktree creation and management helpers."""

from __future__ import annotations

import datetime
import shutil
import sys
from pathlib import Path

from ai_router.config import WORKTREE_COPY_FILES
from ai_router.security import ensure_private_file, validate_git_ref
from ai_router.utils import run_cmd


def create_worktree(repo_root: Path, slug: str, base_ref: str = "HEAD") -> tuple[Path, str]:
    """Create a new isolated git worktree on a timestamped `ai/<slug>-<ts>` branch."""
    safe_base = validate_git_ref(base_ref, "base ref")
    status = run_cmd(["git", "status", "--porcelain"], cwd=repo_root, check=False).stdout.strip()
    if status:
        print(
            "[warn] Uncommitted changes detected in the main repository. "
            "The new worktree is created from the base commit and will not include uncommitted changes.",
            file=sys.stderr,
        )

    ts = datetime.datetime.now().strftime("%m%d-%H%M%S")
    branch_name = validate_git_ref(f"ai/{slug}-{ts}", "branch name")
    wt_base = repo_root.parent / f"{repo_root.name}-worktrees"
    wt_base.mkdir(parents=True, exist_ok=True)
    wt_path = wt_base / f"{slug}-{ts}"

    run_cmd(["git", "worktree", "add", "-b", branch_name, str(wt_path), safe_base], cwd=repo_root)

    resolved_root = repo_root.resolve()
    for rel in WORKTREE_COPY_FILES:
        src = repo_root / rel
        dst = wt_path / rel
        if src.is_symlink():
            print(
                f"[warn] Skipping symlinked config file '{rel}' when copying into worktree (security).",
                file=sys.stderr,
            )
            continue
        if not src.is_file() or dst.exists():
            continue
        try:
            src_resolved = src.resolve()
            if not src_resolved.is_relative_to(resolved_root):
                print(
                    f"[warn] Skipping '{rel}' because it resolves outside the repository root.",
                    file=sys.stderr,
                )
                continue
        except OSError:
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        ensure_private_file(dst)

    return wt_path, branch_name
