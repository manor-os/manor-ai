"""Credential-safe logging shared by API and worker process entrypoints."""

from __future__ import annotations

import logging
import re


_SENSITIVE_QUERY_RE = re.compile(
    r"([?&](?:token|access_token|refresh_token|id_token|api_?key|client_secret|password|code)=)"
    r"[^&\s\"']+",
    re.IGNORECASE,
)
_SENSITIVE_LOG_VALUE_RE = re.compile(
    r"(?i)\b(verification[_ -]?code|password[_ -]?reset[_ -]?token|"
    r"access[_ -]?token|refresh[_ -]?token|client[_ -]?secret|api[_ -]?key)"
    r"(\s*(?:for\s+[^\s:]+)?\s*[:=]\s*)([^\s,;&\"']+)"
)


def redact_sensitive_log_text(value: str) -> str:
    """Remove credentials and verification material before log emission."""
    redacted = _SENSITIVE_QUERY_RE.sub(r"\1<redacted>", value)
    return _SENSITIVE_LOG_VALUE_RE.sub(r"\1\2<redacted>", redacted)


class SensitiveQueryStringFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if record.name == "uvicorn.access" and isinstance(record.args, tuple) and len(record.args) == 5:
            # AccessFormatter unpacks these arguments after filters run. Preserve
            # their shape while still removing credentials from the request path.
            record.msg = redact_sensitive_log_text(str(record.msg))
            record.args = tuple(
                redact_sensitive_log_text(value) if isinstance(value, str) else value
                for value in record.args
            )
            return True

        # HTTPX passes a URL object, and callers may split a key and value
        # across format arguments. Redact the rendered message, not just str args.
        if record.name == "uvicorn.access" and isinstance(record.args, tuple) and len(record.args) == 5:
            record.args = tuple(
                redact_sensitive_log_text(value) if isinstance(value, str) else value for value in record.args
            )
        else:
            record.msg = redact_sensitive_log_text(record.getMessage())
            record.args = ()
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = redact_sensitive_log_text(record.exc_text)
        if record.stack_info:
            record.stack_info = redact_sensitive_log_text(record.stack_info)
        return True


def install_sensitive_log_filter(logger: logging.Logger | None = None) -> None:
    targets = [logger or logging.getLogger()]
    targets.extend(logging.getLogger(name) for name in ("uvicorn.access", "uvicorn.error", "apps.api.middleware_core"))
    for target in targets:
        for sink in (target, *target.handlers):
            if not any(isinstance(item, SensitiveQueryStringFilter) for item in sink.filters):
                sink.addFilter(SensitiveQueryStringFilter())
