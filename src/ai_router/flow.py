"""Agent execution flows shared by `run`, `escalate`, `handoff`, and `review`.

* `run_and_record`  - synchronous headless run + blocked detection + history/meta update
* `escalate_session` - ask a stronger model for a decision (no edits) and store it
* `launch_implementer` - tmux dispatch (with output logging) or synchronous `--wait` run,
  with optional automatic escalation when the agent stops waiting for a decision
"""

from __future__ import annotations

import dataclasses
import sys
from pathlib import Path
from typing import Any

from ai_router.blocking import blocked_signature, detect_blocked
from ai_router.config import AUTO_ESCALATE_MAX_ROUNDS, HEADLESS_BLOCKED_INSTRUCTION, STAGE_DEFAULTS
from ai_router.dispatch import (
    EXIT_LINE_PREFIX,
    RC_COMMAND_NOT_FOUND,
    RC_TIMEOUT,
    build_agent_shell_cmd,
    dispatch_to_tmux,
    resolve_workdir,
    run_agent_headless_once,
    begin_session_log,
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


def classify_status(rc: int, signal: BlockSignal | None) -> str:
    """Map a run outcome to completed | blocked | timeout | launch_failed | failed."""
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
) -> tuple[AgentRunResult, dict[str, Any]]:
    """Run an agent synchronously, detect blocking, and record `agent_result`/`blocked`."""
    print(f"[running] {tool}:{model} (stage={stage}, timeout={f'{timeout:g}s' if timeout else 'none'})")
    rc, output, latency_ms = run_agent_headless_once(tool, model, prompt, workdir, timeout=timeout)
    signal = detect_blocked(rc, output)
    status = classify_status(rc, signal)

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
            "stage": stage,
            "rc": rc,
            "latency_ms": latency_ms,
            "status": status,
            "blocked_reason": signal.reason if signal else None,
        }
    )
    if signal is not None:
        record_blocked(
            session_id=session_id, workdir=workdir, tool=tool, model=model,
            signal=signal, source="wait", stage=stage,
        )

    prev = meta or {}
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
        log_path=str(log_path),
        log_checked={"size": log_path.stat().st_size, "signature": blocked_signature(signal)},
    )
    if save_meta:
        save_session_meta(workdir, new_meta)
    print(f"[{status}] {tool}:{model} rc={rc} ({latency_ms} ms)")
    return AgentRunResult(rc, output, latency_ms, status, signal), new_meta


def escalate_session(
    *,
    workdir: Path,
    meta: dict[str, Any],
    tool: str,
    model: str,
    note: str = "",
    timeout: float | None,
    trigger: str = "manual",
) -> dict[str, Any] | None:
    """Ask `tool:model` for a decision on a (blocked) session; store it in meta["decision"].

    Returns the plan dict, or None when the escalation model itself failed.
    """
    session_id = str(meta.get("session_id", ""))
    prompt = build_escalation_prompt(meta, note)
    print(f"[escalating] {session_id} -> {tool}:{model} (trigger={trigger})")
    rc, output, latency_ms = run_agent_headless_once(tool, model, prompt, workdir, timeout=timeout)
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
            "tool": tool,
            "model": model,
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

    meta["decision"] = {**plan, "tool": tool, "model": model, "ts": now_iso(), "valid": valid}
    meta["status"] = "decided"
    meta["updated_at"] = now_iso()
    append_history(meta, "escalated", tool=tool, model=model, trigger=trigger, plan_valid=valid)
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
    stage: str = "single",
    split: bool = False,
    win_name: str = "",
    pane_title: str = "",
) -> AgentRunResult | None:
    """Launch an implementer: tmux (logged) by default, or synchronously with `wait`.

    `prompt` must not contain HEADLESS_BLOCKED_INSTRUCTION; it is appended here for `wait`
    runs. `meta` must already be saved for the session. With `wait` (headless only), a run that
    ends waiting for a decision is escalated to STAGE_DEFAULTS["escalate"] and re-run with
    the decision, at most AUTO_ESCALATE_MAX_ROUNDS times. Raises AiRouteError when the
    final status is not `completed`.

    `workdir` is resolved to an absolute, existing directory first, so an invalid one fails
    before any session log, metadata or prompt file is written.
    """
    workdir = resolve_workdir(workdir)
    if not wait:
        log_path = begin_session_log(session_id, tool, model)
        meta["log_path"] = str(log_path)
        save_session_meta(workdir, meta)
        prompt_file = write_prompt_file(prompt)
        shell_cmd = build_agent_shell_cmd(tool, model, prompt_file, mode, log_path=log_path)
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
        print(f"[log] {log_path}  (check with: ai-route status --from-session {session_id})")
        return None

    if mode != "headless":
        raise AiRouteError("--wait requires --mode headless.")

    rounds = 0
    current_prompt = append_headless_instruction(prompt)
    while True:
        result, meta = run_and_record(
            tool=tool, model=model, prompt=current_prompt, workdir=workdir,
            session_id=session_id, meta=meta, timeout=timeout, stage=stage,
        )
        if result.status != "blocked" or not auto_escalate or rounds >= AUTO_ESCALATE_MAX_ROUNDS:
            break
        rounds += 1
        esc_tool, esc_model = resolve_spec(STAGE_DEFAULTS["escalate"], "high")
        plan = escalate_session(
            workdir=workdir, meta=meta, tool=esc_tool, model=esc_model,
            timeout=timeout, trigger="auto",
        )
        if plan is None:
            break
        current_prompt = append_headless_instruction(build_resume_prompt(prompt, plan))

    print("\n" + (result.output or "(no output)"))
    if result.status == "completed":
        return result

    hint = f"ai-route status --from-session {session_id}"
    if result.status == "blocked":
        question = result.signal.question if result.signal else ""
        raise AiRouteError(
            f"Agent is waiting for a decision: {question or '(see output)'}\n"
            f"  Next: ai-route escalate --from-session {session_id} --then-implement  "
            f"or  ai-route handoff --from-session {session_id}"
        )
    raise AiRouteError(f"Agent run ended with status '{result.status}' (rc={result.rc}). See: {hint}")
