"""Helpers for redacting credentials before data leaves trusted runtime paths."""
from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

REDACTED = "<redacted>"

_SENSITIVE_KEYS = {
    "api_key",
    "apikey",
    "llm_api_key",
    "_resolved_api_key",
    "new_api_key",
    "access_token",
    "refresh_token",
    "id_token",
    "auth_token",
    "bearer_token",
    "secret_token",
    "token",
    "authorization",
    "auth_header",
    "client_secret",
    "oauth_client_secret",
    "app_secret",
    "signing_secret",
    "webhook_secret",
    "secret_key",
    "secret",
    "private_key",
    "key_hash",
    "password",
    "password_hash",
    "credential",
    "credentials",
    "credential_ref",
    "encrypted_credentials",
    "encrypted_blob",
    "totp_secret",
}

_SECRET_TEXT_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)([A-Za-z0-9._~+/=-]{8,})"),
        r"\1" + REDACTED,
    ),
    (
        re.compile(r"(?i)(bearer\s+)(sk-[A-Za-z0-9._-]{6,}|[A-Za-z0-9._~+/=-]{16,})"),
        r"\1" + REDACTED,
    ),
    (
        re.compile(
            r"(?i)(['\"]?(?:llm_)?api_key['\"]?\s*[:=]\s*['\"]?)([^'\"\s,}&]{6,})"
        ),
        r"\1" + REDACTED,
    ),
    (
        re.compile(r"(?i)(['\"]?_resolved_api_key['\"]?\s*[:=]\s*['\"]?)([^'\"\s,}&]{6,})"),
        r"\1" + REDACTED,
    ),
    (
        re.compile(r"(?i)([?&](?:api_key|access_token|refresh_token|token|secret)=)([^&#\s]+)"),
        r"\1" + REDACTED,
    ),
    (
        re.compile(r"\b(sk-(?:or|ant|proj|live|test)?-?[A-Za-z0-9._-]{8,})\b"),
        REDACTED,
    ),
    (
        re.compile(r"\b(ark-[A-Za-z0-9._-]{8,})\b"),
        REDACTED,
    ),
)


def is_sensitive_key(key: object) -> bool:
    """Return true when a field name is an explicit credential field.

    Keep this exact-schema based rather than substring based. A broad
    keyword matcher would incorrectly redact ordinary fields like
    ``token_count`` or ``secretary_note``.
    """
    normalized = str(key or "").strip().lower().replace("-", "_")
    return normalized in _SENSITIVE_KEYS


def redact_sensitive_text(text: str | None, *, replacement: str = REDACTED) -> str | None:
    """Redact common secret shapes inside free-form text."""
    if text is None:
        return None
    redacted = str(text)
    for pattern, repl in _SECRET_TEXT_PATTERNS:
        redacted = pattern.sub(repl.replace(REDACTED, replacement), redacted)
    return redacted


def sanitize_sensitive_payload(
    value: Any,
    *,
    replacement: str = REDACTED,
    max_depth: int = 12,
    _depth: int = 0,
) -> Any:
    """Recursively redact credential-shaped fields while preserving structure."""
    if _depth > max_depth:
        return value
    if isinstance(value, str):
        return redact_sensitive_text(value, replacement=replacement)
    if isinstance(value, bytes):
        return value
    if isinstance(value, Mapping):
        sanitized: dict[Any, Any] = {}
        for key, item in value.items():
            if is_sensitive_key(key):
                sanitized[key] = replacement
            else:
                sanitized[key] = sanitize_sensitive_payload(
                    item,
                    replacement=replacement,
                    max_depth=max_depth,
                    _depth=_depth + 1,
                )
        return sanitized
    if isinstance(value, tuple):
        return tuple(
            sanitize_sensitive_payload(
                item,
                replacement=replacement,
                max_depth=max_depth,
                _depth=_depth + 1,
            )
            for item in value
        )
    if isinstance(value, set):
        return {
            sanitize_sensitive_payload(
                item,
                replacement=replacement,
                max_depth=max_depth,
                _depth=_depth + 1,
            )
            for item in value
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [
            sanitize_sensitive_payload(
                item,
                replacement=replacement,
                max_depth=max_depth,
                _depth=_depth + 1,
            )
            for item in value
        ]
    return value
