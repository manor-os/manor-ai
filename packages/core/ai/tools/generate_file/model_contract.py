"""Read-only, account-scoped media model contract for generation planning."""

from __future__ import annotations

from typing import Any

from packages.core.constants.models import video_model_capabilities
from packages.core.contracts.audio_generation import (
    AudioGenerationFormat,
    AudioGenerationPurpose,
    AudioGenerationRole,
    normalize_audio_generation_purpose,
)


async def _existing_narration_profile(
    *, entity_id: str, workspace_id: str | None, task_id: str | None,
    conversation_id: str | None, mode: str | None,
) -> dict[str, Any] | None:
    """Read a pinned narrator without creating a profile or changing scope."""
    if not entity_id:
        return None
    from sqlalchemy import select

    from packages.core.database import async_session
    from packages.core.models.task import Conversation, Task
    from packages.core.models.workspace import Workspace

    selected_mode = str(mode or "").strip()
    async with async_session() as db:
        workspace = None
        if workspace_id:
            workspace = (
                await db.execute(select(Workspace).where(
                    Workspace.id == workspace_id,
                    Workspace.entity_id == entity_id,
                    Workspace.deleted_at.is_(None),
                ))
            ).scalar_one_or_none()
            if workspace is not None:
                studio = (workspace.settings or {}).get("stickman_studio_profile") or {}
                if isinstance(studio, dict) and studio.get("narration_voice_mode") == "fixed_per_workspace":
                    selected_mode = "fixed_per_workspace"
        if selected_mode == "fixed_per_workspace" and workspace is not None:
            profile = (workspace.settings or {}).get("stickman_narrator_profile")
        elif selected_mode == "random_per_task" and task_id:
            task = (
                await db.execute(select(Task).where(
                    Task.id == task_id, Task.entity_id == entity_id,
                ))
            ).scalar_one_or_none()
            profile = (task.details or {}).get("stickman_narrator_profile") if task else None
        elif selected_mode == "random_per_conversation" and conversation_id:
            conversation = (
                await db.execute(select(Conversation).where(
                    Conversation.id == conversation_id,
                    Conversation.entity_id == entity_id,
                ))
            ).scalar_one_or_none()
            profile = (conversation.meta or {}).get("stickman_narrator_profile") if conversation else None
        else:
            profile = None
    return profile if isinstance(profile, dict) else None


async def selected_media_model_contract(
    *, kind: str, entity_id: str, user_id: str, task_id: str | None = None,
    purpose: str | None = None, workspace_id: str | None = None,
    conversation_id: str | None = None, narration_voice_mode: str | None = None,
) -> dict[str, Any]:
    # Use the same actor and model resolution as the paid generation handlers.
    from packages.core.ai.tools import extended_tools

    actor_id = await extended_tools._resolve_media_task_user_id(user_id, entity_id, task_id)
    if kind == "audio":
        selected_purpose = normalize_audio_generation_purpose(purpose)
        model, role = await extended_tools._resolve_user_audio_model(
            actor_id, entity_id, purpose=selected_purpose,
        )
        account_selected_model = model
        model_source = "account_selection"
        if selected_purpose == AudioGenerationPurpose.NARRATION and (
            narration_voice_mode or workspace_id
        ):
            profile = await _existing_narration_profile(
                entity_id=entity_id, workspace_id=workspace_id,
                task_id=task_id, conversation_id=conversation_id,
                mode=narration_voice_mode,
            )
            if profile is not None:
                try:
                    checked_profile = extended_tools._validated_task_narrator_profile(profile)
                except extended_tools._NarrationProfileError as exc:
                    return {"kind": kind, "purpose": selected_purpose.value, "status": "blocked", "error": str(exc)}
                model = str(checked_profile["model"])
                model_source = "pinned_narrator_profile"
        provider = model.split("/", 1)[0] if model else ""
        schema: dict[str, Any] = {
            "prompt": {"type": "string", "required": True},
            "response_format": {
                "type": "string", "enum": AudioGenerationFormat.values(),
                "note": "Final stored artifact format; provider synthesis and conversion are automatic.",
            },
        }
        if role == AudioGenerationRole.VOICE:
            schema.update({
                "voice": {"type": "string", "default": extended_tools._default_openrouter_voice(model), "note": "Provider-specific voice name."},
                "voice_instructions": {"type": "string", "supported": extended_tools._speech_model_supports_instructions(model), "note": "tts-1 and tts-1-hd do not accept delivery instructions; other routes may send them as a parameter or prompt guidance."},
                "language": {"type": "string", "note": "Language hint; defaults to Workspace audio language."},
                "narration_voice_mode": {"type": "string", "enum": ["random_per_task", "random_per_conversation", "fixed_per_workspace"], "available_for": "narration only"},
                "duration_seconds": {"supported_as_provider_parameter": False, "note": "No exact speech duration control through this tool."},
            })
        elif model == "suno/music":
            schema.pop("response_format")  # Suno jobs retain the provider's downloaded audio format.
            schema.update({
                "instrumental": {"type": "boolean", "default": False},
                "lyrics": {"type": "string", "note": "Required in custom mode when instrumental is false."},
                "style": {"type": "string", "note": "With lyrics, enables custom mode."},
                "title": {"type": "string"},
                "duration_seconds": {"supported_as_provider_parameter": False},
                "output_format": {"controlled_by_provider": True},
            })
        elif provider == "google" and "lyria-3-clip" in model:
            schema["duration_seconds"] = {"fixed_output_seconds": 30, "supported_as_provider_parameter": False}
            schema["provider_output_format"] = "mp3"
        elif provider == "google" and "lyria-3-pro" in model:
            schema["duration_seconds"] = {"supported_as_provider_parameter": False, "note": "Added to the prompt as a target, not guaranteed by the provider."}
            schema["provider_output_formats"] = ["mp3", "wav"]
        else:
            schema["duration_seconds"] = {"supported_as_provider_parameter": False, "note": "Added to the prompt as a target, not an exact provider duration control."}
        return {
            "kind": kind,
            "purpose": selected_purpose.value,
            "role": role.value,
            "model": model,
            "expected_model": model,
            "model_source": model_source,
            "account_selected_model": account_selected_model,
            "generation_schema": schema,
            "route_note": "Credential and deployment choose the provider route at execution time; output formats may be converted after synthesis.",
            "narration_note": "Pass narration_voice_mode and the active Task/Conversation/Workspace context to inspect a pinned narrator model." if selected_purpose == AudioGenerationPurpose.NARRATION else None,
        }
    if kind == "image":
        model = await extended_tools._resolve_user_image_model(actor_id, entity_id, False)
        provider = model.split("/", 1)[0]
        return {
            "kind": kind,
            "model": model,
            "expected_model": model,
            "generation_schema": {
                "prompt": {"type": "string", "required": True},
                "size": {"type": "string", "enum": ["1024x1024", "1536x1024", "1024x1536"], "default": "1024x1024", "note": "Sent as a provider parameter on native OpenAI; other routes may use aspect_ratio or prompt guidance."},
                "aspect_ratio": {"type": "string", "enum": ["16:9", "9:16", "1:1"], "note": "Output is padded to this ratio if necessary; composition is also requested in the prompt."},
                "reference_urls": {"type": "array", "maxItems": 16},
                "quality": {"type": "string", "enum": ["low", "medium", "high"], "default": "medium", "provider_parameter": provider == "openai", "note": "Native OpenAI uses this field; other routes may ignore it."},
                "input_fidelity": {"type": "string", "enum": ["low", "high"], "provider_parameter": provider == "openai", "requires_reference": True, "note": "Native OpenAI image edits only; other routes may ignore it."},
            },
            "route_note": "The selected credential and deployment choose the provider route at execution time; provider-specific fields are not guaranteed on every route.",
        }

    model = await extended_tools._resolve_user_video_model(actor_id, entity_id)
    capabilities = video_model_capabilities(model)
    capabilities.setdefault("audio_requires_visual_reference", True)
    duration_by_mode = dict((capabilities.get("generation_schema") or {}).get("duration") or {})
    duration_source = {}
    for mode in ("text_to_video", "image_to_video"):
        if mode in duration_by_mode:
            duration_source[mode] = "model_catalog"
        else:
            # The generation tool accepts this range; upstream routes can be narrower.
            duration_by_mode[mode] = {"type": "integer", "minimum": 4, "maximum": 15, "default": 5}
            duration_source[mode] = "tool_validation; provider may impose narrower limits"
    resolutions = ["480p", "720p"] if model == "bytedance/seedance-2.0-fast" else ["480p", "720p", "1080p"]
    return {
        "kind": kind,
        "model": model,
        "expected_model": model,
        "capabilities": capabilities,
        "generation_schema": {
            "prompt": {"type": "string", "required": True},
            "duration": duration_by_mode,
            "duration_source": duration_source,
            "resolution": {"type": "string", "enum": resolutions, "default": "720p"},
            "aspect_ratio": {"type": "string", "enum": ["adaptive", "21:9", "16:9", "4:3", "3:4", "1:1", "9:16"], "default": "16:9", "note": "Seedance 2.5 uses adaptive for first/last-frame input."},
            "generate_audio": {"type": "boolean", "enum": [False, True] if capabilities.get("native_audio") else [False], "default": bool(capabilities.get("native_audio"))},
            "reference_urls": {"type": "array", "maxItems": capabilities.get("max_reference_images", 0)},
            "reference_video_urls": {"type": "array", "maxItems": capabilities.get("max_reference_videos", 0), "maxTotalSeconds": capabilities.get("max_reference_video_total_seconds")},
            "audio_reference_urls": {"type": "array", "maxItems": capabilities.get("max_audio_references", 0), "maxTotalSeconds": capabilities.get("max_audio_reference_total_seconds")},
        },
        "route_requirement": "native Volcengine" if model == "bytedance/seedance-2.5" else "depends on selected inputs and credentials",
        "route_note": "Seedance video/audio reference inputs require its native route; route availability also depends on the configured credentials.",
        "input_rules": ["Seedance 2.5 first/last frames cannot be combined with reference media", "Seedance 2.5 does not accept seed, camera_fixed, or frames", "Seedance 2.5 draft requires 480p"] if model == "bytedance/seedance-2.5" else [],
    }
