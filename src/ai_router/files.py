"""File resolution, preview generation, and task text loading utilities."""

from __future__ import annotations

import sys
from pathlib import Path

from ai_router.config import ROUTER_FILE_MAX_LINES, ROUTER_TOTAL_PREVIEW_CHARS
from ai_router.models import ResolvedFile
from ai_router.utils import run_cmd


def resolve_files(file_args: list[str], repo_root: Path | None) -> list[ResolvedFile]:
    """Validate and normalize user-specified files and generate lightweight router previews."""
    resolved: list[ResolvedFile] = []
    seen: set[Path] = set()
    budget = ROUTER_TOTAL_PREVIEW_CHARS

    for raw in file_args:
        path = Path(raw).expanduser().resolve()
        if not path.exists():
            print(f"[error] Specified file not found: {raw}", file=sys.stderr)
            sys.exit(1)
        if path.is_dir():
            print(
                f"[warn] Skipping directory (specify individual files or a glob instead): {raw}",
                file=sys.stderr,
            )
            continue
        if path in seen:
            continue
        seen.add(path)

        in_repo = False
        is_tracked = False
        display_path = str(path)
        if repo_root:
            try:
                rel = path.relative_to(repo_root.resolve())
                in_repo = True
                display_path = rel.as_posix()
                chk = run_cmd(
                    ["git", "ls-files", "--error-unmatch", display_path],
                    cwd=repo_root,
                    check=False,
                )
                is_tracked = chk.returncode == 0
            except ValueError:
                in_repo = False

        line_count: int | None = None
        preview = ""
        try:
            text = path.read_text(encoding="utf-8")
            lines = text.splitlines()
            line_count = len(lines)
            if budget > 0:
                head = "\n".join(lines[:ROUTER_FILE_MAX_LINES])
                if len(lines) > ROUTER_FILE_MAX_LINES:
                    head += f"\n... (全 {line_count} 行中 {ROUTER_FILE_MAX_LINES} 行まで抜粋)"
                if len(head) > budget:
                    head = head[:budget] + "\n... (プレビュー上限により省略)"
                preview = head
                budget -= len(head)
        except UnicodeDecodeError:
            preview = "(binary file)"

        resolved.append(
            ResolvedFile(
                display_path=display_path,
                abs_path=path,
                in_repo=in_repo,
                is_tracked=is_tracked,
                line_count=line_count,
                preview=preview,
            )
        )
    return resolved


def format_files_for_router(files: list[ResolvedFile]) -> str:
    """Format resolved file previews for inclusion in the Japanese router prompt."""
    if not files:
        return "(指定ファイルなし)"
    blocks: list[str] = []
    for rf in files:
        loc = "repo内" if rf.in_repo else "repo外(参照のみ)"
        lc = f"{rf.line_count}行" if rf.line_count is not None else "binary"
        blocks.append(f"### {rf.display_path} [{loc}, {lc}]\n```\n{rf.preview}\n```")
    return "\n\n".join(blocks)


def format_files_for_agent(files: list[ResolvedFile], *, using_worktree: bool) -> str:
    """Format target/reference file annotations for inclusion in the Japanese agent prompt."""
    if not files:
        return ""
    lines = ["## 対象・参照ファイル"]
    for rf in files:
        note = ""
        if using_worktree and rf.in_repo and not rf.is_tracked:
            note = f" （※未追跡ファイルのためworktree内に存在しません。原本参照先: `{rf.abs_path}`）"
        elif not rf.in_repo:
            note = " （※リポジトリ外の参照専用ファイル）"
        lines.append(f"- `{rf.display_path}`{note}")
    lines.append("\n上記ファイルを優先的に確認したうえで対応してください。")
    return "\n".join(lines)


def load_task_text(task_arg: str | None, task_file: str | None) -> str:
    """Combine positional task text with `--task-file` (or stdin when `'-'`)."""
    parts: list[str] = []
    if task_file:
        if task_file == "-":
            parts.append(sys.stdin.read().strip())
        else:
            tf = Path(task_file).expanduser()
            if not tf.is_file():
                print(f"[error] Task file not found: {task_file}", file=sys.stderr)
                sys.exit(1)
            parts.append(tf.read_text(encoding="utf-8").strip())
    if task_arg:
        parts.append(task_arg.strip())

    combined = "\n\n".join(p for p in parts if p)
    if not combined:
        print("[error] Please provide a task description argument or --task-file.", file=sys.stderr)
        sys.exit(1)
    return combined
