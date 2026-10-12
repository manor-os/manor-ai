"""Suno MCP server — asynchronous song generation through Suno's API.

Auth: ``bearer_token`` is the user's Suno API key, stored in the ordinary
encrypted Integration credential record as ``{"api_key": "sk_live_..."}``.

Suno creation endpoints are billable and asynchronous.  This adapter submits
exactly one request and returns the provider job immediately; it never polls or
retries a create call.  Callers can use ``get_generation`` to observe the same
job without risking a duplicate charge.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List

import httpx

logger = logging.getLogger(__name__)

_API = "https://api.suno.com/v0"
_TIMEOUT = 60.0
_CREATE_TOOLS = frozenset({"generate_music", "create_cover", "create_mashup"})
_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,160}$")


def list_tools() -> List[Dict[str, Any]]:
    return [
        {
            "name": "generate_music",
            "description": (
                "Submit one paid Suno music-generation job. Use description for "
                "simple mode, or lyrics + style for custom mode. Instrumental "
                "custom mode requires style but not lyrics. Returns a job id; "
                "use get_generation to check progress. Never retry blindly."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "description": {
                        "type": "string",
                        "description": "Simple-mode song prompt; cannot be combined with lyrics or style.",
                    },
                    "lyrics": {
                        "type": "string",
                        "description": "Custom-mode lyrics. Required unless instrumental is true.",
                    },
                    "style": {
                        "type": "string",
                        "description": "Custom-mode musical style; required when description is omitted.",
                    },
                    "title": {"type": "string"},
                    "instrumental": {"type": "boolean", "default": False},
                    "voice_id": {
                        "type": "string",
                        "description": "Optional Suno voice/persona id available to the connected account.",
                    },
                },
            },
        },
        {
            "name": "get_generation",
            "description": (
                "Get a Suno generation job by id. Read-only. Status progresses "
                "through submitted/queued/streaming/complete or error; completed "
                "responses include the provider audio URL."
            ),
            "parameters": {
                "type": "object",
                "required": ["generation_id"],
                "properties": {"generation_id": {"type": "string"}},
            },
        },
        {
            "name": "create_cover",
            "description": (
                "Submit one paid Suno cover job from an existing Suno audio id. "
                "Optionally replace lyrics, style, title, or voice. Returns a new "
                "job id; use get_generation to check progress."
            ),
            "parameters": {
                "type": "object",
                "required": ["source_audio_id"],
                "properties": {
                    "source_audio_id": {"type": "string"},
                    "lyrics": {"type": "string"},
                    "style": {"type": "string"},
                    "title": {"type": "string"},
                    "voice_id": {"type": "string"},
                },
            },
        },
        {
            "name": "create_mashup",
            "description": (
                "Submit one paid Suno mashup job combining two existing Suno "
                "audio ids. Returns a new job id; use get_generation to check progress."
            ),
            "parameters": {
                "type": "object",
                "required": ["source_audio_id", "additional_audio_id"],
                "properties": {
                    "source_audio_id": {"type": "string"},
                    "additional_audio_id": {"type": "string"},
                    "lyrics": {"type": "string"},
                    "style": {"type": "string"},
                    "title": {"type": "string"},
                },
            },
        },
        {
            "name": "get_usage",
            "description": "Get the connected Suno account's current API usage. Read-only.",
            "parameters": {"type": "object", "properties": {}},
        },
    ]


async def call_tool(
    name: str,
    arguments: Dict[str, Any],
    bearer_token: str,
) -> Dict[str, Any]:
    api_key = bearer_token.strip() if isinstance(bearer_token, str) else ""
    if not api_key:
        return _error(
            "Suno API key is missing. Create one at https://platform.suno.com/ "
            "and add it under Integrations → Suno."
        )

    handler = _HANDLERS.get(name)
    if handler is None:
        return _error(f"Unknown suno tool: {name}")
    if not isinstance(arguments, dict):
        return _error("arguments must be an object")

    try:
        result = await handler(dict(arguments), api_key)
        return _content(result)
    except httpx.TimeoutException:
        if name in _CREATE_TOOLS:
            return _error(
                "Suno timed out after the paid request may have been accepted. "
                "Do not submit it again automatically; check Suno Platform for "
                "the job before asking the user whether to retry."
            )
        return _error("Suno request timed out; retry this read-only check later.")
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        body = (exc.response.text or "")[:500]
        if len(api_key) >= 4:
            body = body.replace(api_key, "[redacted]")
        return _error(f"Suno HTTP {status}: {body or 'request rejected'}")
    except (TypeError, ValueError) as exc:
        return _error(str(exc))
    except Exception as exc:  # noqa: BLE001
        logger.exception("Suno tool %s crashed", name)
        return _error(f"Suno call failed: {exc}")


async def _generate_music(args: Dict[str, Any], api_key: str) -> Any:
    description = _optional_string(args, "description")
    lyrics = _optional_string(args, "lyrics")
    style = _optional_string(args, "style")
    title = _optional_string(args, "title")
    voice_id = _optional_string(args, "voice_id")
    instrumental = args.get("instrumental", False)
    if not isinstance(instrumental, bool):
        raise ValueError("instrumental must be a boolean")

    if description:
        if lyrics or style:
            raise ValueError("description cannot be combined with lyrics or style")
    else:
        if not style:
            raise ValueError("style is required when description is omitted")
        if not instrumental and not lyrics:
            raise ValueError("lyrics are required unless instrumental is true")

    payload: Dict[str, Any] = {"instrumental": instrumental}
    _put_if_present(payload, "description", description)
    _put_if_present(payload, "lyrics", lyrics)
    _put_if_present(payload, "style", style)
    _put_if_present(payload, "title", title)
    _put_if_present(payload, "voice_id", voice_id)
    return await _request_json("POST", "/audio", api_key, payload=payload)


async def submit_music_generation(
    *,
    api_key: str,
    description: str | None = None,
    lyrics: str | None = None,
    style: str | None = None,
    title: str | None = None,
    instrumental: bool = False,
) -> Any:
    """Submit exactly one music job for the durable Catalog worker.

    Callers must not retry this function after a timeout because the paid
    request may already have been accepted upstream.
    """
    return await _generate_music(
        {
            "description": description,
            "lyrics": lyrics,
            "style": style,
            "title": title,
            "instrumental": instrumental,
        },
        api_key,
    )


async def _get_generation(args: Dict[str, Any], api_key: str) -> Any:
    generation_id = _required_id(args, "generation_id")
    return await _request_json("GET", f"/audio/{generation_id}", api_key)


async def fetch_generation(*, api_key: str, generation_id: str) -> Any:
    """Fetch one existing Suno job without creating billable work."""
    return await _get_generation({"generation_id": generation_id}, api_key)


def generation_id(data: Any) -> str:
    """Extract a provider job id from common Suno response envelopes."""
    for item in _walk_dicts(data):
        for key in ("id", "generation_id", "generationId", "task_id", "taskId"):
            value = item.get(key)
            if isinstance(value, str) and _SAFE_ID.fullmatch(value.strip()):
                return value.strip()
    return ""


def generation_status(data: Any) -> str:
    """Normalize Suno provider state to pending/completed/failed."""
    for item in _walk_dicts(data):
        raw = item.get("status") or item.get("state")
        if not isinstance(raw, str):
            continue
        status = raw.strip().lower()
        if status in {"complete", "completed", "success", "succeeded", "ready"}:
            return "completed"
        if status in {"error", "failed", "failure", "cancelled", "canceled"}:
            return "failed"
        if status:
            return "pending"
    return "completed" if generation_audio_url(data) else "pending"


def generation_audio_url(data: Any) -> str:
    """Extract the first HTTPS audio URL from a completed response."""
    for item in _walk_dicts(data):
        for key in ("audio_url", "audioUrl", "stream_audio_url", "streamAudioUrl"):
            value = item.get(key)
            if isinstance(value, str) and value.strip().startswith("https://"):
                return value.strip()
    for item in _walk_dicts(data):
        value = item.get("url")
        if isinstance(value, str) and value.strip().startswith("https://"):
            return value.strip()
    return ""


def generation_error(data: Any) -> str:
    for item in _walk_dicts(data):
        for key in ("error", "message", "detail"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()[:500]
            if isinstance(value, dict):
                nested = value.get("message") or value.get("detail")
                if isinstance(nested, str) and nested.strip():
                    return nested.strip()[:500]
    return "Suno generation failed"


def _walk_dicts(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_dicts(child)


async def _create_cover(args: Dict[str, Any], api_key: str) -> Any:
    source_audio_id = _required_id(args, "source_audio_id")
    payload = _optional_payload(args, ("lyrics", "style", "title", "voice_id"))
    return await _request_json(
        "POST",
        f"/audio/{source_audio_id}/covers",
        api_key,
        payload=payload,
    )


async def _create_mashup(args: Dict[str, Any], api_key: str) -> Any:
    source_audio_id = _required_id(args, "source_audio_id")
    additional_audio_id = _required_id(args, "additional_audio_id")
    payload = _optional_payload(args, ("lyrics", "style", "title"))
    payload["additional_audio_id"] = additional_audio_id
    return await _request_json(
        "POST",
        f"/audio/{source_audio_id}/mashups",
        api_key,
        payload=payload,
    )


async def _get_usage(args: Dict[str, Any], api_key: str) -> Any:
    if args:
        raise ValueError("get_usage does not accept arguments")
    return await _request_json("GET", "/account/usage", api_key)


async def _request_json(
    method: str,
    path: str,
    api_key: str,
    *,
    payload: Dict[str, Any] | None = None,
) -> Any:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
    }
    kwargs: Dict[str, Any] = {"headers": headers}
    if payload is not None:
        headers["Content-Type"] = "application/json"
        kwargs["json"] = payload
    async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as client:
        response = await client.request(method, f"{_API}{path}", **kwargs)
    response.raise_for_status()
    try:
        return response.json()
    except (json.JSONDecodeError, ValueError) as exc:
        raise ValueError("Suno returned a non-JSON response") from exc


def _optional_string(args: Dict[str, Any], field: str) -> str | None:
    value = args.get(field)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    value = value.strip()
    if not value:
        raise ValueError(f"{field} cannot be blank")
    return value


def _required_id(args: Dict[str, Any], field: str) -> str:
    value = _optional_string(args, field)
    if value is None:
        raise ValueError(f"{field} is required")
    if not _SAFE_ID.fullmatch(value):
        raise ValueError(f"{field} must be a Suno id")
    return value


def _optional_payload(args: Dict[str, Any], fields: tuple[str, ...]) -> Dict[str, Any]:
    payload: Dict[str, Any] = {}
    for field in fields:
        _put_if_present(payload, field, _optional_string(args, field))
    return payload


def _put_if_present(payload: Dict[str, Any], field: str, value: Any) -> None:
    if value is not None:
        payload[field] = value


def _content(data: Any) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False)}],
        "isError": False,
    }
    if isinstance(data, dict):
        result["structuredContent"] = data
    return result


def _error(message: str) -> Dict[str, Any]:
    return {
        "content": [{"type": "text", "text": message}],
        "isError": True,
    }


_HANDLERS = {
    "generate_music": _generate_music,
    "get_generation": _get_generation,
    "create_cover": _create_cover,
    "create_mashup": _create_mashup,
    "get_usage": _get_usage,
}
