from __future__ import annotations

from typing import Any

from packages.core.contracts.generate_file_input import coerce_params as coerce_params


def _merge_params(kwargs: dict[str, Any]) -> dict[str, Any]:
    params = coerce_params(kwargs.get("params"))
    params.pop("prompt", None)
    params.pop("name", None)
    params.pop("output_name", None)
    params.pop("filename", None)
    for key in (
        "size",
        "quality",
        "output_format",
        "frame_count",
        "frame_duration_ms",
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
        # A named asset given beside params is still that name, never dropped.
        "workspace_asset_key",
        "reuse_if_exists",
        "reference_assets",
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
        "language",
        "narration_voice_mode",
        "voice_role",
        "voice_instructions",
        "response_format",
        "format",
        "model",
        "expected_model",
    ):
        if kwargs.get(key) is not None and key not in params:
            params[key] = kwargs[key]
    return params


async def _scope_workspace_output_name(
    *,
    entity_id: str,
    workspace_id: str | None,
    task_id: str | None = None,
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
        task_id=task_id,
    )
    return scope_workspace_artifact_path(
        name,
        workspace_base_dir,
        default_subdir=default_subdir,
    )
