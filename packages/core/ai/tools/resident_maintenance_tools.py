"""Only-own resident maintenance status, armed by a verified public browser turn."""

import json
from pydantic import ValidationError
from packages.core.services.webchat_resident import (
    ResidentAccessError,
    ResidentReadRequest,
    current_runtime_scope,
    read_maintenance,
)


async def _status(entity_id: str, user_id: str | None = None, **kwargs):
    from packages.core.ai.runtime.surfaces import ChatSurface
    from packages.core.models.webchat_resident import WebchatResidentBinding
    from packages.core.database import async_session
    from packages.core.ai.runtime.tool_context import runtime_tool_call_context_from_kwargs

    scope = current_runtime_scope()
    context = runtime_tool_call_context_from_kwargs(kwargs, entity_id=entity_id, user_id=user_id)
    envelope = context.runtime_envelope
    if (
        scope is None
        or envelope is None
        or envelope.surface != ChatSurface.PUBLIC_CUSTOMER_CHAT
        or not envelope.principal.is_external
        or scope.entity_id != entity_id
        or scope.conversation_id != context.conversation_id
        or envelope.principal.metadata.get("channel_contact_id") != scope.contact_id
        or envelope.principal.external_sender_id != scope.session_id
    ):
        return json.dumps({"error": "resident_auth_required"})
    try:
        from packages.core.ai.runtime.tool_context import RUNTIME_TOOL_CONTEXT_KEYS

        args = ResidentReadRequest.model_validate(
            {key: value for key, value in kwargs.items() if key not in RUNTIME_TOOL_CONTEXT_KEYS}
        )
        async with async_session() as db:
            row = await db.get(WebchatResidentBinding, scope.binding_id)
            if not row or row.generation != scope.generation:
                return json.dumps({"error": "resident_auth_required"})
            return json.dumps(await read_maintenance(db, row, args), default=str)
    except ResidentAccessError as exc:
        return json.dumps({"error": exc.code, "message": exc.message})
    except ValidationError:
        return json.dumps({"error": "resident_invalid_request"})


async def _draft(entity_id: str, user_id: str | None = None, **kwargs):
    from packages.core.ai.runtime.surfaces import ChatSurface
    from packages.core.ai.runtime.tool_context import runtime_tool_call_context_from_kwargs, RUNTIME_TOOL_CONTEXT_KEYS
    from packages.core.models.webchat_resident import WebchatResidentBinding
    from packages.core.database import async_session
    from packages.core.services import webchat_resident_create as create

    scope = current_runtime_scope()
    context = runtime_tool_call_context_from_kwargs(kwargs, entity_id=entity_id, user_id=user_id)
    envelope = context.runtime_envelope
    if (
        scope is None
        or envelope is None
        or envelope.surface != ChatSurface.PUBLIC_CUSTOMER_CHAT
        or not envelope.principal.is_external
        or scope.entity_id != entity_id
        or scope.conversation_id != context.conversation_id
        or envelope.principal.metadata.get("channel_contact_id") != scope.contact_id
        or envelope.principal.external_sender_id != scope.session_id
    ):
        return json.dumps({"error": "resident_auth_required"})
    try:
        content = create.DraftContent.model_validate(
            {key: value for key, value in kwargs.items() if key not in RUNTIME_TOOL_CONTEXT_KEYS}
        )
        async with async_session() as db:
            binding = await db.get(WebchatResidentBinding, scope.binding_id)
            if not binding or binding.generation != scope.generation:
                return json.dumps({"error": "resident_auth_required"})
            draft = await create.make_draft(
                db,
                binding,
                content,
                source_call_id=context.runtime_tool_call_id,
                expected_read_generation=scope.generation,
            )
            await create.validate_grant(db, binding, expected_read_generation=scope.generation)
            return json.dumps(create.project_draft(draft), default=str)
    except ResidentAccessError as exc:
        return json.dumps({"error": exc.code, "message": exc.message})
    except ValidationError:
        return json.dumps({"error": "resident_invalid_request"})


def get_tools():
    schema = ResidentReadRequest.model_json_schema()
    schema.pop("title", None)
    result = [
        (
            {
                "type": "function",
                "function": {
                    "name": "resident_maintenance_status",
                    "description": "Read only the verified resident's maintenance requests or one request's current status. Never creates, updates, pays, assigns or reads another resident. If verification expires, ask the visitor to verify again.",
                    "parameters": schema,
                },
            },
            _status,
        )
    ]

    from packages.core.services.webchat_resident_create import DraftContent, TOOL_NAME

    schema = DraftContent.model_json_schema()
    schema.pop("title", None)
    result.append(
        (
            {
                "type": "function",
                "function": {
                    "name": TOOL_NAME,
                    "description": "Prepare a private repair draft for this verified resident. This only saves a draft: it never creates a PMS request. The visitor must review the address and fields, then explicitly confirm in the browser. Never claim a draft is submitted.",
                    "parameters": schema,
                },
            },
            _draft,
        )
    )
    return result
