from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from packages.core.services.dashboard_http import (
    DashboardHttpPolicyError,
    validate_dashboard_http_url,
)


DASHBOARD_MODULE_CONTRACT_VERSION = 2
DASHBOARD_MODULE_SOURCES = frozenset(
    {
        "tasks",
        "workspaces",
        "activity",
        "task_trends",
        "stats",
        "news",
        "stocks",
        "http_json",
        "tool",
    }
)
DASHBOARD_SAFE_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
DASHBOARD_SAFE_TOOL_NAME = re.compile(r"^[A-Za-z0-9_.:-]{2,180}$")
DASHBOARD_BLOCKED_HTML = re.compile(
    r"<(?:script|style|link|iframe|object|embed|form|meta|base)\b|"
    r"\son[a-z]+\s*=|javascript\s*:",
    flags=re.IGNORECASE,
)
DASHBOARD_BLOCKED_CSS = re.compile(
    r"@import\b|url\s*\(|</style",
    flags=re.IGNORECASE,
)
DASHBOARD_BLOCKED_JAVASCRIPT = re.compile(
    r"\b(?:fetch|XMLHttpRequest|WebSocket|EventSource|importScripts|"
    r"localStorage|sessionStorage|indexedDB|postMessage|eval)\b|"
    r"navigator\s*\.\s*sendBeacon|document\s*\.\s*cookie|"
    r"window\s*\.\s*(?:parent|top|opener)|\bimport\s*\(|"
    r"\bnew\s+Function\b|while\s*\(\s*true\s*\)|"
    r"for\s*\(\s*;\s*;|</script",
    flags=re.IGNORECASE,
)


def dashboard_module_code_hash(code: dict[str, Any]) -> str:
    payload = json.dumps(
        code,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _issue(code: str, message: str, path: str) -> dict[str, str]:
    return {"code": code, "message": message, "path": path}


def _validate_json_value(
    value: object,
    *,
    path: str,
    errors: list[dict[str, str]],
    depth: int = 0,
) -> None:
    if depth > 5:
        errors.append(_issue("tool_arguments_depth", "Tool arguments are too deeply nested.", path))
        return
    if value is None or isinstance(value, (int, float, bool)):
        return
    if isinstance(value, str):
        if len(value) > 2000:
            errors.append(_issue("tool_argument_length", "Tool argument strings may not exceed 2000 characters.", path))
        return
    if isinstance(value, list):
        if len(value) > 50:
            errors.append(_issue("tool_argument_items", "Tool argument arrays may contain at most 50 items.", path))
        for index, item in enumerate(value[:51]):
            _validate_json_value(item, path=f"{path}[{index}]", errors=errors, depth=depth + 1)
        return
    if isinstance(value, dict):
        if len(value) > 40:
            errors.append(_issue("tool_argument_keys", "Tool argument objects may contain at most 40 keys.", path))
        for key, item in list(value.items())[:41]:
            if not isinstance(key, str) or len(key) > 100:
                errors.append(_issue("tool_argument_key", "Tool argument keys must be short strings.", path))
                continue
            _validate_json_value(item, path=f"{path}.{key}", errors=errors, depth=depth + 1)
        return
    errors.append(_issue("tool_argument_type", "Tool arguments must contain JSON values.", path))


def _validate_data_requests(
    value: object,
    errors: list[dict[str, str]],
) -> int:
    if not isinstance(value, list):
        errors.append(_issue("data_requests_type", "data_requests must be an array.", "code.data_requests"))
        return 0
    if len(value) > 8:
        errors.append(_issue("data_requests_limit", "A module may declare at most 8 data requests.", "code.data_requests"))
    keys: set[str] = set()
    for index, request in enumerate(value[:9]):
        path = f"code.data_requests[{index}]"
        if not isinstance(request, dict):
            errors.append(_issue("data_request_type", "Each data request must be an object.", path))
            continue
        key = str(request.get("key") or "")
        source = str(request.get("source") or "")
        if not DASHBOARD_SAFE_IDENTIFIER.fullmatch(key):
            errors.append(_issue("data_request_key", "Data request keys must be safe identifiers.", f"{path}.key"))
        elif key in keys:
            errors.append(_issue("data_request_duplicate", "Data request keys must be unique.", f"{path}.key"))
        keys.add(key)
        if source not in DASHBOARD_MODULE_SOURCES:
            errors.append(_issue("data_request_source", "Data request source is not supported.", f"{path}.source"))
        params = request.get("params", {})
        if not isinstance(params, dict) or len(params) > 16:
            errors.append(_issue("data_request_params", "Data request params must be an object with at most 16 keys.", f"{path}.params"))
        else:
            _validate_json_value(
                params,
                path=f"{path}.params",
                errors=errors,
            )
        if source == "http_json":
            url = request.get("url")
            try:
                validate_dashboard_http_url(str(url or ""))
            except DashboardHttpPolicyError as exc:
                errors.append(
                    _issue(
                        "data_request_http_url",
                        str(exc),
                        f"{path}.url",
                    )
                )
            refresh_seconds = request.get("refresh_seconds", 300)
            if not isinstance(refresh_seconds, int) or not 30 <= refresh_seconds <= 3600:
                errors.append(
                    _issue(
                        "data_request_refresh",
                        "Public JSON refresh_seconds must be between 30 and 3600.",
                        f"{path}.refresh_seconds",
                    )
                )
            if any(request.get(name) is not None for name in ("tool_name", "tool_arguments")):
                errors.append(
                    _issue(
                        "data_request_http_scope",
                        "Tool fields cannot be used with source=http_json.",
                        path,
                    )
                )
        elif source == "tool":
            tool_name = str(request.get("tool_name") or "")
            if not DASHBOARD_SAFE_TOOL_NAME.fullmatch(tool_name):
                errors.append(_issue("data_request_tool", "Tool-backed requests require a registered tool name.", f"{path}.tool_name"))
            _validate_json_value(
                request.get("tool_arguments", {}),
                path=f"{path}.tool_arguments",
                errors=errors,
            )
            refresh_seconds = request.get("refresh_seconds", 300)
            if not isinstance(refresh_seconds, int) or not 30 <= refresh_seconds <= 3600:
                errors.append(_issue("data_request_refresh", "Tool refresh_seconds must be between 30 and 3600.", f"{path}.refresh_seconds"))
            if request.get("url") is not None:
                errors.append(
                    _issue(
                        "data_request_tool_scope",
                        "Public JSON url cannot be used with source=tool.",
                        path,
                    )
                )
        elif any(
            request.get(name) is not None
            for name in ("url", "tool_name", "tool_arguments", "refresh_seconds")
        ):
            errors.append(
                _issue(
                    "data_request_scope",
                    "Network fields require source=http_json or source=tool.",
                    path,
                )
            )
    return len(value)


def validate_dashboard_module_code(code: object) -> dict[str, Any]:
    errors: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []
    if not isinstance(code, dict):
        return {
            "valid": False,
            "errors": [_issue("code_type", "Module code must be an object.", "code")],
            "warnings": [],
            "contract_version": DASHBOARD_MODULE_CONTRACT_VERSION,
        }

    version = code.get("version")
    runtime = code.get("runtime")
    html = code.get("html")
    css = code.get("css")
    javascript = code.get("javascript")
    if version != 1:
        errors.append(_issue("version", "Module version must be 1.", "code.version"))
    if runtime != "sandboxed_html":
        errors.append(_issue("runtime", "Module runtime must be sandboxed_html.", "code.runtime"))
    for path, value, maximum in (
        ("code.html", html, 20_000),
        ("code.css", css, 30_000),
        ("code.javascript", javascript, 50_000),
    ):
        if not isinstance(value, str):
            errors.append(_issue("code_string", f"{path.rsplit('.', 1)[-1]} must be a string.", path))
        elif len(value) > maximum:
            errors.append(_issue("code_length", f"{path.rsplit('.', 1)[-1]} exceeds {maximum} characters.", path))

    if isinstance(html, str):
        if not html.strip():
            errors.append(_issue("html_empty", "Module HTML must contain a visible root structure.", "code.html"))
        if DASHBOARD_BLOCKED_HTML.search(html):
            errors.append(_issue("html_capability", "HTML contains a blocked element or inline handler.", "code.html"))
        if re.search(r"\sstyle\s*=", html, flags=re.IGNORECASE):
            warnings.append(_issue("inline_style", "Prefer module CSS over inline styles.", "code.html"))
        if re.search(r"<h1\b", html, flags=re.IGNORECASE):
            warnings.append(_issue("host_title", "The host renders the module title; use compact headings inside the module.", "code.html"))

    if isinstance(css, str):
        if DASHBOARD_BLOCKED_CSS.search(css):
            errors.append(_issue("css_capability", "CSS cannot load external resources.", "code.css"))
        if re.search(r"(?:linear|radial|conic)-gradient\s*\(", css, flags=re.IGNORECASE):
            errors.append(_issue("platform_gradient", "Platform modules do not use decorative gradients.", "code.css"))
        if re.search(r"font-size\s*:[^;}]*\b\d*\.?\d+vw\b", css, flags=re.IGNORECASE):
            errors.append(_issue("viewport_type", "Font sizes may not scale with viewport width.", "code.css"))
        if re.search(r"letter-spacing\s*:\s*-", css, flags=re.IGNORECASE):
            errors.append(_issue("negative_tracking", "Letter spacing may not be negative.", "code.css"))
        if re.search(
            r":\s*(?:#[0-9a-f]{3,8}\b|(?:rgb|hsl)a?\s*\()",
            css,
            flags=re.IGNORECASE,
        ):
            warnings.append(
                _issue(
                    "hardcoded_color",
                    "Use Manor semantic color tokens instead of hard-coded colors.",
                    "code.css",
                )
            )
        if re.search(r"box-shadow\s*:", css, flags=re.IGNORECASE):
            warnings.append(
                _issue(
                    "nested_elevation",
                    "The Dashboard host owns elevation; avoid shadows inside modules.",
                    "code.css",
                )
            )
        font_families = re.findall(
            r"font-family\s*:\s*([^;}]+)",
            css,
            flags=re.IGNORECASE,
        )
        if any(
            "--module-font" not in value and "inherit" not in value.lower()
            for value in font_families
        ):
            warnings.append(
                _issue(
                    "platform_font",
                    "Use var(--module-font) or inherit the Manor platform font.",
                    "code.css",
                )
            )
        font_sizes = [
            float(value)
            for value in re.findall(
                r"font-size\s*:\s*(\d+(?:\.\d+)?)px",
                css,
                flags=re.IGNORECASE,
            )
        ]
        if any(size > 32 for size in font_sizes):
            warnings.append(
                _issue(
                    "oversized_type",
                    "Keep module typography within the Manor type scale (32px maximum).",
                    "code.css",
                )
            )
        token_count = sum(css.count(token) for token in (
            "--module-text",
            "--module-muted",
            "--module-border",
            "--module-surface",
            "--module-row",
            "--module-accent",
        ))
        if token_count < 2:
            warnings.append(_issue("platform_tokens", "Use Manor module tokens for text, surfaces, and borders.", "code.css"))
        radii = [float(value) for value in re.findall(r"border-radius\s*:\s*(\d+(?:\.\d+)?)px", css, flags=re.IGNORECASE)]
        if any(radius > 8 for radius in radii):
            warnings.append(_issue("large_radius", "Keep framed module surfaces at 8px radius or less.", "code.css"))
        fixed_widths = [float(value) for value in re.findall(r"(?:^|[;{])\s*width\s*:\s*(\d+(?:\.\d+)?)px", css, flags=re.IGNORECASE)]
        if any(width > 320 for width in fixed_widths):
            warnings.append(_issue("fixed_width", "Prefer responsive minmax, grid, or percentage widths.", "code.css"))

    if isinstance(javascript, str):
        if len(javascript) < 20:
            errors.append(_issue("javascript_length", "Module JavaScript is too short to define a renderer.", "code.javascript"))
        if DASHBOARD_BLOCKED_JAVASCRIPT.search(javascript):
            errors.append(_issue("javascript_capability", "JavaScript uses a blocked browser capability.", "code.javascript"))
        if "window.renderDashboardModule" not in javascript:
            errors.append(_issue("renderer", "Define window.renderDashboardModule(data, context).", "code.javascript"))
        if "textContent" not in javascript:
            warnings.append(_issue("safe_text", "Render data-derived text with textContent.", "code.javascript"))

    request_count = _validate_data_requests(code.get("data_requests"), errors)
    result = {
        "valid": not errors,
        "platform_ready": not errors and not warnings,
        "errors": errors,
        "warnings": warnings,
        "contract_version": DASHBOARD_MODULE_CONTRACT_VERSION,
        "metrics": {
            "html_characters": len(html) if isinstance(html, str) else 0,
            "css_characters": len(css) if isinstance(css, str) else 0,
            "javascript_characters": len(javascript) if isinstance(javascript, str) else 0,
            "data_requests": request_count,
        },
    }
    if not errors:
        result["code_hash"] = dashboard_module_code_hash(code)
    return result
