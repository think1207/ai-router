"""Security primitives: input validation, path confinement, secret redaction, and safe permissions."""

from __future__ import annotations

import os
import re
import shlex
import sys
from pathlib import Path
from typing import Any

from ai_router.errors import AiRouteError

# Safe identifier for tool names, preset names, and session IDs (prevents shell/path injection)
_SAFE_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_SAFE_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

# Safe git ref / revision: cannot start with '-' (flag injection), no control chars, whitespace, globs, or NUL
_UNSAFE_GIT_REF_RE = re.compile(r"[\x00-\x1f\x7f\s:?*\[\\]")

# Allowed GitHub Copilot CLI permission flags (strict allowlist prevents command injection)
_ALLOWED_COPILOT_SIMPLE_FLAGS: frozenset[str] = frozenset(
    {
        "--allow-all",
        "--allow-all-tools",
        "--allow-all-paths",
        "--allow-all-urls",
        "--yolo",
        "--assisted-approval",
    }
)
_ALLOWED_COPILOT_VALUE_FLAG_PREFIXES: tuple[str, ...] = (
    "--allow-tool=",
    "--deny-tool=",
    "--allow-url=",
    "--deny-url=",
)
_SHELL_META_RE = re.compile(r"[;&|`$<>()\n\r\x00]")

# Nested quantifier heuristic to reject obvious ReDoS patterns from user env vars
_REDOS_HEURISTIC_RE = re.compile(r"\([^)]*[+*][^)]*\)[+*{]")

# Terminal ANSI escape & dangerous C0/C1 control characters (keeps \n and \t)
_ANSI_ESC_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-_]")
_CTRL_CHARS_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")

# Known secret / credential patterns redacted before persisting logs or session snapshots
_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z0-9 ]*PRIVATE KEY-----"),
        "[REDACTED_PRIVATE_KEY]",
    ),
    (re.compile(r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{20,}\b"), "[REDACTED_API_KEY]"),
    (re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b"), "[REDACTED_GITHUB_TOKEN]"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"), "[REDACTED_GITHUB_TOKEN]"),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"), "[REDACTED_GOOGLE_API_KEY]"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[REDACTED_AWS_KEY]"),
    (re.compile(r"(?i)\b(authorization\s*:\s*bearer\s+)[A-Za-z0-9._~+/=-]{16,}"), r"\1[REDACTED_TOKEN]"),
    (
        re.compile(r"(?i)\b((?:api[_-]?key|secret|token|password)\s*[=:]\s*['\"]?)[A-Za-z0-9._/+=-]{16,}['\"]?"),
        r"\1[REDACTED]",
    ),
)

# Sensitive file names warned about when passed to `-f`
_SENSITIVE_FILENAMES: frozenset[str] = frozenset(
    {
        ".env",
        ".env.local",
        ".env.production",
        ".env.development",
        ".netrc",
        ".npmrc",
        ".pypirc",
        "id_rsa",
        "id_ed25519",
        "id_ecdsa",
        "id_dsa",
        "credentials.json",
        "service-account.json",
    }
)
_SENSITIVE_SUFFIXES: tuple[str, ...] = (".pem", ".key", ".p12", ".pfx", ".keystore")

# Maximum file size read into memory for router previews or reviews (10 MiB)
MAX_READ_FILE_BYTES: int = 10 * 1024 * 1024

# Dangerous command patterns blocked even in ACP headless `auto` mode
_DANGEROUS_ACP_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"\brm\s+-(?:[a-zA-Z]*r[a-zA-Z]*f|[a-zA-Z]*f[a-zA-Z]*r)[a-zA-Z]*\s+"
        r"(?:/(?:\*|\s|$|[;/&|])|~(?:/|\s|$|[;/&|])|\$HOME(?:/|\s|$|[;/&|])|\.\.(?:/|\s|$|[;/&|]))"
    ),
    re.compile(r"\b(?:curl|wget)\b[^|\n]*\|\s*(?:ba|z|da)?sh\b", re.IGNORECASE),
    re.compile(r"\bmkfs(?:\.[a-z0-9]+)?\b"),
    re.compile(r"\bdd\s+if="),
    re.compile(r":\(\)\s*\{\s*:\|:\s*&\s*\}\s*;:"),
    re.compile(r"\bgit\s+push\b[^\n]*(?:--force(?:-with-lease)?|\s-f\b)"),
    re.compile(r"\bchmod\s+(?:-R\s+)?777\b"),
    re.compile(r"\bsudo\s+"),
)


def validate_safe_identifier(value: str, label: str) -> str:
    """Validate that `value` is a safe identifier (alphanumeric + `._-`, no leading punctuation)."""
    cleaned = (value or "").strip()
    if not cleaned or ".." in cleaned or not _SAFE_IDENTIFIER_RE.match(cleaned):
        raise ValueError(
            f"Invalid {label} {value!r}: must match [A-Za-z0-9][A-Za-z0-9._-]* (max 64 chars, no '..')"
        )
    return cleaned


def sanitize_session_id(session_id: str) -> str:
    """Validate/sanitize a session ID so it can never escape its target directory (prevents CWE-22)."""
    raw = (session_id or "").strip()
    if not raw or ".." in raw or "/" in raw or "\\" in raw or not _SAFE_SESSION_ID_RE.match(raw):
        cleaned = re.sub(r"[^A-Za-z0-9_-]+", "_", raw).strip("_")[:64]
        if not cleaned:
            raise AiRouteError(f"Invalid session ID: {session_id!r}")
        return cleaned
    return raw


def validate_git_ref(ref: str, label: str = "git ref") -> str:
    """Reject git refs that start with '-' (flag injection) or contain unsafe characters."""
    cleaned = (ref or "").strip()
    if not cleaned:
        raise AiRouteError(f"Empty {label}.")
    if cleaned.startswith("-") or _UNSAFE_GIT_REF_RE.search(cleaned):
        raise AiRouteError(f"Unsafe {label} {ref!r}: leading '-' and control/whitespace characters are not allowed.")
    return cleaned


def validate_copilot_permission_flag(raw: str) -> list[str]:
    """Parse and validate `copilot_permission_flag` against an allowlist (prevents CWE-78)."""
    text = (raw or "").strip()
    if not text:
        return []
    if _SHELL_META_RE.search(text):
        raise ValueError(f"Unsafe shell metacharacters in copilot_permission_flag: {raw!r}")
    try:
        tokens = shlex.split(text)
    except ValueError as e:
        raise ValueError(f"Invalid quoting in copilot_permission_flag {raw!r}: {e}") from e

    for tok in tokens:
        if tok in _ALLOWED_COPILOT_SIMPLE_FLAGS:
            continue
        if any(tok.startswith(pref) and len(tok) > len(pref) for pref in _ALLOWED_COPILOT_VALUE_FLAG_PREFIXES):
            continue
        raise ValueError(
            f"Disallowed token {tok!r} in copilot_permission_flag. "
            f"Allowed flags: {', '.join(sorted(_ALLOWED_COPILOT_SIMPLE_FLAGS))} "
            "or --allow-tool=/--deny-tool=/--allow-url=/--deny-url="
        )
    return tokens


def redact_secrets(text: str) -> str:
    """Mask known API keys, tokens, and private keys before storing logs or session snapshots."""
    if not text:
        return text
    out = text
    for pattern, replacement in _SECRET_PATTERNS:
        out = pattern.sub(replacement, out)
    return out


def sanitize_terminal_text(text: str) -> str:
    """Strip ANSI escape sequences and non-printable control chars before printing untrusted text."""
    if not text:
        return ""
    cleaned = _ANSI_ESC_RE.sub("", text)
    return _CTRL_CHARS_RE.sub("", cleaned)


def is_sensitive_filename(path: Path) -> bool:
    """Return True when `path` looks like a secret/credential file (e.g. `.env`, `*.pem`, `id_rsa`)."""
    name = path.name.lower()
    if name in _SENSITIVE_FILENAMES or name.startswith(".env."):
        return True
    return any(name.endswith(suf) for suf in _SENSITIVE_SUFFIXES)


def compile_safe_regex(pattern: str, *, label: str = "pattern", max_len: int = 256) -> re.Pattern[str] | None:
    """Compile a user-supplied regex while rejecting overly long or ReDoS-prone patterns."""
    if not pattern:
        return None
    if len(pattern) > max_len:
        print(f"[warn] Ignoring overly long {label} regex (> {max_len} chars).", file=sys.stderr)
        return None
    if _REDOS_HEURISTIC_RE.search(pattern):
        print(f"[warn] Ignoring potentially unsafe (ReDoS) {label} regex: {pattern!r}", file=sys.stderr)
        return None
    try:
        return re.compile(pattern, re.IGNORECASE)
    except re.error as e:
        print(f"[warn] Ignoring invalid {label} pattern {pattern!r}: {e}", file=sys.stderr)
        return None


def ensure_private_dir(path: Path) -> None:
    """Create `path` (if needed) and restrict permissions to owner-only (`0o700`)."""
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


def ensure_private_file(path: Path) -> None:
    """Restrict an existing file's permissions to owner-only (`0o600`)."""
    try:
        if path.exists() and not path.is_symlink():
            os.chmod(path, 0o600)
    except OSError:
        pass


def is_dangerous_acp_action(params: dict[str, Any]) -> str | None:
    """Return a short reason if an ACP permission request describes a destructive shell action."""
    tool_call = params.get("toolCall") if isinstance(params.get("toolCall"), dict) else {}
    haystack_parts = [
        str(params.get("title") or ""),
        str(tool_call.get("title") or ""),
        str(tool_call.get("rawInput") or ""),
        str(tool_call.get("command") or ""),
    ]
    haystack = "\n".join(p for p in haystack_parts if p)
    if not haystack:
        return None
    for pat in _DANGEROUS_ACP_PATTERNS:
        m = pat.search(haystack)
        if m:
            return m.group(0).strip()
    return None
