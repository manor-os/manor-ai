"""Jimeng (即梦) — image + short-video generation.

Calls a self-hosted reverse-engineered Jimeng gateway
(https://github.com/iptag/jimeng-api) that exposes OpenAI-compatible
/v1/images/generations and /v1/videos/generations endpoints. We
delegate the actual Jimeng-internal HTTP signing / payload building
to the gateway so this wrapper stays in pure Python.

Auth model
----------
Each user pastes their Jimeng ``sessionid`` cookie into Manor as the
"API key" of an Integration row (provider="jimeng", auth_type=api_key).
The agent layer's standard ``_resolve_bearer_token`` picks that up
and passes it as the bearer to ``call_tool`` here. We forward it as
``Authorization: Bearer <sessionid>`` to the gateway.

Where to get sessionid:
  1. Sign in at https://jimeng.jianying.com
  2. Open DevTools → Application → Cookies
  3. Copy the value of the ``sessionid`` cookie
  4. Paste into Manor's /integrations Jimeng card

Operations
----------
  * ``generate_image``  — text-to-image; returns CDN URL(s)
  * ``generate_video``  — text-to-video; returns video URL
  * ``edit_image``      — image-to-image; takes a source image URL
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)


_GATEWAY = os.environ.get("JIMENG_API_URL", "http://jimeng-api:5100").rstrip("/")
_TIMEOUT = 180.0     # Image gen typically 5–30s; videos 1–4 min.
_MAX_PAYLOAD_CHARS = 8_000


# ── MCP protocol ────────────────────────────────────────────────────────────

def list_tools() -> List[Dict[str, Any]]:
    return [
        {
            "name": "generate_image",
            "description": (
                "Generate one or more images from a Chinese or English "
                "text prompt using Jimeng (即梦). Returns CDN URLs the "
                "operator should download immediately — they're signed "
                "and expire."
            ),
            "parameters": {
                "type": "object",
                "required": ["prompt"],
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "Image description. Chinese tends to perform better than English on Jimeng.",
                    },
                    "model": {
                        "type": "string",
                        "description": "Jimeng model id. Defaults to 'jimeng-4.5'. Other choices: jimeng-4.0, jimeng-4.1, jimeng-4.6, jimeng-5.0.",
                    },
                    "ratio": {
                        "type": "string",
                        "description": "Aspect ratio. One of '1:1', '4:3', '3:4', '16:9', '9:16', '3:2', '2:3'. Default '1:1'.",
                    },
                    "resolution": {
                        "type": "string",
                        "description": "'1k' | '2k' | '4k'. Default '2k'. Higher uses more credits.",
                    },
                    "n": {
                        "type": "integer",
                        "description": "Number of variants to generate (1–4). Default 1.",
                    },
                    "intelligent_ratio": {
                        "type": "boolean",
                        "description": "When true, Jimeng infers the best ratio from the prompt (e.g. 'landscape' → 16:9). Only effective on jimeng-4.0+.",
                    },
                },
            },
        },
        {
            "name": "edit_image",
            "description": (
                "Edit/transform an existing image with a text instruction "
                "via Jimeng image-to-image. Pass the source image URL "
                "and a description of the desired change."
            ),
            "parameters": {
                "type": "object",
                "required": ["prompt", "image_url"],
                "properties": {
                    "prompt": {"type": "string"},
                    "image_url": {
                        "type": "string",
                        "description": "Public URL of the source image (Jimeng-CDN, S3, or any reachable HTTP(S) URL).",
                    },
                    "model": {"type": "string"},
                    "ratio": {"type": "string"},
                    "resolution": {"type": "string"},
                },
            },
        },
        {
            "name": "generate_video",
            "description": (
                "Generate a short video from a text prompt using "
                "Jimeng's video model. Slower than image gen — typically "
                "1–4 minutes. Returns the video URL when ready."
            ),
            "parameters": {
                "type": "object",
                "required": ["prompt"],
                "properties": {
                    "prompt": {"type": "string"},
                    "model": {
                        "type": "string",
                        "description": "Video model id. Defaults to 'jimeng-video-3.0'.",
                    },
                    "ratio": {"type": "string"},
                    "resolution": {"type": "string"},
                    "duration_seconds": {"type": "integer"},
                },
            },
        },
    ]


async def call_tool(
    name: str,
    arguments: Dict[str, Any],
    bearer_token: str,
) -> Dict[str, Any]:
    """``bearer_token`` here is the user's Jimeng sessionid cookie."""
    if not bearer_token:
        return _error(
            "Jimeng sessionid is missing. Sign in at "
            "https://jimeng.jianying.com → DevTools → Cookies → "
            "copy the ``sessionid`` value into Integrations → Jimeng."
        )

    handler = _HANDLERS.get(name)
    if handler is None:
        return _error(f"Unknown jimeng tool: {name}")

    try:
        result = await handler(arguments, bearer_token)
        return _content(result)
    except httpx.HTTPStatusError as exc:
        body = exc.response.text[:500] if exc.response is not None else ""
        return _error(f"Jimeng gateway HTTP {exc.response.status_code}: {body}")
    except httpx.RequestError as exc:
        return _error(
            f"Could not reach the Jimeng gateway at {_GATEWAY}. "
            f"Start it with `docker compose --profile jimeng up -d jimeng-api`. "
            f"({exc})"
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Jimeng tool %s crashed", name)
        return _error(f"Jimeng call failed: {exc}")


# ── Handlers ────────────────────────────────────────────────────────────────

async def _generate_image(args: Dict[str, Any], session_id: str) -> str:
    body: Dict[str, Any] = {
        "model": args.get("model") or "jimeng-4.5",
        "prompt": args.get("prompt") or "",
        "ratio": args.get("ratio") or "1:1",
        "resolution": args.get("resolution") or "2k",
    }
    if args.get("n"):
        body["n"] = int(args["n"])
    if args.get("intelligent_ratio"):
        body["intelligent_ratio"] = bool(args["intelligent_ratio"])

    data = await _post(session_id, "/v1/images/generations", body)
    return _format_image_result(data, body["prompt"])


async def _edit_image(args: Dict[str, Any], session_id: str) -> str:
    body: Dict[str, Any] = {
        "model": args.get("model") or "jimeng-4.5",
        "prompt": args.get("prompt") or "",
        "image": args.get("image_url"),
    }
    if args.get("ratio"):
        body["ratio"] = args["ratio"]
    if args.get("resolution"):
        body["resolution"] = args["resolution"]

    data = await _post(session_id, "/v1/images/generations", body)
    return _format_image_result(data, body["prompt"])


async def _generate_video(args: Dict[str, Any], session_id: str) -> str:
    body: Dict[str, Any] = {
        "model": args.get("model") or "jimeng-video-3.0",
        "prompt": args.get("prompt") or "",
    }
    if args.get("ratio"):
        body["ratio"] = args["ratio"]
    if args.get("resolution"):
        body["resolution"] = args["resolution"]
    if args.get("duration_seconds"):
        body["duration"] = int(args["duration_seconds"])

    data = await _post(session_id, "/v1/videos/generations", body)
    videos = [item.get("url") for item in (data.get("data") or []) if item.get("url")]
    return _truncate(json.dumps({
        "prompt": body["prompt"],
        "model": body["model"],
        "videos": videos,
        "primary": videos[0] if videos else None,
    }, ensure_ascii=False, indent=2))


# ── HTTP helpers ────────────────────────────────────────────────────────────

async def _post(session_id: str, path: str, body: Dict[str, Any]) -> Dict[str, Any]:
    async with httpx.AsyncClient(timeout=_TIMEOUT) as cx:
        r = await cx.post(
            f"{_GATEWAY}{path}",
            headers={
                "Authorization": f"Bearer {session_id}",
                "Content-Type": "application/json",
            },
            json=body,
        )
        r.raise_for_status()
        return r.json()


def _format_image_result(data: Dict[str, Any], prompt: str) -> str:
    items = data.get("data") or []
    urls = [it.get("url") for it in items if it.get("url")]
    return _truncate(json.dumps({
        "prompt": prompt,
        "model": data.get("model"),
        "count": len(urls),
        "images": urls,
        "primary": urls[0] if urls else None,
    }, ensure_ascii=False, indent=2))


def _truncate(s: str) -> str:
    if len(s) <= _MAX_PAYLOAD_CHARS:
        return s
    return s[:_MAX_PAYLOAD_CHARS] + "\n… (truncated)"


def _content(text: str) -> Dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": False}


def _error(msg: str) -> Dict[str, Any]:
    return {"content": [{"type": "text", "text": msg}], "isError": True}


_HANDLERS = {
    "generate_image": _generate_image,
    "edit_image": _edit_image,
    "generate_video": _generate_video,
}
