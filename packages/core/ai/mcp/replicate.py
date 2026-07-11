"""Replicate MCP server — image / video / audio generation via the
Replicate API.

Why Replicate over per-provider wrappers (Flux / Luma / LTX directly)?
One API key gives access to hundreds of models. Manor agents call the
typed tools below for the common cases (image, video) and fall back to
``run_model`` for anything else.

Auth: bearer_token = the user's Replicate API token (``r8_...``),
stored as an entity Integration with provider="replicate" and
credentials ``{"api_key": "r8_..."}``. Replicate uses
``Authorization: Bearer <token>``.

We use the ``Prefer: wait`` header for sync responses on fast models
(Flux Schnell is typically 1-3s). Slow models (video) fall back to
async polling.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, List

import httpx

logger = logging.getLogger(__name__)

_API = "https://api.replicate.com/v1"
_TIMEOUT = 240.0
_MAX_PAYLOAD_CHARS = 8_000

# A small allowlist of "default" model IDs per intent. Agents can
# override per-call via ``model``. Keep these conservative and
# inexpensive — demo and quick iteration matter more than max quality.
_DEFAULT_IMAGE_MODEL = "black-forest-labs/flux-schnell"
_DEFAULT_VIDEO_MODEL = "luma/ray-flash-2-540p"


# ── MCP protocol ────────────────────────────────────────────────────────────

def list_tools() -> List[Dict[str, Any]]:
    return [
        {
            "name": "generate_image",
            "description": (
                "Generate an image from a text prompt using a Replicate "
                "model (default: Flux Schnell, ~$0.003/image, 1-3s). "
                "Returns the resulting image URL(s)."
            ),
            "parameters": {
                "type": "object",
                "required": ["prompt"],
                "properties": {
                    "prompt": {"type": "string"},
                    "model": {
                        "type": "string",
                        "description": "owner/name of the model. Default 'black-forest-labs/flux-schnell'. Other good choices: 'black-forest-labs/flux-1.1-pro', 'ideogram-ai/ideogram-v3-turbo'.",
                    },
                    "aspect_ratio": {
                        "type": "string",
                        "description": "'1:1', '16:9', '9:16', '4:3', '3:4'. Default '1:1'.",
                    },
                    "num_outputs": {
                        "type": "integer",
                        "description": "Number of variants (1-4). Default 1.",
                    },
                    "seed": {"type": "integer"},
                },
            },
        },
        {
            "name": "generate_video",
            "description": (
                "Generate a short video from a text prompt using a "
                "Replicate video model (default: Luma Ray Flash 540p — "
                "~5s clip, ~$0.05/s, completes in 30-90s). Returns the "
                "video URL once ready. Slow — set agent timeout >= 180s."
            ),
            "parameters": {
                "type": "object",
                "required": ["prompt"],
                "properties": {
                    "prompt": {"type": "string"},
                    "model": {
                        "type": "string",
                        "description": "owner/name. Default 'luma/ray-flash-2-540p'. Cheaper option: 'lightricks/ltx-video'. Pricier+longer: 'minimax/video-01'.",
                    },
                    "duration": {
                        "type": "integer",
                        "description": "Clip length in seconds. Defaults vary by model (5s for Luma Flash).",
                    },
                    "aspect_ratio": {"type": "string"},
                },
            },
        },
        {
            "name": "run_model",
            "description": (
                "Escape hatch for any Replicate model not covered by the "
                "typed tools above. Pass the model id and the input dict "
                "exactly as the model expects. Polls for completion."
            ),
            "parameters": {
                "type": "object",
                "required": ["model", "input"],
                "properties": {
                    "model": {
                        "type": "string",
                        "description": "owner/name (e.g. 'meta/musicgen', 'openai/whisper').",
                    },
                    "input": {
                        "type": "object",
                        "description": "Model-specific input. See the model's Replicate page for the schema.",
                    },
                },
            },
        },
    ]


async def call_tool(
    name: str,
    arguments: Dict[str, Any],
    bearer_token: str,
) -> Dict[str, Any]:
    if not bearer_token:
        return _error(
            "Replicate API token is missing. Get one at "
            "https://replicate.com/account/api-tokens and add it under "
            "Integrations → Replicate."
        )

    handler = _HANDLERS.get(name)
    if handler is None:
        return _error(f"Unknown replicate tool: {name}")

    try:
        result = await handler(arguments, bearer_token)
        return _content(result)
    except httpx.HTTPStatusError as exc:
        body = exc.response.text[:500] if exc.response is not None else ""
        return _error(f"Replicate HTTP {exc.response.status_code}: {body}")
    except Exception as exc:  # noqa: BLE001
        logger.exception("Replicate tool %s crashed", name)
        return _error(f"Replicate call failed: {exc}")


# ── Handlers ────────────────────────────────────────────────────────────────

async def _generate_image(args: Dict[str, Any], token: str) -> str:
    model = (args.get("model") or _DEFAULT_IMAGE_MODEL).strip()
    inp: Dict[str, Any] = {
        "prompt": args.get("prompt") or "",
        "aspect_ratio": args.get("aspect_ratio") or "1:1",
    }
    if args.get("num_outputs"):
        inp["num_outputs"] = int(args["num_outputs"])
    if args.get("seed") is not None:
        inp["seed"] = int(args["seed"])
    return await _run_and_format(token, model, inp, intent="image")


async def _generate_video(args: Dict[str, Any], token: str) -> str:
    model = (args.get("model") or _DEFAULT_VIDEO_MODEL).strip()
    inp: Dict[str, Any] = {"prompt": args.get("prompt") or ""}
    if args.get("aspect_ratio"):
        inp["aspect_ratio"] = args["aspect_ratio"]
    if args.get("duration"):
        inp["duration"] = int(args["duration"])
    return await _run_and_format(token, model, inp, intent="video")


async def _run_model(args: Dict[str, Any], token: str) -> str:
    model = (args.get("model") or "").strip()
    inp = args.get("input") or {}
    if not model:
        raise ValueError("model is required")
    if not isinstance(inp, dict):
        raise ValueError("input must be a JSON object")
    return await _run_and_format(token, model, inp, intent="generic")


# ── Replicate API client ────────────────────────────────────────────────────

async def _run_and_format(
    token: str, model: str, input_payload: Dict[str, Any], *, intent: str,
) -> str:
    """Submit a prediction, poll until done, return a JSON-ish summary."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        # Sync-mode hint — Replicate waits up to 60s before falling
        # back to async. Fast models (Flux Schnell) usually complete
        # in this window.
        "Prefer": "wait=60",
    }
    submit_url = f"{_API}/models/{model}/predictions"

    async with httpx.AsyncClient(timeout=_TIMEOUT) as cx:
        r = await cx.post(submit_url, headers=headers, json={"input": input_payload})
        r.raise_for_status()
        body = r.json()

        # Sync hit — the response already carries output.
        if body.get("status") == "succeeded":
            return _format_output(body, intent)
        if body.get("status") in ("failed", "canceled"):
            raise RuntimeError(_format_failure(body))

        # Async path: poll the get URL.
        get_url = (body.get("urls") or {}).get("get")
        if not get_url:
            raise RuntimeError(json.dumps({"error": "Replicate did not return a poll URL", "raw": body}))

        deadline = asyncio.get_event_loop().time() + _TIMEOUT
        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(2.0)
            poll = await cx.get(get_url, headers={"Authorization": f"Bearer {token}"})
            poll.raise_for_status()
            poll_body = poll.json()
            status = poll_body.get("status")
            if status == "succeeded":
                return _format_output(poll_body, intent)
            if status in ("failed", "canceled"):
                raise RuntimeError(_format_failure(poll_body))

    raise RuntimeError(f"Replicate prediction did not complete within {_TIMEOUT}s")


def _format_output(body: Dict[str, Any], intent: str) -> str:
    output = body.get("output")
    urls: List[str] = []
    if isinstance(output, list):
        urls = [str(x) for x in output if isinstance(x, str)]
    elif isinstance(output, str):
        urls = [output]
    return _truncate(json.dumps({
        "intent": intent,
        "model": body.get("model"),
        "id": body.get("id"),
        "status": "succeeded",
        "prompt": (body.get("input") or {}).get("prompt"),
        "outputs": urls,
        "primary": urls[0] if urls else None,
        "metrics": body.get("metrics") or {},
    }, ensure_ascii=False, indent=2))


def _format_failure(body: Dict[str, Any]) -> str:
    return _truncate(json.dumps({
        "status": body.get("status"),
        "error": body.get("error"),
        "logs": (body.get("logs") or "")[-1500:],
    }, ensure_ascii=False, indent=2))


def _truncate(s: str) -> str:
    return s if len(s) <= _MAX_PAYLOAD_CHARS else s[:_MAX_PAYLOAD_CHARS] + "\n… (truncated)"


def _content(text: str) -> Dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": False}


from packages.core.ai.mcp._http import mcp_err as _error  # noqa: E402, F401


_HANDLERS = {
    "generate_image": _generate_image,
    "generate_video": _generate_video,
    "run_model": _run_model,
}
