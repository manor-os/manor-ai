from __future__ import annotations

import json
from typing import Any

from packages.core.ai.runtime.tool_context import runtime_tool_call_context_from_kwargs

from . import common
from .audio import handle_audio
from .code import handle_code
from .diagram import handle_diagram
from .document import handle_document
from .image import handle_image
from .pdf import handle_pdf
from .presentation import handle_presentation
from .schema import (
    GENERATE_FILE_SCHEMA,
    VIDEO_ASPECT_RATIO_CHOICES,
    VIDEO_DURATION_CHOICES,
    VIDEO_RESOLUTION_CHOICES,
    VIDEO_SEEDANCE_FAST_RESOLUTION_CHOICES,
    _CAPABILITIES,
)
from .spreadsheet import handle_spreadsheet
from .video import handle_video
from .word_document import handle_word_document


async def _active_document_skill_media_guard(
    conversation_id: str | None, kind: str
) -> str | None:
    """Block video/audio generation while a document/office sandbox skill drives.

    A pptx/docx/xlsx skill is driven by the parent agent through the sandbox; if
    it gets stuck it can call generate_file(kind="video") with a prompt bled from
    earlier in the conversation, ending a deck request in an unrelated clip. The
    active sandbox conversation context records the driving skill (its slug is
    stored as skill_id for built-ins), so refuse the media kind when that skill
    is a document one. Best-effort: any lookup failure allows the call.
    """
    if not conversation_id:
        return None
    try:
        from packages.core.ai.runtime import runtime_load_sandbox_context
        from packages.core.ai.runtime.skills import document_skill_media_guard

        ctx = await runtime_load_sandbox_context(conversation_id)
        skill_slug = (ctx or {}).get("skill_id") if isinstance(ctx, dict) else None
        return document_skill_media_guard(skill_slug, "generate_file", {"kind": kind})
    except Exception:
        return None


async def _generate_file_handler(
    entity_id: str = "",
    user_id: str = "",
    conversation_id: str = "",
    **kwargs: Any,
) -> str:
    runtime_context = runtime_tool_call_context_from_kwargs(kwargs)
    kind = str(kwargs.get("kind") or "").strip().lower().replace("-", "_")
    if kind == "search":
        return json.dumps({
            "capabilities": _CAPABILITIES,
            "parameter_options": {
                "video": {
                    "duration": VIDEO_DURATION_CHOICES,
                    "resolution": VIDEO_RESOLUTION_CHOICES,
                    "aspect_ratio": VIDEO_ASPECT_RATIO_CHOICES,
                    "seedance_reference_limits": {
                        "reference_urls": 9,
                        "reference_video_urls": 3,
                        "audio_reference_urls": 3,
                        "audio_requires_visual_reference": True,
                        "video_audio_refs_require_native_official_route": True,
                    },
                    "generate_audio_default": True,
                    "provider_audio_policy": (
                        "Seedance video clips request native provider audio by default "
                        "when the official route supports it. Set generate_audio=false "
                        "for a silent clean picture. Use audio_reference_urls/audio_url "
                        "only when timing or performance should be followed. For editable "
                        "finals, generate and mix dialogue, BGM, ambience, SFX, and "
                        "subtitles separately after the picture clip."
                    ),
                    "resolution_by_model": {
                        "bytedance/seedance-2.0": VIDEO_RESOLUTION_CHOICES,
                        "bytedance/seedance-2.0-fast": VIDEO_SEEDANCE_FAST_RESOLUTION_CHOICES,
                    },
                },
                "audio": {
                    "purpose": [
                        "speech",
                        "dialogue",
                        "narration",
                        "music",
                        "ambience",
                        "soundscape",
                        "sfx",
                        "transition",
                    ],
                    "duration_seconds": 12,
                    "response_format": ["mp3", "wav", "flac", "opus", "pcm", "pcm16"],
                },
                "diagram": {
                    "file_type": "diagram.json",
                    "output": "editable canvas JSON for Diagram Canvas",
                },
                "code": {
                    "files": [{"path": "index.html", "content": "..."}],
                    "entry": "index.html",
                    "output": "multi-file code bundle saved to Knowledge",
                },
            },
        }, ensure_ascii=False)
    if kind not in _CAPABILITIES:
        return json.dumps({
            "error": f"Unknown generate_file kind: {kind or '(empty)'}",
            "capabilities": _CAPABILITIES,
        }, ensure_ascii=False)

    if kind in {"video", "audio"}:
        guard = await _active_document_skill_media_guard(
            runtime_context.conversation_id, kind
        )
        if guard is not None:
            return guard

    params = common._merge_params(kwargs)
    raw_params = common.coerce_params(kwargs.get("params"))
    has_bundle_files = (
        raw_params.get("files") is not None
        or kwargs.get("files") is not None
    )
    if kind == "document" and has_bundle_files:
        kind = "code"
    prompt = str(kwargs.get("prompt") or "").strip()
    agent_id = kwargs.get("agent_id") or runtime_context.agent_id
    name = str(
        kwargs.get("name")
        or raw_params.get("name")
        or raw_params.get("output_name")
        or raw_params.get("filename")
        or ""
    ).strip()

    handler_kwargs = {
        "entity_id": entity_id,
        "user_id": user_id,
        "conversation_id": conversation_id,
        "prompt": prompt,
        "name": name,
        "params": params,
        "kwargs": kwargs,
        "agent_id": agent_id,
    }

    if kind == "diagram":
        return await handle_diagram(**handler_kwargs)
    if kind == "code":
        return await handle_code(**handler_kwargs)
    if kind == "document":
        return await handle_document(**handler_kwargs)
    if kind == "word_document":
        return await handle_word_document(**handler_kwargs)
    if kind == "pdf":
        return await handle_pdf(**handler_kwargs)
    if kind == "presentation":
        return await handle_presentation(**handler_kwargs)
    if kind == "spreadsheet":
        return await handle_spreadsheet(**handler_kwargs)
    if kind == "image":
        return await handle_image(**handler_kwargs)
    if kind == "video":
        return await handle_video(**handler_kwargs)
    if kind == "audio":
        return await handle_audio(**handler_kwargs)

    return json.dumps({"error": f"Unhandled kind: {kind}"}, ensure_ascii=False)


def get_tools() -> list[tuple[dict[str, Any], Any]]:
    return [(GENERATE_FILE_SCHEMA, _generate_file_handler)]
