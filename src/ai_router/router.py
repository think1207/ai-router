"""Lightweight task difficulty & effort classification and routing via OpenCode."""

from __future__ import annotations

import subprocess
import sys
import time

from ai_router.config import (
    DIFFICULTY_TO_DEFAULT_EFFORT,
    VALID_EFFORTS,
    AppConfig,
    get_active_config,
)
from ai_router.credits import (
    detect_credit_exhausted,
    get_exhausted_tools,
    mark_tool_exhausted,
    select_alternative_candidate,
)
from ai_router.models import RouteDecision
from ai_router.utils import append_plan_first, extract_first_json_object, run_cmd, sanitize_slug


def build_router_prompt(
    task: str,
    repo_ctx: str,
    files_ctx: str,
    candidates: list[dict[str, str]] | None = None,
) -> str:
    """Build the Japanese classification prompt for the lightweight router model.

    The plan-first instruction is intentionally not requested from the model; it is
    appended deterministically by `route_task` instead.
    """
    if candidates is None:
        candidates = get_active_config().candidates

    candidates_text = "\n".join(
        f'{i + 1}. tool: "{c["tool"]}", model: "{c["model"]}" (目安: {c["difficulty"]})\n'
        f'   - 用途: {c["description"]}'
        for i, c in enumerate(candidates)
    )
    available_tools = list(dict.fromkeys(c["tool"] for c in candidates))
    tool_union = " | ".join(f'"{t}"' for t in available_tools) if available_tools else '"opencode" | "agy" | "claude"'

    return f"""あなたはソフトウェア開発タスクのルーターです。
以下のタスクの難易度(difficulty)と必要な推論の深さ(effort)を判定し、最適な実行ツールとモデルを1つ選定してください。
必ず有効なJSONオブジェクトのみを出力し、Markdownのコードブロックや前置きは一切含めないでください。

## 選定候補（この中から厳密に選ぶこと）
{candidates_text}

## effort（推論の深さ）の目安
- "low": 単純な修正、typo、ボイラープレート生成、定型作業
- "medium": 標準的な機能実装、一般的なバグ修正・テスト追加
- "high": 複数ファイルにまたがる設計・リファクタリング、複雑な原因調査
- "xhigh": 高度なアーキテクチャ設計、難解な並行処理・競合バグ解析
- "max": 極めて複雑な設計判断や広範囲の破壊的変更

## 出力JSONスキーマ
{{
  "difficulty": "low" | "medium" | "high",
  "effort": "low" | "medium" | "high" | "xhigh" | "max",
  "tool": {tool_union},
  "model": "<上記候補のmodel文字列を完全一致で記載>",
  "branch_slug": "<gitブランチ用の短い英語スラッグ例: fix-parser-edge-case>",
  "reason": "<日本語で簡潔な選定理由>",
  "refined_prompt": "<選定ツールに渡す整理済みの実装指示（日本語）>"
}}

refined_prompt には作業の進め方（方針の提示や合意の取り方など）に関する指示を含めないでください。必要な指示はシステム側で付与します。

## リポジトリ状況
{repo_ctx}

## 指定ファイル概要
{files_ctx}

## ユーザーからのタスク
{task}
"""


def _normalize_effort(raw_effort: object, difficulty: str, candidate: dict[str, str] | None = None) -> str:
    val = str(raw_effort or "").strip().lower()
    if val in VALID_EFFORTS:
        return val
    if candidate and candidate.get("effort") in VALID_EFFORTS:
        return candidate["effort"]
    return DIFFICULTY_TO_DEFAULT_EFFORT.get(difficulty, "medium")


def _fallback_decision(
    task: str,
    latency_ms: int,
    plan_first: bool,
    fallback_decision: dict[str, str] | None = None,
    *,
    excluded_tools: set[str] | None = None,
    config: AppConfig | None = None,
) -> RouteDecision:
    cfg = config or get_active_config()
    fb = dict(fallback_decision if fallback_decision is not None else cfg.fallback_decision)
    excluded = excluded_tools or set()

    if fb.get("tool") in excluded:
        alt = select_alternative_candidate(
            fb.get("difficulty", "medium"),
            excluded_tools=excluded,
            config=cfg,
        )
        if alt is not None:
            alt_tool, alt_model, alt_effort = alt
            fb["tool"] = alt_tool
            fb["model"] = alt_model
            fb["effort"] = fb.get("effort") or alt_effort
            fb["reason"] = f"Fallback routed to alternative service ({alt_tool}) due to credit exhaustion"

    fb.setdefault("effort", DIFFICULTY_TO_DEFAULT_EFFORT.get(fb.get("difficulty", "medium"), "medium"))
    return RouteDecision(
        **fb,
        refined_prompt=append_plan_first(task, plan_first),
        router_latency_ms=latency_ms,
        is_fallback=True,
    )


def route_task(
    task: str,
    repo_ctx: str,
    files_ctx: str,
    plan_first: bool,
    *,
    timeout: float | None = None,
    config: AppConfig | None = None,
    excluded_tools: set[str] | None = None,
) -> RouteDecision:
    """Invoke the lightweight router model and return a validated RouteDecision.

    Never raises for router-side failures: a missing `opencode` binary, a timeout, a
    non-zero exit, unparsable output, credit exhaustion, or a disallowed model all yield
    a fallback decision (preferring services that are not credit-exhausted).

    When `plan_first` is True, PLAN_FIRST_INSTRUCTION is appended to `refined_prompt` on
    every path (including fallbacks), regardless of the classified difficulty.
    """
    cfg = config or get_active_config()
    exhausted = set(get_exhausted_tools().keys())
    effective_excluded = exhausted | set(excluded_tools or ())

    active_candidates = [c for c in cfg.candidates if c["tool"] not in effective_excluded]
    if active_candidates and effective_excluded:
        skipped = ", ".join(sorted(effective_excluded & {c["tool"] for c in cfg.candidates}))
        if skipped:
            print(f"[info] Excluding credit-exhausted service(s) from routing: {skipped}", file=sys.stderr)
    elif not active_candidates:
        if effective_excluded:
            print(
                "[warn] All configured services are marked credit-exhausted; falling back to full candidate list.",
                file=sys.stderr,
            )
        active_candidates = list(cfg.candidates)
        effective_excluded = set()

    # If the router service itself ('opencode') is already marked credit-exhausted,
    # skip waiting on it and fall back immediately to an available alternative service.
    if "opencode" in effective_excluded:
        print(
            "[warn] Router service 'opencode' is currently marked credit-exhausted. "
            "Routing to an available alternative service.",
            file=sys.stderr,
        )
        return _fallback_decision(
            task, 0, plan_first, cfg.fallback_decision, excluded_tools=effective_excluded, config=cfg
        )

    prompt = build_router_prompt(task, repo_ctx, files_ctx, candidates=active_candidates)
    cmd = ["opencode", "run"]
    if cfg.opencode_attach_url:
        cmd.extend(["--attach", cfg.opencode_attach_url])
    cmd.extend(["-m", cfg.router_model, prompt])

    t0 = time.monotonic()
    try:
        res = run_cmd(cmd, check=False, timeout=timeout)
    except subprocess.TimeoutExpired:
        latency_ms = int((time.monotonic() - t0) * 1000)
        print(f"[warn] Router timed out after {timeout:g}s. Falling back.", file=sys.stderr)
        return _fallback_decision(
            task, latency_ms, plan_first, cfg.fallback_decision, excluded_tools=effective_excluded, config=cfg
        )
    except OSError as e:
        latency_ms = int((time.monotonic() - t0) * 1000)
        print(f"[warn] Router command could not be executed ({e}). Falling back.", file=sys.stderr)
        return _fallback_decision(
            task, latency_ms, plan_first, cfg.fallback_decision, excluded_tools=effective_excluded, config=cfg
        )
    latency_ms = int((time.monotonic() - t0) * 1000)

    if res.returncode != 0:
        combined = f"{res.stdout or ''}\n{res.stderr or ''}".strip()
        credit_reason = detect_credit_exhausted(res.returncode, combined)
        if credit_reason:
            mark_tool_exhausted("opencode", credit_reason, model=cfg.router_model)
            effective_excluded.add("opencode")
            print(
                f"[warn] Router service 'opencode' credit exhausted ({credit_reason}). "
                "Routing to an alternative service.",
                file=sys.stderr,
            )
        else:
            print(f"[warn] Router execution failed: {res.stderr.strip()}", file=sys.stderr)
        return _fallback_decision(
            task, latency_ms, plan_first, cfg.fallback_decision, excluded_tools=effective_excluded, config=cfg
        )

    parsed = extract_first_json_object(res.stdout)
    if not parsed:
        print("[warn] Failed to parse JSON from router output. Falling back.", file=sys.stderr)
        return _fallback_decision(
            task, latency_ms, plan_first, cfg.fallback_decision, excluded_tools=effective_excluded, config=cfg
        )

    difficulty = str(parsed.get("difficulty", "medium")).strip().lower()
    if difficulty not in {"low", "medium", "high"}:
        difficulty = "medium"

    active_allowed_pairs = {(c["tool"], c["model"]) for c in active_candidates}
    cand_by_pair = {(c["tool"], c["model"]): c for c in active_candidates}
    tool = str(parsed.get("tool", ""))
    model = str(parsed.get("model", ""))
    is_fallback = False

    if (tool, model) not in active_allowed_pairs:
        print(f"[warn] Disallowed (tool, model)=({tool}, {model}). Falling back.", file=sys.stderr)
        if cfg.fallback_decision["tool"] not in effective_excluded:
            tool = cfg.fallback_decision["tool"]
            model = cfg.fallback_decision["model"]
        else:
            alt = select_alternative_candidate(difficulty, excluded_tools=effective_excluded, config=cfg)
            if alt is not None:
                tool, model, _ = alt
            else:
                tool = cfg.fallback_decision["tool"]
                model = cfg.fallback_decision["model"]
        is_fallback = True

    effort = _normalize_effort(parsed.get("effort"), difficulty, cand_by_pair.get((tool, model)))
    refined = append_plan_first(str(parsed.get("refined_prompt") or "").strip() or task, plan_first)

    return RouteDecision(
        difficulty=difficulty,
        effort=effort,
        tool=tool,
        model=model,
        branch_slug=sanitize_slug(str(parsed.get("branch_slug", "ai-task"))),
        reason=str(parsed.get("reason", "")),
        refined_prompt=refined,
        router_latency_ms=latency_ms,
        is_fallback=is_fallback,
    )
