"""Agent execution flows shared by `run`, `escalate`, `handoff`, `resume`, and `review`.

* `run_and_record`  - synchronous headless run + credit/blocked detection + history/meta update
* `escalate_session` - ask a stronger model for a decision (no edits) and store it
* `launch_implementer` - tmux dispatch (with output logging) or synchronous `--wait` run,
  with optional automatic escalation on blocking and automatic cross-service failover on
  credit exhaustion
"""

from __future__ import annotations

import dataclasses
import sys
from pathlib import Path
from typing import Any

from ai_router.blocking import blocked_signature, detect_blocked
from ai_router.config import AUTO_ESCALATE_MAX_ROUNDS, HEADLESS_BLOCKED_INSTRUCTION, STAGE_DEFAULTS
from ai_router.credits import (
    detect_credit_exhausted,
    get_exhausted_tools,
    mark_tool_exhausted,
    select_alternative_candidate,
)
from ai_router.dispatch import (
    EXIT_LINE_PREFIX,
    RC_COMMAND_NOT_FOUND,
    RC_TIMEOUT,
    begin_session_log,
    build_agent_shell_cmd,
    dispatch_to_tmux,
    resolve_workdir,
    run_agent_headless_once,
    write_prompt_file,
)
from ai_router.errors import AiRouteError
from ai_router.models import BlockSignal
from ai_router.sessions import append_history, build_session_meta
from ai_router.storage import append_jsonl, save_session_meta
from ai_router.twostage import build_escalation_prompt, build_resume_prompt, extract_plan, resolve_spec
from ai_router.utils import get_repo_and_branch, now_iso


@dataclasses.dataclass
class AgentRunResult:
    rc: int
    output: str
    latency_ms: int
    status: str
    signal: BlockSignal | None
    credit_exhausted_reason: str = ""


def classify_status(rc: int, signal: BlockSignal | None, *, credit_reason: str | None = None) -> str:
    """Map a run outcome to completed | blocked | credit_exhausted | timeout | launch_failed | failed."""
    if credit_reason:
        return "credit_exhausted"
    if signal is not None:
        return signal.reason
    if rc == RC_TIMEOUT:
        return "timeout"
    if rc == RC_COMMAND_NOT_FOUND:
        return "launch_failed"
    return "completed" if rc == 0 else "failed"


def append_headless_instruction(prompt: str) -> str:
    """Append HEADLESS_BLOCKED_INSTRUCTION (synchronous headless runs only)."""
    return f"{prompt.rstrip()}\n\n{HEADLESS_BLOCKED_INSTRUCTION}"


def record_blocked(
    *, session_id: str, workdir: Path, tool: str, model: str, signal: BlockSignal, source: str, stage: str = ""
) -> None:
    """Append a `blocked` event to history.jsonl."""
    repo, branch = get_repo_and_branch(workdir)
    append_jsonl(
        {
            "ts": now_iso(),
            "event": "blocked",
            "session_id": session_id,
            "repo": repo,
            "branch": branch,
            "workdir": str(workdir),
            "tool": tool,
            "model": model,
            "stage": stage,
            "source": source,
            **signal.to_dict(),
        }
    )


def record_credit_exhausted(
    *, session_id: str, workdir: Path, tool: str, model: str, reason: str, source: str, stage: str = ""
) -> None:
    """Append a `credit_exhausted` event to history.jsonl."""
    repo, branch = get_repo_and_branch(workdir)
    append_jsonl(
        {
            "ts": now_iso(),
            "event": "credit_exhausted",
            "session_id": session_id,
            "repo": repo,
            "branch": branch,
            "workdir": str(workdir),
            "tool": tool,
            "model": model,
            "stage": stage,
            "source": source,
            "reason": reason,
        }
    )


_REAL_RUN_HEADLESS = run_agent_headless_once


def _call_headless(
    tool: str,
    model: str,
    prompt: str,
    cwd: Path,
    *,
    timeout: float | None,
    effort: str = "",
    read_only: bool = False,
) -> tuple[int, str, int]:
    """Invoke `run_agent_headless_once`, passing optional kwargs safely for test-mock compatibility."""
    kwargs: dict[str, Any] = {"timeout": timeout}
    if effort:
        kwargs["effort"] = effort
    if read_only and run_agent_headless_once is _REAL_RUN_HEADLESS:
        kwargs["read_only"] = True
    return run_agent_headless_once(tool, model, prompt, cwd, **kwargs)


def run_and_record(
    *,
    tool: str,
    model: str,
    prompt: str,
    workdir: Path,
    session_id: str,
    meta: dict[str, Any] | None,
    timeout: float | None,
    stage: str = "single",
    save_meta: bool = True,
    effort: str = "",
    read_only: bool | None = None,
) -> tuple[AgentRunResult, dict[str, Any]]:
    """Run an agent synchronously, detect credit exhaustion/blocking, and record events."""
    effort_tag = f", effort={effort}" if effort else ""
    effective_read_only = (stage in {"planner", "escalate", "review"}) if read_only is None else read_only
    print(f"[running] {tool}:{model} (stage={stage}{effort_tag}, timeout={f'{timeout:g}s' if timeout else 'none'})")
    rc, output, latency_ms = _call_headless(
        tool, model, prompt, workdir, timeout=timeout, effort=effort, read_only=effective_read_only
    )
    credit_reason = detect_credit_exhausted(rc, output)
    signal = None if credit_reason else detect_blocked(rc, output)
    status = classify_status(rc, signal, credit_reason=credit_reason)

    log_path = begin_session_log(session_id, tool, model)
    with log_path.open("a", encoding="utf-8") as f:
        f.write(f"{output}\n{EXIT_LINE_PREFIX}{rc}\n")

    repo, branch = get_repo_and_branch(workdir)
    append_jsonl(
        {
            "ts": now_iso(),
            "event": "agent_result",
            "session_id": session_id,
            "repo": repo,
            "branch": branch,
            "workdir": str(workdir),
            "tool": tool,
            "model": model,
            "effort": effort,
            "stage": stage,
            "rc": rc,
            "latency_ms": latency_ms,
            "status": status,
            "blocked_reason": signal.reason if signal else None,
            "credit_exhausted": credit_reason or None,
        }
    )
    if credit_reason:
        mark_tool_exhausted(tool, credit_reason, model=model)
        record_credit_exhausted(
            session_id=session_id, workdir=workdir, tool=tool, model=model,
            reason=credit_reason, source="wait", stage=stage,
        )
    elif signal is not None:
        record_blocked(
            session_id=session_id, workdir=workdir, tool=tool, model=model,
            signal=signal, source="wait", stage=stage,
        )

    prev = meta or {}
    extra_fields: dict[str, Any] = {
        "log_path": str(log_path),
        "log_checked": {"size": log_path.stat().st_size, "signature": blocked_signature(signal)},
        "credit_exhausted": {"tool": tool, "model": model, "reason": credit_reason} if credit_reason else None,
    }
    if effort:
        extra_fields["effort"] = effort
    new_meta = build_session_meta(
        session_id=session_id,
        tool=tool,
        model=model,
        workdir=workdir,
        previous=prev,
        status=status,
        output=output,
        blocked=signal,
        include_diff=save_meta,
        event="agent_result",
        **extra_fields,
    )
    if save_meta:
        save_session_meta(workdir, new_meta)
    print(f"[{status}] {tool}:{model} rc={rc} ({latency_ms} ms)")
    return AgentRunResult(rc, output, latency_ms, status, signal, credit_exhausted_reason=credit_reason or ""), new_meta


def escalate_session(
    *,
    workdir: Path,
    meta: dict[str, Any],
    tool: str,
    model: str,
    note: str = "",
    timeout: float | None,
    trigger: str = "manual",
    effort: str = "",
    auto_failover: bool = True,
) -> dict[str, Any] | None:
    """Ask `tool:model` for a decision on a (blocked) session; store it in meta["decision"].

    Automatically fails over to an alternative high-difficulty service when credit exhaustion
    is detected on the chosen escalation model (if `auto_failover` is True).
    Returns the plan dict, or None when the escalation model itself failed.
    """
    session_id = str(meta.get("session_id", ""))
    prompt = build_escalation_prompt(meta, note)
    tried_tools: set[str] = set()
    cur_tool, cur_model = tool, model

    while True:
        print(f"[escalating] {session_id} -> {cur_tool}:{cur_model} (trigger={trigger})")
        rc, output, latency_ms = _call_headless(
            cur_tool, cur_model, prompt, workdir, timeout=timeout, effort=effort, read_only=True
        )
        credit_reason = detect_credit_exhausted(rc, output)
        if credit_reason:
            mark_tool_exhausted(cur_tool, credit_reason, model=cur_model)
            record_credit_exhausted(
                session_id=session_id, workdir=workdir, tool=cur_tool, model=cur_model,
                reason=credit_reason, source="escalate", stage="escalate",
            )
            tried_tools.add(cur_tool)
            if auto_failover:
                excluded = tried_tools | set(get_exhausted_tools().keys())
                alt = select_alternative_candidate("high", excluded_tools=excluded)
                if alt is not None:
                    alt_tool, alt_model, _ = alt
                    print(
                        f"[failover] Escalation service {cur_tool}:{cur_model} credit exhausted "
                        f"({credit_reason}); retrying with {alt_tool}:{alt_model}.",
                        file=sys.stderr,
                    )
                    cur_tool, cur_model = alt_tool, alt_model
                    continue
        break

    plan, valid = extract_plan(output) if rc == 0 else ({}, False)
    blocked = meta.get("blocked") or {}

    repo, branch = get_repo_and_branch(workdir)
    append_jsonl(
        {
            "ts": now_iso(),
            "event": "escalated",
            "session_id": session_id,
            "repo": repo,
            "branch": branch,
            "workdir": str(workdir),
            "from_tool": meta.get("current_tool", "unknown"),
            "from_model": meta.get("current_model", "unknown"),
            "tool": cur_tool,
            "model": cur_model,
            "trigger": trigger,
            "rc": rc,
            "latency_ms": latency_ms,
            "plan_valid": valid,
            "question": blocked.get("question", ""),
            "status": "decided" if rc == 0 else "failed",
        }
    )

    if rc != 0:
        print(f"[warn] Escalation failed (rc={rc}):\n{output}", file=sys.stderr)
        return None

    meta["decision"] = {**plan, "tool": cur_tool, "model": cur_model, "ts": now_iso(), "valid": valid}
    meta["status"] = "decided"
    meta["updated_at"] = now_iso()
    append_history(meta, "escalated", tool=cur_tool, model=cur_model, trigger=trigger, plan_valid=valid)
    save_session_meta(workdir, meta)
    return plan


def launch_implementer(
    *,
    tool: str,
    model: str,
    prompt: str,
    workdir: Path,
    mode: str,
    wait: bool,
    session_id: str,
    meta: dict[str, Any],
    timeout: float | None,
    auto_escalate: bool = True,
    auto_failover: bool = True,
    stage: str = "single",
    split: bool = False,
    win_name: str = "",
    pane_title: str = "",
    effort: str = "",
    difficulty: str = "medium",
) -> AgentRunResult | None:
    """Launch an implementer: tmux (logged) by default, or synchronously with `wait`.

    `prompt` must not contain HEADLESS_BLOCKED_INSTRUCTION; it is appended here for `wait`
    runs. `meta` must already be saved for the session. With `wait` (headless only):
    - a run that hits credit/quota exhaustion fails over automatically to an alternative
      service (when `auto_failover` is True), recording the exhausted service's cooldown;
    - a run that ends waiting for a decision is escalated and re-run with the decision,
      at most AUTO_ESCALATE_MAX_ROUNDS times.
    Raises AiRouteError when the final status is not `completed`.
    """
    workdir = resolve_workdir(workdir)
    if not wait:
        log_path = begin_session_log(session_id, tool, model)
        meta["log_path"] = str(log_path)
        if effort:
            meta["effort"] = effort
        save_session_meta(workdir, meta)
        prompt_file = write_prompt_file(prompt)
        cmd_kwargs: dict[str, Any] = {"log_path": log_path}
        if effort:
            cmd_kwargs["effort"] = effort
        shell_cmd = build_agent_shell_cmd(tool, model, prompt_file, mode, **cmd_kwargs)
        dispatch_to_tmux(
            shell_cmd=shell_cmd,
            workdir=workdir,
            win_name=win_name or f"{tool}-{session_id}",
            tool=tool,
            mode=mode,
            prompt_file=prompt_file,
            split_current=split,
            pane_title=pane_title or f"{tool}:{model[:18]}",
            log_path=log_path,
        )
        print(f"[log] {log_path}  (check with: ai-route status  /  continue with: ai-route resume {session_id})")
        return None

    if mode != "headless":
        raise AiRouteError("--wait requires --mode headless.")

    rounds = 0
    tried_tools: set[str] = set()
    cur_tool, cur_model, cur_effort = tool, model, effort
    current_prompt = append_headless_instruction(prompt)
    while True:
        result, meta = run_and_record(
            tool=cur_tool, model=cur_model, prompt=current_prompt, workdir=workdir,
            session_id=session_id, meta=meta, timeout=timeout, stage=stage, effort=cur_effort,
        )
        if result.status == "credit_exhausted" and auto_failover:
            tried_tools.add(cur_tool)
            excluded = tried_tools | set(get_exhausted_tools().keys())
            alt = select_alternative_candidate(difficulty, excluded_tools=excluded)
            if alt is None:
                break
            alt_tool, alt_model, alt_effort = alt
            print(
                f"[failover] Credit exhausted on {cur_tool}:{cur_model} ({result.credit_exhausted_reason}); "
                f"routing to {alt_tool}:{alt_model}.",
                file=sys.stderr,
            )
            repo, branch = get_repo_and_branch(workdir)
            append_jsonl(
                {
                    "ts": now_iso(),
                    "event": "failover",
                    "session_id": session_id,
                    "repo": repo,
                    "branch": branch,
                    "workdir": str(workdir),
                    "from_tool": cur_tool,
                    "from_model": cur_model,
                    "tool": alt_tool,
                    "model": alt_model,
                    "reason": result.credit_exhausted_reason,
                    "stage": stage,
                }
            )
            cur_tool, cur_model = alt_tool, alt_model
            cur_effort = cur_effort or alt_effort
            continue

        if result.status != "blocked" or not auto_escalate or rounds >= AUTO_ESCALATE_MAX_ROUNDS:
            break
        rounds += 1
        exhausted_now = set(get_exhausted_tools().keys())
        esc_tool, esc_model = resolve_spec(STAGE_DEFAULTS["escalate"], "high")
        if esc_tool in exhausted_now and auto_failover:
            alt_esc = select_alternative_candidate("high", excluded_tools=exhausted_now)
            if alt_esc is not None:
                esc_tool, esc_model, _ = alt_esc
        plan = escalate_session(
            workdir=workdir, meta=meta, tool=esc_tool, model=esc_model,
            timeout=timeout, trigger="auto", auto_failover=auto_failover,
        )
        if plan is None:
            break
        current_prompt = append_headless_instruction(build_resume_prompt(prompt, plan))

    print("\n" + (result.output or "(no output)"))
    if result.status == "completed":
        return result

    hint = f"ai-route resume {session_id}"
    if result.status == "credit_exhausted":
        raise AiRouteError(
            f"Credit exhausted on {cur_tool}:{cur_model} ({result.credit_exhausted_reason}) "
            f"and no alternative service is available.\n"
            f"  Next: ai-route status --reset-credits  (once credits are restored)"
        )
    if result.status == "blocked":
        question = result.signal.question if result.signal else ""
        raise AiRouteError(
            f"Agent is waiting for a decision: {question or '(see output)'}\n"
            f"  Next: {hint}  "
            f"(or: ai-route escalate --from-session {session_id} --then-implement)"
        )
    raise AiRouteError(f"Agent run ended with status '{result.status}' (rc={result.rc}). See: {hint}")
