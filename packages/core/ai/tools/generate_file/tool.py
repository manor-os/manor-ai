from __future__ import annotations

import json
from typing import Any

from packages.core.contracts.audio_generation import (
    AudioGenerationFormat,
    AudioGenerationPurpose,
    GenerateFileKind,
)
from packages.core.ai.runtime.tool_context import runtime_tool_call_context_from_kwargs
from packages.core.contracts.file_engine import (
    OPERATION_GENERATION_TYPES,
    TEMPLATE_OPERATION_GENERATION_TYPES,
    file_type_from_path,
)
from packages.core.contracts.generate_file_input import generate_file_input_error

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
    *,
    entity_id: str | None,
    user_id: str | None,
    agent_id: str | None,
    binding_key: str | None,
    kind: str,
) -> str | None:
    """Block video/audio generation while a document/office sandbox skill drives.

    A pptx/docx/xlsx skill is driven by the parent agent through the sandbox; if
    it gets stuck it can call generate_file(kind="video") with a prompt bled from
    earlier in the conversation, ending a deck request in an unrelated clip. The
    calling agent's view of the conversation sandbox records the Skill it drives
    (its slug is stored as skill_id for built-ins), so refuse the media kind when
    that skill is a document one. A Skill another agent drives in the shared
    sandbox does not constrain this agent. Best-effort: any lookup failure
    allows the call.
    """
    try:
        from packages.core.ai.runtime import (
            RuntimeSandboxKind,
            runtime_agent_sandbox,
            runtime_agent_skill_in,
            runtime_sandbox_scope,
        )
        from packages.core.ai.runtime.skills import document_skill_media_guard

        scope = runtime_sandbox_scope(
            entity_id=entity_id,
            user_id=user_id,
            binding_key=binding_key,
        )
        if scope is None:
            return None
        driving = await runtime_agent_sandbox(scope, agent_id, RuntimeSandboxKind.SKILL)
        # The Skill THIS agent drives there (others may load beside it).
        held = await runtime_agent_skill_in(scope, agent_id, driving[0]) if driving is not None else None
        skill_slug = (held or {}).get("skill_id")
        return document_skill_media_guard(skill_slug, "generate_file", {"kind": kind})
    except Exception:
        return None


def _apply_authorized_destination(
    handler_kwargs: dict[str, Any],
    arguments: dict[str, Any],
) -> None:
    """Write exactly where the public call's authorization receipt points.

    The receipt is minted from ``generate_file_destination`` before the
    handler runs. Taking the materializer's name and typed directory from that
    same resolution is what keeps the inner write inside the receipt; a
    handler-local prefix (the 2026-10-06 ``images/`` incident) or bucket is an
    ``authorization_receipt_mismatch`` waiting to happen.
    """
    from packages.core.ai.runtime.authorization_receipts import (
        WorkspaceFileAuthorizationResourceFactory,
        workspace_file_resource_id,
    )

    requested = handler_kwargs["name"] or None
    if requested is not None and workspace_file_resource_id(requested) is None:
        # Leave an unsafe name to the materializer's own rejection.
        return
    destination = WorkspaceFileAuthorizationResourceFactory.generate_file_destination(
        arguments, requested,
    )
    if destination is None:
        return
    if destination.name is not None:
        handler_kwargs["name"] = destination.name
    handler_kwargs["default_subdir"] = destination.directory


async def _generate_file_handler(
    entity_id: str = "",
    user_id: str = "",
    conversation_id: str = "",
    **kwargs: Any,
) -> str:
    from packages.core.services.document_service import StorageLimitExceeded

    try:
        return await _generate_file(entity_id=entity_id, user_id=user_id, conversation_id=conversation_id, **kwargs)
    except StorageLimitExceeded as exc:
        # A full Knowledge store is the person's to resolve (free space or a
        # larger plan); retrying or repairing the file would only fail again.
        return json.dumps({"status": "blocked", "code": "knowledge_storage_limit", "error": str(exc),
                           "plan": exc.plan, "limit": exc.limit, "current": exc.current}, ensure_ascii=False)


async def _generate_file(
    entity_id: str = "",
    user_id: str = "",
    conversation_id: str = "",
    **kwargs: Any,
) -> str:
    runtime_context = runtime_tool_call_context_from_kwargs(
        kwargs,
        user_id=user_id,
        conversation_id=conversation_id,
    )
    input_error = generate_file_input_error(kwargs)
    if input_error is not None:
        return json.dumps({"error": input_error}, ensure_ascii=False)
    raw_kind = str(kwargs.get("kind") or "").strip().lower().replace("-", "_")
    try:
        kind = GenerateFileKind(raw_kind)
    except ValueError:
        return json.dumps({
            "error": f"Unknown generate_file kind: {raw_kind or '(empty)'}",
            "capabilities": _CAPABILITIES,
        }, ensure_ascii=False)
    if kind is GenerateFileKind.SEARCH:
        search_params = common.coerce_params(kwargs.get("params"))
        media_kind = str(kwargs.get("media_kind") or search_params.get("media_kind") or "").strip().lower()
        if media_kind and media_kind not in {"image", "video", "audio"}:
            return json.dumps({"error": "media_kind must be image, video, or audio"})
        if media_kind:
            from .model_contract import selected_media_model_contract

            selected_model = await selected_media_model_contract(
                kind=media_kind,
                entity_id=entity_id,
                user_id=runtime_context.user_id or user_id,
                task_id=runtime_context.task_id,
                purpose=search_params.get("purpose") or kwargs.get("purpose"),
                workspace_id=runtime_context.workspace_id,
                conversation_id=runtime_context.conversation_id,
                narration_voice_mode=search_params.get("narration_voice_mode") or kwargs.get("narration_voice_mode"),
                project_id=runtime_context.workflow_project_id,
                voice_role=search_params.get("voice_role") or kwargs.get("voice_role"),
            )
            return json.dumps({"selected_model": selected_model}, ensure_ascii=False)
        result = {
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
                        "by_model": {
                            "bytedance/seedance-2.5": {
                                "reference_urls": 30,
                                "reference_video_urls": 10,
                                "audio_reference_urls": 10,
                                "reference_video_total_seconds": 30,
                                "audio_reference_total_seconds": 30,
                                "audio_requires_visual_reference": False,
                            },
                        },
                        "video_audio_refs_require_native_official_route": True,
                    },
                    "duration_by_model": {
                        "bytedance/seedance-2.0": list(range(4, 16)),
                        "bytedance/seedance-2.5": list(range(4, 31)),
                    },
                    "native_only_models": ["bytedance/seedance-2.5"],
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
                        "bytedance/seedance-2.5": VIDEO_RESOLUTION_CHOICES,
                        "bytedance/seedance-2.0": VIDEO_RESOLUTION_CHOICES,
                        "bytedance/seedance-2.0-fast": VIDEO_SEEDANCE_FAST_RESOLUTION_CHOICES,
                        "bytedance/seedance-v1.0-pro-fast": VIDEO_RESOLUTION_CHOICES,
                    },
                },
                "audio": {
                    "purpose": AudioGenerationPurpose.values(),
                    "duration_seconds": 12,
                    "response_format": AudioGenerationFormat.values(),
                    "format_policy": (
                        "response_format is the final stored artifact format. Provider "
                        "synthesis format and conversion are automatic; if the provider "
                        "is unavailable, retry the same request instead of changing formats."
                    ),
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
        }
        return json.dumps(result, ensure_ascii=False)
    if kind in {GenerateFileKind.VIDEO, GenerateFileKind.AUDIO}:
        guard = await _active_document_skill_media_guard(
            entity_id=entity_id,
            user_id=runtime_context.user_id,
            agent_id=runtime_context.agent_id,
            binding_key=runtime_context.sandbox_binding_key or runtime_context.conversation_id,
            kind=kind,
        )
        if guard is not None:
            return guard

    params = common._merge_params(kwargs)
    raw_params = common.coerce_params(kwargs.get("params"))
    has_bundle_files = (
        raw_params.get("files") is not None
        or kwargs.get("files") is not None
    )
    if kind is GenerateFileKind.DOCUMENT and has_bundle_files:
        kind = GenerateFileKind.CODE
    prompt = str(kwargs.get("prompt") or "").strip()
    user_id = runtime_context.user_id or ""
    conversation_id = runtime_context.conversation_id or ""
    agent_id = runtime_context.agent_id
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

    if "source" in kwargs or "source" in raw_params:
        if kind not in {GenerateFileKind.DOCUMENT, GenerateFileKind.PDF, GenerateFileKind.IMAGE}:
            return json.dumps({"error": "source conversion requires kind=document, pdf or image"})
        if any(source.get(key) is not None for source in (kwargs, raw_params) for key in (
            "content", "prompt", "files", "options", "operations", "template", "expected_sha256",
        )):
            return json.dumps({"error": "source conversion cannot be combined with content/prompt/files/options/operations/template/expected_sha256"})
        if "source" in kwargs and "source" in raw_params and kwargs["source"] != raw_params["source"]:
            return json.dumps({"error": "Conflicting source in top-level arguments and params"})
        source = kwargs["source"] if "source" in kwargs else raw_params["source"]
        if not isinstance(source, dict):
            return json.dumps({"error": "source requires path and expected_sha256 from read_file"})
        requested_type = str(kwargs.get("file_type") or raw_params.get("file_type") or ("pdf" if kind is GenerateFileKind.PDF else file_type_from_path(name))).lower().lstrip(".")
        if kwargs.get("file_type") and raw_params.get("file_type") and str(raw_params["file_type"]).lower().lstrip(".") != requested_type:
            return json.dumps({"error": "Conflicting file_type in top-level arguments and params"})
        if not name or not requested_type or (kind is GenerateFileKind.PDF and requested_type != "pdf"):
            return json.dumps({"error": "source conversion requires a new output name and matching target file_type"})
        if kind is GenerateFileKind.IMAGE:
            from packages.core.contracts.file_engine import IMAGE_CONVERSION_TARGETS, IMAGE_CONVERSION_TYPES

            if file_type_from_path(str(source.get("path") or "")) not in IMAGE_CONVERSION_TYPES or requested_type not in IMAGE_CONVERSION_TARGETS:
                return json.dumps({"error": "kind=image source conversion requires an image source and PNG/JPEG/WebP target"})
        _apply_authorized_destination(handler_kwargs, kwargs)
        handler_kwargs["kwargs"] = {**kwargs, "source": source, "file_type": requested_type}
        return await handle_document(**handler_kwargs)

    has_template = "template" in kwargs or "template" in raw_params
    if has_template or "operations" in kwargs or "operations" in raw_params:
        office_types = {
            GenerateFileKind.WORD_DOCUMENT: "docx",
            GenerateFileKind.PRESENTATION: "pptx",
            GenerateFileKind.SPREADSHEET: "xlsx",
        }
        if kind not in {GenerateFileKind.DOCUMENT, *office_types}:
            return json.dumps({"error": "operations generation supports document/word_document/presentation/spreadsheet only"})
        if "operations" in kwargs and "operations" in raw_params and kwargs["operations"] != raw_params["operations"]:
            return json.dumps({"error": "Conflicting operations in top-level arguments and params"})
        operations = kwargs["operations"] if "operations" in kwargs else raw_params.get("operations")
        if not isinstance(operations, list) or not operations:
            return json.dumps({"error": "operations must contain at least one patch operation"})
        if any(source.get(key) is not None for source in (kwargs, raw_params) for key in ("content", "prompt", "files", "options")):
            return json.dumps({"error": "operations cannot be combined with content, prompt, files or options"})
        requested_type = str(kwargs.get("file_type") or params.get("file_type") or "").lower().lstrip(".")
        if kwargs.get("file_type") and params.get("file_type") and str(params["file_type"]).lower().lstrip(".") != requested_type:
            return json.dumps({"error": "Conflicting file_type in top-level arguments and params"})
        selected_type = requested_type or office_types.get(kind) or file_type_from_path(name)
        allowed_types = (
            TEMPLATE_OPERATION_GENERATION_TYPES if has_template else OPERATION_GENERATION_TYPES
        )
        kind_matches = (
            kind not in office_types
            or office_types[kind] == selected_type
            or (kind is GenerateFileKind.SPREADSHEET and selected_type == "xlsm")
        )
        if selected_type not in allowed_types or not kind_matches:
            return json.dumps({
                "error": (
                    "operations file_type must match the selected Office kind "
                    "(docx/pptx/xlsx; xlsm requires a template)"
                ),
            })
        handler_kwargs["kwargs"] = {**kwargs, "operations": operations, "file_type": selected_type}
        if has_template:
            if "template" in kwargs and "template" in raw_params and kwargs["template"] != raw_params["template"]:
                return json.dumps({"error": "Conflicting template in top-level arguments and params"})
            template = kwargs["template"] if "template" in kwargs else raw_params["template"]
            if not isinstance(template, dict):
                return json.dumps({"error": "template requires path and expected_sha256 from read_file"})
            handler_kwargs["kwargs"]["template"] = template
        # Every office kind takes the document (operations) route; its name
        # and kind directory come from the same resolution as the receipt.
        _apply_authorized_destination(handler_kwargs, kwargs)
        return await handle_document(**handler_kwargs)

    if kind is GenerateFileKind.DIAGRAM:
        return await handle_diagram(**handler_kwargs)
    if kind is GenerateFileKind.CODE:
        return await handle_code(**handler_kwargs)
    if kind is GenerateFileKind.DOCUMENT:
        # A fresh Office file uses the same specialist Skill regardless of
        # whether the model called the generic document alias. Explicit
        # template/patch/conversion requests already took their typed branch.
        requested_type = str(
            kwargs.get("file_type") or raw_params.get("file_type") or file_type_from_path(name)
        ).strip().lower().lstrip(".")
        office_handlers = {
            "pdf": handle_pdf,
            "docx": handle_word_document,
            "pptx": handle_presentation,
            "xlsx": handle_spreadsheet,
        }
        if requested_type in office_handlers:
            return await office_handlers[requested_type](**handler_kwargs)
        _apply_authorized_destination(handler_kwargs, kwargs)
        return await handle_document(**handler_kwargs)
    if kind is GenerateFileKind.WORD_DOCUMENT:
        return await handle_word_document(**handler_kwargs)
    if kind is GenerateFileKind.PDF:
        return await handle_pdf(**handler_kwargs)
    if kind is GenerateFileKind.PRESENTATION:
        return await handle_presentation(**handler_kwargs)
    if kind is GenerateFileKind.SPREADSHEET:
        return await handle_spreadsheet(**handler_kwargs)
    if kind is GenerateFileKind.IMAGE:
        return await handle_image(**handler_kwargs)
    if kind is GenerateFileKind.VIDEO:
        return await handle_video(**handler_kwargs)
    if kind is GenerateFileKind.AUDIO:
        return await handle_audio(**handler_kwargs)

    return json.dumps({"error": f"Unhandled kind: {kind}"}, ensure_ascii=False)


def get_tools() -> list[tuple[dict[str, Any], Any]]:
    return [(GENERATE_FILE_SCHEMA, _generate_file_handler)]
