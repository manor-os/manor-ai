from __future__ import annotations

import json
from typing import Any

from packages.core.ai.runtime import runtime_generate_video_media
from packages.core.ai.runtime.tool_context import runtime_tool_call_context_from_kwargs


def _merge_image_reference(params: dict[str, Any]) -> dict[str, Any]:
    """Fold a back-compat ``source_image_url`` into ``reference_urls``.

    ``source_image_url`` is no longer advertised (use ``first_frame_url`` /
    ``reference_urls``), but old plans may still pass it. Treat it as an image
    reference for real video generation rather than a static image-loop (the
    old behavior produced "a video that is just one image"). The dead
    ``source_image_path`` / ``title_card_image_url`` aliases — which nothing
    ever produced — are gone.
    """
    out = dict(params)
    source_image = str(out.pop("source_image_url", None) or "").strip()
    if source_image:
        refs = list(out.get("reference_urls") or [])
        if source_image not in refs:
            refs.insert(0, source_image)
        out["reference_urls"] = refs
    return out


async def handle_video(
    *,
    entity_id: str,
    user_id: str,
    conversation_id: str,
    prompt: str,
    name: str,
    params: dict[str, Any],
    kwargs: dict[str, Any],
    agent_id: str | None,
) -> str:
    if not prompt:
        return json.dumps({"error": "kind=video requires prompt"}, ensure_ascii=False)

    gen_params = _merge_image_reference(params)
    runtime_context = runtime_tool_call_context_from_kwargs(kwargs)

    return await runtime_generate_video_media(
        entity_id=entity_id,
        user_id=user_id,
        prompt=prompt,
        name=name,
        params=gen_params,
        workspace_id=kwargs.get("workspace_id"),
        task_id=kwargs.get("task_id"),
        agent_id=agent_id,
        conversation_id=conversation_id or kwargs.get("conversation_id"),
        active_user_message=runtime_context.active_user_message,
        runtime_artifact_urls=runtime_context.runtime_artifact_urls,
        dependency_artifact_urls=runtime_context.dependency_artifact_urls,
    )
