"""Configuration defaults, model candidates, and presets for ai-router.

Supports external configuration via TOML files (e.g. ~/.config/ai-route/config.toml,
overridable by $AI_ROUTE_CONFIG or --config CLI option).
Configuration priority: CLI arguments > Environment variables > User config file > Builtin defaults.
"""

from __future__ import annotations

import copy
import dataclasses
import os
import re
import sys
import tomllib
from pathlib import Path
from typing import Any

from ai_router.errors import ConfigError
from ai_router.security import (
    compile_safe_regex,
    validate_copilot_permission_flag,
    validate_safe_identifier,
)

# ---------------------------------------------------------------------------
# Builtin Defaults
# ---------------------------------------------------------------------------

DEFAULT_SUPPORTED_TOOLS: tuple[str, ...] = ("opencode", "agy", "claude", "copilot", "codex")

DEFAULT_CANDIDATES: list[dict[str, str]] = [
    {
        "tool": "opencode",
        "model": "opencode-go/deepseek-v4.1-flash",
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
        "tool": "copilot",
        "model": "gpt-5.6-sol",
        "difficulty": "medium",
        "description": "中規模な実装、コード修正、GitHub連携開発（GitHub Copilot CLI）",
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
    {
        "tool": "codex",
        "model": "gpt-6.1-sol",
        "difficulty": "high",
        "description": "高難度の設計・大規模実装・自律的なデバッグ（OpenAI Codex CLI）",
    },
]

DEFAULT_MODEL_PRESETS: dict[str, list[tuple[str, str]]] = {
    "fast": [
        ("opencode", "opencode-go/deepseek-v4.1-flash"),
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
    "all": [(c["tool"], c["model"]) for c in DEFAULT_CANDIDATES],
}

DEFAULT_FALLBACK_DECISION: dict[str, str] = {
    "difficulty": "medium",
    "effort": "medium",
    "tool": "claude",
    "model": "sonnet",
    "branch_slug": "ai-task",
    "reason": "Fallback due to router output parse error or disallowed model selection",
}

VALID_EFFORTS: tuple[str, ...] = ("low", "medium", "high", "xhigh", "max")
VALID_ACP_MODES: tuple[str, ...] = ("default", "accept-edits", "auto", "plan")
DIFFICULTY_TO_DEFAULT_EFFORT: dict[str, str] = {
    "low": "low",
    "medium": "medium",
    "high": "high",
}

DEFAULT_ROUTER_TOOL: str = ""
DEFAULT_ROUTER_MODEL: str = "opencode-go/deepseek-v4.1-flash"
DEFAULT_OPENCODE_ATTACH_URL: str = ""
DEFAULT_AGY_PASTE_FALLBACK: bool = False
DEFAULT_AGY_PASTE_DELAY_SEC: float = 2.0
DEFAULT_AGY_ACP_MODE: str = "auto"
DEFAULT_COPILOT_PERMISSION_FLAG: str = "--allow-all-tools"
DEFAULT_AUTO_DETECT_TOOLS: bool = True

DEFAULT_ROUTER_TIMEOUT_SEC: float = 60.0
DEFAULT_REVIEW_TIMEOUT_SEC: float = 300.0
DEFAULT_TEST_CMD_TIMEOUT_SEC: float = 300.0
DEFAULT_AGENT_TIMEOUT_SEC: float = 1800.0
DEFAULT_CREDIT_COOLDOWN_SEC: float = 3600.0

ROUTER_TIMEOUT_ENV = "AI_ROUTE_ROUTER_TIMEOUT"
REVIEW_TIMEOUT_ENV = "AI_ROUTE_REVIEW_TIMEOUT"
TEST_CMD_TIMEOUT_ENV = "AI_ROUTE_TEST_CMD_TIMEOUT"
AGENT_TIMEOUT_ENV = "AI_ROUTE_AGENT_TIMEOUT"
CREDIT_COOLDOWN_ENV = "AI_ROUTE_CREDIT_COOLDOWN_SEC"
CREDIT_PATTERNS_ENV = "AI_ROUTE_CREDIT_PATTERNS"

PLAN_FIRST_INSTRUCTION: str = (
    "※まずは考えられる解決アプローチと実装順序の案を提示し、合意を得てから編集に着手してください。"
)

WORKTREE_COPY_FILES: tuple[str, ...] = (".env", ".env.local")
ROUTER_FILE_MAX_LINES: int = 60
ROUTER_TOTAL_PREVIEW_CHARS: int = 12000

REVIEW_ROLES: tuple[str, ...] = (
    "バグ・エッジケース・競合状態・エラーハンドリングの厳密な検出",
    "アーキテクチャ設計・責務分離・保守性・パフォーマンスへの影響",
    "セキュリティ・境界値・テスト網羅性・可読性",
)

# ---------------------------------------------------------------------------
# Two-stage (planner -> implementer) and escalation
# ---------------------------------------------------------------------------

STAGE_DEFAULTS: dict[str, str] = {
    "planner": "claude:opus",
    "implementer": "",
    "escalate": "claude:opus",
}

STAGE_PRESETS: dict[str, dict[str, str]] = {
    "claude": {"planner": "claude:opus", "implementer": "claude:sonnet"},
    "cheap": {"planner": "claude:sonnet", "implementer": "opencode:opencode-go/deepseek-v4.1-flash"},
    "gemini": {"planner": "agy:gemini-4-neon-high", "implementer": "agy:gemini-3.8-flash-medium"},
}

PLANNER_ENV = "AI_ROUTE_PLANNER"
IMPLEMENTER_ENV = "AI_ROUTE_IMPLEMENTER"


def _resolve_stage_spec(cli_value: str | None, env_name: str, preset: str | None, key: str) -> str:
    if cli_value:
        return cli_value.strip()
    env_val = os.environ.get(env_name, "").strip()
    if env_val:
        return env_val
    if preset:
        if preset not in STAGE_PRESETS:
            raise ValueError(f"Unknown plan preset: {preset}")
        return STAGE_PRESETS[preset][key]
    return STAGE_DEFAULTS[key]


def resolve_planner_spec(cli_value: str | None = None, preset: str | None = None) -> str:
    """Planner spec: CLI > $AI_ROUTE_PLANNER > preset > STAGE_DEFAULTS."""
    return _resolve_stage_spec(cli_value, PLANNER_ENV, preset, "planner")


def resolve_implementer_spec(cli_value: str | None = None, preset: str | None = None) -> str:
    """Implementer spec: CLI > $AI_ROUTE_IMPLEMENTER > preset > STAGE_DEFAULTS.

    May return a full 'tool:model', a bare tool name (pick the model by difficulty),
    or "" (pick tool and model by difficulty).
    """
    return _resolve_stage_spec(cli_value, IMPLEMENTER_ENV, preset, "implementer")


# ---------------------------------------------------------------------------
# Blocked (waiting-for-decision) detection
# ---------------------------------------------------------------------------

# Explicit marker agents are asked to print when they need a human decision
BLOCKED_MARKER: str = "[[AI_ROUTE_BLOCKED]]"

# Instruction appended to synchronous headless prompts (`run --wait`, two-stage, escalate
# implementers) so a stuck agent stops with a machine-readable marker instead of guessing.
HEADLESS_BLOCKED_INSTRUCTION: str = (
    "※人間の判断が必要で作業を進められない場合は、推測で進めずに作業を止め、"
    "出力の最後に次の形式で記載して終了してください。\n"
    f"{BLOCKED_MARKER}\n"
    "質問: <判断が必要な点>\n"
    "選択肢: <A / B / ...>\n"
    "推奨: <推奨案とその理由>"
)

# Question-like phrases searched in the tail of an agent's output (case-insensitive).
BLOCKED_PATTERNS: tuple[str, ...] = (
    # Japanese
    r"(して|進めて)?(も)?よろしいですか",
    r"よろしいでしょうか",
    r"(どちら|どれ|どの(案|方針|方法))(に|で|を)(しますか|しましょうか|進めますか|すべきでしょうか)",
    r"いかが(しますか|しましょうか|でしょうか)",
    r"ご(指示|判断|確認|選択)(を)?(ください|お願いします|いただけますか)",
    r"(選んで|教えて|決めて)ください",
    r"(進めて|続けて|実行して)(も)?(よい|いい)(です|でしょう)か",
    r"判断をお願いします",
    # English
    r"\b(should|shall) i\b",
    r"\b(do|would) you (want|like) me to\b",
    r"\bwhich (option|approach|one|way)\b.*\?",
    r"\bplease (confirm|choose|advise|decide|let me know)\b",
    r"\blet me know (which|if|whether|how)\b",
    r"\bwaiting for (your )?(input|confirmation|approval|decision)\b",
    r"\bhow would you like (me )?to\b",
)
BLOCKED_PATTERNS_ENV = "AI_ROUTE_BLOCKED_PATTERNS"  # extra regexes separated by ";;"

# Number of trailing non-empty output lines inspected for question patterns
BLOCKED_TAIL_LINES: int = 15
# Max characters of agent output kept in session metadata / handoff prompts
OUTPUT_SUMMARY_CHARS: int = 2000
# Max characters of `git diff` stored in session metadata
SESSION_DIFF_MAX_CHARS: int = 20000
# Max entries kept in session metadata `history`
SESSION_HISTORY_MAX: int = 50
# How many times `--auto-escalate` may escalate and re-run the implementer per command
AUTO_ESCALATE_MAX_ROUNDS: int = 1


def get_blocked_patterns() -> list[re.Pattern[str]]:
    """Compile BLOCKED_PATTERNS plus extra patterns from $AI_ROUTE_BLOCKED_PATTERNS.

    Extra patterns are separated by ``;;``. Invalid or unsafe (ReDoS) regexes are skipped with a warning.
    Evaluated at call time so the environment can change between calls.
    """
    raw_patterns = list(BLOCKED_PATTERNS)
    extra = os.environ.get(BLOCKED_PATTERNS_ENV, "")
    raw_patterns.extend(p.strip() for p in extra.split(";;") if p.strip())
    compiled: list[re.Pattern[str]] = []
    for pat in raw_patterns:
        regex = compile_safe_regex(pat, label="blocked")
        if regex is not None:
            compiled.append(regex)
    return compiled


# ---------------------------------------------------------------------------
# Credit / quota exhaustion detection patterns
# ---------------------------------------------------------------------------

# High-confidence patterns checked even when exit code is unknown (e.g. tmux log)
STRONG_CREDIT_PATTERNS: tuple[str, ...] = (
    r"credit balance is too low",
    r"out of credits?",
    r"insufficient[_ ]credits?",
    r"no credits? remaining",
    r"credits? exhausted",
    r"insufficient[_ ]quota",
    r"exceeded your current quota",
    r"usage limit reached",
    r"hit your (usage |plan |rate )?limit",
    r"spending limit (reached|exceeded)",
    r"billing (hard )?limit",
    r"クレジット.{0,6}(不足|切れ|上限|残高)",
    r"クォータ.{0,6}(超過|上限)",
    r"利用上限に達",
)

# Broader patterns checked when the CLI exited with a non-zero return code
DEFAULT_CREDIT_PATTERNS: tuple[str, ...] = (
    *STRONG_CREDIT_PATTERNS,
    r"quota[_ ]exceeded",
    r"resource[_ ]exhausted",
    r"rate[_ ]limit(?:ed| exceeded)?",
    r"too many requests",
    r"\bhttp\s*429\b|\bstatus[: ]+429\b|\berror\s*429\b",
    r"monthly limit (reached|exceeded)",
    r"plan limit (reached|exceeded)",
    r"account.{0,20}billing",
    r"overloaded_error",
)


def _compile_patterns(raw_patterns: list[str], label: str) -> list[re.Pattern[str]]:
    compiled: list[re.Pattern[str]] = []
    for pat in raw_patterns:
        regex = compile_safe_regex(pat, label=label)
        if regex is not None:
            compiled.append(regex)
    return compiled


def get_strong_credit_patterns() -> list[re.Pattern[str]]:
    """Return high-confidence regex patterns for credit exhaustion."""
    return _compile_patterns(list(STRONG_CREDIT_PATTERNS), "credit")


def get_credit_patterns() -> list[re.Pattern[str]]:
    """Return credit/quota exhaustion regex patterns plus $AI_ROUTE_CREDIT_PATTERNS."""
    raw = list(DEFAULT_CREDIT_PATTERNS)
    extra = os.environ.get(CREDIT_PATTERNS_ENV, "")
    raw.extend(p.strip() for p in extra.split(";;") if p.strip())
    return _compile_patterns(raw, "credit")


# ---------------------------------------------------------------------------
# AppConfig & DefaultsConfig Dataclasses
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class DefaultsConfig:
    """User-configurable CLI flag defaults (`[defaults]` section in config.toml)."""

    worktree: bool = False
    mode: str = "interactive"
    auto: bool = False
    split: bool = False
    plan_first: bool = True
    preset: str = ""
    effort: str = ""
    acp_mode: str = DEFAULT_AGY_ACP_MODE


@dataclasses.dataclass
class AppConfig:
    supported_tools: tuple[str, ...] = DEFAULT_SUPPORTED_TOOLS
    candidates: list[dict[str, str]] = dataclasses.field(
        default_factory=lambda: [copy.deepcopy(c) for c in DEFAULT_CANDIDATES]
    )
    presets: dict[str, list[tuple[str, str]]] = dataclasses.field(
        default_factory=lambda: {k: list(v) for k, v in DEFAULT_MODEL_PRESETS.items()}
    )
    fallback_decision: dict[str, str] = dataclasses.field(
        default_factory=lambda: copy.deepcopy(DEFAULT_FALLBACK_DECISION)
    )
    allowed_pairs: set[tuple[str, str]] = dataclasses.field(
        default_factory=lambda: {(c["tool"], c["model"]) for c in DEFAULT_CANDIDATES}
    )
    router_tool: str = DEFAULT_ROUTER_TOOL
    router_fallback_tools: tuple[str, ...] = ()
    router_model: str = DEFAULT_ROUTER_MODEL
    opencode_attach_url: str = DEFAULT_OPENCODE_ATTACH_URL
    agy_paste_fallback: bool = DEFAULT_AGY_PASTE_FALLBACK
    agy_paste_delay_sec: float = DEFAULT_AGY_PASTE_DELAY_SEC
    agy_acp_mode: str = DEFAULT_AGY_ACP_MODE
    copilot_permission_flag: str = DEFAULT_COPILOT_PERMISSION_FLAG
    auto_detect_tools: bool = DEFAULT_AUTO_DETECT_TOOLS
    router_timeout_sec: float = DEFAULT_ROUTER_TIMEOUT_SEC
    review_timeout_sec: float = DEFAULT_REVIEW_TIMEOUT_SEC
    test_cmd_timeout_sec: float = DEFAULT_TEST_CMD_TIMEOUT_SEC
    agent_timeout_sec: float = DEFAULT_AGENT_TIMEOUT_SEC
    credit_cooldown_sec: float = DEFAULT_CREDIT_COOLDOWN_SEC
    defaults: DefaultsConfig = dataclasses.field(default_factory=DefaultsConfig)

    @property
    def available_tools(self) -> tuple[str, ...]:
        """Return the tuple of tools that have at least one allowed (tool, model) pair."""
        tools_in_pairs = {tool for tool, _ in self.allowed_pairs}
        return tuple(t for t in self.supported_tools if t in tools_in_pairs) or self.supported_tools


# ---------------------------------------------------------------------------
# Validation & Parsing Helpers
# ---------------------------------------------------------------------------

VALID_DIFFICULTIES = {"low", "medium", "high"}


def _parse_pair_spec(item: Any, supported_tools: tuple[str, ...], context: str) -> tuple[str, str]:
    """Parse 'tool:model' string or [tool, model] list into a (tool, model) tuple."""
    if isinstance(item, str):
        if ":" not in item:
            raise ConfigError(f"Invalid model spec in {context}: expected 'tool:model' string, got {item!r}")
        tool, model = item.split(":", 1)
        tool, model = tool.strip(), model.strip()
    elif isinstance(item, (list, tuple)) and len(item) == 2:
        tool, model = str(item[0]).strip(), str(item[1]).strip()
    elif isinstance(item, dict) and "tool" in item and "model" in item:
        tool, model = str(item["tool"]).strip(), str(item["model"]).strip()
    else:
        raise ConfigError(
            f"Invalid model spec in {context}: expected 'tool:model' or [tool, model], got {item!r}"
        )

    if not tool or tool not in supported_tools:
        raise ConfigError(
            f"Unsupported tool {tool!r} in {context} (supported: {', '.join(supported_tools)})"
        )
    if not model:
        raise ConfigError(f"Empty model name in {context}")

    return tool, model


def validate_config(cfg: AppConfig) -> None:
    """Validate that the configuration values are internally consistent and non-empty."""
    if not cfg.supported_tools:
        raise ConfigError("supported_tools cannot be empty")
    for t in cfg.supported_tools:
        if not t or not isinstance(t, str):
            raise ConfigError(f"Invalid tool in supported_tools: {t!r}")
        try:
            validate_safe_identifier(t, "tool name")
        except ValueError as e:
            raise ConfigError(str(e)) from e

    try:
        validate_copilot_permission_flag(cfg.copilot_permission_flag)
    except ValueError as e:
        raise ConfigError(str(e)) from e

    if not cfg.candidates:
        raise ConfigError("No candidates configured; at least one candidate model is required")

    seen_pairs: set[tuple[str, str]] = set()
    for idx, c in enumerate(cfg.candidates):
        if not isinstance(c, dict):
            raise ConfigError(f"Candidate #{idx + 1} must be a table/dictionary, got {type(c).__name__}")
        tool = str(c.get("tool", "")).strip()
        model = str(c.get("model", "")).strip()
        diff = str(c.get("difficulty", "")).strip().lower()
        desc = str(c.get("description", ""))

        if not tool or tool not in cfg.supported_tools:
            raise ConfigError(
                f"Candidate #{idx + 1} has unsupported tool {tool!r} (supported: {', '.join(cfg.supported_tools)})"
            )
        if not model:
            raise ConfigError(f"Candidate #{idx + 1} has an empty model")
        if diff not in VALID_DIFFICULTIES:
            raise ConfigError(
                f"Candidate #{idx + 1} ({tool}:{model}) has invalid difficulty {diff!r} "
                f"(must be one of: {', '.join(sorted(VALID_DIFFICULTIES))})"
            )
        cand_effort = str(c.get("effort", "")).strip().lower()
        if cand_effort and cand_effort not in VALID_EFFORTS:
            raise ConfigError(
                f"Candidate #{idx + 1} ({tool}:{model}) has invalid effort {cand_effort!r} "
                f"(must be one of: {', '.join(VALID_EFFORTS)})"
            )
        pair = (tool, model)
        if pair in seen_pairs:
            raise ConfigError(f"Duplicate candidate model: {tool}:{model}")
        seen_pairs.add(pair)

    if not cfg.allowed_pairs:
        raise ConfigError("No allowed (tool, model) pairs configured or available")

    # Fallback decision check
    fb_tool = str(cfg.fallback_decision.get("tool", "")).strip()
    fb_model = str(cfg.fallback_decision.get("model", "")).strip()
    fb_diff = str(cfg.fallback_decision.get("difficulty", "medium")).strip().lower()
    fb_effort = str(cfg.fallback_decision.get("effort", "")).strip().lower()

    if not fb_tool or fb_tool not in cfg.supported_tools:
        raise ConfigError(
            f"FALLBACK_DECISION has unsupported tool {fb_tool!r} (supported: {', '.join(cfg.supported_tools)})"
        )
    if not fb_model:
        raise ConfigError("FALLBACK_DECISION has an empty model")
    if fb_diff not in VALID_DIFFICULTIES:
        raise ConfigError(f"FALLBACK_DECISION has invalid difficulty {fb_diff!r}")
    if fb_effort and fb_effort not in VALID_EFFORTS:
        raise ConfigError(
            f"FALLBACK_DECISION has invalid effort {fb_effort!r} "
            f"(must be one of: {', '.join(VALID_EFFORTS)})"
        )
    if (fb_tool, fb_model) not in cfg.allowed_pairs:
        raise ConfigError(
            f"FALLBACK_DECISION ({fb_tool}:{fb_model}) is not among the allowed candidates: "
            f"{', '.join(f'{t}:{m}' for t, m in sorted(cfg.allowed_pairs))}"
        )

    # Presets check
    for pname, pairs in cfg.presets.items():
        try:
            validate_safe_identifier(pname, "preset name")
        except ValueError as e:
            raise ConfigError(str(e)) from e
        if not pairs:
            raise ConfigError(f"Preset {pname!r} has no models")
        for tool, model in pairs:
            if tool not in cfg.supported_tools:
                raise ConfigError(
                    f"Preset {pname!r} references unsupported tool {tool!r} (supported: {', '.join(cfg.supported_tools)})"
                )
            if (tool, model) not in cfg.allowed_pairs:
                raise ConfigError(
                    f"Preset {pname!r} references model {tool}:{model} which is not among allowed candidates"
                )


# ---------------------------------------------------------------------------
# Loading & Merging
# ---------------------------------------------------------------------------

def get_default_config_path() -> Path:
    """Return the default configuration file path (~/.config/ai-route/config.toml)."""
    xdg_config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return xdg_config_home / "ai-route" / "config.toml"


def resolve_config_path(cli_path: str | Path | None = None) -> Path | None:
    """Resolve the effective configuration file path.

    Returns the path if specified or present; None if the default file does not exist.
    """
    if cli_path is not None:
        p = Path(cli_path).expanduser()
        if not p.is_file():
            raise ConfigError(f"Specified configuration file not found: {p}")
        return p

    env_path = os.environ.get("AI_ROUTE_CONFIG")
    if env_path:
        p = Path(env_path).expanduser()
        if not p.is_file():
            raise ConfigError(f"Configuration file specified in $AI_ROUTE_CONFIG not found: {p}")
        return p

    default_path = get_default_config_path()
    if default_path.is_file():
        return default_path
    return None


def load_config_from_dict(raw: dict[str, Any]) -> AppConfig:
    """Construct and validate an AppConfig from a raw parsed dictionary (TOML)."""
    # 1. supported_tools
    raw_tools = raw.get("supported_tools")
    if raw_tools is not None:
        if not isinstance(raw_tools, (list, tuple)):
            raise ConfigError(f"'supported_tools' must be a list of strings, got {type(raw_tools).__name__}")
        supported_tools = tuple(str(t).strip() for t in raw_tools)
    else:
        supported_tools = DEFAULT_SUPPORTED_TOOLS

    # 2. Base candidates: either reset to empty or start with builtin defaults
    reset_candidates = bool(raw.get("reset_candidates", False))
    if reset_candidates:
        candidates_map: dict[tuple[str, str], dict[str, str]] = {}
    else:
        candidates_map = {
            (c["tool"], c["model"]): copy.deepcopy(c)
            for c in DEFAULT_CANDIDATES
            if c["tool"] in supported_tools
        }

    # Add or override candidates
    raw_candidates = raw.get("candidates")
    if raw_candidates is not None:
        if not isinstance(raw_candidates, list):
            raise ConfigError(f"'candidates' must be a list of tables, got {type(raw_candidates).__name__}")
        seen_in_config: set[tuple[str, str]] = set()
        for idx, item in enumerate(raw_candidates):
            if not isinstance(item, dict):
                raise ConfigError(f"Candidate #{idx + 1} must be a table, got {type(item).__name__}")
            tool = str(item.get("tool", "")).strip()
            model = str(item.get("model", "")).strip()
            diff = str(item.get("difficulty", "")).strip().lower()
            desc = str(item.get("description", ""))

            if not tool or tool not in supported_tools:
                raise ConfigError(
                    f"Candidate #{idx + 1} has unsupported tool {tool!r} (supported: {', '.join(supported_tools)})"
                )
            if not model:
                raise ConfigError(f"Candidate #{idx + 1} has an empty model")
            if diff not in VALID_DIFFICULTIES:
                raise ConfigError(
                    f"Candidate #{idx + 1} ({tool}:{model}) has invalid difficulty {diff!r} "
                    f"(must be one of: {', '.join(sorted(VALID_DIFFICULTIES))})"
                )
            pair = (tool, model)
            if pair in seen_in_config:
                raise ConfigError(f"Duplicate candidate in configuration: {tool}:{model}")
            seen_in_config.add(pair)

            entry: dict[str, str] = {
                "tool": tool,
                "model": model,
                "difficulty": diff,
                "description": desc,
            }
            if "effort" in item and item["effort"] is not None:
                cand_effort = str(item["effort"]).strip().lower()
                if cand_effort not in VALID_EFFORTS:
                    raise ConfigError(
                        f"Candidate #{idx + 1} ({tool}:{model}) has invalid effort {cand_effort!r} "
                        f"(must be one of: {', '.join(VALID_EFFORTS)})"
                    )
                entry["effort"] = cand_effort
            candidates_map[pair] = entry

    # Remove candidates
    raw_removals = raw.get("remove_candidates")
    if raw_removals is not None:
        if not isinstance(raw_removals, list):
            raise ConfigError(f"'remove_candidates' must be a list, got {type(raw_removals).__name__}")
        for item in raw_removals:
            pair = _parse_pair_spec(item, supported_tools, "remove_candidates")
            candidates_map.pop(pair, None)

    # 3. Allowed models / pairs (subscription declarations)
    raw_allowed = raw.get("allowed_models")
    if raw_allowed is not None:
        if not isinstance(raw_allowed, list):
            raise ConfigError(f"'allowed_models' must be a list, got {type(raw_allowed).__name__}")
        allowed_pairs_set = {
            _parse_pair_spec(item, supported_tools, "allowed_models")
            for item in raw_allowed
        }
        candidates_map = {
            pair: c for pair, c in candidates_map.items() if pair in allowed_pairs_set
        }
    else:
        allowed_pairs_set = set(candidates_map.keys())

    candidates = list(candidates_map.values())
    if not candidates:
        raise ConfigError(
            "No candidate models available after applying configuration and allowed_models filters"
        )

    # 4. Fallback decision
    raw_fallback = raw.get("fallback")
    fallback_decision = copy.deepcopy(DEFAULT_FALLBACK_DECISION)
    if raw_fallback is not None:
        if not isinstance(raw_fallback, dict):
            raise ConfigError(f"'fallback' must be a table, got {type(raw_fallback).__name__}")
        if "tool" in raw_fallback:
            fallback_decision["tool"] = str(raw_fallback["tool"]).strip()
        if "model" in raw_fallback:
            fallback_decision["model"] = str(raw_fallback["model"]).strip()
        if "difficulty" in raw_fallback:
            fallback_decision["difficulty"] = str(raw_fallback["difficulty"]).strip().lower()
        if "effort" in raw_fallback:
            fallback_decision["effort"] = str(raw_fallback["effort"]).strip().lower()
        if "branch_slug" in raw_fallback:
            fallback_decision["branch_slug"] = str(raw_fallback["branch_slug"]).strip()
        if "reason" in raw_fallback:
            fallback_decision["reason"] = str(raw_fallback["reason"]).strip()

    if (fallback_decision["tool"], fallback_decision["model"]) not in allowed_pairs_set:
        if raw_fallback is None:
            first_c = candidates[0]
            fallback_decision["tool"] = first_c["tool"]
            fallback_decision["model"] = first_c["model"]
            fallback_decision["difficulty"] = first_c["difficulty"]
            fallback_decision["effort"] = first_c.get(
                "effort", DIFFICULTY_TO_DEFAULT_EFFORT.get(first_c["difficulty"], "medium")
            )

    # 5. Presets
    presets: dict[str, list[tuple[str, str]]] = {}
    for pname, pairs in DEFAULT_MODEL_PRESETS.items():
        if pname == "all":
            continue
        filtered_pairs = [p for p in pairs if p in allowed_pairs_set]
        if filtered_pairs:
            presets[pname] = filtered_pairs

    raw_presets = raw.get("presets")
    if raw_presets is not None:
        if not isinstance(raw_presets, dict):
            raise ConfigError(f"'presets' must be a table, got {type(raw_presets).__name__}")
        for pname, items in raw_presets.items():
            if not isinstance(items, list):
                raise ConfigError(f"Preset {pname!r} must be a list of model specs")
            parsed_preset = [
                _parse_pair_spec(item, supported_tools, f"preset '{pname}'")
                for item in items
            ]
            presets[pname] = parsed_preset

    if "all" not in presets:
        presets["all"] = [(c["tool"], c["model"]) for c in candidates]

    # 6. Runtime settings
    raw_runtime = raw.get("runtime", {})
    if not isinstance(raw_runtime, dict):
        raise ConfigError(f"'runtime' must be a table, got {type(raw_runtime).__name__}")

    router_tool = str(
        raw.get("router_tool")
        or raw_runtime.get("router_tool")
        or DEFAULT_ROUTER_TOOL
    ).strip()
    if router_tool and router_tool not in supported_tools:
        raise ConfigError(
            f"Unsupported router_tool {router_tool!r} (supported: {', '.join(supported_tools)})"
        )

    raw_router_fallbacks = raw.get("router_fallback_tools", raw_runtime.get("router_fallback_tools"))
    if raw_router_fallbacks is not None:
        if not isinstance(raw_router_fallbacks, (list, tuple)):
            raise ConfigError("'router_fallback_tools' must be a list of tool names")
        router_fallback_tools = tuple(str(t).strip() for t in raw_router_fallbacks if str(t).strip())
        for t in router_fallback_tools:
            if t not in supported_tools:
                raise ConfigError(
                    f"Unsupported tool {t!r} in router_fallback_tools (supported: {', '.join(supported_tools)})"
                )
    else:
        router_fallback_tools = ()

    router_model = str(
        raw.get("router_model")
        or raw_runtime.get("router_model")
        or DEFAULT_ROUTER_MODEL
    ).strip()

    opencode_attach_url = str(
        raw.get("opencode_attach_url")
        or raw_runtime.get("opencode_attach_url")
        or DEFAULT_OPENCODE_ATTACH_URL
    ).strip()

    agy_paste_fallback = bool(
        raw.get("agy_paste_fallback", raw_runtime.get("agy_paste_fallback", DEFAULT_AGY_PASTE_FALLBACK))
    )

    agy_delay = float(
        raw.get("agy_delay", raw_runtime.get("agy_delay", DEFAULT_AGY_PASTE_DELAY_SEC))
    )

    agy_acp_mode = str(
        raw.get("agy_acp_mode")
        or raw_runtime.get("agy_acp_mode")
        or DEFAULT_AGY_ACP_MODE
    ).strip().lower()
    if agy_acp_mode not in VALID_ACP_MODES:
        raise ConfigError(
            f"Invalid agy_acp_mode {agy_acp_mode!r} (must be one of: {', '.join(VALID_ACP_MODES)})"
        )

    copilot_permission_flag = str(
        raw.get(
            "copilot_permission_flag",
            raw_runtime.get("copilot_permission_flag", DEFAULT_COPILOT_PERMISSION_FLAG),
        )
    ).strip()

    auto_detect_tools = bool(
        raw.get("auto_detect_tools", raw_runtime.get("auto_detect_tools", DEFAULT_AUTO_DETECT_TOOLS))
    )

    router_timeout = float(
        raw.get("router_timeout", raw_runtime.get("router_timeout", DEFAULT_ROUTER_TIMEOUT_SEC))
    )
    review_timeout = float(
        raw.get("review_timeout", raw_runtime.get("review_timeout", DEFAULT_REVIEW_TIMEOUT_SEC))
    )
    test_cmd_timeout = float(
        raw.get("test_cmd_timeout", raw_runtime.get("test_cmd_timeout", DEFAULT_TEST_CMD_TIMEOUT_SEC))
    )
    agent_timeout = float(
        raw.get("agent_timeout", raw_runtime.get("agent_timeout", DEFAULT_AGENT_TIMEOUT_SEC))
    )
    credit_cooldown = float(
        raw.get("credit_cooldown_sec", raw_runtime.get("credit_cooldown_sec", DEFAULT_CREDIT_COOLDOWN_SEC))
    )

    # 7. CLI defaults (`[defaults]` section)
    raw_defaults = raw.get("defaults", {})
    if not isinstance(raw_defaults, dict):
        raise ConfigError(f"'defaults' must be a table, got {type(raw_defaults).__name__}")
    def_mode = str(raw_defaults.get("mode", "interactive")).strip().lower()
    if def_mode not in {"interactive", "headless"}:
        raise ConfigError(f"Invalid defaults.mode {def_mode!r} (must be 'interactive' or 'headless')")
    def_effort = str(raw_defaults.get("effort", "")).strip().lower()
    if def_effort and def_effort not in VALID_EFFORTS:
        raise ConfigError(
            f"Invalid defaults.effort {def_effort!r} (must be one of: {', '.join(VALID_EFFORTS)})"
        )
    def_acp_mode = str(raw_defaults.get("acp_mode", agy_acp_mode)).strip().lower()
    if def_acp_mode not in VALID_ACP_MODES:
        raise ConfigError(
            f"Invalid defaults.acp_mode {def_acp_mode!r} (must be one of: {', '.join(VALID_ACP_MODES)})"
        )
    def_preset = str(raw_defaults.get("preset", "")).strip()
    if def_preset and def_preset not in presets:
        raise ConfigError(
            f"Invalid defaults.preset {def_preset!r} (available: {', '.join(sorted(presets.keys()))})"
        )

    defaults_cfg = DefaultsConfig(
        worktree=bool(raw_defaults.get("worktree", False)),
        mode=def_mode,
        auto=bool(raw_defaults.get("auto", False)),
        split=bool(raw_defaults.get("split", False)),
        plan_first=bool(raw_defaults.get("plan_first", True)),
        preset=def_preset,
        effort=def_effort,
        acp_mode=def_acp_mode,
    )

    cfg = AppConfig(
        supported_tools=supported_tools,
        candidates=candidates,
        presets=presets,
        fallback_decision=fallback_decision,
        allowed_pairs=allowed_pairs_set,
        router_tool=router_tool,
        router_fallback_tools=router_fallback_tools,
        router_model=router_model,
        opencode_attach_url=opencode_attach_url,
        agy_paste_fallback=agy_paste_fallback,
        agy_paste_delay_sec=agy_delay,
        agy_acp_mode=def_acp_mode,
        copilot_permission_flag=copilot_permission_flag,
        auto_detect_tools=auto_detect_tools,
        router_timeout_sec=router_timeout,
        review_timeout_sec=review_timeout,
        test_cmd_timeout_sec=test_cmd_timeout,
        agent_timeout_sec=agent_timeout,
        credit_cooldown_sec=credit_cooldown,
        defaults=defaults_cfg,
    )

    validate_config(cfg)
    return cfg


def load_config(path: str | Path | None = None) -> AppConfig:
    """Load, parse, merge, and validate configuration from file and environment variables.

    Priority: Environment variables > Configuration file > Builtin defaults.
    """
    resolved_path = resolve_config_path(path)
    if resolved_path is not None:
        try:
            with open(resolved_path, "rb") as f:
                data = tomllib.load(f)
        except tomllib.TOMLDecodeError as e:
            raise ConfigError(f"Invalid TOML in configuration file {resolved_path}: {e}") from e
        except OSError as e:
            raise ConfigError(f"Failed to read configuration file {resolved_path}: {e}") from e
    else:
        data = {}

    cfg = load_config_from_dict(data)

    # Apply environment variable overrides (Environment variables > config file)
    if "AI_ROUTE_ROUTER_TOOL" in os.environ:
        rt = os.environ["AI_ROUTE_ROUTER_TOOL"].strip()
        if rt and rt not in cfg.supported_tools:
            raise ConfigError(
                f"Unsupported AI_ROUTE_ROUTER_TOOL={rt!r} (supported: {', '.join(cfg.supported_tools)})"
            )
        cfg.router_tool = rt
    if "AI_ROUTE_ROUTER_MODEL" in os.environ:
        cfg.router_model = os.environ["AI_ROUTE_ROUTER_MODEL"]
    if "OPENCODE_ATTACH_URL" in os.environ:
        cfg.opencode_attach_url = os.environ["OPENCODE_ATTACH_URL"]
    if "AI_ROUTE_AGY_PASTE_FALLBACK" in os.environ:
        cfg.agy_paste_fallback = os.environ["AI_ROUTE_AGY_PASTE_FALLBACK"].lower() in {"1", "true", "yes"}
    if "AI_ROUTE_AGY_DELAY" in os.environ:
        try:
            cfg.agy_paste_delay_sec = float(os.environ["AI_ROUTE_AGY_DELAY"])
        except ValueError:
            pass
    if "AI_ROUTE_AGY_ACP_MODE" in os.environ:
        acp_m = os.environ["AI_ROUTE_AGY_ACP_MODE"].strip().lower()
        if acp_m in VALID_ACP_MODES:
            cfg.agy_acp_mode = acp_m
            cfg.defaults.acp_mode = acp_m
    if "AI_ROUTE_COPILOT_PERMISSION_FLAG" in os.environ:
        raw_perm = os.environ["AI_ROUTE_COPILOT_PERMISSION_FLAG"].strip()
        try:
            validate_copilot_permission_flag(raw_perm)
        except ValueError as e:
            raise ConfigError(f"Unsafe AI_ROUTE_COPILOT_PERMISSION_FLAG: {e}") from e
        cfg.copilot_permission_flag = raw_perm
    if "AI_ROUTE_AUTO_DETECT_TOOLS" in os.environ:
        cfg.auto_detect_tools = os.environ["AI_ROUTE_AUTO_DETECT_TOOLS"].strip().lower() not in {
            "0",
            "false",
            "no",
            "off",
        }

    return cfg


# ---------------------------------------------------------------------------
# Global / Active Configuration State & Backward Compatibility
# ---------------------------------------------------------------------------

_ACTIVE_CONFIG: AppConfig = load_config()


def get_active_config() -> AppConfig:
    """Return the currently active AppConfig instance."""
    return _ACTIVE_CONFIG


def set_active_config(cfg: AppConfig) -> None:
    """Set the active AppConfig and synchronize module-level convenience exports."""
    global _ACTIVE_CONFIG, SUPPORTED_TOOLS, CANDIDATES, MODEL_PRESETS, FALLBACK_DECISION
    global ROUTER_MODEL, OPENCODE_ATTACH_URL, AGY_PASTE_FALLBACK, AGY_PASTE_DELAY_SEC
    global ROUTER_TIMEOUT_SEC, REVIEW_TIMEOUT_SEC, TEST_CMD_TIMEOUT_SEC, AGENT_TIMEOUT_SEC

    _ACTIVE_CONFIG = cfg
    SUPPORTED_TOOLS = cfg.supported_tools
    CANDIDATES = cfg.candidates
    MODEL_PRESETS = cfg.presets
    FALLBACK_DECISION = cfg.fallback_decision
    ROUTER_MODEL = cfg.router_model
    OPENCODE_ATTACH_URL = cfg.opencode_attach_url
    AGY_PASTE_FALLBACK = cfg.agy_paste_fallback
    AGY_PASTE_DELAY_SEC = cfg.agy_paste_delay_sec
    ROUTER_TIMEOUT_SEC = cfg.router_timeout_sec
    REVIEW_TIMEOUT_SEC = cfg.review_timeout_sec
    TEST_CMD_TIMEOUT_SEC = cfg.test_cmd_timeout_sec
    AGENT_TIMEOUT_SEC = cfg.agent_timeout_sec


# Backward-compatible module-level variable exports
SUPPORTED_TOOLS: tuple[str, ...] = _ACTIVE_CONFIG.supported_tools
CANDIDATES: list[dict[str, str]] = _ACTIVE_CONFIG.candidates
MODEL_PRESETS: dict[str, list[tuple[str, str]]] = _ACTIVE_CONFIG.presets
FALLBACK_DECISION: dict[str, str] = _ACTIVE_CONFIG.fallback_decision
ROUTER_MODEL: str = _ACTIVE_CONFIG.router_model
OPENCODE_ATTACH_URL: str = _ACTIVE_CONFIG.opencode_attach_url
AGY_PASTE_FALLBACK: bool = _ACTIVE_CONFIG.agy_paste_fallback
AGY_PASTE_DELAY_SEC: float = _ACTIVE_CONFIG.agy_paste_delay_sec
ROUTER_TIMEOUT_SEC: float = _ACTIVE_CONFIG.router_timeout_sec
REVIEW_TIMEOUT_SEC: float = _ACTIVE_CONFIG.review_timeout_sec
TEST_CMD_TIMEOUT_SEC: float = _ACTIVE_CONFIG.test_cmd_timeout_sec
AGENT_TIMEOUT_SEC: float = _ACTIVE_CONFIG.agent_timeout_sec


# ---------------------------------------------------------------------------
# Timeout & Path Resolvers
# ---------------------------------------------------------------------------

def resolve_timeout(cli_value: float | None, env_name: str, default: float) -> float | None:
    """Resolve an effective timeout: CLI value > environment variable > config default.

    Returns ``None`` (no timeout) when the resolved value is <= 0.
    """
    if cli_value is not None:
        value = cli_value
    else:
        raw = os.environ.get(env_name, "").strip()
        value = default
        if raw:
            try:
                value = float(raw)
            except ValueError:
                print(
                    f"[warn] Ignoring invalid {env_name}={raw!r}; using default {default:g}s.",
                    file=sys.stderr,
                )
    return value if value > 0 else None


def get_router_timeout(cli_value: float | None = None) -> float | None:
    """Effective router timeout in seconds (None = unlimited)."""
    return resolve_timeout(cli_value, ROUTER_TIMEOUT_ENV, _ACTIVE_CONFIG.router_timeout_sec)


def get_review_timeout(cli_value: float | None = None) -> float | None:
    """Effective per-model review timeout in seconds (None = unlimited)."""
    return resolve_timeout(cli_value, REVIEW_TIMEOUT_ENV, _ACTIVE_CONFIG.review_timeout_sec)


def get_test_cmd_timeout(cli_value: float | None = None) -> float | None:
    """Effective `compare --test-cmd` timeout in seconds (None = unlimited)."""
    return resolve_timeout(cli_value, TEST_CMD_TIMEOUT_ENV, _ACTIVE_CONFIG.test_cmd_timeout_sec)


def get_agent_timeout(cli_value: float | None = None) -> float | None:
    """Effective timeout for synchronous agent runs (None = unlimited)."""
    return resolve_timeout(cli_value, AGENT_TIMEOUT_ENV, _ACTIVE_CONFIG.agent_timeout_sec)


def get_log_file() -> Path:
    """Return the path to the JSONL history log file."""
    override = os.environ.get("AI_ROUTE_LOG_FILE")
    if override:
        return Path(override).expanduser()
    xdg_data_home = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return xdg_data_home / "ai-route" / "history.jsonl"


def get_session_log_dir() -> Path:
    """Directory holding per-session agent output logs (tee / tmux pipe-pane)."""
    return get_log_file().parent / "sessions"


def get_provider_state_file() -> Path:
    """Return the path to the provider credit/cooldown state JSON file."""
    override = os.environ.get("AI_ROUTE_PROVIDER_STATE_FILE")
    if override:
        return Path(override).expanduser()
    return get_log_file().parent / "provider_state.json"


def get_credit_cooldown_sec() -> float:
    """Effective cooldown duration in seconds for a credit-exhausted service."""
    raw = os.environ.get(CREDIT_COOLDOWN_ENV, "").strip()
    if raw:
        try:
            val = float(raw)
            if val >= 0:
                return val
        except ValueError:
            print(
                f"[warn] Ignoring invalid {CREDIT_COOLDOWN_ENV}={raw!r}; "
                f"using default {_ACTIVE_CONFIG.credit_cooldown_sec:g}s.",
                file=sys.stderr,
            )
    return _ACTIVE_CONFIG.credit_cooldown_sec
