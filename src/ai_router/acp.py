"""Agent Client Protocol (ACP) JSON-RPC 2.0 stdio client and interactive bridge.

Used to run `agy` exclusively via `agy acp --stdio` (and extensible to any ACP-compliant
agent CLI such as `copilot --acp`). Implemented with Python standard library only.
"""

from __future__ import annotations

import json
import os
import select
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

from ai_router.security import is_dangerous_acp_action, sanitize_terminal_text

# Shell-style return codes (shared with dispatch.py)
RC_COMMAND_NOT_FOUND = 127
RC_TIMEOUT = 124

# Mapping from ai-router effort levels to ACP thought_level values
_ACP_THOUGHT_LEVEL_MAP: dict[str, str] = {
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "xhigh",
    "max": "xhigh",
}

_EFFORT_SUFFIXES: tuple[str, ...] = ("-xhigh", "-high", "-medium", "-low", "-max")


def resolve_acp_model_and_thought(
    model: str,
    effort: str,
    session_info: dict[str, Any],
) -> tuple[str, str]:
    """Resolve the best `(model_id, thought_level)` against ACP `session/new` metadata.

    Handles both compound model IDs (e.g. `gemini-4-neon-high`) and base model IDs +
    `thought_level` config options (e.g. `gemini-4-neon` + `xhigh`).
    """
    available_models = [
        str(m.get("modelId", ""))
        for m in ((session_info.get("models") or {}).get("availableModels") or [])
        if isinstance(m, dict) and m.get("modelId")
    ]
    config_options = session_info.get("configOptions") or []
    thought_opt = next(
        (
            opt
            for opt in config_options
            if isinstance(opt, dict) and (opt.get("id") == "thought_level" or opt.get("category") == "thought_level")
        ),
        None,
    )
    allowed_thoughts = {
        str(o.get("value", "")).lower()
        for o in (thought_opt.get("options") or [])
        if isinstance(o, dict) and o.get("value")
    } if thought_opt else set()

    mapped_thought = _ACP_THOUGHT_LEVEL_MAP.get(effort.lower(), "") if effort else ""
    if mapped_thought and allowed_thoughts and mapped_thought not in allowed_thoughts:
        # Clamp to highest available if e.g. xhigh isn't supported for this model
        for fallback_level in ("high", "medium", "low"):
            if fallback_level in allowed_thoughts:
                mapped_thought = fallback_level
                break
        else:
            mapped_thought = ""

    chosen_model = model
    if available_models:
        base_model = model
        for suf in _EFFORT_SUFFIXES:
            if model.endswith(suf):
                base_model = model[: -len(suf)]
                break

        if mapped_thought:
            candidate_with_effort = f"{base_model}-{mapped_thought}"
            if candidate_with_effort in available_models:
                chosen_model = candidate_with_effort
            elif model in available_models:
                chosen_model = model
        elif model in available_models:
            chosen_model = model

    return chosen_model, mapped_thought


def _extract_update_text(update: dict[str, Any]) -> str:
    """Extract displayable text from a `session/update` payload."""
    kind = str(update.get("sessionUpdate") or update.get("type") or "")
    if kind in {"agent_message_chunk", "agent_message", "text", "message"}:
        content = update.get("content")
        if isinstance(content, dict):
            if content.get("type", "text") == "text":
                return str(content.get("text") or "")
        elif isinstance(content, list):
            parts = [
                str(block.get("text") or "")
                for block in content
                if isinstance(block, dict) and block.get("type", "text") == "text"
            ]
            return "".join(parts)
        elif isinstance(content, str):
            return content
        if "text" in update:
            return str(update.get("text") or "")
    return ""


def _pick_permission_option(
    params: dict[str, Any],
    *,
    allow: bool,
) -> dict[str, Any]:
    """Select an appropriate `session/request_permission` outcome from server options."""
    options = params.get("options") or []
    preferred_kinds = (
        ("allow_once", "allow_always", "allow")
        if allow
        else ("reject_once", "reject_always", "reject", "deny")
    )
    for target in preferred_kinds:
        for opt in options:
            if not isinstance(opt, dict):
                continue
            kind = str(opt.get("kind") or "").lower()
            opt_id = str(opt.get("optionId") or opt.get("id") or "")
            if opt_id and (kind == target or target in kind or target in opt_id.lower()):
                return {"outcome": {"outcome": "selected", "optionId": opt_id}}
    if allow and options and isinstance(options[0], dict):
        opt_id = str(options[0].get("optionId") or options[0].get("id") or "")
        if opt_id:
            return {"outcome": {"outcome": "selected", "optionId": opt_id}}
    return {"outcome": {"outcome": "cancelled"}}


def _auto_permission_handler(params: dict[str, Any], *, allow: bool) -> dict[str, Any]:
    """Automatically approve/reject ACP permissions while blocking destructive shell actions."""
    if allow:
        danger = is_dangerous_acp_action(params)
        if danger:
            print(
                f"[warn] ACP blocked dangerous action in auto mode: {sanitize_terminal_text(danger)}",
                file=sys.stderr,
                flush=True,
            )
            return _pick_permission_option(params, allow=False)
    return _pick_permission_option(params, allow=allow)


def _interactive_permission_handler(params: dict[str, Any]) -> dict[str, Any]:
    """Prompt the user in the terminal to approve/reject an ACP permission request."""
    tool_call = params.get("toolCall") or {}
    title = sanitize_terminal_text(str(tool_call.get("title") or params.get("title") or "Tool action"))
    options = [o for o in (params.get("options") or []) if isinstance(o, dict)]
    print(f"\n[acp permission] {title}", flush=True)
    for idx, opt in enumerate(options, 1):
        name = sanitize_terminal_text(
            str(opt.get("name") or opt.get("optionId") or opt.get("kind") or f"option-{idx}")
        )
        print(f"  {idx}. {name}", flush=True)
    if not options:
        return {"outcome": {"outcome": "cancelled"}}
    try:
        raw = input("Approve? [1/Enter=allow, n=reject]: ").strip().lower()
    except EOFError:
        return _pick_permission_option(params, allow=False)
    if raw in {"", "y", "yes"}:
        return _pick_permission_option(params, allow=True)
    if raw in {"n", "no", "c", "cancel"}:
        return _pick_permission_option(params, allow=False)
    if raw.isdigit():
        idx = int(raw) - 1
        if 0 <= idx < len(options):
            opt_id = str(options[idx].get("optionId") or options[idx].get("id") or "")
            if opt_id:
                return {"outcome": {"outcome": "selected", "optionId": opt_id}}
    return _pick_permission_option(params, allow=False)


class ACPTimeoutError(TimeoutError):
    """Raised when an ACP RPC call or prompt turn exceeds its deadline."""


class ACPClient:
    """Minimal JSON-RPC 2.0 over stdio client for ACP servers (e.g. `agy acp --stdio`)."""

    def __init__(self, cmd: list[str], cwd: Path) -> None:
        self.cmd = list(cmd)
        self.cwd = cwd
        self._proc: subprocess.Popen[str] | None = None
        self._next_id = 1
        self._stderr_chunks: list[str] = []

    def start(self) -> None:
        self._proc = subprocess.Popen(
            self.cmd,
            cwd=str(self.cwd),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )

    def close(self) -> str:
        """Terminate the ACP subprocess cleanly and return any captured stderr text."""
        if self._proc is None:
            return ""
        proc = self._proc
        self._proc = None
        try:
            if proc.stdin and not proc.stdin.closed:
                proc.stdin.close()
        except OSError:
            pass
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    pass
        stderr_out = ""
        try:
            if proc.stderr and not proc.stderr.closed:
                stderr_out = proc.stderr.read()
        except OSError:
            pass
        return stderr_out.strip()

    def __enter__(self) -> ACPClient:
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _send(self, payload: dict[str, Any]) -> None:
        if self._proc is None or self._proc.stdin is None:
            raise RuntimeError("ACP subprocess is not running (stdin unavailable)")
        self._proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
        self._proc.stdin.flush()

    def _readline(self, deadline: float | None) -> str | None:
        if self._proc is None or self._proc.stdout is None:
            raise RuntimeError("ACP subprocess is not running (stdout unavailable)")
        stdout = self._proc.stdout
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ACPTimeoutError("ACP request timed out")
            try:
                ready, _, _ = select.select([stdout], [], [], remaining)
                if not ready:
                    raise ACPTimeoutError("ACP request timed out")
            except (ValueError, OSError):
                # Fallback if stdout is not selectable
                pass
        line = stdout.readline()
        return line if line else None

    def request(
        self,
        method: str,
        params: dict[str, Any],
        *,
        timeout: float | None = None,
        on_update: Callable[[dict[str, Any]], None] | None = None,
        on_permission: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Send a JSON-RPC request and wait for its matching response, handling notifications."""
        req_id = self._next_id
        self._next_id += 1
        self._send({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params})
        deadline = (time.monotonic() + timeout) if timeout is not None and timeout > 0 else None

        while True:
            raw = self._readline(deadline)
            if raw is None:
                rc = self._proc.poll() if self._proc else None
                raise RuntimeError(f"ACP server closed stdout unexpectedly while awaiting {method} (rc={rc})")
            stripped = raw.strip()
            if not stripped:
                continue
            try:
                msg = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if not isinstance(msg, dict):
                continue

            # Matching response to our request
            if msg.get("id") == req_id and ("result" in msg or "error" in msg):
                return msg

            # Server-initiated notification or request
            srv_method = msg.get("method")
            srv_params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
            if srv_method == "session/update" and on_update is not None:
                on_update(srv_params)
            elif srv_method == "session/request_permission" and "id" in msg:
                result = (
                    on_permission(srv_params)
                    if on_permission is not None
                    else _auto_permission_handler(srv_params, allow=True)
                )
                self._send({"jsonrpc": "2.0", "id": msg["id"], "result": result})
            elif srv_method and "id" in msg:
                # Unknown server-to-client RPC request: respond with method-not-found so server doesn't stall
                self._send(
                    {
                        "jsonrpc": "2.0",
                        "id": msg["id"],
                        "error": {"code": -32601, "message": f"Method not supported by client: {srv_method}"},
                    }
                )

    def initialize(self, *, timeout: float | None = 30.0) -> dict[str, Any]:
        resp = self.request(
            "initialize",
            {
                "protocolVersion": 1,
                "clientInfo": {"name": "ai-router", "version": "0.1.0"},
                "clientCapabilities": {},
            },
            timeout=timeout,
        )
        if "error" in resp:
            raise RuntimeError(_format_rpc_error(resp["error"]))
        return resp.get("result") or {}

    def open_session(
        self,
        *,
        resume_id: str = "",
        timeout: float | None = 30.0,
    ) -> dict[str, Any]:
        if resume_id:
            for method in ("session/resume", "session/load"):
                resp = self.request(
                    method,
                    {"sessionId": resume_id, "cwd": str(self.cwd), "mcpServers": []},
                    timeout=timeout,
                )
                if "result" in resp:
                    res = dict(resp.get("result") or {})
                    res.setdefault("sessionId", resume_id)
                    return res
        resp = self.request(
            "session/new",
            {"cwd": str(self.cwd), "mcpServers": []},
            timeout=timeout,
        )
        if "error" in resp:
            raise RuntimeError(_format_rpc_error(resp["error"]))
        return resp.get("result") or {}

    def configure_session(
        self,
        session_id: str,
        *,
        model: str = "",
        effort: str = "",
        acp_mode: str = "auto",
        session_info: dict[str, Any] | None = None,
        timeout: float | None = 15.0,
    ) -> None:
        info = session_info or {}
        if acp_mode:
            available_modes = {
                str(m.get("id", ""))
                for m in ((info.get("modes") or {}).get("availableModes") or [])
                if isinstance(m, dict)
            }
            if not available_modes or acp_mode in available_modes:
                self.request(
                    "session/set_mode",
                    {"sessionId": session_id, "modeId": acp_mode},
                    timeout=timeout,
                )

        chosen_model, thought_level = resolve_acp_model_and_thought(model, effort, info)
        if chosen_model:
            self.request(
                "session/set_model",
                {"sessionId": session_id, "modelId": chosen_model},
                timeout=timeout,
            )
        if thought_level:
            self.request(
                "session/set_config_option",
                {"sessionId": session_id, "configId": "thought_level", "value": thought_level},
                timeout=timeout,
            )

    def send_prompt(
        self,
        session_id: str,
        text: str,
        *,
        timeout: float | None = None,
        on_chunk: Callable[[str], None] | None = None,
        on_permission: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    ) -> tuple[int, str]:
        """Send a prompt turn and return `(returncode, aggregated_text)`."""
        chunks: list[str] = []

        def handle_update(params: dict[str, Any]) -> None:
            upd = params.get("update") if isinstance(params.get("update"), dict) else params
            piece = _extract_update_text(upd)
            if piece:
                chunks.append(piece)
                if on_chunk is not None:
                    on_chunk(piece)

        resp = self.request(
            "session/prompt",
            {
                "sessionId": session_id,
                "prompt": [{"type": "text", "text": text}],
            },
            timeout=timeout,
            on_update=handle_update,
            on_permission=on_permission,
        )
        output = "".join(chunks).strip()
        if "error" in resp:
            err_msg = _format_rpc_error(resp["error"])
            combined = f"{output}\n[error] {err_msg}".strip() if output else f"[error] {err_msg}"
            return 1, combined
        return 0, output


def _format_rpc_error(err: Any) -> str:
    if isinstance(err, dict):
        code = err.get("code")
        msg = err.get("message") or str(err)
        data = err.get("data")
        detail = f"{msg} ({data})" if data else str(msg)
        return f"ACP error {code}: {detail}" if code is not None else detail
    return str(err)


def run_acp_headless_once(
    acp_cmd: list[str],
    model: str,
    prompt: str,
    cwd: Path,
    *,
    timeout: float | None = None,
    effort: str = "",
    acp_mode: str = "auto",
    read_only: bool = False,
    resume_session_id: str = "",
) -> tuple[int, str, int]:
    """Execute a single prompt non-interactively over ACP stdio and return `(rc, output, latency_ms)`."""
    effective_mode = "plan" if read_only else acp_mode
    allow_perms = not read_only

    def perm_handler(params: dict[str, Any]) -> dict[str, Any]:
        return _auto_permission_handler(params, allow=allow_perms)

    t0 = time.monotonic()
    client = ACPClient(acp_cmd, cwd=cwd)
    try:
        client.start()
        init_timeout = min(timeout, 30.0) if timeout is not None and timeout > 0 else 30.0
        client.initialize(timeout=init_timeout)
        session_info = client.open_session(resume_id=resume_session_id, timeout=init_timeout)
        sid = str(session_info.get("sessionId") or "")
        if not sid:
            raise RuntimeError("ACP session/new did not return a sessionId")
        client.configure_session(
            sid,
            model=model,
            effort=effort,
            acp_mode=effective_mode,
            session_info=session_info,
            timeout=init_timeout,
        )
        remaining = None
        if timeout is not None and timeout > 0:
            remaining = max(1.0, timeout - (time.monotonic() - t0))
        rc, output = client.send_prompt(sid, prompt, timeout=remaining, on_permission=perm_handler)
    except ACPTimeoutError:
        stderr_out = client.close()
        latency_ms = int((time.monotonic() - t0) * 1000)
        t_label = f"{timeout:g}s" if timeout else "?"
        msg = f"[error] ACP timed out after {t_label}: {acp_cmd[0]}:{model}"
        if stderr_out:
            msg = f"{msg}\n[stderr]\n{stderr_out}"
        return RC_TIMEOUT, msg, latency_ms
    except OSError as e:
        client.close()
        latency_ms = int((time.monotonic() - t0) * 1000)
        return RC_COMMAND_NOT_FOUND, f"[error] Failed to launch '{acp_cmd[0]}': {e}", latency_ms
    except Exception as e:  # noqa: BLE001
        stderr_out = client.close()
        latency_ms = int((time.monotonic() - t0) * 1000)
        msg = f"[error] ACP failure ({acp_cmd[0]}:{model}): {e}"
        if stderr_out:
            msg = f"{msg}\n[stderr]\n{stderr_out}"
        return 1, msg, latency_ms

    stderr_out = client.close()
    latency_ms = int((time.monotonic() - t0) * 1000)
    if rc != 0 and stderr_out:
        output = f"{output}\n[stderr]\n{stderr_out}".strip()
    return rc, output, latency_ms


def run_acp_interactive_session(
    *,
    acp_cmd: list[str],
    model: str,
    prompt_file: Path,
    mode: str = "interactive",
    effort: str = "",
    acp_mode: str = "auto",
    resume_session_id: str = "",
    cwd: Path | None = None,
) -> int:
    """Run an ACP session in the current terminal/tmux pane, streaming output.

    In `headless` mode, runs the initial prompt and exits with the turn's return code.
    In `interactive` mode, streams the initial turn and then enters a lightweight REPL
    on the same ACP session so the user can continue the conversation until EOF / `/exit`.
    """
    workdir = cwd or Path.cwd()
    prompt_text = prompt_file.read_text(encoding="utf-8")
    effective_mode = acp_mode or "auto"
    is_interactive = mode == "interactive"

    def stream_chunk(chunk: str) -> None:
        sys.stdout.write(chunk)
        sys.stdout.flush()

    perm_handler = (
        _interactive_permission_handler
        if is_interactive and sys.stdin.isatty()
        else (lambda params: _auto_permission_handler(params, allow=(effective_mode != "plan")))
    )

    try:
        with ACPClient(acp_cmd, cwd=workdir) as client:
            client.initialize()
            session_info = client.open_session(resume_id=resume_session_id)
            sid = str(session_info.get("sessionId") or "")
            if not sid:
                print("[error] ACP session/new did not return a sessionId", file=sys.stderr)
                return 1
            client.configure_session(
                sid,
                model=model,
                effort=effort,
                acp_mode=effective_mode,
                session_info=session_info,
            )
            eff_tag = f", effort={effort}" if effort else ""
            print(f"[acp] Connected ({acp_cmd[0]} acp, model={model}{eff_tag}, mode={effective_mode}, session={sid})", flush=True)

            rc, output = client.send_prompt(sid, prompt_text, on_chunk=stream_chunk, on_permission=perm_handler)
            print(flush=True)
            if rc != 0:
                if output:
                    print(output, file=sys.stderr, flush=True)
                return rc

            if not is_interactive:
                return 0

            print("[acp] Type a follow-up message (or /exit / Ctrl-D to finish).", flush=True)
            while True:
                try:
                    follow_up = input("\nagy> ").strip()
                except (EOFError, KeyboardInterrupt):
                    print()
                    break
                if not follow_up:
                    continue
                if follow_up in {"/exit", "/quit", "exit", "quit"}:
                    break
                turn_rc, turn_out = client.send_prompt(
                    sid, follow_up, on_chunk=stream_chunk, on_permission=perm_handler
                )
                print(flush=True)
                if turn_rc != 0 and turn_out:
                    print(turn_out, file=sys.stderr, flush=True)
            return 0
    except OSError as e:
        print(f"[error] Failed to launch '{acp_cmd[0]}': {e}", file=sys.stderr)
        return RC_COMMAND_NOT_FOUND
    except Exception as e:  # noqa: BLE001
        print(f"[error] ACP session failed: {e}", file=sys.stderr)
        return 1
