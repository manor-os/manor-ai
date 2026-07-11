from __future__ import annotations

import json
from typing import Any


def coerce_params(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        if isinstance(parsed, dict):
            return dict(parsed)
    return {}


def _merge_params(kwargs: dict[str, Any]) -> dict[str, Any]:
    params = coerce_params(kwargs.get("params"))
    params.pop("prompt", None)
    params.pop("name", None)
    params.pop("output_name", None)
    params.pop("filename", None)
    for key in (
        "size",
        "quality",
        "duration",
        "frames",
        "resolution",
        "aspect_ratio",
        "first_frame_url",
        "last_frame_url",
        "reference_url",
        "reference_urls",
        "reference_video_url",
        "reference_video_urls",
        "video_reference_url",
        "video_reference_urls",
        "audio_reference_url",
        "audio_reference_urls",
        "reference_audio_url",
        "reference_audio_urls",
        "audio_url",
        "image_url",
        "input_image_url",
        "input_image_urls",
        "input_fidelity",
        "save_to_knowledge",
        "expected_sha256",
        "seed",
        "generate_audio",
        "requires_reference_media",
        "reference_media_required",
        "return_last_frame",
        "camera_fixed",
        "watermark",
        "draft",
        "purpose",
        "duration_seconds",
        "voice",
        "response_format",
        "format",
        "model",
    ):
        if kwargs.get(key) is not None and key not in params:
            params[key] = kwargs[key]
    return params


async def _scope_workspace_output_name(
    *,
    entity_id: str,
    workspace_id: str | None,
    name: str,
    default_subdir: str,
) -> str:
    from packages.core.services.generated_media_naming import (
        resolve_workspace_artifact_base_dir,
        scope_workspace_artifact_path,
    )

    workspace_base_dir = await resolve_workspace_artifact_base_dir(
        entity_id=entity_id,
        workspace_id=workspace_id,
    )
    return scope_workspace_artifact_path(
        name,
        workspace_base_dir,
        default_subdir=default_subdir,
    )
