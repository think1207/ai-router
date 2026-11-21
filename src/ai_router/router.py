"""Lightweight task difficulty classification and routing via OpenCode."""

from __future__ import annotations

import subprocess
import sys
import time

from ai_router.config import AppConfig, get_active_config
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

    return f"""あなたはソフトウェア開発タスクのルーターです。
以下のタスクの難易度と性質を判定し、最適な実行ツールとモデルを1つ選定してください。
必ず有効なJSONオブジェクトのみを出力し、Markdownのコードブロックや前置きは一切含めないでください。

## 選定候補（この中から厳密に選ぶこと）
{candidates_text}

## 出力JSONスキーマ
{{
  "difficulty": "low" | "medium" | "high",
  "tool": "opencode" | "agy" | "claude",
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


def _fallback_decision(
    task: str,
    latency_ms: int,
    plan_first: bool,
    fallback_decision: dict[str, str] | None = None,
) -> RouteDecision:
    fb = fallback_decision if fallback_decision is not None else get_active_config().fallback_decision
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
) -> RouteDecision:
    """Invoke the lightweight router model and return a validated RouteDecision.

    Never raises for router-side failures: a missing `opencode` binary, a timeout, a
    non-zero exit, unparsable output, or a disallowed model all yield fallback_decision.

    When `plan_first` is True, PLAN_FIRST_INSTRUCTION is appended to `refined_prompt` on
    every path (including fallbacks), regardless of the classified difficulty.
    """
    cfg = config or get_active_config()
    prompt = build_router_prompt(task, repo_ctx, files_ctx, candidates=cfg.candidates)
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
        return _fallback_decision(task, latency_ms, plan_first, cfg.fallback_decision)
    except OSError as e:
        latency_ms = int((time.monotonic() - t0) * 1000)
        print(f"[warn] Router command could not be executed ({e}). Falling back.", file=sys.stderr)
        return _fallback_decision(task, latency_ms, plan_first, cfg.fallback_decision)
    latency_ms = int((time.monotonic() - t0) * 1000)

    if res.returncode != 0:
        print(f"[warn] Router execution failed: {res.stderr.strip()}", file=sys.stderr)
        return _fallback_decision(task, latency_ms, plan_first, cfg.fallback_decision)

    parsed = extract_first_json_object(res.stdout)
    if not parsed:
        print("[warn] Failed to parse JSON from router output. Falling back.", file=sys.stderr)
        return _fallback_decision(task, latency_ms, plan_first, cfg.fallback_decision)

    allowed_pairs = cfg.allowed_pairs
    tool = str(parsed.get("tool", ""))
    model = str(parsed.get("model", ""))
    is_fallback = False

    if (tool, model) not in allowed_pairs:
        print(f"[warn] Disallowed (tool, model)=({tool}, {model}). Falling back.", file=sys.stderr)
        tool = cfg.fallback_decision["tool"]
        model = cfg.fallback_decision["model"]
        is_fallback = True

    difficulty = str(parsed.get("difficulty", "medium"))
    if difficulty not in {"low", "medium", "high"}:
        difficulty = "medium"

    refined = append_plan_first(str(parsed.get("refined_prompt") or "").strip() or task, plan_first)

    return RouteDecision(
        difficulty=difficulty,
        tool=tool,
        model=model,
        branch_slug=sanitize_slug(str(parsed.get("branch_slug", "ai-task"))),
        reason=str(parsed.get("reason", "")),
        refined_prompt=refined,
        router_latency_ms=latency_ms,
        is_fallback=is_fallback,
    )
