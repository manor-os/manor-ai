"""Notion MCP server — in-process wrapper for the Notion REST API.

The bearer token is either a direct Notion integration token or a fresh
OAuth token resolved through Nango by the runtime. The module deliberately
keeps the surface small and typed: search/read operations plus controlled
page and block mutations.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)

_API = "https://api.notion.com/v1"
_NOTION_VERSION = "2022-06-28"
_TIMEOUT = 20.0
_MAX_CHARS = 12_000
_MAX_PAGE_SIZE = 100


def _tool_def(name: str, spec: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": name,
        "description": spec["description"],
        "parameters": {
            "type": "object",
            "required": list(spec.get("required", [])),
            "properties": dict(spec.get("properties", {})),
        },
    }


def list_tools() -> List[Dict[str, Any]]:
    return [_tool_def(name, spec) for name, spec in _TOOLS.items()]


async def call_tool(
    name: str,
    arguments: Dict[str, Any],
    bearer_token: str,
) -> Dict[str, Any]:
    token = bearer_token.strip() if isinstance(bearer_token, str) else ""
    if not token:
        return _error(
            "Notion access token is missing. Connect Notion or reconnect the Nango account."
        )

    handler = _HANDLERS.get(name)
    if handler is None:
        return _error(f"Unknown tool: {name}")
    if not isinstance(arguments, dict):
        return _error("arguments must be an object")
    arguments = dict(arguments)

    spec = _TOOLS[name]
    missing = [
        field
        for field in spec.get("required", [])
        if _is_blank(arguments.get(field))
    ]
    if missing:
        return _error(f"Missing required params: {', '.join(missing)}")
    for field, property_spec in (spec.get("properties") or {}).items():
        if field not in arguments or arguments[field] is None:
            continue
        if property_spec.get("type") == "string" and not isinstance(arguments[field], str):
            return _error(f"{field} must be a string")
        if property_spec.get("type") == "boolean" and not isinstance(arguments[field], bool):
            return _error(f"{field} must be a boolean")

    try:
        return _content(await handler(arguments, token))
    except httpx.HTTPStatusError as exc:
        response = exc.response
        return _error(f"Notion API error ({response.status_code}): {response.text[:500]}")
    except Exception as exc:  # noqa: BLE001
        logger.exception("Notion MCP tool %s failed", name)
        return _error(str(exc))


def _is_blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _error(message: str) -> Dict[str, Any]:
    return {"content": [{"type": "text", "text": message}], "isError": True}


def _content(value: Any) -> Dict[str, Any]:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return {"content": [{"type": "text", "text": text}], "isError": False}


def _page_size(value: Any, *, default: int = 20) -> int:
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        raise ValueError("page_size must be an integer between 1 and 100")
    try:
        size = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("page_size must be an integer between 1 and 100") from exc
    if isinstance(value, float) and value != size:
        raise ValueError("page_size must be an integer between 1 and 100")
    if size < 1 or size > _MAX_PAGE_SIZE:
        raise ValueError("page_size must be an integer between 1 and 100")
    return size


def _object_dict(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be an object")
    return value


async def _request(
    token: str,
    method: str,
    path: str,
    *,
    body: Optional[dict[str, Any]] = None,
    params: Optional[dict[str, Any]] = None,
) -> str:
    url = f"{_API}/{path.lstrip('/')}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Notion-Version": _NOTION_VERSION,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        response = await client.request(
            method,
            url,
            headers=headers,
            json=body,
            params=params or {},
        )
    if response.status_code == 401:
        raise RuntimeError("Notion authorization failed; reconnect the account")
    if response.status_code == 403:
        raise RuntimeError(f"Notion API error (403): forbidden (integration access): {response.text[:300]}")
    if response.status_code == 404:
        raise RuntimeError("Notion page, database, or block was not found")
    if not response.is_success:
        raise RuntimeError(f"Notion API error ({response.status_code}): {response.text[:300]}")
    if response.status_code == 204 or not response.text:
        return json.dumps({"success": True})
    try:
        data = response.json()
    except Exception:
        return response.text[:_MAX_CHARS]
    text = json.dumps(data, ensure_ascii=False, indent=2, default=str)
    return text[:_MAX_CHARS] + "\n… (truncated)" if len(text) > _MAX_CHARS else text


async def _search(args: Dict[str, Any], token: str) -> str:
    body: dict[str, Any] = {"page_size": _page_size(args.get("page_size"))}
    if args.get("query") is not None:
        body["query"] = str(args["query"]).strip()
    if args.get("filter") is not None:
        body["filter"] = _object_dict(args["filter"], "filter")
    if args.get("sort") is not None:
        body["sort"] = _object_dict(args["sort"], "sort")
    if args.get("start_cursor"):
        body["start_cursor"] = str(args["start_cursor"]).strip()
    return await _request(token, "POST", "search", body=body)


async def _get_page(args: Dict[str, Any], token: str) -> str:
    return await _request(token, "GET", f"pages/{quote(str(args['page_id']), safe='')}")


async def _query_database(args: Dict[str, Any], token: str) -> str:
    body: dict[str, Any] = {"page_size": _page_size(args.get("page_size"))}
    for key in ("filter", "sorts"):
        if args.get(key) is not None:
            value = args[key]
            if key == "filter":
                body[key] = _object_dict(value, key)
            elif not isinstance(value, list):
                raise ValueError("sorts must be an array")
            else:
                body[key] = value
    if args.get("start_cursor"):
        body["start_cursor"] = str(args["start_cursor"]).strip()
    return await _request(
        token,
        "POST",
        f"databases/{quote(str(args['database_id']), safe='')}/query",
        body=body,
    )


async def _create_page(args: Dict[str, Any], token: str) -> str:
    parent_type = str(args.get("parent_type") or "page_id").strip()
    if parent_type not in {"page_id", "database_id"}:
        raise ValueError("parent_type must be page_id or database_id")
    properties = args.get("properties")
    if properties is None:
        properties = {
            "title": {
                "title": [{"type": "text", "text": {"content": str(args["title"]).strip()}}]
            }
        }
    properties = _object_dict(properties, "properties")
    body: dict[str, Any] = {
        "parent": {parent_type: str(args["parent_id"]).strip()},
        "properties": properties,
    }
    if isinstance(args.get("children"), list):
        body["children"] = args["children"]
    elif args.get("children") is not None:
        raise ValueError("children must be an array")
    return await _request(token, "POST", "pages", body=body)


async def _update_page(args: Dict[str, Any], token: str) -> str:
    properties = _object_dict(args["properties"], "properties")
    body: dict[str, Any] = {"properties": properties}
    if args.get("archived") is not None:
        if not isinstance(args["archived"], bool):
            raise ValueError("archived must be a boolean")
        body["archived"] = args["archived"]
    return await _request(
        token,
        "PATCH",
        f"pages/{quote(str(args['page_id']), safe='')}",
        body=body,
    )


async def _append_block_children(args: Dict[str, Any], token: str) -> str:
    children = args["children"]
    if not isinstance(children, list) or not children:
        raise ValueError("children must be a non-empty array")
    return await _request(
        token,
        "PATCH",
        f"blocks/{quote(str(args['block_id']), safe='')}/children",
        body={"children": children},
    )


_TOOLS: dict[str, dict[str, Any]] = {
    "search": {
        "description": "Search pages and databases shared with the Notion integration.",
        "required": ["query"],
        "properties": {
            "query": {"type": "string"},
            "page_size": {"type": "integer", "minimum": 1, "maximum": 100},
            "filter": {"type": "object"},
            "sort": {"type": "object"},
            "start_cursor": {"type": "string"},
        },
    },
    "get_page": {
        "description": "Read one Notion page and its properties.",
        "required": ["page_id"],
        "properties": {"page_id": {"type": "string"}},
    },
    "query_database": {
        "description": "Query rows in a Notion database.",
        "required": ["database_id"],
        "properties": {
            "database_id": {"type": "string"},
            "page_size": {"type": "integer", "minimum": 1, "maximum": 100},
            "filter": {"type": "object"},
            "sorts": {"type": "array"},
            "start_cursor": {"type": "string"},
        },
    },
    "create_page": {
        "description": "Create a Notion page under a page or database.",
        "required": ["parent_id", "title"],
        "properties": {
            "parent_id": {"type": "string"},
            "parent_type": {"type": "string", "enum": ["page_id", "database_id"]},
            "title": {"type": "string"},
            "properties": {"type": "object"},
            "children": {"type": "array"},
        },
    },
    "update_page": {
        "description": "Update Notion page properties or archive a page.",
        "required": ["page_id", "properties"],
        "properties": {
            "page_id": {"type": "string"},
            "properties": {"type": "object"},
            "archived": {"type": "boolean"},
        },
    },
    "append_block_children": {
        "description": "Append block children to a Notion page or block.",
        "required": ["block_id", "children"],
        "properties": {
            "block_id": {"type": "string"},
            "children": {"type": "array"},
        },
    },
}

_HANDLERS = {
    "search": _search,
    "get_page": _get_page,
    "query_database": _query_database,
    "create_page": _create_page,
    "update_page": _update_page,
    "append_block_children": _append_block_children,
}
