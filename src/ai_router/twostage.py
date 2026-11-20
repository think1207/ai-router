"""Separation of decision-making (planner / escalation) from implementation.

A planner produces a JSON plan (decisions, constraints, open questions, steps and a
suggested implementer difficulty) without editing files; the implementer then receives a
prompt with those decisions fixed. The same JSON schema is used for `escalate`.
"""

from __future__ import annotations

import json
from typing import Any

from ai_router.config import (
    CANDIDATES,
    FALLBACK_DECISION,
    OUTPUT_SUMMARY_CHARS,
    SUPPORTED_TOOLS,
    resolve_implementer_spec,
    resolve_planner_spec,
)
from ai_router.errors import AiRouteError
from ai_router.utils import extract_first_json_object, parse_tool_model_spec

DIFFICULTY_ORDER: tuple[str, ...] = ("low", "medium", "high")
PLAN_LIST_KEYS: tuple[str, ...] = ("decisions", "constraints", "open_questions", "implementation_steps")
DEFAULT_IMPLEMENTER_DIFFICULTY = "medium"

PLAN_JSON_SCHEMA = """{
  "decisions": ["<確定した設計・方針の判断>", "..."],
  "constraints": ["<実装時に守るべき制約>", "..."],
  "open_questions": ["<まだ判断できない点（なければ空配列）>"],
  "implementation_steps": ["<実装手順（順序付き）>", "..."],
  "implementer_difficulty": "low" | "medium" | "high"
}"""

_JUDGE_ONLY_RULES = (
    "- あなたの役割は判断と計画のみです。ファイルの作成・編集・削除やコマンドによる変更は一切行わないでください。\n"
    "- 必要ならコードを読んで調査して構いません。\n"
    "- 出力は次のスキーマに従う有効なJSONオブジェクトのみとし、前置きやMarkdownのコードブロックは含めないでください。\n"
    "- implementer_difficulty は、決定事項に従って実装する担当者に必要な難易度です（判断はあなたが済ませる前提）。"
)


def default_plan(raw_output: str = "") -> dict[str, Any]:
    """Safe fallback plan used when the planner output is missing or invalid."""
    plan: dict[str, Any] = {key: [] for key in PLAN_LIST_KEYS}
    plan["implementer_difficulty"] = DEFAULT_IMPLEMENTER_DIFFICULTY
    raw = (raw_output or "").strip()
    if raw:
        plan["raw_notes"] = raw[-OUTPUT_SUMMARY_CHARS * 2 :]
    return plan


def _as_str_list(value: Any) -> list[str] | None:
    if value is None:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, list):
        items = []
        for item in value:
            if isinstance(item, (dict, list)):
                return None
            text = str(item).strip()
            if text:
                items.append(text)
        return items
    return None


def validate_plan(obj: Any) -> dict[str, Any] | None:
    """Normalize a parsed plan object; return None when it is not a usable plan.

    A plan is usable when it is a dict whose list fields are strings/lists of scalars and
    at least one of `decisions` / `implementation_steps` is non-empty. An unknown
    `implementer_difficulty` is replaced with the default.
    """
    if not isinstance(obj, dict):
        return None
    plan: dict[str, Any] = {}
    for key in PLAN_LIST_KEYS:
        items = _as_str_list(obj.get(key))
        if items is None:
            return None
        plan[key] = items
    if not plan["decisions"] and not plan["implementation_steps"]:
        return None
    diff = str(obj.get("implementer_difficulty", "")).strip().lower()
    plan["implementer_difficulty"] = diff if diff in DIFFICULTY_ORDER else DEFAULT_IMPLEMENTER_DIFFICULTY
    return plan


def extract_plan(raw_output: str) -> tuple[dict[str, Any], bool]:
    """Extract and validate a plan from model output; returns `(plan, is_valid)`.

    Invalid or missing JSON yields `default_plan(raw_output)` (keeping the raw text as
    reference notes) and `is_valid=False`.
    """
    text = raw_output or ""
    plan = validate_plan(extract_first_json_object(text))
    if plan is None:
        # Prose-heavy outputs (e.g. review synthesis) may contain other JSON first;
        # fall back to the last JSON object that is a valid plan.
        for obj in reversed(_iter_json_objects(text)):
            plan = validate_plan(obj)
            if plan is not None:
                break
    if plan is None:
        return default_plan(raw_output), False
    return plan, True


def _iter_json_objects(text: str) -> list[dict[str, Any]]:
    decoder = json.JSONDecoder()
    found: list[dict[str, Any]] = []
    pos = 0
    while True:
        start = text.find("{", pos)
        if start == -1:
            return found
        try:
            obj, end = decoder.raw_decode(text, start)
        except json.JSONDecodeError:
            pos = start + 1
            continue
        if isinstance(obj, dict):
            found.append(obj)
        pos = end


def build_planner_prompt(task: str, files_block: str = "", repo_ctx: str = "") -> str:
    """Prompt for the planner stage (decisions only, no edits, JSON output)."""
    parts = [
        "あなたはソフトウェア開発タスクの設計判断を担当するシニアエンジニアです。",
        "以下のタスクについて、実装担当者が迷わず作業できるよう判断と計画を確定してください。\n",
        _JUDGE_ONLY_RULES,
        f"\n## 出力JSONスキーマ\n{PLAN_JSON_SCHEMA}\n",
    ]
    if repo_ctx:
        parts.append(f"## リポジトリ状況\n{repo_ctx}\n")
    if files_block:
        parts.append(f"{files_block}\n")
    parts.append(f"## タスク\n{task}\n")
    return "\n".join(parts)


def build_escalation_prompt(meta: dict[str, Any], note: str = "") -> str:
    """Prompt asking a stronger model to resolve a blocked session (decision only)."""
    blocked = meta.get("blocked") or {}
    parts = [
        "あなたは作業が判断待ちで止まった開発セッションの意思決定を担当するシニアエンジニアです。",
        "これまでの経緯を踏まえて判断を確定し、実装担当者が作業を再開できるようにしてください。\n",
        _JUDGE_ONLY_RULES,
        f"\n## 出力JSONスキーマ\n{PLAN_JSON_SCHEMA}\n",
        f"## 元のタスク\n{meta.get('task') or '(不明)'}\n",
    ]
    if meta.get("refined_prompt"):
        parts.append(f"## 実装担当者への指示（refined_prompt）\n{meta['refined_prompt']}\n")
    if blocked:
        lines = [f"- 質問: {blocked.get('question') or '(明示なし)'}"]
        if blocked.get("options"):
            lines.append(f"- 選択肢: {' / '.join(blocked['options'])}")
        if blocked.get("recommendation"):
            lines.append(f"- 担当者の推奨: {blocked['recommendation']}")
        lines.append(f"- 検出種別: {blocked.get('kind', '?')} / {blocked.get('reason', '?')}")
        parts.append("## 判断待ちの内容\n" + "\n".join(lines) + "\n")
    if meta.get("last_output_summary"):
        parts.append(f"## 直近の出力（末尾）\n```\n{meta['last_output_summary']}\n```\n")
    if meta.get("diff_stat") or meta.get("diff"):
        parts.append(f"## 現在の変更\n```\n{meta.get('diff_stat', '')}\n```\n")
        if meta.get("diff"):
            parts.append(f"```diff\n{meta['diff']}\n```\n")
    if note:
        parts.append(f"## 追加の指示\n{note}\n")
    return "\n".join(parts)


def _section(title: str, items: list[str], *, numbered: bool = False) -> str:
    if not items:
        body = "- (なし)"
    elif numbered:
        body = "\n".join(f"{i}. {item}" for i, item in enumerate(items, 1))
    else:
        body = "\n".join(f"- {item}" for item in items)
    return f"## {title}\n{body}"


def format_plan_sections(plan: dict[str, Any]) -> str:
    """Render the plan as ## 決定事項 / ## 制約 / ## 実装手順 / ## 未解決点 sections."""
    sections = [
        _section("決定事項", plan.get("decisions", [])),
        _section("制約", plan.get("constraints", [])),
        _section("実装手順", plan.get("implementation_steps", []), numbered=True),
        _section("未解決点", plan.get("open_questions", [])),
    ]
    if plan.get("raw_notes"):
        sections.append(f"## 参考: 判断担当の出力（構造化に失敗したため原文）\n{plan['raw_notes']}")
    return "\n\n".join(sections)


def build_implementation_prompt(plan: dict[str, Any], task: str, files_block: str = "") -> str:
    """Build the implementer prompt with the planner's decisions fixed."""
    head = f"{files_block}\n\n" if files_block else ""
    return (
        f"{head}## タスク\n{task.strip()}\n\n"
        f"{format_plan_sections(plan)}\n\n"
        "上記の決定事項と制約は確定済みです。再検討せずにそれに従い、実装手順に沿って実装してください。"
        "未解決点は合理的な既定で進め、どうしても判断が必要な場合のみ確認してください。"
    )


def build_resume_prompt(base_prompt: str, plan: dict[str, Any]) -> str:
    """Prompt for re-running an implementer after an escalation decision."""
    return (
        f"{base_prompt.rstrip()}\n\n"
        "# 判断待ちに対する決定（エスカレーション結果）\n\n"
        f"{format_plan_sections(plan)}\n\n"
        "上記の決定に従い、作業を最後まで続行してください。"
    )


def select_candidate(difficulty: str, tool: str | None = None) -> tuple[str, str]:
    """Pick `(tool, model)` from CANDIDATES for a difficulty, optionally restricted to a tool.

    Exact difficulty matches win (first in CANDIDATES order); otherwise the nearest
    difficulty is used, preferring the stronger side on ties. Falls back to
    FALLBACK_DECISION when no candidate exists for the tool.
    """
    pool = [c for c in CANDIDATES if tool is None or c["tool"] == tool]
    if not pool:
        if tool is not None and tool not in SUPPORTED_TOOLS:
            raise AiRouteError(f"Unsupported tool '{tool}' (supported: {', '.join(SUPPORTED_TOOLS)})")
        return FALLBACK_DECISION["tool"], FALLBACK_DECISION["model"]
    target = DIFFICULTY_ORDER.index(difficulty) if difficulty in DIFFICULTY_ORDER else 1

    def rank(c: dict[str, str]) -> tuple[int, int]:
        level = DIFFICULTY_ORDER.index(c["difficulty"]) if c["difficulty"] in DIFFICULTY_ORDER else 1
        return abs(level - target), 0 if level >= target else 1

    best = min(pool, key=rank)  # min() is stable, so CANDIDATES order breaks ties
    return best["tool"], best["model"]


def resolve_spec(spec: str, difficulty: str) -> tuple[str, str]:
    """Resolve a stage spec: 'tool:model' as-is, bare 'tool' or '' via select_candidate."""
    spec = (spec or "").strip()
    if ":" in spec:
        return parse_tool_model_spec(spec)
    if spec:
        if spec not in SUPPORTED_TOOLS:
            raise AiRouteError(f"Invalid stage spec '{spec}' (use 'tool:model' or a tool name).")
        return select_candidate(difficulty, spec)
    return select_candidate(difficulty)


def resolve_planner(cli_value: str | None = None, preset: str | None = None) -> tuple[str, str]:
    """Planner `(tool, model)`: --planner > $AI_ROUTE_PLANNER > preset > STAGE_DEFAULTS."""
    return resolve_spec(_checked(resolve_planner_spec, cli_value, preset), "high")


def resolve_implementer(
    difficulty: str, cli_value: str | None = None, preset: str | None = None
) -> tuple[str, str]:
    """Implementer `(tool, model)`: --implementer > $AI_ROUTE_IMPLEMENTER > preset > by difficulty."""
    return resolve_spec(_checked(resolve_implementer_spec, cli_value, preset), difficulty)


def _checked(fn: Any, cli_value: str | None, preset: str | None) -> str:
    try:
        return fn(cli_value, preset)
    except ValueError as e:
        raise AiRouteError(str(e)) from e
