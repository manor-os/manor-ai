from __future__ import annotations

import copy
import asyncio
import json
import logging
from collections.abc import Mapping, Sequence
from typing import Any, Awaitable, Callable

from packages.core.ai.runtime.control import RuntimeTurnAborted
from packages.core.ai.runtime.surfaces import ChatSurface
from packages.core.ai.runtime.workspace_creation_authorization import (
    WorkspaceCreationAuthorizationStatus,
)


logger = logging.getLogger(__name__)

AGENT_BINDING_REPAIR_TOOLS = frozenset({
    "ws_search_capabilities", "ws_request_custom_agent", "ws_lint_draft",
})


# The authorization classifier starts with the turn; by the time the model
# decides to start a draft it has almost always finished.
WORKSPACE_CREATION_AUTHORIZATION_WAIT_SECONDS = 15.0


async def _workspace_draft_start_allowed(runtime_envelope: Any | None) -> bool:
    """Require explicit create intent before Global Chat may persist a draft."""

    if runtime_envelope is None:
        return True
    surface = getattr(runtime_envelope, "surface", None)
    surface_value = getattr(surface, "value", surface)
    if surface_value != ChatSurface.GLOBAL_OWNER_CHAT.value:
        return True
    metadata = getattr(runtime_envelope, "metadata", None)
    if not isinstance(metadata, Mapping):
        return False
    authorization = metadata.get("workspace_creation_authorization")
    if (
        isinstance(authorization, Mapping)
        and authorization.get("status") == WorkspaceCreationAuthorizationStatus.DEFERRED.value
    ):
        from packages.core.services.chat_intent_routing import wait_for_deferred_chat_intent

        published = await wait_for_deferred_chat_intent(
            authorization.get("key"),
            timeout=WORKSPACE_CREATION_AUTHORIZATION_WAIT_SECONDS,
        )
        authorization = (
            published.get("workspace_creation_authorization")
            if isinstance(published, Mapping)
            else None
        )
    return bool(
        isinstance(authorization, Mapping)
        and authorization.get("status")
        == WorkspaceCreationAuthorizationStatus.AUTHORIZED.value
    )


def _draft_artifact_payload(draft: Any, reply: str) -> dict[str, Any]:
    fields = dict(getattr(draft, "fields", None) or {})
    return {
        "artifact_kind": "workspace_draft",
        "draft_id": draft.id,
        "status": draft.status,
        "ready": bool(draft.ready),
        "missing": list(getattr(draft, "missing", None) or []),
        "fields": fields,
        "assistant_reply": reply,
        "title": fields.get("name") or "Workspace draft",
    }


async def runtime_start_workspace_draft_action(
    *,
    entity_id: str,
    user_id: str = "",
    initial_brief: str | None = None,
    runtime_envelope: Any | None = None,
) -> str:
    """Start a workspace draft through the Runtime draft action boundary."""

    if not entity_id or not user_id:
        return json.dumps({"error": "workspace draft user context missing"})
    if not await _workspace_draft_start_allowed(runtime_envelope):
        return json.dumps({
            "error": "workspace_creation_confirmation_required",
            "message": (
                "Start a Workspace draft only after the user explicitly asks to "
                "create a new Workspace."
            ),
        })

    try:
        from packages.core.database import async_session
        from packages.core.services.plan_gate import WorkspacePlanLimitError, check
        from packages.core.services.workspace_draft_service import start_draft

        async with async_session() as db:
            gate = await check(db, entity_id, "workspaces")
            if not gate.allowed:
                limit_error = WorkspacePlanLimitError(gate)
                return json.dumps({
                    "error": gate.message,
                    "detail": limit_error.detail,
                })
            reply, draft = await start_draft(
                db,
                entity_id=entity_id,
                user_id=user_id,
                initial_brief=(initial_brief or "").strip() or None,
            )
            await db.commit()

        return json.dumps({
            **_draft_artifact_payload(draft, reply),
            "next_step": (
                "Keep configuring this draft in the current Chat. Use "
                "continue_workspace_draft for later user changes."
            ),
        })
    except Exception as exc:
        logger.exception("start_workspace_draft failed")
        return json.dumps({"error": f"failed to start draft: {exc}"})


async def runtime_continue_workspace_draft_action(
    *,
    entity_id: str,
    user_id: str,
    draft_id: str,
    message: str,
) -> str:
    """Apply another ordinary-Chat turn to the caller's draft."""
    if not entity_id or not user_id:
        return json.dumps({"error": "workspace draft user context missing"})
    if not draft_id or not message.strip():
        return json.dumps({"error": "draft_id and message are required"})

    try:
        from packages.core.database import async_session
        from packages.core.services.workspace_draft_service import (
            process_draft_message,
        )

        async with async_session() as db:
            reply, draft = await process_draft_message(
                db,
                draft_id=draft_id,
                entity_id=entity_id,
                user_id=user_id,
                user_message=message.strip(),
            )
            await db.commit()
        return json.dumps(_draft_artifact_payload(draft, reply))
    except Exception as exc:
        logger.exception("continue_workspace_draft failed")
        return json.dumps({"error": f"failed to update draft: {exc}"})


async def runtime_run_workspace_architect_turn(
    db: Any,
    *,
    draft_id: str,
    entity_id: str,
    user_id: str | None,
    user_message: str,
    history: Sequence[Mapping[str, Any]] | None = None,
    stream_handler: Any | None = None,
    on_tool_start: Any | None = None,
    on_tool_end: Any | None = None,
    repair_service_keys: tuple[str, ...] | None = None,
) -> str:
    """Run one workspace architect turn through the Runtime draft boundary."""

    from packages.core.services.workspace_architect import architect_run_turn

    return await architect_run_turn(
        db,
        draft_id=draft_id,
        entity_id=entity_id,
        user_id=user_id,
        user_message=user_message,
        history=list(history or []),
        stream_handler=stream_handler,
        on_tool_start=on_tool_start,
        on_tool_end=on_tool_end,
        **({"repair_service_keys": repair_service_keys} if repair_service_keys is not None else {}),
    )


def runtime_workspace_architect_tool_schemas() -> list[dict[str, Any]]:
    """Return the Runtime-owned workspace architect typed-tool schemas."""

    from packages.core.ai.tools.workspace_arch_tools import ALL_TOOL_SCHEMAS

    return list(ALL_TOOL_SCHEMAS)


def runtime_workspace_architect_tool_executor(
    db: Any,
    *,
    draft_id: str,
    entity_id: str,
    user_id: str | None,
    repair_service_keys: tuple[str, ...] | None = None,
) -> Callable[[str, dict[str, Any]], Awaitable[str]]:
    """Build the Runtime-owned executor for workspace architect draft tools."""

    from packages.core.ai.tools.workspace_arch_tools import (
        HANDLERS,
        REQUEST_CUSTOM_AGENT_SCHEMA,
    )
    from packages.core.contracts.json_schema import SchemaContractValidatorFactory

    tool_lock = asyncio.Lock()
    agent_design_validator = SchemaContractValidatorFactory.build(
        REQUEST_CUSTOM_AGENT_SCHEMA["function"]["parameters"],
    )

    async def executor(name: str, args: dict[str, Any]) -> str:
        if repair_service_keys is not None:
            if (
                name not in AGENT_BINDING_REPAIR_TOOLS
                or args.get("draft_id", draft_id) != draft_id
                or (
                    name != "ws_lint_draft"
                    and args.get("service_key") not in repair_service_keys
                )
            ):
                return json.dumps({"ok": False, "error": "outside agent binding repair scope"})
        handler = HANDLERS.get(name)
        if handler is None:
            return json.dumps({"ok": False, "error": f"unknown tool: {name}"})
        next_args = dict(args or {})
        next_args.setdefault("draft_id", draft_id)
        if name == "ws_request_custom_agent":
            public_args = {
                key: value for key, value in next_args.items()
                if key not in {
                    "_runtime_run_id_from_context",
                    "_runtime_tool_call_id_from_context",
                    "_runtime_tool_attempt_from_context",
                }
            }
            error = next(agent_design_validator.iter_errors(public_args), None)
            if error is not None:
                return json.dumps({"ok": False, "error": f"invalid agent design: {error.message}"})
        async with tool_lock:
            try:
                original_mapping = None
                if repair_service_keys is not None and name == "ws_request_custom_agent":
                    from packages.core.services.workspace_draft_service import get_draft

                    draft = await get_draft(db, draft_id, entity_id, user_id or "", for_update=True)
                    original_mapping = next((
                        copy.deepcopy(mapping)
                        for mapping in (getattr(draft, "fields", None) or {}).get("agent_mappings") or []
                        if mapping.get("service_key") == next_args["service_key"]
                        and mapping.get("strategy") == "create_custom"
                    ), None)
                    if original_mapping is None:
                        raise RuntimeTurnAborted("Agent mapping changed before binding repair")
                    original_agent = original_mapping.get("create_agent_draft") or {}
                    # Recheck the current references while the draft is locked;
                    # only validator-identified failures may be removed. A new
                    # semantic match need not repeat every valid saved binding.
                    lint = await runtime_lint_workspace_draft(
                        db, entity_id=entity_id, draft_id=draft_id, user_id=user_id,
                    )
                    if not lint or not lint.get("ok"):
                        raise RuntimeTurnAborted("Agent bindings could not be verified before repair")
                    invalid_bindings = {}
                    for key in ("tool_bindings", "business_capabilities", "skill_bindings", "mcp_bindings"):
                        binding_issues = [
                            issue for issue in lint.get("issues", [])
                            if issue.get("severity") == "P0"
                            and issue.get("where") == f"agent_mappings.{next_args['service_key']}.{key}"
                        ]
                        if any(
                            not isinstance(issue.get("binding_ref"), str) or not issue["binding_ref"]
                            for issue in binding_issues
                        ):
                            raise RuntimeTurnAborted("Invalid Agent binding reference could not be identified")
                        invalid_bindings[key] = {issue["binding_ref"] for issue in binding_issues}
                    if not any(invalid_bindings.values()):
                        return json.dumps({
                            "ok": True, "service_key": next_args["service_key"], "already_valid": True,
                        })
                    # The model selects bindings; it cannot rewrite the user's
                    # Agent prompt or drop optional skill/setup requirements.
                    for key in ("agent_name", "agent_description", "missing_skill_specs", "missing_integrations"):
                        default = [] if key.startswith("missing_") else ""
                        next_args[key] = copy.deepcopy(original_agent.get(key, default))
                    next_args["system_prompt"] = (
                        original_agent.get("system_prompt") or original_agent.get("system_prompt_seed", "")
                    )
                    next_args["rationale"] = original_mapping.get("rationale", "")
                result = await handler(
                    db,
                    entity_id=entity_id,
                    user_id=user_id or "",
                    **next_args,
                )
                if original_mapping is not None and json.loads(result).get("ok"):
                    from sqlalchemy.orm.attributes import flag_modified

                    fields = copy.deepcopy(draft.fields)
                    for mapping in fields.get("agent_mappings") or []:
                        if mapping.get("service_key") == next_args["service_key"]:
                            generated = mapping["create_agent_draft"]
                            # Retain settings outside the binding contract, even
                            # legacy fields absent from the public design schema.
                            mapping.update(original_mapping)
                            mapping["create_agent_draft"] = dict(original_agent)
                            repaired = mapping["create_agent_draft"]
                            if "missing_integrations" in generated:
                                repaired["missing_integrations"] = generated["missing_integrations"]
                            for key, invalid_refs in invalid_bindings.items():
                                if key not in original_agent and key not in generated:
                                    continue
                                repaired[key] = list(dict.fromkeys([
                                    *(ref for ref in original_agent.get(key) or [] if ref not in invalid_refs),
                                    *(generated.get(key) or []),
                                ]))
                            # Keep exact actions even when the matcher omitted
                            # a retained server or selected its whole provider.
                            repaired["mcp_allowed_tools"] = {
                                **(generated.get("mcp_allowed_tools") or {}),
                                **(original_agent.get("mcp_allowed_tools") or {}),
                            }
                    draft.fields = fields
                    flag_modified(draft, "fields")
                return result
            except RuntimeTurnAborted:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.exception("architect tool %s crashed", name)
                return json.dumps({"ok": False, "error": f"tool crashed: {exc}"})

    return executor


def runtime_reconcile_workspace_draft_fields(
    fields: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Apply Runtime-owned workspace draft reconciliation rules."""

    from packages.core.ai.tools.workspace_arch_tools import (
        _clear_agent_capability_plans,
        _reconcile_agent_design_flags,
        _reconcile_removed_channel_references,
    )
    from packages.core.services.workspace_setup_service import normalize_workspace_draft_governance

    next_fields = copy.deepcopy(dict(fields or {}))
    _reconcile_agent_design_flags(next_fields)
    _reconcile_removed_channel_references(next_fields)
    projected = normalize_workspace_draft_governance(next_fields)
    if any(
        projected.get(key) != next_fields.get(key)
        for key in ("rules", "governance", "governance_policy")
    ):
        _clear_agent_capability_plans(projected)
    return projected


async def runtime_lint_workspace_draft(
    db: Any,
    *,
    entity_id: str,
    draft_id: str,
    user_id: str | None = None,
) -> dict[str, Any] | None:
    """Run the workspace architect draft lint through the Runtime boundary."""

    from packages.core.ai.tools.workspace_arch_tools import _lint_draft

    raw = await _lint_draft(
        db, entity_id=entity_id, user_id=user_id or "", draft_id=draft_id,
    )
    try:
        parsed = json.loads(raw)
    except Exception:
        return None
    return parsed if isinstance(parsed, dict) else None
