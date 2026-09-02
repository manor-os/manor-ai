"""
Security layer: environment variable sanitization and path validation.

Mirrors the security model from OpenClaw's sandbox:
- sanitize-env-vars.ts  → env var filtering
- validate-sandbox-security.ts → bind mount / path blocking
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

# ── Blocked env var patterns ──
# Keys matching any of these are stripped unless explicitly allowed.

BLOCKED_ENV_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"^ANTHROPIC_API_KEY$", re.I),
    re.compile(r"^OPENAI_API_KEY$", re.I),
    re.compile(r"^GEMINI_API_KEY$", re.I),
    re.compile(r"^OPENROUTER_API_KEY$", re.I),
    re.compile(r"^AWS_(SECRET_ACCESS_KEY|SECRET_KEY|SESSION_TOKEN)$", re.I),
    re.compile(r"^(GH|GITHUB)_TOKEN$", re.I),
    re.compile(r"^TELEGRAM_BOT_TOKEN$", re.I),
    re.compile(r"^DISCORD_BOT_TOKEN$", re.I),
    re.compile(r"^SLACK_(BOT|APP)_TOKEN$", re.I),
    re.compile(r"_?(API_KEY|TOKEN|PASSWORD|PRIVATE_KEY|SECRET)$", re.I),
]

# Keys that always pass through regardless of pattern match.
SAFE_PASSTHROUGH_KEYS: set[str] = {
    "LANG", "LC_ALL", "LC_CTYPE", "TZ", "HOME", "USER", "PATH",
    "SHELL", "TERM", "NODE_ENV", "PYTHONDONTWRITEBYTECODE",
    "PYTHONUNBUFFERED", "PIP_NO_CACHE_DIR",
}

# Sandbox execution events cross the untrusted process/runtime boundary. Keep
# their secret vocabulary and free-text detection here so both the service and
# the injected bridge enforce the same contract. ``credential_ref`` is
# intentionally absent: it is the opaque reference that need_credential
# responses are allowed to carry.
EVENT_SENSITIVE_KEYS: frozenset[str] = frozenset(
    {
        "api_key",
        "apikey",
        "x_api_key",
        "xapikey",
        "llm_api_key",
        "_resolved_api_key",
        "new_api_key",
        "access_token",
        "refresh_token",
        "id_token",
        "auth_token",
        "api_token",
        "bearer_token",
        "secret_token",
        "token",
        "authorization",
        "proxy_authorization",
        "auth_header",
        "client_secret",
        "oauth_client_secret",
        "app_secret",
        "signing_secret",
        "webhook_secret",
        "secret_key",
        "secret",
        "private_key",
        "cookie",
        "cookies",
        "set_cookie",
        "session_cookie",
        "key_hash",
        "password",
        "password_hash",
        "credential",
        "credentials",
        "credential_value",
        "encrypted_credentials",
        "encrypted_blob",
        "totp_secret",
    }
)

EVENT_SECRET_TEXT_PATTERNS: tuple[str, ...] = (
    (
        r"(?is)-----BEGIN (?:RSA |EC |OPENSSH |ENCRYPTED )?PRIVATE KEY-----.*?"
        r"-----END (?:RSA |EC |OPENSSH |ENCRYPTED )?PRIVATE KEY-----"
    ),
    r"(?i)proxy[-_]authorization\s*[:=]\s*(?:(?:bearer|basic|token)\s+)?[^\s,;]+",
    r"(?i)authorization\s*[:=]\s*(?:(?:bearer|basic|token)\s+)?[^\s,;]+",
    r"(?i)x[-_]?api[-_]?key\s*[:=]\s*[^\s,;]+",
    (
        r'''(?i)['"]?(?:password|client[-_]?secret|access[-_]?token|'''
        r'''refresh[-_]?token|id[-_]?token|auth[-_]?token|api[-_]?token)'''
        r'''['"]?\s*[:=]\s*['"]?[^'"\s,}&;]+'''
    ),
    r"(?i)(?:set[-_])?cookie\s*[:=]\s*[^\r\n]+",
    r"(?i)bearer\s+(?:sk-[A-Za-z0-9._-]{6,}|[A-Za-z0-9._~+/=-]{16,})",
    r'''(?i)['"]?(?:llm_)?api_key['"]?\s*[:=]\s*['"]?[^'"\s,}&]{6,}''',
    (
        r"(?i)(?:^|[?&;,\s])(?:api[-_]?key|access[-_]?token|"
        r"refresh[-_]?token|token|secret)\s*=\s*[^&#\s,;]+"
    ),
    r"\bsk-(?:or|ant|proj|live|test)?-?[A-Za-z0-9._-]{8,}\b",
    r"\bark-[A-Za-z0-9._-]{8,}\b",
)

_EVENT_SECRET_TEXT_REGEXES = tuple(
    re.compile(pattern) for pattern in EVENT_SECRET_TEXT_PATTERNS
)


def normalize_event_key(key: object) -> str:
    """Normalize common JSON key spellings without broad substring matching."""

    value = str(key or "").strip().replace("-", "_")
    value = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", value)
    return value.casefold()


def contains_sensitive_event_key(value: object) -> bool:
    """Return true when a nested event payload contains a secret-shaped key."""

    if isinstance(value, dict):
        return any(
            normalize_event_key(key) in EVENT_SENSITIVE_KEYS
            or contains_sensitive_event_key(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(contains_sensitive_event_key(item) for item in value)
    return False


def contains_sensitive_event_text(value: object) -> bool:
    """Detect common plaintext-secret shapes in nested event text values."""

    if isinstance(value, str):
        return any(pattern.search(value) for pattern in _EVENT_SECRET_TEXT_REGEXES)
    if isinstance(value, dict):
        return any(contains_sensitive_event_text(item) for item in value.values())
    if isinstance(value, list):
        return any(contains_sensitive_event_text(item) for item in value)
    return False

# ── Blocked host paths ──

BLOCKED_HOST_PATHS: list[str] = [
    "/etc",
    "/private/etc",
    "/proc",
    "/sys",
    "/dev",
    "/root",
    "/boot",
    "/run",
    "/var/run",
    "/var/run/docker.sock",
    "/private/var/run",
    "/private/var/run/docker.sock",
    "/run/docker.sock",
]


class SecurityError(Exception):
    """Raised when a security check fails."""


# ── Environment sanitization ──


def _matches_blocked(key: str) -> bool:
    return any(p.search(key) for p in BLOCKED_ENV_PATTERNS)


def _validate_env_value(value: str) -> str | None:
    """Return a warning string if value looks suspicious, else None."""
    if "\0" in value:
        return "contains null bytes"
    if len(value) > 32768:
        return "value exceeds 32 KiB"
    if re.fullmatch(r"[A-Za-z0-9+/=]{80,}", value):
        return "looks like base64 credential data"
    return None


def sanitize_env_vars(
    env: dict[str, str],
    allowed_sensitive: set[str] | None = None,
) -> tuple[dict[str, str], list[str]]:
    """
    Filter environment variables for sandbox injection.

    Returns (safe_env, blocked_keys).

    - Keys in SAFE_PASSTHROUGH_KEYS always pass.
    - Keys matching BLOCKED_ENV_PATTERNS are blocked unless listed in
      ``allowed_sensitive`` (used for skill-declared API keys).
    - Values with null bytes or excessive length are always blocked.
    """
    allowed_sensitive = allowed_sensitive or set()
    safe: dict[str, str] = {}
    blocked: list[str] = []

    for raw_key, value in env.items():
        key = raw_key.strip()
        if not key:
            continue

        # Always-safe keys
        if key in SAFE_PASSTHROUGH_KEYS:
            safe[key] = value
            continue

        # Blocked patterns (unless explicitly allowed)
        if _matches_blocked(key) and key not in allowed_sensitive:
            blocked.append(key)
            logger.info("Blocked env var: %s (matches sensitive pattern)", key)
            continue

        # Value-level checks (hard block)
        warning = _validate_env_value(value)
        if warning == "contains null bytes":
            blocked.append(key)
            continue
        if warning:
            logger.warning("Suspicious env var %s: %s", key, warning)

        safe[key] = value

    return safe, blocked


# ── Path validation ──


def validate_host_path(source: str) -> None:
    """
    Validate that a host path is safe to mount into a sandbox container.
    Raises SecurityError if the path targets a dangerous location.
    """
    normalized = str(Path(source).resolve())

    if normalized == "/":
        raise SecurityError(
            "Mounting the root filesystem into a sandbox is not allowed."
        )

    for blocked in BLOCKED_HOST_PATHS:
        if normalized == blocked or normalized.startswith(blocked + "/"):
            raise SecurityError(
                f"Mounting '{blocked}' (or children) into a sandbox is not allowed. "
                f"Source path resolves to: {normalized}"
            )


def validate_container_path(path: str) -> None:
    """Block container paths that could shadow critical mounts."""
    normalized = path.rstrip("/") or "/"
    reserved = {"/proc", "/sys", "/dev", "/etc"}
    for r in reserved:
        if normalized == r or normalized.startswith(r + "/"):
            raise SecurityError(
                f"Container path '{path}' targets reserved path '{r}'."
            )
