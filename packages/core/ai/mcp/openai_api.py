"""OpenAI MCP server — chat completions, structured output, embeddings.

Auth: bearer_token = the user's OpenAI API key (sk-... or sk-proj-...),
stored as an entity Integration with provider="openai" and credentials
``{"api_key": "sk-..."}``. The Manor agent layer's ``_resolve_bearer_token``
picks it up via the standard ``api_key`` flow.

This is the first AI-platform wrapper — the pattern (list_tools +
call_tool returning MCP-shaped {content, isError}) carries over to
Anthropic / Doubao / Kimi / Qwen / Deepseek with only auth-header and
endpoint differences.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

_API = "https://api.openai.com/v1"
_TIMEOUT = 60.0  # generous because completions can take a while
_MAX_CHARS = 24_000


# ── MCP protocol ────────────────────────────────────────────────────────────

def list_tools() -> List[Dict[str, Any]]:
    return [
        {
            "name": "chat",
            "description": (
                "Run a chat completion against an OpenAI model. "
                "Returns the assistant's text reply (and tool calls if "
                "any). Use for one-shot reasoning, drafting, "
                "classification, or any task that doesn't need multi-turn."
            ),
            "inputSchema": {
                "type": "object",
                "required": ["messages"],
                "properties": {
                    "model": {
                        "type": "string",
                        "description": (
                            "OpenAI model id, e.g. 'gpt-4o', 'gpt-4o-mini', "
                            "'o1', 'o3-mini'. Defaults to gpt-4o-mini."
                        ),
                    },
                    "messages": {
                        "type": "array",
                        "description": (
                            "Standard OpenAI chat messages array: "
                            "[{role: 'system'|'user'|'assistant', content: str}]."
                        ),
                        "items": {
                            "type": "object",
                            "properties": {
                                "role": {"type": "string"},
                                "content": {"type": "string"},
                            },
                        },
                    },
                    "temperature": {
                        "type": "number",
                        "description": "0.0 deterministic … 2.0 creative. Default 0.7.",
                    },
                    "max_tokens": {
                        "type": "integer",
                        "description": "Cap on the reply length in tokens.",
                    },
                    "json_mode": {
                        "type": "boolean",
                        "description": (
                            "When true, force the response into a JSON "
                            "object via response_format. The system "
                            "message must instruct the model to emit JSON."
                        ),
                    },
                },
            },
        },
        {
            "name": "embed",
            "description": (
                "Embed one or more text strings into a vector via "
                "text-embedding-3-small or your chosen model. Returns "
                "the vectors as a JSON array."
            ),
            "inputSchema": {
                "type": "object",
                "required": ["input"],
                "properties": {
                    "input": {
                        "oneOf": [
                            {"type": "string"},
                            {"type": "array", "items": {"type": "string"}},
                        ],
                        "description": "Single string or list of strings to embed.",
                    },
                    "model": {
                        "type": "string",
                        "description": (
                            "Embedding model id. Defaults to "
                            "text-embedding-3-small (1536 dim, cheap)."
                        ),
                    },
                },
            },
        },
        {
            "name": "list_models",
            "description": (
                "List the OpenAI models the current API key has access to. "
                "Useful for verifying credential setup."
            ),
            "inputSchema": {"type": "object", "properties": {}},
        },
    ]


async def call_tool(
    name: str,
    arguments: Dict[str, Any],
    bearer_token: str,
) -> Dict[str, Any]:
    if not bearer_token:
        return _error(
            "OpenAI API key is missing. Add one under Integrations → OpenAI."
        )

    handler = _HANDLERS.get(name)
    if handler is None:
        return _error(f"Unknown OpenAI tool: {name}")

    try:
        text = await handler(bearer_token, arguments)
        return _content(text)
    except httpx.HTTPStatusError as exc:
        body = exc.response.text[:500] if exc.response is not None else ""
        return _error(f"OpenAI HTTP {exc.response.status_code}: {body}")
    except Exception as exc:  # noqa: BLE001
        logger.exception("OpenAI tool %s crashed", name)
        return _error(f"OpenAI call failed: {exc}")


# ── Handlers ────────────────────────────────────────────────────────────────

async def _chat(api_key: str, args: Dict[str, Any]) -> str:
    messages = args.get("messages") or []
    if not isinstance(messages, list) or not messages:
        return "Error: messages array is required and non-empty."
    body: Dict[str, Any] = {
        "model": args.get("model") or "gpt-4o-mini",
        "messages": messages,
        "temperature": args.get("temperature", 0.7),
    }
    if args.get("max_tokens"):
        body["max_tokens"] = int(args["max_tokens"])
    if args.get("json_mode"):
        body["response_format"] = {"type": "json_object"}

    async with httpx.AsyncClient(timeout=_TIMEOUT) as cx:
        r = await cx.post(
            f"{_API}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=body,
        )
        r.raise_for_status()
        data = r.json()

    choices = data.get("choices") or []
    if not choices:
        return json.dumps(data, ensure_ascii=False)
    msg = choices[0].get("message") or {}
    content = msg.get("content") or ""
    return _truncate(content)


async def _embed(api_key: str, args: Dict[str, Any]) -> str:
    inputs = args.get("input")
    if not inputs:
        return "Error: input is required."
    body = {
        "model": args.get("model") or "text-embedding-3-small",
        "input": inputs,
    }
    async with httpx.AsyncClient(timeout=_TIMEOUT) as cx:
        r = await cx.post(
            f"{_API}/embeddings",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=body,
        )
        r.raise_for_status()
        data = r.json()

    embeddings = [item.get("embedding") for item in (data.get("data") or [])]
    return _truncate(json.dumps({
        "model": data.get("model"),
        "count": len(embeddings),
        "dimensions": len(embeddings[0]) if embeddings and embeddings[0] else 0,
        "embeddings": embeddings,
    }, ensure_ascii=False))


async def _list_models(api_key: str, args: Dict[str, Any]) -> str:
    async with httpx.AsyncClient(timeout=_TIMEOUT) as cx:
        r = await cx.get(
            f"{_API}/models",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        r.raise_for_status()
        data = r.json()
    ids = sorted({m.get("id") for m in (data.get("data") or []) if m.get("id")})
    return json.dumps({"count": len(ids), "models": ids}, ensure_ascii=False, indent=2)


# ── Helpers ─────────────────────────────────────────────────────────────────

def _truncate(s: str) -> str:
    if len(s) <= _MAX_CHARS:
        return s
    return s[:_MAX_CHARS] + "\n… (truncated)"


def _content(text: str) -> Dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": False}


def _error(msg: str) -> Dict[str, Any]:
    return {"content": [{"type": "text", "text": msg}], "isError": True}


_HANDLERS = {
    "chat": _chat,
    "embed": _embed,
    "list_models": _list_models,
}
