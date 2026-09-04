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
