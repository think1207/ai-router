"""Git worktree creation and management helpers."""

from __future__ import annotations

import datetime
import shutil
import sys
from pathlib import Path

from ai_router.config import WORKTREE_COPY_FILES
from ai_router.utils import run_cmd


def create_worktree(repo_root: Path, slug: str, base_ref: str = "HEAD") -> tuple[Path, str]:
    """Create a new isolated git worktree on a timestamped `ai/<slug>-<ts>` branch."""
    status = run_cmd(["git", "status", "--porcelain"], cwd=repo_root, check=False).stdout.strip()
    if status:
        print(
            "[warn] Uncommitted changes detected in the main repository. "
            "The new worktree is created from the base commit and will not include uncommitted changes.",
            file=sys.stderr,
        )

    ts = datetime.datetime.now().strftime("%m%d-%H%M%S")
    branch_name = f"ai/{slug}-{ts}"
    wt_base = repo_root.parent / f"{repo_root.name}-worktrees"
    wt_base.mkdir(parents=True, exist_ok=True)
    wt_path = wt_base / f"{slug}-{ts}"

    run_cmd(["git", "worktree", "add", "-b", branch_name, str(wt_path), base_ref], cwd=repo_root)

    for rel in WORKTREE_COPY_FILES:
        src = repo_root / rel
        dst = wt_path / rel
        if src.is_file() and not dst.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)

    return wt_path, branch_name
