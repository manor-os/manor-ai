"""Lark Open API tools using user OAuth or a legacy app's tenant identity.

The dispatcher passes a bounded credential bundle: a Lark user access token,
or ``{app_id, app_secret, platform: lark|feishu}`` for legacy app accounts.
Provider permissions and the selected account determine resource access.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import Any

import httpx
from jsonschema import Draft202012Validator

from packages.core.ai.mcp._http import mcp_err, mcp_ok

logger = logging.getLogger(__name__)

_BASES = {
    "lark": "https://open.larksuite.com/open-apis",
    "feishu": "https://open.feishu.cn/open-apis",
}
_TOKEN_CACHE: dict[str, tuple[str, float]] = {}
_TOKEN_ERRORS = {99991661, 99991663, 99991665, 99991668, 99991671, 99991677}
_CREDENTIAL_ERRORS = {10015, 99991543}
_PERMISSION_ERRORS = {99991672, 99991676, 99991679, 230027}
_RESULT_MAX_CHARS = 3500


class LarkError(RuntimeError):
    def __init__(self, message: str, reason_code: str = "provider_unavailable") -> None:
        super().__init__(message)
        self.reason_code = reason_code


class LarkClient:
    def __init__(self, credentials: dict[str, Any]) -> None:
        if not isinstance(credentials, dict):
            raise LarkError("Lark credentials must be an object.", "credentials_missing")
        platform = credentials.get("platform") or "lark"
        if not isinstance(platform, str) or platform not in _BASES:
            raise LarkError("Lark platform must be lark or feishu.", "configuration_invalid")
        self.base = _BASES[platform]
        self.user_token = None
        self.cache_key = ""
        if "access_token" in credentials or "token_type" in credentials:
            token = credentials.get("access_token")
            if platform != "lark" or credentials.get("token_type") != "user_access_token":
                raise LarkError("Reconnect this account through Lark OAuth.", "configuration_invalid")
            if (
                not isinstance(token, str) or not token or not token.isascii()
                or any(char.isspace() for char in token)
            ):
                raise LarkError("Lark user access token is missing; reconnect.", "credentials_missing")
            self.user_token = token
            return
        self.app_id = credentials.get("app_id")
        self.app_secret = credentials.get("app_secret")
        if not all(isinstance(v, str) and v.strip() for v in (self.app_id, self.app_secret)):
            raise LarkError(
                "Lark needs App ID and App Secret. Configure them on the Integrations page.",
                "credentials_missing",
            )
        self.app_id = self.app_id.strip()
        self.app_secret = self.app_secret.strip()
        self.cache_key = hashlib.sha256(
            json.dumps([platform, self.app_id, self.app_secret]).encode()
        ).hexdigest()

    async def get_tenant_token(self, *, force_refresh: bool = False) -> str:
        if self.user_token is not None:
            raise LarkError("A user OAuth account cannot act as the app bot.", "configuration_invalid")
        cached = _TOKEN_CACHE.get(self.cache_key)
        if not force_refresh and cached and cached[1] > time.monotonic():
            return cached[0]
        _TOKEN_CACHE.pop(self.cache_key, None)
        data = await self._request(
            "POST", "/auth/v3/tenant_access_token/internal",
            body={"app_id": self.app_id, "app_secret": self.app_secret},
        )
        token = data.get("tenant_access_token")
        expires = data.get("expire")
        if not isinstance(token, str) or not token or not isinstance(expires, (int, float)) or expires <= 0:
            raise LarkError("Lark returned an invalid token response.")
        if len(_TOKEN_CACHE) >= 128:
            _TOKEN_CACHE.pop(next(iter(_TOKEN_CACHE)))
        _TOKEN_CACHE[self.cache_key] = (token, time.monotonic() + max(0, expires - 60))
        return token

    async def request(
        self, method: str, path: str, *, params: dict | None = None, body: dict | None = None,
    ) -> dict:
        token = self.user_token or await self.get_tenant_token()
        response = await self._request(method, path, params=params, body=body, token=token)
        data = response.get("data")
        if not isinstance(data, dict):
            raise LarkError("Lark returned an invalid data response.")
        return data

    async def get_user_info(self) -> dict:
        if self.user_token is None:
            raise LarkError("Lark user OAuth is required for this identity check.", "configuration_invalid")
        return await self.request("GET", "/authen/v1/user_info")

    async def _request(
        self, method: str, path: str, *, params: dict | None = None,
        body: dict | None = None, token: str | None = None,
    ) -> dict:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                response = await client.request(
                    method, self.base + path, headers=headers, params=params, json=body,
                )
        except httpx.HTTPError as exc:
            raise LarkError(
                "Could not reach Lark. For writes, check the destination before retrying."
            ) from exc
        try:
            data = response.json()
        except ValueError as exc:
            raise LarkError(f"Lark returned an invalid response (HTTP {response.status_code}).") from exc
        if not isinstance(data, dict):
            raise LarkError("Lark returned an invalid response.")
        code = data.get("code")
        # Do not echo response bodies/messages: providers may include credentials.
        if code in _CREDENTIAL_ERRORS:
            raise LarkError(
                "Lark authentication failed: invalid credentials. Check App ID, App Secret and platform in Integrations.",
                "credentials_rejected",
            )
        if code in {10014, 99991662, 99991673}:
            raise LarkError("Lark app unavailable. Check its installation, status and release in the Developer Console.",
                            "app_unavailable")
        if code in _TOKEN_ERRORS or response.status_code == 401:
            _TOKEN_CACHE.pop(self.cache_key, None)
            message = ("Lark authorization expired or was revoked; reconnect this account."
                       if self.user_token else "Lark access token rejected. Retry to obtain a fresh token.")
            raise LarkError(message, "token_rejected")
        if code in _PERMISSION_ERRORS or response.status_code == 403:
            raise LarkError(
                "Lark permission denied. Check the granted scopes and this account's access to the resource.",
                "permission_denied",
            )
        if code == 99991400 or response.status_code == 429:
            raise LarkError("Lark rate limit reached. Wait before retrying.", "rate_limited")
        if not response.is_success or code != 0:
            safe_code = code if isinstance(code, int) and not isinstance(code, bool) else "unknown"
            raise LarkError(f"Lark API failed (HTTP {response.status_code}, code {safe_code}).")
        return data


_ID = {"type": "string", "pattern": "^[A-Za-z0-9_-]+$", "maxLength": 256}
_PAGE = {
    "page_size": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
    "page_token": {"type": "string", "maxLength": 4096},
}
_TABLE = {"app_token": {**_ID, "description": "Base app token from its /base/ URL."}, "table_id": _ID}
_MESSAGE = {
    "text": {"type": "string", "minLength": 1, "maxLength": 20000},
    "uuid": {"type": "string", "minLength": 1, "maxLength": 50,
             "description": "Optional idempotency key; reuse the same value when retrying a send."},
}
_TOOLS = {
    "list_chats": {
        "description": "List group chats available to the selected Lark user or app bot. Does not enumerate personal DMs.",
        "method": "GET", "path": "/im/v1/chats", "properties": _PAGE,
    },
    "get_chat": {
        "description": "Read a Lark chat's metadata by chat_id.",
        "method": "GET", "path": "/im/v1/chats/{chat_id}",
        "required": ["chat_id"], "properties": {"chat_id": _ID},
    },
    "list_messages": {
        "description": "Read accessible chat messages or thread replies. Pass exactly one of chat_id or thread_id. "
                       "Chat history includes thread roots; use a returned thread_id to read its replies. "
                       "Follow returned page_token.",
        "method": "GET", "path": "/im/v1/messages",
        "oneOf": [{"required": ["chat_id"], "not": {"required": ["thread_id"]}},
                  {"required": ["thread_id"], "not": {"required": ["chat_id"]}}],
        "properties": {
            "chat_id": _ID, "thread_id": _ID, **_PAGE,
            "page_size": {**_PAGE["page_size"], "maximum": 50},
            "sort_type": {"type": "string", "enum": ["ByCreateTimeAsc", "ByCreateTimeDesc"]},
            "start_time": {"type": "string", "pattern": "^[0-9]+$", "description": "Unix seconds."},
            "end_time": {"type": "string", "pattern": "^[0-9]+$", "description": "Unix seconds."},
        },
    },
    "send_message": {
        "description": "Send a text message to a specified chat as the selected Lark user (OAuth) or app bot.",
        "method": "POST", "path": "/im/v1/messages",
        "required": ["chat_id", "text"], "properties": {"chat_id": _ID, **_MESSAGE},
    },
    "reply_message": {
        "description": "Reply to an existing Lark message as the selected Lark user (OAuth) or app bot.",
        "method": "POST", "path": "/im/v1/messages/{message_id}/reply",
        "required": ["message_id", "text"],
        "properties": {"message_id": _ID, **_MESSAGE, "reply_in_thread": {"type": "boolean"}},
    },
    "get_document": {
        "description": "Read metadata of a shared Lark Docx document using the token from its /docx/ URL.",
        "method": "GET", "path": "/docx/v1/documents/{document_id}",
        "required": ["document_id"], "properties": {"document_id": _ID},
    },
    "get_document_content": {
        "description": "Read shared Lark Docx plain text. Follow next_offset to read long documents completely.",
        "method": "GET", "path": "/docx/v1/documents/{document_id}/raw_content",
        "required": ["document_id"], "properties": {
            "document_id": _ID,
            "content_offset": {"type": "integer", "minimum": 0, "default": 0},
            "content_limit": {"type": "integer", "minimum": 1, "maximum": 8000, "default": 8000},
            "content_digest": {"type": "string", "pattern": "^[a-f0-9]{64}$",
                               "description": "Digest from the first content chunk; required at nonzero offsets. "
                                              "Restart at 0 if the document changes."},
        },
        "if": {"required": ["content_offset"], "properties": {"content_offset": {"minimum": 1}}},
        "then": {"required": ["content_digest"]},
    },
    "create_document": {
        "description": "Create a Lark Docx document. With app identity, folder_token must identify an app-created folder.",
        "method": "POST", "path": "/docx/v1/documents", "required": ["title"],
        "properties": {"title": {"type": "string", "minLength": 1, "maxLength": 800}, "folder_token": _ID},
    },
    "append_document_text": {
        "description": "Append up to 50 text paragraphs to a shared Lark Docx document without replacing existing blocks.",
        "method": "POST", "path": "/docx/v1/documents/{document_id}/blocks/{document_id}/children",
        "required": ["document_id", "paragraphs"], "properties": {
            "document_id": _ID,
            "paragraphs": {"type": "array", "minItems": 1, "maxItems": 50,
                           "items": {"type": "string", "minLength": 1, "maxLength": 2000}},
            "client_token": {"type": "string", "minLength": 1, "maxLength": 64,
                             "description": "Optional stable idempotency token for this append operation."},
        },
    },
    "list_bitable_tables": {
        "description": "List tables in a Lark Base (Bitable) shared with the app.",
        "method": "GET", "path": "/bitable/v1/apps/{app_token}/tables",
        "required": ["app_token"], "properties": {"app_token": _TABLE["app_token"], **_PAGE},
    },
    "list_bitable_fields": {
        "description": "Read Base table field types before writing records.",
        "method": "GET", "path": "/bitable/v1/apps/{app_token}/tables/{table_id}/fields",
        "required": ["app_token", "table_id"], "properties": {**_TABLE, **_PAGE},
    },
    "list_bitable_records": {
        "description": "Query records in a shared Base table using Lark's read-only search endpoint.",
        "method": "POST", "path": "/bitable/v1/apps/{app_token}/tables/{table_id}/records/search",
        "required": ["app_token", "table_id"], "properties": {
            **_TABLE, **_PAGE, "view_id": _ID,
            "field_names": {"type": "array", "items": {"type": "string"}},
            "sort": {"type": "array", "items": {
                "type": "object", "required": ["field_name"],
                "properties": {"field_name": {"type": "string"}, "desc": {"type": "boolean"}},
                "additionalProperties": False,
            }},
            "filter": {"type": "object", "required": ["conjunction", "conditions"], "properties": {
                "conjunction": {"type": "string", "enum": ["and", "or"]},
                "conditions": {"type": "array", "items": {
                    "type": "object", "required": ["field_name", "operator"],
                    "properties": {
                        "field_name": {"type": "string"}, "operator": {"type": "string"},
                        "value": {"type": "array", "items": {"type": "string"}},
                    }, "additionalProperties": False,
                }},
            }, "additionalProperties": False},
            "automatic_fields": {"type": "boolean"},
        },
    },
    "create_bitable_record": {
        "description": "Create one Base record. Read the table field schema and use its exact names/types.",
        "method": "POST", "path": "/bitable/v1/apps/{app_token}/tables/{table_id}/records",
        "required": ["app_token", "table_id", "fields"],
        "properties": {**_TABLE, "fields": {"type": "object", "minProperties": 1}},
    },
    "update_bitable_record": {
        "description": "Update supplied fields of a Base record identified by record_id.",
        "method": "PUT", "path": "/bitable/v1/apps/{app_token}/tables/{table_id}/records/{record_id}",
        "required": ["app_token", "table_id", "record_id", "fields"],
        "properties": {**_TABLE, "record_id": _ID, "fields": {"type": "object", "minProperties": 1}},
    },
}


def _schema(spec: dict) -> dict:
    schema = {"type": "object", "required": spec.get("required", []),
              "properties": dict(spec["properties"]), "additionalProperties": False}
    if _chunkable_read(spec):
        schema["properties"]["result_offset"] = {
            "type": "integer", "minimum": 0, "maximum": 10000000, "default": 0,
            "description": "For a large JSON result, follow result_pagination.next_offset and result_digest with unchanged "
                           "query arguments (including page_token), then follow the provider's page_token. "
                           "Chunks re-fetch live data, not a frozen snapshot.",
        }
        schema["properties"]["result_digest"] = {
            "type": "string", "pattern": "^[a-f0-9]{64}$",
            "description": "Digest from the first JSON chunk. Required when result_offset > 0; "
                           "a changed provider response requires restarting at offset 0.",
        }
        schema["if"] = {"required": ["result_offset"], "properties": {"result_offset": {"minimum": 1}}}
        schema["then"] = {"required": ["result_digest"]}
    for key in ("oneOf", "if", "then"):
        if key in spec:
            schema[key] = spec[key]
    return schema


def _chunkable_read(spec: dict) -> bool:
    return (spec["method"] == "GET" and not spec["path"].endswith("/raw_content")) or (
        spec["path"].endswith("/records/search"))


def _read_result(data: dict, offset: int, expected_digest: str | None) -> dict:
    serialized = json.dumps(data, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    digest = hashlib.sha256(serialized.encode()).hexdigest()
    if expected_digest and expected_digest != digest:
        raise LarkError("Lark response changed between chunks. Restart at result_offset 0 without result_digest.")
    if not offset and len(serialized) <= _RESULT_MAX_CHARS:
        return mcp_ok(serialized)
    if offset >= len(serialized):
        raise LarkError("result_offset is outside the current response. Restart at 0.")
    end = min(offset + 2500, len(serialized))
    result = {
        "result_json": serialized[offset:end],
        "result_digest": digest,
        "result_pagination": {"offset": offset, "next_offset": end if end < len(serialized) else None,
                              "total_characters": len(serialized), "source": "refetched_provider_response"},
    }
    if "page_token" in data:
        result["page_token"] = data["page_token"]
    if "has_more" in data:
        result["provider_has_more"] = data["has_more"]
    while len(json.dumps(result, ensure_ascii=False)) > _RESULT_MAX_CHARS:
        if end - offset <= 1:
            raise LarkError("Lark pagination metadata is too large. Restart with a smaller page_size.")
        end = offset + max(1, (end - offset) // 2)
        result["result_json"] = serialized[offset:end]
        result["result_pagination"]["next_offset"] = end
    return mcp_ok(json.dumps(result, ensure_ascii=False))


# Providers (Anthropic) reject top-level anyOf/oneOf/allOf in a tool input
# schema. The model reads such a rule in the description; calls are still
# validated against the full schema.
_MODEL_HIDDEN_KEYS = ("oneOf",)


def list_tools() -> list[dict[str, Any]]:
    return [{"name": name, "description": spec["description"],
             "inputSchema": {key: value for key, value in _schema(spec).items() if key not in _MODEL_HIDDEN_KEYS}}
            for name, spec in _TOOLS.items()]


async def call_tool(name: str, arguments: dict[str, Any], bearer_token: str) -> dict[str, Any]:
    spec = _TOOLS.get(name)
    if spec is None:
        return mcp_err(f"Unknown Lark tool: {name}")
    error = next(Draft202012Validator(_schema(spec)).iter_errors(arguments), None)
    if error:
        path = ".".join(str(part) for part in error.absolute_path) or "arguments"
        return mcp_err(f"Invalid Lark {path} ({error.validator}). Check the tool schema.")
    try:
        try:
            credentials = json.loads(bearer_token)
        except (ValueError, TypeError):
            return mcp_err("Lark credentials are malformed. Reconfigure them in Integrations.")
        client = LarkClient(credentials)
        data = await _execute(client, name, spec, arguments)
        if _chunkable_read(spec):
            return _read_result(data, arguments.get("result_offset", 0), arguments.get("result_digest"))
        return mcp_ok(data)
    except LarkError as exc:
        return mcp_err(str(exc))
    except Exception as exc:  # noqa: BLE001
        logger.warning("Lark tool %s failed (%s)", name, type(exc).__name__)
        return mcp_err("Lark call failed unexpectedly. For writes, check the destination before retrying.")


async def _execute(client: LarkClient, name: str, spec: dict, args: dict) -> dict:
    path = spec["path"].format(**args)
    params = {}
    body = None
    if "page_size" in spec["properties"]:
        params["page_size"] = args.get("page_size", 20)
        if args.get("page_token"):
            params["page_token"] = args["page_token"]
    if name == "list_messages":
        container = "thread" if "thread_id" in args else "chat"
        params.update(container_id_type=container, container_id=args[f"{container}_id"])
        params.update({k: args[k] for k in ("sort_type", "start_time", "end_time") if k in args})
    elif name in {"send_message", "reply_message"}:
        body = {"msg_type": "text", "content": json.dumps({"text": args["text"]}, ensure_ascii=False)}
        if "uuid" in args:
            body["uuid"] = args["uuid"]
        if name == "send_message":
            params["receive_id_type"] = "chat_id"
            body["receive_id"] = args["chat_id"]
        elif "reply_in_thread" in args:
            body["reply_in_thread"] = args["reply_in_thread"]
    elif name == "list_bitable_records":
        body = {k: args[k] for k in ("view_id", "field_names", "sort", "filter", "automatic_fields") if k in args}
    elif name == "create_document":
        body = {k: args[k] for k in ("title", "folder_token") if k in args}
    elif name == "append_document_text":
        params["document_revision_id"] = -1
        if "client_token" in args:
            params["client_token"] = args["client_token"]
        body = {"index": -1, "children": [
            {"block_type": 2, "text": {"elements": [{"text_run": {"content": paragraph}}]}}
            for paragraph in args["paragraphs"]
        ]}
    elif name in {"create_bitable_record", "update_bitable_record"}:
        body = {"fields": args["fields"]}
    data = await client.request(spec["method"], path, params=params or None, body=body)
    if name in {"send_message", "reply_message"}:
        return {"completed": True, **{key: data[key] for key in (
            "message_id", "chat_id", "root_id", "parent_id", "thread_id", "create_time",
        ) if key in data}}
    if name in {"create_bitable_record", "update_bitable_record"}:
        record = data.get("record") or {}
        return {"completed": True, "app_token": args["app_token"], "table_id": args["table_id"],
                "record_id": record.get("record_id")}
    if name == "append_document_text":
        return {"document_id": args["document_id"], "document_revision_id": data.get("document_revision_id"),
                "client_token": data.get("client_token"),
                "block_ids": [block.get("block_id") for block in data.get("children", []) if isinstance(block, dict)]}
    if name == "get_document_content":
        content = data.get("content")
        if not isinstance(content, str):
            raise LarkError("Lark returned invalid document content.")
        digest = hashlib.sha256(content.encode()).hexdigest()
        if args.get("content_digest") and args["content_digest"] != digest:
            raise LarkError("Lark document changed between chunks. Restart at content_offset 0 without content_digest.")
        offset = args.get("content_offset", 0)
        end = min(offset + args.get("content_limit", 8000), len(content))
        result = {"document_id": args["document_id"], "content": content[offset:end],
                  "content_digest": digest,
                  "total_characters": len(content), "has_more": end < len(content),
                  "next_offset": end if end < len(content) else None}
        # Escaped quotes/control characters can expand past the MCP envelope cap.
        while len(json.dumps(result, ensure_ascii=False, indent=2)) > _RESULT_MAX_CHARS:
            end = offset + max(1, (end - offset) // 2)
            result.update(content=content[offset:end], has_more=True, next_offset=end)
        return result
    return data
