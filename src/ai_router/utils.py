"""Shared utility functions for subprocess execution, git metadata, and parsing."""

from __future__ import annotations

import datetime
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from ai_router.config import MODEL_PRESETS, SUPPORTED_TOOLS


def now_iso() -> str:
    """Return the current local timestamp formatted as an ISO 8601 string."""
    return datetime.datetime.now(datetime.timezone.utc).astimezone().isoformat(timespec="seconds")


def run_cmd(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    check: bool = True,
    capture: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Execute a subprocess command and return the CompletedProcess result."""
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        check=check,
        text=True,
        capture_output=capture,
    )


def get_git_root(cwd: Path | None = None) -> Path | None:
    """Return the top-level directory of the current git repository, or None."""
    res = run_cmd(["git", "rev-parse", "--show-toplevel"], cwd=cwd, check=False)
    if res.returncode != 0:
        return None
    return Path(res.stdout.strip())


def get_git_dir(cwd: Path) -> Path | None:
    """Return the resolved `.git` directory (supports git worktrees), or None."""
    res = run_cmd(["git", "rev-parse", "--git-dir"], cwd=cwd, check=False)
    if res.returncode != 0:
        return None
    raw = Path(res.stdout.strip())
    return raw if raw.is_absolute() else (cwd / raw).resolve()


def get_repo_and_branch(cwd: Path) -> tuple[str, str]:
    """Return `(repo_name, branch_name)` for the given directory."""
    root = get_git_root(cwd)
    repo_name = root.name if root else cwd.name
    branch = run_cmd(["git", "branch", "--show-current"], cwd=cwd, check=False).stdout.strip()
    return repo_name, branch or "(detached)"


def get_git_context(repo_root: Path) -> str:
    """Build a short summary of the current branch and modified files for the router."""
    branch = run_cmd(["git", "branch", "--show-current"], cwd=repo_root, check=False).stdout.strip()
    status = run_cmd(["git", "status", "--short"], cwd=repo_root, check=False).stdout.strip()
    status_lines = "\n".join(status.splitlines()[:25])
    return f"Git branch: {branch}\nChanged files:\n{status_lines or '(clean)'}"


def sanitize_slug(slug: str) -> str:
    """Normalize an arbitrary string into a safe git branch / tmux slug."""
    cleaned = re.sub(r"[^a-z0-9-]+", "-", slug.lower()).strip("-")
    cleaned = re.sub(r"-{2,}", "-", cleaned)
    return cleaned[:32] or "ai-task"


def extract_first_json_object(raw_text: str) -> dict[str, Any] | None:
    """Extract the first valid JSON object even when surrounded by markdown or prose."""
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", raw_text):
        start = match.start()
        try:
            obj, _ = decoder.raw_decode(raw_text[start:])
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            continue
    return None


def parse_tool_model_spec(raw_spec: str) -> tuple[str, str]:
    """Parse a single `'tool:model'` specification string and validate the tool name."""
    if ":" not in raw_spec:
        print(f"[error] Model spec must use 'tool:model' format: {raw_spec}", file=sys.stderr)
        sys.exit(1)
    tool, model = raw_spec.split(":", 1)
    tool = tool.strip()
    model = model.strip()
    if tool not in SUPPORTED_TOOLS:
        print(
            f"[error] Unsupported tool '{tool}' (supported: {', '.join(SUPPORTED_TOOLS)})",
            file=sys.stderr,
        )
        sys.exit(1)
    if not model:
        print(f"[error] Model name cannot be empty in spec: {raw_spec}", file=sys.stderr)
        sys.exit(1)
    return tool, model


def parse_model_specs(models_arg: str | None, preset_arg: str) -> list[tuple[str, str]]:
    """Return a list of `(tool, model)` pairs from `--models` or a named `--preset`."""
    if not models_arg:
        return list(MODEL_PRESETS[preset_arg])

    specs: list[tuple[str, str]] = []
    for raw in models_arg.split(","):
        item = raw.strip()
        if not item:
            continue
        specs.append(parse_tool_model_spec(item))

    if not specs:
        print("[error] No valid models specified.", file=sys.stderr)
        sys.exit(1)
    if len(specs) < 2:
        print("[warn] Only one model specified; using multiple models is recommended for comparison.", file=sys.stderr)
    return specs
