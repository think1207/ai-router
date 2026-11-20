"""Configuration defaults, model candidates, and presets for ai-router.

Adjust model IDs or override runtime behavior via environment variables as needed.
Note: Prompt descriptions and review role templates intentionally remain in Japanese
for Japanese agent interactions.
"""

from __future__ import annotations

import os
from pathlib import Path

SUPPORTED_TOOLS: tuple[str, ...] = ("opencode", "agy", "claude")

# Runtime settings configurable via environment variables
ROUTER_MODEL: str = os.environ.get("AI_ROUTE_ROUTER_MODEL", "opencode/deepseek-v4.1-flash")
OPENCODE_ATTACH_URL: str = os.environ.get("OPENCODE_ATTACH_URL", "")

# Legacy tmux paste-buffer fallback for agy interactive mode (opt-in)
AGY_PASTE_FALLBACK: bool = os.environ.get("AI_ROUTE_AGY_PASTE_FALLBACK", "").lower() in {
    "1",
    "true",
    "yes",
}
AGY_PASTE_DELAY_SEC: float = float(os.environ.get("AI_ROUTE_AGY_DELAY", "2.0"))

# Untracked files automatically copied into newly created git worktrees when present
WORKTREE_COPY_FILES: tuple[str, ...] = (".env", ".env.local")

# Preview limits passed to the lightweight router model to prevent latency/token bloat
ROUTER_FILE_MAX_LINES: int = 60
ROUTER_TOTAL_PREVIEW_CHARS: int = 12000


def get_log_file() -> Path:
    """Return the path to the JSONL history log file."""
    override = os.environ.get("AI_ROUTE_LOG_FILE")
    if override:
        return Path(override).expanduser()
    xdg_data_home = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return xdg_data_home / "ai-route" / "history.jsonl"


# Whitelisted (tool, model) candidates available for automatic routing
CANDIDATES: list[dict[str, str]] = [
    {
        "tool": "opencode",
        "model": "opencode/deepseek-v4.1-flash",
        "difficulty": "low",
        "description": "軽微な修正、ボイラープレート生成、単一ファイルのテスト追加、ドキュメント修正",
    },
    {
        "tool": "agy",
        "model": "gemini-3.8-flash-medium",
        "difficulty": "medium",
        "description": "中規模な実装、複数ファイルにまたがる調査・リファクタリング、解決方針の比較検討",
    },
    {
        "tool": "claude",
        "model": "sonnet",
        "difficulty": "medium",
        "description": "複雑なロジック実装、精密なコード修正、デバッグ",
    },
    {
        "tool": "agy",
        "model": "gemini-4-neon-high",
        "difficulty": "high",
        "description": "高難度の設計・大規模リファクタリング、複数モジュール横断の複雑な問題解決",
    },
    {
        "tool": "claude",
        "model": "opus",
        "difficulty": "high",
        "description": "高難度のアーキテクチャ設計、難解なバグ解析、広範囲の破壊的変更",
    },
]

# Model combination presets for `review` and `multi` subcommands
MODEL_PRESETS: dict[str, list[tuple[str, str]]] = {
    "fast": [
        ("opencode", "opencode/deepseek-v4.1-flash"),
        ("agy", "gemini-3.8-flash-medium"),
    ],
    "balanced": [
        ("agy", "gemini-3.8-flash-medium"),
        ("claude", "sonnet"),
    ],
    "strong": [
        ("claude", "opus"),
        ("agy", "gemini-4-neon-high"),
        ("claude", "sonnet"),
    ],
    "all": [(c["tool"], c["model"]) for c in CANDIDATES],
}

# Perspective assignments when `review --roles` is enabled (kept in Japanese for prompts)
REVIEW_ROLES: tuple[str, ...] = (
    "バグ・エッジケース・競合状態・エラーハンドリングの厳密な検出",
    "アーキテクチャ設計・責務分離・保守性・パフォーマンスへの影響",
    "セキュリティ・境界値・テスト網羅性・可読性",
)

FALLBACK_DECISION: dict[str, str] = {
    "difficulty": "medium",
    "tool": "claude",
    "model": "sonnet",
    "branch_slug": "ai-task",
    "reason": "Fallback due to router output parse error or disallowed model selection",
}
