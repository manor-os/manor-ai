"""Manor PMS REST bridge using an actor-authorized management access token.

The builtin dispatcher supplies the selected connection's credentials as a
JSON bearer bundle (access_token, optional base_url), or a plain access token.
PMS derives tenant and management permissions from that token. Runtime context
is required, but is never forwarded as an alternative authentication identity.
PMS_API_URL is a trusted deployment override; connection URLs use public HTTPS
validation and DNS-pinned transport. Calls are never retried automatically.
Cleaning record tools expose cleaning_task_id in place of PMS task_id,
which is reserved for the Manor runtime's own Task context.
Cleaning assignment similarly exposes assigned_to_user_id for the PMS assignee.
"""
from __future__ import annotations

import contextvars
import copy
import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx
from jsonschema import Draft202012Validator, FormatChecker

from packages.core.services.dashboard_http import (
    DashboardHttpError,
    create_public_https_transport,
    resolve_public_https_target,
)

_DEFAULT_URL = "https://portfolio.manorai.xyz/api/mcp"
_TOOLS = {
    tool["name"]: tool
    for tool in json.loads(Path(__file__).with_name("manor_pms_tools.json").read_text())["tools"]
}
_call_context: contextvars.ContextVar[dict[str, Any]] = contextvars.ContextVar(
    "manor_pms_call_context", default={},
)


def set_call_context(context: dict[str, Any]) -> None:
    _call_context.set(dict(context or {}))


def clear_call_context() -> None:
    _call_context.set({})


def list_tools() -> list[dict[str, Any]]:
    """Return the shipped PMS management contract without requiring credentials."""
    return copy.deepcopy(list(_TOOLS.values()))


def _error(message: str, *, code: str = "pms_error", status_code: int | None = None) -> dict:
    error: dict[str, Any] = {"message": message, "code": code}
    if status_code is not None:
        error["status_code"] = status_code
    return {
        "content": [{"type": "text", "text": json.dumps(error, ensure_ascii=False)}],
        "isError": True,
    }


def _credentials(raw: str) -> tuple[str, str]:
    raw = str(raw or "").strip()
    if raw.startswith(("{", "[")):
        try:
            credentials = json.loads(raw)
        except ValueError as exc:
            raise ValueError("PMS credentials are malformed; reconnect PMS.") from exc
        if not isinstance(credentials, dict):
            raise ValueError("PMS credentials must be an object.")
        token = credentials.get("access_token", "")
        base_url = credentials.get("base_url", "")
        if not isinstance(token, str) or not isinstance(base_url, str):
            raise ValueError("PMS access_token and base_url must be strings.")
        return token.strip(), base_url.strip()
    return raw, ""


def _mcp_url(base_url: str) -> str:
    parsed = urlsplit(base_url.strip())
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("PMS base_url must be an HTTP(S) service URL without credentials, query or fragment.")
    # Validate malformed ports before constructing an httpx request.
    if parsed.port == 0:
        raise ValueError("PMS base_url port is invalid.")
    path = parsed.path.rstrip("/")
    if path.endswith("/api/mcp"):
        pass
    elif path.endswith("/api"):
        path += "/mcp"
    else:
        path += "/api/mcp"
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


class _ConnectionError(ValueError):
    def __init__(self, message: str, code: str):
        super().__init__(message)
        self.code = code


async def _resolve_connection(raw: str, context: dict | None = None) -> tuple[str, str, Any]:
    """Use the identical credential and endpoint boundary for probe and call."""
    try:
        token, configured_url = _credentials(raw)
        if not token:
            raise _ConnectionError("Connect PMS with a management access token first.", "missing_credentials")
        if not token.isascii() or any(char.isspace() for char in token):
            raise _ConnectionError("PMS management access token is malformed.", "invalid_credentials")
        connection_url = configured_url or str((context or {}).get("base_url") or "").strip()
        base_url = _mcp_url(connection_url or os.getenv("PMS_API_URL", "").strip() or _DEFAULT_URL)
        transport = None
        if connection_url:
            target = await resolve_public_https_target(base_url)
            base_url = target.url.rstrip("/")
            transport = create_public_https_transport(target)
        return token, base_url, transport
    except _ConnectionError:
        raise
    except (ValueError, DashboardHttpError) as exc:
        raise _ConnectionError("PMS connection URL or credentials are invalid; use a public HTTPS endpoint for a custom connection.", "invalid_connection") from exc


async def check_connection(credentials: dict) -> dict:
    """Verify a user-supplied management connection without reading PMS records."""
    if not isinstance(credentials, dict):
        return {"ok": False, "detail": "PMS credentials must be an object.", "reason_code": "invalid_credentials"}
    try:
        token, base_url, transport = await _resolve_connection(json.dumps(credentials))
    except _ConnectionError as exc:
        return {"ok": False, "detail": str(exc), "reason_code": exc.code}
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(15.0, connect=6.0),
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        ) as client:
            response = await client.get(
                base_url.removesuffix("/mcp") + "/auth/verify",
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            )
    except httpx.HTTPError:
        return {"ok": False, "detail": "PMS authentication endpoint is unreachable.", "reason_code": "unreachable"}
    if response.status_code in {401, 403}:
        return {"ok": False, "detail": "PMS rejected these management credentials.", "reason_code": "credentials_rejected"}
    if not 200 <= response.status_code < 300:
        return {"ok": False, "detail": f"PMS authentication endpoint returned HTTP {response.status_code}; redirects are not followed.", "reason_code": "http_error"}
    try:
        body = response.json()
    except ValueError:
        return {"ok": False, "detail": "PMS authentication endpoint returned invalid JSON.", "reason_code": "invalid_response"}
    if not isinstance(body, dict) or body.get("valid") is not True:
        return {"ok": False, "detail": "PMS did not validate these credentials.", "reason_code": "credentials_rejected"}
    if body.get("type") != "management":
        return {"ok": False, "detail": "A PMS management account is required; portal accounts cannot operate management tools.", "reason_code": "management_required"}
    entity_id = body.get("entity_id")
    if (
        isinstance(entity_id, bool)
        or not isinstance(entity_id, (str, int))
        or (isinstance(entity_id, str) and not entity_id.strip())
        or (isinstance(entity_id, int) and entity_id == 0)
    ):
        return {"ok": False, "detail": "PMS management credentials have no valid entity scope.", "reason_code": "missing_entity_scope"}
    return {"ok": True, "detail": "PMS management connection verified."}


async def call_tool(name: str, arguments: dict[str, Any], bearer_token: str) -> dict:
    tool = _TOOLS.get(name)
    if tool is None:
        return _error("Unknown PMS tool.", code="unknown_tool")
    if not isinstance(arguments, dict):
        return _error("PMS arguments must be an object.", code="invalid_arguments")
    # The exact source schema also rejects actor, entity, token and endpoint
    # overrides. Do not echo validation values, which might contain secrets.
    validator = Draft202012Validator(tool["inputSchema"], format_checker=FormatChecker())
    if next(validator.iter_errors(arguments), None) is not None:
        return _error("PMS arguments do not match the tool schema; check required fields and allowed parameters.", code="invalid_arguments")
    provider_arguments = dict(arguments)
    if "cleaning_task_id" in tool["inputSchema"]["properties"]:
        provider_arguments["task_id"] = provider_arguments.pop("cleaning_task_id")
    if name == "cleaning_assign_task":
        provider_arguments["user_id"] = provider_arguments.pop("assigned_to_user_id")
    context = _call_context.get()
    if not context.get("entity_id") or not context.get("user_id"):
        return _error("PMS calls require the current Manor user and entity context.", code="missing_actor")
    try:
        token, base_url, transport = await _resolve_connection(bearer_token, context)
    except _ConnectionError as exc:
        return _error(str(exc), code=exc.code)

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=10.0),
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        ) as client:
            response = await client.post(
                base_url + "/call",
                json={"name": name, "arguments": provider_arguments},
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            )
    except httpx.TimeoutException:
        return _error("PMS request timed out; the operation outcome is unknown. Check PMS before retrying a write.", code="timeout")
    except httpx.HTTPError:
        return _error("PMS request failed; the operation outcome may be unknown. Check PMS before retrying a write.", code="transport_error")

    status = response.status_code
    if status == 401:
        return _error("PMS rejected the management token; reconnect PMS.", code="unauthorized", status_code=status)
    if status == 403:
        return _error("The connected PMS management account cannot perform this action.", code="forbidden", status_code=status)
    if 300 <= status < 400:
        return _error("PMS redirected the request; update the connection endpoint. Redirects are not followed.", code="redirect", status_code=status)
    if status >= 400:
        return _error(f"PMS returned HTTP {status}.", code="http_error", status_code=status)
    try:
        body = response.json()
    except ValueError:
        return _error("PMS returned invalid JSON.", code="invalid_response", status_code=status)
    if not isinstance(body, dict):
        return _error("PMS returned an invalid result envelope.", code="invalid_response", status_code=status)
    provider_status = body.get("status_code")
    failed = (
        body.get("success") is not True
        or bool(body.get("error"))
        or body.get("status") in ("error", "failed")
        or (isinstance(provider_status, int) and provider_status >= 400)
    )
    if failed:
        # Do not return arbitrary upstream text: proxies can echo Authorization.
        code = body.get("code")
        safe_code = code if isinstance(code, str) and code.replace("_", "").isalnum() and len(code) <= 80 and token not in code else "provider_error"
        return _error("PMS reported that the operation failed.", code=safe_code, status_code=provider_status if isinstance(provider_status, int) else status)
    text = json.dumps(body.get("data"), ensure_ascii=False, default=str)
    return {"content": [{"type": "text", "text": text.replace(token, "[redacted]")}], "isError": False}
