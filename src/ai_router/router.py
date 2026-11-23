"""Lightweight task difficulty & effort classification and multi-service routing."""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

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
from ai_router.dispatch import run_agent_headless_once
from ai_router.models import RouteDecision
from ai_router.tools import BUILTIN_TOOL_ADAPTERS, get_tool_adapter, is_tool_installed
from ai_router.utils import append_plan_first, extract_first_json_object, run_cmd, sanitize_slug

_DEFAULT_ROUTER_FALLBACK_ORDER: tuple[str, ...] = ("opencode", "agy", "copilot", "claude", "codex")


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

    fb_tool = str(fb.get("tool", ""))
    has_installed_alt = any(
        is_tool_installed(c["tool"]) for c in cfg.candidates if c["tool"] not in excluded
    )
    need_alt = fb_tool in excluded or (
        cfg.auto_detect_tools and has_installed_alt and not is_tool_installed(fb_tool)
    )
    if need_alt:
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
            fb["reason"] = f"Fallback routed to alternative service ({alt_tool})"

    fb.setdefault("effort", DIFFICULTY_TO_DEFAULT_EFFORT.get(fb.get("difficulty", "medium"), "medium"))
    return RouteDecision(
        **fb,
        refined_prompt=append_plan_first(task, plan_first),
        router_latency_ms=latency_ms,
        is_fallback=True,
    )


def _router_model_for_tool(tool: str, cfg: AppConfig) -> str:
    if tool == "opencode" or (cfg.router_tool and tool == cfg.router_tool):
        return cfg.router_model
    # Prefer the lowest-difficulty configured candidate for this tool, else built-in default
    tool_cands = [c for c in cfg.candidates if c["tool"] == tool]
    if tool_cands:
        diff_rank = {"low": 0, "medium": 1, "high": 2}
        best = min(tool_cands, key=lambda c: diff_rank.get(c["difficulty"], 1))
        return best["model"]
    adapter = get_tool_adapter(tool)
    return adapter.default_router_model if adapter else cfg.router_model


def resolve_router_service(
    cfg: AppConfig,
    *,
    excluded_tools: set[str] | None = None,
) -> tuple[str, str] | None:
    """Resolve the `(router_tool, router_model)` to use for classification.

    - If `cfg.router_tool` is explicitly configured, uses it (unless exhausted).
    - If `cfg.router_fallback_tools` is configured, tries tools in that order (skipping
      exhausted or uninstalled ones).
    - Otherwise uses `opencode` when installed; if `opencode` is NOT installed on the host,
      automatically selects the first installed, non-exhausted alternative service so
      environments without `opencode` still get intelligent routing.
    """
    excluded = excluded_tools or set()

    if cfg.router_tool:
        if cfg.router_tool in excluded:
            # Try explicit fallback tools if configured
            for t in cfg.router_fallback_tools:
                if t not in excluded and (not cfg.auto_detect_tools or is_tool_installed(t)):
                    return t, _router_model_for_tool(t, cfg)
            return None
        return cfg.router_tool, _router_model_for_tool(cfg.router_tool, cfg)

    if cfg.router_fallback_tools:
        for t in cfg.router_fallback_tools:
            if t not in excluded and (not cfg.auto_detect_tools or is_tool_installed(t)):
                return t, _router_model_for_tool(t, cfg)
        return None

    # Default behavior: prefer opencode if installed (or when auto_detect_tools is off)
    opencode_installed = is_tool_installed("opencode")
    if opencode_installed or not cfg.auto_detect_tools:
        if "opencode" in excluded:
            return None
        return "opencode", cfg.router_model

    # `opencode` is not installed on this machine: auto-pick an installed alternative router
    for t in _DEFAULT_ROUTER_FALLBACK_ORDER:
        if t == "opencode" or t in excluded:
            continue
        if t in BUILTIN_TOOL_ADAPTERS and is_tool_installed(t):
            return t, _router_model_for_tool(t, cfg)
    if "opencode" not in excluded:
        return "opencode", cfg.router_model
    return None


def _invoke_router(
    router_tool: str,
    router_model: str,
    prompt: str,
    *,
    timeout: float | None,
    cfg: AppConfig,
) -> tuple[int, str, str, int]:
    """Invoke the chosen router service and return `(returncode, stdout, stderr, latency_ms)`."""
    if router_tool == "opencode":
        cmd = ["opencode", "run"]
        if cfg.opencode_attach_url:
            cmd.extend(["--attach", cfg.opencode_attach_url])
        cmd.extend(["-m", router_model, prompt])
        t0 = time.monotonic()
        res = run_cmd(cmd, check=False, timeout=timeout)
        latency_ms = int((time.monotonic() - t0) * 1000)
        return res.returncode, res.stdout or "", res.stderr or "", latency_ms

    # Non-opencode routers (e.g. agy via ACP plan mode, copilot, claude, codex)
    rc, output, latency_ms = run_agent_headless_once(
        router_tool,
        router_model,
        prompt,
        Path.cwd(),
        timeout=timeout,
        effort="low",
        read_only=True,
    )
    return rc, output, "" if rc == 0 else output, latency_ms


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

    Automatically filters out credit-exhausted services and (when `auto_detect_tools` is
    enabled) uninstalled CLIs. Supports multi-service routing when `opencode` is absent or
    when `router_tool` / `router_fallback_tools` are configured.

    Never raises for router-side failures: a missing binary, timeout, non-zero exit,
    unparsable output, credit exhaustion, or disallowed model all yield a fallback decision.
    """
    cfg = config or get_active_config()
    exhausted = set(get_exhausted_tools().keys())
    effective_excluded = exhausted | set(excluded_tools or ())

    # Filter candidates by credit exhaustion and (optionally) installed CLI detection
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

    if cfg.auto_detect_tools:
        installed_cands = [c for c in active_candidates if is_tool_installed(c["tool"])]
        if installed_cands:
            active_candidates = installed_cands

    router_choice = resolve_router_service(cfg, excluded_tools=effective_excluded)
    if router_choice is None:
        print(
            "[warn] No available router service (credit-exhausted or uninstalled). "
            "Routing to an available alternative service.",
            file=sys.stderr,
        )
        return _fallback_decision(
            task, 0, plan_first, cfg.fallback_decision, excluded_tools=effective_excluded, config=cfg
        )

    router_tool, router_model = router_choice
    prompt = build_router_prompt(task, repo_ctx, files_ctx, candidates=active_candidates)

    t0 = time.monotonic()
    try:
        rc, stdout_text, stderr_text, latency_ms = _invoke_router(
            router_tool, router_model, prompt, timeout=timeout, cfg=cfg
        )
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

    if rc != 0:
        combined = f"{stdout_text}\n{stderr_text}".strip()
        credit_reason = detect_credit_exhausted(rc, combined)
        if credit_reason:
            mark_tool_exhausted(router_tool, credit_reason, model=router_model)
            effective_excluded.add(router_tool)
            print(
                f"[warn] Router service '{router_tool}' credit exhausted ({credit_reason}). "
                "Routing to an alternative service.",
                file=sys.stderr,
            )
            # If alternative router fallback tools are configured, try the next one once
            if cfg.router_fallback_tools:
                next_router = resolve_router_service(cfg, excluded_tools=effective_excluded)
                if next_router is not None and next_router[0] != router_tool:
                    return route_task(
                        task,
                        repo_ctx,
                        files_ctx,
                        plan_first,
                        timeout=timeout,
                        config=cfg,
                        excluded_tools=effective_excluded,
                    )
        else:
            print(f"[warn] Router execution failed: {stderr_text.strip()}", file=sys.stderr)
        return _fallback_decision(
            task, latency_ms, plan_first, cfg.fallback_decision, excluded_tools=effective_excluded, config=cfg
        )

    parsed = extract_first_json_object(stdout_text)
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
