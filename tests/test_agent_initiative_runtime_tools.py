"""Agent Initiative chat-tool visibility, scope, and governance contracts."""
from __future__ import annotations

import inspect
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from jsonschema import Draft202012Validator, ValidationError
from pydantic import ValidationError as PydanticValidationError

from packages.core.ai.agentic_loop import (
    TOOL_RESULT_MAX_CHARS,
    _compact_tool_result_for_context,
)
from packages.core.ai.runtime.approval_classifier import classify_runtime_tool
from packages.core.ai.runtime.approvals import RuntimeApprovalDecision
from packages.core.ai.runtime.capabilities import (
    AGENT_INITIATIVE_TOOL_NAMES,
    CORE_CAPABILITIES,
)
from packages.core.ai.runtime.envelope import RuntimeEnvelope
from packages.core.ai.runtime.harness import RuntimeHarness
from packages.core.ai.runtime.policies import (
    RuntimeToolPolicyCode,
    check_runtime_tool_policy,
    filter_runtime_tools,
)
from packages.core.ai.runtime.principals import resolve_runtime_principal
from packages.core.ai.runtime.profiles import (
    WORKSPACE_AGENT_TOOL_PROFILE,
    RuntimeProfile,
)
from packages.core.ai.runtime.surfaces import ChatSurface
from packages.core.ai.runtime.tool_effect_classification import RuntimeToolEffect
from packages.core.ai.runtime.tool_execution import (
    RUNTIME_ENVELOPE_AWARE_TOOLS,
    RUNTIME_LLM_METADATA_AWARE_TOOLS,
    runtime_execute_registered_tool,
)
from packages.core.ai.runtime.tool_visibility import (
    runtime_tool_registry_surface_from_schemas,
    runtime_tool_surface_spec,
)
from packages.core.ai.tool_pool import ToolPool
from packages.core.ai.tools import agent_initiative_tools
from packages.core.ai.tools.agent_initiative_tools import (
    _create_handler,
    _actor,
    _assert_current_initiative_scope,
    _current_subscription,
    _validate_subscription_scope,
    get_tools,
)
from packages.core.constants.agents import MANOR_AGENT_ID
from packages.core.contracts.agent_initiative import DeliveryPolicy
from packages.core.models.task import Conversation
from packages.core.models.workspace import AgentSubscription
from packages.core.services.agent_initiative_service import (
    AgentInitiativeServiceError,
    _driver_fields,
    _validate_conversation,
)
from packages.core.services.runtime_chat_context import (
    _personal_agent_subscription_for_chat,
)


pytestmark = pytest.mark.oss_regression

_READ_TOOLS = {
    "list_agent_initiatives",
    "get_agent_initiative",
    "list_agent_initiative_runs",
}
_MUTATION_TOOLS = set(AGENT_INITIATIVE_TOOL_NAMES) - _READ_TOOLS


def _tool_schemas() -> dict[str, dict]:
    return {schema["function"]["name"]: schema for schema, _ in get_tools()}


def _context(
    surface: ChatSurface,
    profile: RuntimeProfile,
    *,
    agent_id: str | None = "agent-1",
    workspace_id: str | None = None,
    conversation_id: str = "conversation-1",
):
    return SimpleNamespace(
        entity_id="entity-1",
        user_id="user-1",
        agent_id=agent_id,
        workspace_id=workspace_id,
        conversation_id=conversation_id,
        runtime_envelope=SimpleNamespace(surface=surface, profile=profile),
    )


@pytest.mark.asyncio
async def test_list_tool_keeps_many_initiatives_usable_in_fresh_context(
    monkeypatch,
) -> None:
    """List is an identity receipt; full configuration belongs to get."""

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return None

    rows = [
        SimpleNamespace(
            id=f"initiative-{index:02d}",
            name=f"Product release monitor {index:02d}",
            status="active",
            revision=index + 1,
            agent_subscription_id="subscription-1",
            workspace_id=None,
            instruction="Research the release and summarize evidence. " * 100,
            trigger_config={"version": "1", "kind": "interval", "every_seconds": 3600},
            condition_config={"version": "1", "semantic_condition": "Only important changes"},
            decision_policy={"version": "1", "mode": "on_change"},
            delivery_policy={
                "version": "1",
                "surface": {
                    "kind": "conversation",
                    "conversation_id": "conversation-1",
                },
                "notification": {
                    "enabled": True,
                    "recipient": "owner",
                    "channels": ["inapp"],
                    "content_mode": "full",
                },
            },
            interruption_policy={
                "version": "1",
                "quiet_hours": {"mode": "inherit_user"},
                "during_quiet_hours": "suppress_external",
            },
        )
        for index in range(16)
    ]

    monkeypatch.setattr(agent_initiative_tools, "async_session", FakeSession)
    monkeypatch.setattr(
        agent_initiative_tools,
        "runtime_tool_call_context_from_kwargs",
        lambda _kwargs: _context(
            ChatSurface.AGENT_DM,
            RuntimeProfile.AGENT_DELEGATE,
        ),
    )
    monkeypatch.setattr(
        agent_initiative_tools,
        "_actor",
        AsyncMock(
            return_value=SimpleNamespace(
                id="user-1",
                entity_id="entity-1",
                role="owner",
            )
        ),
    )
    monkeypatch.setattr(
        agent_initiative_tools,
        "_current_subscription",
        AsyncMock(
            return_value=SimpleNamespace(
                id="subscription-1",
                workspace_id=None,
            )
        ),
    )
    monkeypatch.setattr(
        agent_initiative_tools,
        "list_initiatives",
        AsyncMock(return_value=(rows, True)),
    )
    monkeypatch.setattr(
        agent_initiative_tools,
        "_assert_current_initiative_scope",
        lambda **_kwargs: None,
    )
    state_lookup = AsyncMock(return_value=SimpleNamespace())
    monkeypatch.setattr(agent_initiative_tools, "initiative_state", state_lookup)

    result = await agent_initiative_tools._list_handler()
    compacted = _compact_tool_result_for_context(
        "list_agent_initiatives",
        result,
    )
    payload = json.loads(compacted)

    assert len(result) <= TOOL_RESULT_MAX_CHARS
    assert compacted == result
    assert "_tool_result_truncated" not in payload
    assert payload["count"] == len(rows)
    assert len(payload["initiatives"]) == len(rows)
    assert payload["initiatives"][7] == {
        "id": "initiative-07",
        "name": "Product release monitor 07",
        "status": "active",
        "revision": 8,
    }
    state_lookup.assert_not_awaited()


def test_initiative_tools_are_registered_and_use_valid_strict_schemas() -> None:
    schemas = _tool_schemas()
    assert tuple(schemas) == AGENT_INITIATIVE_TOOL_NAMES

    for schema in schemas.values():
        Draft202012Validator.check_schema(schema["function"]["parameters"])

    update = Draft202012Validator(
        schemas["update_agent_initiative"]["function"]["parameters"]
    )
    update.validate({
        "initiative_id": "initiative-1",
        "expected_revision": 3,
        "patch": {
            "trigger": {
                "kind": "interval",
                "every_seconds": 3600,
                "timezone": "UTC",
            },
            "interruption_policy": {
                "quiet_hours": {
                    "mode": "custom",
                    "start": "22:00",
                    "end": "08:00",
                    "timezone": "America/Los_Angeles",
                },
            },
        },
    })
    with pytest.raises(ValidationError):
        update.validate({
            "initiative_id": "initiative-1",
            "expected_revision": 3,
            "patch": {
                # Regression: the original schema accidentally embedded the
                # create-tool properties map instead of a Trigger object.
                "trigger": {"schedule_kind": "interval", "every_seconds": 3600},
            },
        })

    pool = ToolPool()
    pool.initialize()
    assert all(pool.get(name) is not None for name in AGENT_INITIATIVE_TOOL_NAMES)

    for tool_name in ("create_agent_initiative", "list_agent_initiatives"):
        subscription_property = schemas[tool_name]["function"]["parameters"][
            "properties"
        ]["subscription_id"]
        description = subscription_property["description"]
        assert "Omit" in description
        assert "exact AgentSubscription ID" in description
        assert "never use placeholders" in description
        assert "'current'" in description


def test_chat_delivery_keeps_secondary_notifications_opt_in() -> None:
    policy = DeliveryPolicy.model_validate({
        "surface": {
            "kind": "conversation",
            "conversation_id": "conversation-1",
        },
    })
    assert policy.notification.enabled is False
    assert policy.notification.channels == ["inapp"]
    disabled_email = DeliveryPolicy.model_validate({
        "surface": {
            "kind": "conversation",
            "conversation_id": "conversation-1",
        },
        "notification": {"enabled": False, "channels": ["email"]},
    })
    assert disabled_email.notification.channels == ["email"]
    with pytest.raises(PydanticValidationError):
        DeliveryPolicy.model_validate({
            "surface": {
                "kind": "conversation",
                "conversation_id": "conversation-1",
            },
            "notification": {"enabled": False, "channels": []},
        })
    assert inspect.signature(_create_handler).parameters[
        "notification_enabled"
    ].default is False

    create = _tool_schemas()["create_agent_initiative"]["function"]
    enabled = create["parameters"]["properties"]["notification_enabled"]
    channels = create["parameters"]["properties"]["notification_channels"]
    assert enabled["default"] is False
    assert "secondary notification" in enabled["description"]
    assert "Chat delivery" in enabled["description"]
    assert "do not choose the Chat destination" in channels["description"]
    assert "notification_enabled controls only an additional" in (
        create["description"]
    )


def test_every_initiative_tool_receives_runtime_envelope_without_llm_metadata() -> None:
    initiative_tools = set(AGENT_INITIATIVE_TOOL_NAMES)

    assert initiative_tools.issubset(RUNTIME_ENVELOPE_AWARE_TOOLS)
    assert initiative_tools.isdisjoint(RUNTIME_LLM_METADATA_AWARE_TOOLS)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("surface", "profile", "agent_id", "workspace_id"),
    (
        (
            ChatSurface.GLOBAL_OWNER_CHAT,
            RuntimeProfile.OWNER_COPILOT,
            None,
            None,
        ),
        (
            ChatSurface.AGENT_DM,
            RuntimeProfile.AGENT_DELEGATE,
            "agent-1",
            None,
        ),
    ),
)
async def test_registered_initiative_handler_receives_exact_runtime_envelope(
    monkeypatch,
    surface,
    profile,
    agent_id,
    workspace_id,
) -> None:
    captured = {}

    async def allow_tool_request(self, request):
        return RuntimeApprovalDecision.allow(
            self.approval_middleware.classify_request(request),
            request,
        )

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return None

    async def capture_actor(_db, context):
        captured["context"] = context
        return SimpleNamespace(id="user-1", entity_id="entity-1", role="owner")

    async def no_subscription(_db, *, context, **_kwargs):
        assert context is captured["context"]
        return None

    monkeypatch.setattr(agent_initiative_tools, "async_session", FakeSession)
    monkeypatch.setattr(agent_initiative_tools, "_actor", capture_actor)
    monkeypatch.setattr(
        agent_initiative_tools,
        "_current_subscription",
        no_subscription,
    )
    monkeypatch.setattr(RuntimeHarness, "guard_tool_request", allow_tool_request)

    envelope = RuntimeEnvelope(
        surface=surface,
        principal=resolve_runtime_principal(
            surface=surface,
            entity_id="entity-1",
            user_id="user-1",
            agent_id=agent_id,
            workspace_id=workspace_id,
        ),
        profile=profile,
        entity_id="entity-1",
        user_id="user-1",
        agent_id=agent_id,
        workspace_id=workspace_id,
        conversation_id="conversation-1",
        allowed_tool_names=AGENT_INITIATIVE_TOOL_NAMES,
        capability_ids=("initiative.manage",),
    )
    pool = ToolPool()
    pool.initialize()

    result = await pool.execute(
        "list_agent_initiatives",
        {},
        runtime_envelope=envelope,
        llm_metadata={"provider_marker": "must-not-reach-initiative-handler"},
        llm_model="must-not-reach-initiative-handler",
    )

    assert json.loads(result) == {"ok": True, "initiatives": [], "count": 0}
    context = captured["context"]
    assert context.runtime_envelope is envelope
    assert context.entity_id == envelope.entity_id
    assert context.user_id == envelope.user_id
    assert context.agent_id == envelope.agent_id
    assert context.workspace_id == envelope.workspace_id
    assert context.conversation_id == envelope.conversation_id
    assert context.runtime_envelope.surface is envelope.surface
    assert context.runtime_envelope.profile is envelope.profile
    assert context.llm_metadata is None
    assert context.llm_model is None


@pytest.mark.asyncio
async def test_workspace_initiative_tool_is_denied_before_handler() -> None:
    envelope = RuntimeEnvelope(
        surface=ChatSurface.WORKSPACE_CHAT,
        principal=resolve_runtime_principal(
            surface=ChatSurface.WORKSPACE_CHAT,
            entity_id="entity-1",
            user_id="user-1",
            agent_id="agent-1",
            workspace_id="workspace-1",
        ),
        profile=RuntimeProfile.WORKSPACE_OPERATOR,
        entity_id="entity-1",
        user_id="user-1",
        agent_id="agent-1",
        workspace_id="workspace-1",
        conversation_id="conversation-1",
        allowed_tool_names=AGENT_INITIATIVE_TOOL_NAMES,
        capability_ids=("initiative.manage",),
    )
    handler = AsyncMock(return_value="should not run")

    result = await runtime_execute_registered_tool(
        tool_name="create_agent_initiative",
        arguments={},
        handler_resolver=lambda _name: handler,
        runtime_envelope=envelope,
    )

    assert json.loads(result)["error"] == "tool_not_in_runtime_surface"
    handler.assert_not_awaited()


@pytest.mark.asyncio
async def test_approved_create_forced_replay_uses_fresh_runtime_envelope(
    monkeypatch,
) -> None:
    """Approval freezes business input, never the Runtime-owned actor scope."""
    from packages.core.ai.runtime import approval_service
    from packages.core.ai.runtime.approval_messages import (
        runtime_approval_continuation,
    )

    approved_arguments = {
        "name": "Daily briefing",
        "instruction": "Post the fixed briefing in this chat.",
        "schedule_kind": "interval",
        "every_seconds": 3600,
        "notification_enabled": False,
        # Reproduce an approval minted before the next chat turn. Runtime
        # context is intentionally untrusted continuation data and must not be
        # replayed; the executor injects the next turn's fresh envelope.
        "_user_id_from_context": "stale-user",
        "_runtime_envelope_from_context": {
            "entity_id": "stale-entity",
            "conversation_id": "stale-conversation",
        },
    }
    continuation = runtime_approval_continuation(
        "create_agent_initiative",
        approved_arguments,
    )
    assert "_user_id_from_context" not in continuation["arguments"]
    assert "_runtime_envelope_from_context" not in continuation["arguments"]

    request = SimpleNamespace(
        id="approval-1",
        status="pending",
        context={
            "tool": "create_agent_initiative",
            "continuation": continuation,
        },
        action_key="workspace.automation.agent_initiative.create",
        capability_id="initiative.manage",
        risk_level="admin",
        reason="Create an Initiative",
        matched_rule="intrinsic.runtime",
        workspace_id="workspace-current",
        origin_kind="tool_call",
        origin_step_id=None,
    )
    approval_calls: list[tuple[str, object]] = []
    request_lock_calls: list[bool] = []

    async def load_request(
        _db,
        *,
        request_id,
        entity_id,
        conversation_id,
        for_update,
    ):
        assert (
            request_id,
            entity_id,
            conversation_id,
        ) == (
            "approval-1",
            "entity-current",
            "conversation-current",
        )
        request_lock_calls.append(for_update)
        return request

    async def authorize_resolution(_db, *, request, by_user_id, standing):
        approval_calls.append(("authorize", request))
        assert by_user_id == "user-current"
        assert standing is False
        return SimpleNamespace(allowed=True, reason=None)

    async def grant(_db, request, *, by_user_id, via, **_kwargs):
        approval_calls.append(("grant", request))
        assert by_user_id == "user-current"
        assert via == "chat_card"

    async def mark_resolved(
        _db,
        *,
        conversation_id,
        hitl_id,
        choice,
    ):
        approval_calls.append(("resolve", choice))
        assert conversation_id == "conversation-current"
        assert hitl_id == "approval-1"

    async def registered_schema(tool_name, *, entity_id, user_id):
        assert tool_name == "create_agent_initiative"
        assert entity_id == "entity-current"
        assert user_id == "user-current"
        return _tool_schemas()[tool_name]

    monkeypatch.setattr(approval_service, "_load_runtime_request", load_request)
    monkeypatch.setattr(
        "packages.core.services.runtime_authorization.authorize_hitl_resolution",
        authorize_resolution,
    )
    monkeypatch.setattr(
        "packages.core.governance.approvals.grant_approval",
        grant,
    )
    monkeypatch.setattr(
        approval_service,
        "mark_runtime_hitl_request_resolved",
        mark_resolved,
    )
    monkeypatch.setattr(
        "packages.core.ai.runtime.tool_registry.runtime_tool_schema_for_actor",
        registered_schema,
    )

    resolution = await approval_service.resolve_runtime_approval_turn(
        object(),
        conversation_id="conversation-current",
        entity_id="entity-current",
        user_id="user-current",
        hitl_id="approval-1",
        action="approve",
    )

    assert resolution is not None
    assert resolution.runtime_metadata is not None
    forced_call = resolution.runtime_metadata["forced_tool_calls"][0]
    assert forced_call["name"] == "create_agent_initiative"
    assert forced_call["disable_followup_tools"] is True
    assert forced_call["arguments"]["approval_token"] == "approval-1"
    assert "_runtime_envelope_from_context" not in forced_call["arguments"]
    assert [kind for kind, _value in approval_calls] == [
        "authorize",
        "grant",
        "resolve",
    ]
    assert request_lock_calls == [False, True]

    captured: dict[str, object] = {}

    async def allow_tool_request(self, approval_request):
        assert approval_request.arguments["approval_token"] == "approval-1"
        return RuntimeApprovalDecision.allow(
            self.approval_middleware.classify_request(approval_request),
            approval_request,
        )

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return None

        async def commit(self):
            captured["committed"] = True

    async def capture_actor(_db, context):
        captured["context"] = context
        return SimpleNamespace(
            id="user-current",
            entity_id="entity-current",
            role="owner",
        )

    async def current_subscription(
        _db,
        *,
        context,
        user,
        requested_subscription_id,
        create_if_missing,
    ):
        assert context is captured["context"]
        assert user.id == "user-current"
        assert requested_subscription_id is None
        assert create_if_missing is True
        return SimpleNamespace(id="subscription-current", workspace_id=None)

    async def create(_db, **kwargs):
        captured["create_kwargs"] = kwargs
        return SimpleNamespace(
            id="initiative-1",
            name=kwargs["name"],
            status="active",
            revision=1,
            agent_subscription_id=kwargs["subscription_id"],
            workspace_id=None,
            instruction=kwargs["instruction"],
            trigger_config=kwargs["trigger"].model_dump(mode="json"),
            condition_config=kwargs["condition"].model_dump(mode="json"),
            decision_policy=kwargs["decision_policy"].model_dump(mode="json"),
            delivery_policy=kwargs["delivery_policy"].model_dump(mode="json"),
            interruption_policy=kwargs["interruption_policy"].model_dump(
                mode="json"
            ),
        )

    async def no_lifecycle_lock(_db, *, entity_id):
        assert entity_id == "entity-current"

    monkeypatch.setattr(agent_initiative_tools, "async_session", FakeSession)
    monkeypatch.setattr(agent_initiative_tools, "_actor", capture_actor)
    monkeypatch.setattr(
        agent_initiative_tools,
        "_current_subscription",
        current_subscription,
    )
    monkeypatch.setattr(agent_initiative_tools, "create_initiative", create)
    monkeypatch.setattr(
        agent_initiative_tools,
        "lock_reusable_resource_lifecycle",
        no_lifecycle_lock,
    )
    monkeypatch.setattr(RuntimeHarness, "guard_tool_request", allow_tool_request)

    fresh_envelope = RuntimeEnvelope(
        surface=ChatSurface.AGENT_DM,
        principal=resolve_runtime_principal(
            surface=ChatSurface.AGENT_DM,
            entity_id="entity-current",
            user_id="user-current",
            agent_id="agent-current",
        ),
        profile=RuntimeProfile.AGENT_DELEGATE,
        entity_id="entity-current",
        user_id="user-current",
        agent_id="agent-current",
        conversation_id="conversation-current",
        allowed_tool_names=("create_agent_initiative",),
        capability_ids=("initiative.manage",),
    )
    pool = ToolPool()
    pool.initialize()

    replay_result = await runtime_execute_registered_tool(
        tool_name=forced_call["name"],
        arguments=dict(forced_call["arguments"]),
        handler_resolver=lambda name: (pool.get(name) or {}).get("handler"),
        runtime_envelope=fresh_envelope,
    )

    payload = json.loads(replay_result)
    assert payload["ok"] is True
    assert payload["created"] is True
    assert payload["initiative"]["id"] == "initiative-1"
    assert captured["committed"] is True
    context = captured["context"]
    assert context.runtime_envelope is fresh_envelope
    assert context.entity_id == "entity-current"
    assert context.user_id == "user-current"
    assert context.agent_id == "agent-current"
    assert context.conversation_id == "conversation-current"
    assert context.runtime_envelope.surface is ChatSurface.AGENT_DM
    assert context.runtime_envelope.profile is RuntimeProfile.AGENT_DELEGATE
    create_kwargs = captured["create_kwargs"]
    assert create_kwargs["delivery_policy"].surface.conversation_id == (
        "conversation-current"
    )


@pytest.mark.asyncio
async def test_approved_create_replay_fails_closed_when_fresh_scope_revokes_tool(
    monkeypatch,
) -> None:
    """A grant cannot revive a tool removed from the current tool surface."""
    from packages.core.ai.runtime import approval_service
    from packages.core.ai.runtime.approval_messages import (
        runtime_approval_continuation,
    )

    request = SimpleNamespace(
        id="approval-revoked",
        status="pending",
        consumed_at=None,
        context={
            "tool": "create_agent_initiative",
            "continuation": runtime_approval_continuation(
                "create_agent_initiative",
                {
                    "name": "Revoked briefing",
                    "instruction": "Post a briefing in this chat.",
                    "schedule_kind": "interval",
                    "every_seconds": 3600,
                    "notification_enabled": False,
                },
            ),
        },
        action_key="workspace.automation.agent_initiative.create",
        capability_id="initiative.manage",
        risk_level="admin",
        reason="Create an Initiative",
        matched_rule="intrinsic.runtime",
        workspace_id=None,
        origin_kind="tool_call",
        origin_step_id=None,
    )

    request_lock_calls: list[bool] = []

    async def load_request(
        _db,
        *,
        request_id,
        entity_id,
        conversation_id,
        for_update,
    ):
        assert request_id == request.id
        assert entity_id == "entity-current"
        assert conversation_id == "conversation-current"
        request_lock_calls.append(for_update)
        return request

    async def authorize_resolution(_db, *, request, by_user_id, standing):
        assert request is not None
        assert by_user_id == "user-current"
        assert standing is False
        return SimpleNamespace(allowed=True, reason=None)

    async def grant(_db, granted_request, *, by_user_id, via, **_kwargs):
        assert granted_request is request
        assert by_user_id == "user-current"
        assert via == "chat_card"
        granted_request.status = "granted"
        granted_request.consumed_at = None

    async def mark_resolved(_db, **_kwargs):
        return None

    async def registered_schema(tool_name, *, entity_id, user_id):
        assert tool_name == "create_agent_initiative"
        assert entity_id == "entity-current"
        assert user_id == "user-current"
        return _tool_schemas()[tool_name]

    monkeypatch.setattr(approval_service, "_load_runtime_request", load_request)
    monkeypatch.setattr(
        "packages.core.services.runtime_authorization.authorize_hitl_resolution",
        authorize_resolution,
    )
    monkeypatch.setattr(
        "packages.core.governance.approvals.grant_approval",
        grant,
    )
    monkeypatch.setattr(
        approval_service,
        "mark_runtime_hitl_request_resolved",
        mark_resolved,
    )
    monkeypatch.setattr(
        "packages.core.ai.runtime.tool_registry.runtime_tool_schema_for_actor",
        registered_schema,
    )

    resolution = await approval_service.resolve_runtime_approval_turn(
        object(),
        conversation_id="conversation-current",
        entity_id="entity-current",
        user_id="user-current",
        hitl_id=request.id,
        action="approve",
    )
    assert resolution is not None
    assert resolution.runtime_metadata is not None
    forced_call = resolution.runtime_metadata["forced_tool_calls"][0]
    assert request.status == "granted"
    assert request.consumed_at is None
    assert request_lock_calls == [False, True]

    fresh_envelope = RuntimeEnvelope(
        surface=ChatSurface.AGENT_DM,
        principal=resolve_runtime_principal(
            surface=ChatSurface.AGENT_DM,
            entity_id="entity-current",
            user_id="user-current",
            agent_id="agent-current",
        ),
        profile=RuntimeProfile.AGENT_DELEGATE,
        entity_id="entity-current",
        user_id="user-current",
        agent_id="agent-current",
        conversation_id="conversation-current",
        # Revocation happened after approval and before replay. The current
        # envelope is authoritative even though the approved call is forced.
        allowed_tool_names=("list_agent_initiatives",),
        capability_ids=(),
    )
    pool = ToolPool()
    pool.initialize()
    registered_handler = (pool.get("create_agent_initiative") or {}).get(
        "handler"
    )
    assert registered_handler is not None
    handler_spy = AsyncMock(wraps=registered_handler)

    replay_result = await runtime_execute_registered_tool(
        tool_name=forced_call["name"],
        arguments=dict(forced_call["arguments"]),
        handler_resolver=lambda _name: handler_spy,
        runtime_envelope=fresh_envelope,
    )

    payload = json.loads(replay_result)
    assert payload["error"] == "tool_not_in_runtime_surface"
    assert payload["tool"] == "create_agent_initiative"
    handler_spy.assert_not_awaited()
    # Runtime scope denial happens before the one-time approval lease can be
    # consumed; the durable approval remains available for reconciliation.
    assert request.status == "granted"
    assert request.consumed_at is None


def test_proactive_chat_requests_are_routed_away_from_generic_scheduled_jobs() -> None:
    from packages.core.ai.runtime.tool_search import runtime_tool_manifest
    from packages.core.ai.tools.cron_tools import get_tools as get_cron_tools
    from packages.core.ai.tools.manor_tool import MANOR_SCHEMA

    initiative_description = _tool_schemas()["create_agent_initiative"][
        "function"
    ]["description"]
    assert "private Agent or Manor Chat" in initiative_description
    assert "每天搜新闻发给我" in initiative_description
    assert "Workspace requests must use a Workspace automation" in initiative_description

    cron_schemas = {
        schema["function"]["name"]: schema["function"]["description"]
        for schema, _handler in get_cron_tools()
    }
    create_description = cron_schemas["create_scheduled_job"]
    assert "scheduled automation" in create_description.lower()
    assert "Workspace Chat" in create_description
    assert "create_agent_initiative" in create_description
    create_schema = next(
        schema
        for schema, _handler in get_cron_tools()
        if schema["function"]["name"] == "create_scheduled_job"
    )
    create_manifest = runtime_tool_manifest(
        "create_scheduled_job",
        create_schema,
    )
    assert "Workspace Chat" in create_manifest["description"]

    for tool_name, initiative_tool in {
        "list_scheduled_jobs": "list_agent_initiatives",
        "cancel_scheduled_job": "delete_agent_initiative",
        "toggle_scheduled_job": "pause/resume_agent_initiative",
        "run_scheduled_job_now": "run_agent_initiative_now",
    }.items():
        assert initiative_tool in cron_schemas[tool_name]

    manor_description = MANOR_SCHEMA["function"]["description"]
    assert "create_agent_initiative is only for private" in manor_description
    assert "Workspace recurrence uses create_scheduled_job" in manor_description


def test_initiative_capability_is_deferred_on_internal_agent_surfaces() -> None:
    capability = CORE_CAPABILITIES["initiative.manage"]
    assert capability.tool_names == AGENT_INITIATIVE_TOOL_NAMES
    assert set(capability.profiles) == {
        RuntimeProfile.OWNER_COPILOT,
        RuntimeProfile.AGENT_DELEGATE,
    }

    owner = runtime_tool_surface_spec(is_master=True)
    agent_dm = runtime_tool_surface_spec(is_master=False)
    workspace = runtime_tool_surface_spec(
        is_master=False,
        tool_profile=WORKSPACE_AGENT_TOOL_PROFILE,
    )
    for surface in (owner, agent_dm):
        assert set(AGENT_INITIATIVE_TOOL_NAMES).isdisjoint(surface.eager_tool_names)
        assert set(AGENT_INITIATIVE_TOOL_NAMES).issubset(surface.contextual_tool_names)
        assert "initiative.manage" not in surface.capability_ids
        assert "initiative.manage" in surface.contextual_capability_ids
    assert set(AGENT_INITIATIVE_TOOL_NAMES).isdisjoint(workspace.eager_tool_names)
    assert set(AGENT_INITIATIVE_TOOL_NAMES).isdisjoint(workspace.contextual_tool_names)
    assert "initiative.manage" not in workspace.contextual_capability_ids

    pool = ToolPool()
    pool.initialize()
    matches = pool.search("select:create_agent_initiative")
    assert matches[0]["name"] == "create_agent_initiative"
    loaded = pool.get_schemas_for_names([matches[0]["name"]])
    assert [schema["function"]["name"] for schema in loaded] == [
        "create_agent_initiative"
    ]


@pytest.mark.asyncio
async def test_workspace_main_chat_discovers_automation_not_initiative(monkeypatch) -> None:
    from packages.core.services import workspace_runtime

    async def no_context(_db, **_kwargs):
        return {}

    async def no_agent_bindings(_db, **_kwargs):
        if _kwargs["is_master"]:
            return None, None, ()
        return set(), set(), ()

    async def no_workspace_bindings(_db, **_kwargs):
        return set(), set(), set()

    async def no_skills(_db, **_kwargs):
        return False

    async def can_write(_db, **kwargs):
        return kwargs["user_id"] == "writer-1"

    monkeypatch.setattr(workspace_runtime, "load_conversation_runtime_context", no_context)
    monkeypatch.setattr(workspace_runtime, "_resolve_agent_tool_scope", no_agent_bindings)
    monkeypatch.setattr(workspace_runtime, "_resolve_workspace_operation_tool_scope", no_workspace_bindings)
    monkeypatch.setattr(workspace_runtime, "_agent_has_visible_skills", no_skills)
    monkeypatch.setattr(workspace_runtime, "user_can_write_workspace_artifacts", can_write)

    async def runtime(*, is_master: bool, surface: ChatSurface, user_id: str = "writer-1"):
        return await workspace_runtime.resolve_workspace_runtime(
            object(),
            entity_id="entity-1",
            user_id=user_id,
            workspace_id="workspace-1",
            agent_id="manor-master" if is_master else "agent-1",
            is_master=is_master,
            runtime_surface=surface,
            include_prompt_context=False,
        )

    workspace_main = await runtime(is_master=True, surface=ChatSurface.WORKSPACE_CHAT)
    workspace_viewer = await runtime(is_master=True, surface=ChatSurface.WORKSPACE_CHAT, user_id="viewer-1")
    workspace_agent = await runtime(is_master=False, surface=ChatSurface.WORKSPACE_CHAT)
    task_comment = await runtime(is_master=True, surface=ChatSurface.TASK_COMMENT_THREAD)
    automation_names = set(CORE_CAPABILITIES["automation.manage"].tool_names)
    # The owner's ordinary Chat baseline stays unrestricted; the Workspace
    # overlay must not manufacture eager automation bindings or permissions.
    assert workspace_main.bound_tool_names is None
    assert "automation.manage" not in workspace_main.capability_ids
    assert automation_names.isdisjoint(workspace_viewer.bound_tool_names or ())
    assert "automation.manage" not in workspace_viewer.capability_ids
    assert automation_names.isdisjoint(workspace_agent.bound_tool_names or ())
    assert automation_names.isdisjoint(task_comment.bound_tool_names or ())

    pool = ToolPool()
    pool.initialize()
    resolved = runtime_tool_registry_surface_from_schemas(
        pool.registered_tool_schemas(),
        bound_tool_names=workspace_main.bound_tool_names,
        is_master=True,
        mcp_allowed_names=workspace_main.mcp_allowed_names,
        tool_profile=workspace_main.tool_profile,
    )
    assert automation_names.issubset(resolved.visible_tool_names)
    assert automation_names.issubset(resolved.deferred_tool_names)
    assert "create_scheduled_job" not in {
        schema["function"]["name"] for schema in resolved.prompt_schemas
    }
    assert set(AGENT_INITIATIVE_TOOL_NAMES).isdisjoint(resolved.visible_tool_names)


@pytest.mark.parametrize(
    ("surface", "profile"),
    (
        (
            ChatSurface.PUBLIC_CUSTOMER_CHAT,
            RuntimeProfile.EXTERNAL_CUSTOMER_SAFE,
        ),
        (
            ChatSurface.EXTERNAL_CHANNEL_CHAT,
            RuntimeProfile.EXTERNAL_CHANNEL_SAFE,
        ),
    ),
)
def test_external_profiles_filter_and_hard_deny_every_initiative_tool(
    surface: ChatSurface,
    profile: RuntimeProfile,
) -> None:
    schemas = list(_tool_schemas().values())
    filtered, allowed, blocked = filter_runtime_tools(
        surface=surface,
        profile=profile,
        tools=schemas,
        allowed_tool_names=AGENT_INITIATIVE_TOOL_NAMES,
    )
    assert filtered == []
    assert allowed == set()
    assert blocked.issuperset(AGENT_INITIATIVE_TOOL_NAMES)

    envelope = RuntimeEnvelope(
        surface=surface,
        principal=resolve_runtime_principal(
            surface=surface,
            entity_id="entity-1",
            user_id="legacy-owner",
            channel_context={"source_id": "external-1", "channel_type": "email"},
        ),
        profile=profile,
        entity_id="entity-1",
        allowed_tool_names=AGENT_INITIATIVE_TOOL_NAMES,
    )
    for name in AGENT_INITIATIVE_TOOL_NAMES:
        decision = check_runtime_tool_policy(
            envelope=envelope,
            tool_name=name,
            arguments={},
        )
        assert not decision.allowed
        assert decision.code == RuntimeToolPolicyCode.EXTERNAL_PRINCIPAL_TOOL_DENIED.value


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("surface", "profile"),
    (
        (
            ChatSurface.PUBLIC_CUSTOMER_CHAT,
            RuntimeProfile.EXTERNAL_CUSTOMER_SAFE,
        ),
        (
            ChatSurface.EXTERNAL_CHANNEL_CHAT,
            RuntimeProfile.EXTERNAL_CHANNEL_SAFE,
        ),
        (
            ChatSurface.SCHEDULED_AGENT_RUN,
            RuntimeProfile.BACKGROUND_WORKER,
        ),
        (
            ChatSurface.WORKSPACE_CHAT,
            RuntimeProfile.WORKSPACE_OPERATOR,
        ),
    ),
)
async def test_handler_guard_rejects_non_managing_surfaces_before_database_access(
    surface: ChatSurface,
    profile: RuntimeProfile,
) -> None:
    class UnexpectedDB:
        async def execute(self, _query):
            raise AssertionError("external Initiative calls must fail before database access")

    with pytest.raises(AgentInitiativeServiceError) as exc_info:
        await _actor(UnexpectedDB(), _context(surface, profile))
    assert exc_info.value.code == "initiative_surface_forbidden"


@pytest.mark.parametrize(
    ("mode", "mutation_outcome"),
    [("confirm_changes", "ask"), ("risk_based", "ask"), ("automatic", "allow")],
)
def test_initiative_reads_and_mutations_have_distinct_governance(
    mode: str, mutation_outcome: str,
) -> None:
    from tests.approval_policy_test_utils import evaluate_runtime_action

    for name in _READ_TOOLS:
        classification = classify_runtime_tool(name, {})
        assert classification.effect is RuntimeToolEffect.READ_ONLY
        assert classification.authorization is not None
        assert classification.authorization.capability_id == "initiative.manage"

    expected = {
        "create_agent_initiative": ("high", "create"),
        "update_agent_initiative": ("medium", "modify"),
        "mute_agent_initiative": ("medium", "modify"),
        "unmute_agent_initiative": ("medium", "modify"),
        "pause_agent_initiative": ("medium", "modify"),
        "resume_agent_initiative": ("medium", "modify"),
        "delete_agent_initiative": ("high", "delete"),
        "run_agent_initiative_now": ("medium", "execute"),
    }
    for name, (risk, operation) in expected.items():
        classification = classify_runtime_tool(name, {"initiative_id": "init-1"})
        assert classification.effect is RuntimeToolEffect.ACTION
        assert classification.action is not None
        assert classification.action.capability_id == "initiative.manage"
        assert classification.action.resource_kind == "agent_initiative"
        assert classification.action.risk_level == risk
        assert classification.action.operation == operation
        assert classification.action.action_key.startswith(
            "workspace.automation.agent_initiative."
        )
        decision = evaluate_runtime_action(classification.action, mode=mode)
        assert decision.outcome == mutation_outcome
        assert decision.source == "mode"
        denied = evaluate_runtime_action(
            classification.action,
            mode=mode,
            rules=[{
                "id": "blocked-initiative-action",
                "effect": "deny",
                "selector": {
                    "kind": "action",
                    "action_key": classification.action.action_key,
                },
            }],
        )
        assert denied.outcome == "deny"
        assert denied.rule_id == "blocked-initiative-action"


def test_personal_and_workspace_subscription_scope_is_exact() -> None:
    user = SimpleNamespace(id="user-1", entity_id="entity-1")

    owner_context = _context(
        ChatSurface.GLOBAL_OWNER_CHAT,
        RuntimeProfile.OWNER_COPILOT,
        agent_id=None,
    )
    owner_conversation = Conversation(
        id="conversation-1",
        entity_id="entity-1",
        user_id="user-1",
        agent_id=None,
        agent_subscription_id="subscription-master",
        workspace_id=None,
        status="active",
        scope="channel",
    )
    owner_subscription = AgentSubscription(
        id="subscription-master",
        entity_id="entity-1",
        owner_user_id="user-1",
        agent_id=MANOR_AGENT_ID,
        workspace_id=None,
        status="active",
    )
    _validate_subscription_scope(
        context=owner_context,
        user=user,
        conversation=owner_conversation,
        subscription=owner_subscription,
    )

    personal_context = _context(
        ChatSurface.AGENT_DM,
        RuntimeProfile.AGENT_DELEGATE,
    )
    personal_conversation = Conversation(
        id="conversation-1",
        entity_id="entity-1",
        user_id="user-1",
        agent_id="agent-1",
        agent_subscription_id="subscription-1",
        workspace_id=None,
        status="active",
        scope="channel",
    )
    personal_subscription = AgentSubscription(
        id="subscription-1",
        entity_id="entity-1",
        owner_user_id="user-1",
        agent_id="agent-1",
        workspace_id=None,
        status="active",
    )
    _validate_subscription_scope(
        context=personal_context,
        user=user,
        conversation=personal_conversation,
        subscription=personal_subscription,
    )

    other_owner = AgentSubscription(
        id="subscription-1",
        entity_id="entity-1",
        owner_user_id="user-2",
        agent_id="agent-1",
        workspace_id=None,
        status="active",
    )
    with pytest.raises(AgentInitiativeServiceError):
        _validate_subscription_scope(
            context=personal_context,
            user=user,
            conversation=personal_conversation,
            subscription=other_owner,
        )

    workspace_context = _context(
        ChatSurface.WORKSPACE_CHAT,
        RuntimeProfile.WORKSPACE_OPERATOR,
        workspace_id="workspace-1",
    )
    workspace_conversation = Conversation(
        id="conversation-1",
        entity_id="entity-1",
        workspace_id="workspace-1",
        status="active",
        scope="workspace_main",
    )
    workspace_subscription = AgentSubscription(
        id="subscription-1",
        entity_id="entity-1",
        agent_id="agent-1",
        workspace_id="workspace-1",
        status="active",
    )
    _validate_subscription_scope(
        context=workspace_context,
        user=user,
        conversation=workspace_conversation,
        subscription=workspace_subscription,
    )
    workspace_subscription.workspace_id = "workspace-2"
    with pytest.raises(AgentInitiativeServiceError):
        _validate_subscription_scope(
            context=workspace_context,
            user=user,
            conversation=workspace_conversation,
            subscription=workspace_subscription,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("surface", "workspace_id", "channel_context", "editor_context"),
    [
        (ChatSurface.PUBLIC_CUSTOMER_CHAT, None, None, None),
        (ChatSurface.EXTERNAL_CHANNEL_CHAT, None, {"channel_type": "telegram"}, None),
        (ChatSurface.WORKSPACE_CHAT, "workspace-1", None, None),
        (None, None, None, {"target": "document-1"}),
    ],
)
async def test_personal_runtime_binding_never_crosses_non_personal_surfaces(
    surface,
    workspace_id,
    channel_context,
    editor_context,
) -> None:
    db = SimpleNamespace(execute=AsyncMock())

    resolved = await _personal_agent_subscription_for_chat(
        db,
        entity_id="entity-1",
        user_id="user-1",
        conversation_id="conversation-1",
        requested_agent_id=None,
        requested_workspace_id=workspace_id,
        runtime_surface=surface,
        channel_context=channel_context,
        editor_context=editor_context,
    )

    assert resolved is None
    db.execute.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("subscription_agent_id", "conversation_agent_id"),
    [
        (MANOR_AGENT_ID, None),
        ("agent-1", "agent-1"),
    ],
)
async def test_personal_runtime_binding_resolves_exact_internal_chat(
    subscription_agent_id,
    conversation_agent_id,
) -> None:
    conversation = Conversation(
        id="conversation-1",
        entity_id="entity-1",
        user_id="user-1",
        agent_id=conversation_agent_id,
        agent_subscription_id="subscription-1",
        workspace_id=None,
        channel="web",
        scope="channel",
        status="active",
        meta={},
    )
    subscription = AgentSubscription(
        id="subscription-1",
        entity_id="entity-1",
        owner_user_id="user-1",
        agent_id=subscription_agent_id,
        workspace_id=None,
        status="active",
    )
    db = SimpleNamespace(execute=AsyncMock(side_effect=[
        SimpleNamespace(scalar_one_or_none=lambda: conversation),
        SimpleNamespace(scalar_one_or_none=lambda: subscription),
    ]))

    resolved = await _personal_agent_subscription_for_chat(
        db,
        entity_id="entity-1",
        user_id="user-1",
        conversation_id="conversation-1",
        requested_agent_id=conversation_agent_id,
        requested_workspace_id=None,
        runtime_surface=None,
        channel_context=None,
        editor_context=None,
    )

    assert resolved is subscription
    assert db.execute.await_count == 2


@pytest.mark.asyncio
async def test_personal_runtime_binding_fails_closed_for_wrong_owner() -> None:
    conversation = Conversation(
        id="conversation-1",
        entity_id="entity-1",
        user_id="user-2",
        agent_id="agent-1",
        agent_subscription_id="subscription-1",
        workspace_id=None,
        channel="web",
        scope="channel",
        status="active",
        meta={},
    )
    db = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(
        scalar_one_or_none=lambda: conversation,
    )))

    with pytest.raises(ValueError, match="conversation is unavailable"):
        await _personal_agent_subscription_for_chat(
            db,
            entity_id="entity-1",
            user_id="user-1",
            conversation_id="conversation-1",
            requested_agent_id="agent-1",
            requested_workspace_id=None,
            runtime_surface=ChatSurface.AGENT_DM,
            channel_context=None,
            editor_context=None,
        )

    assert db.execute.await_count == 1


@pytest.mark.asyncio
async def test_master_personal_delivery_binds_without_turning_chat_into_agent_dm() -> None:
    conversation = Conversation(
        id="conversation-1",
        entity_id="entity-1",
        user_id="user-1",
        agent_id=None,
        agent_subscription_id=None,
        workspace_id=None,
        channel="web",
        scope="channel",
        status="active",
        meta={},
    )
    subscription = AgentSubscription(
        id="subscription-master",
        entity_id="entity-1",
        owner_user_id="user-1",
        agent_id=MANOR_AGENT_ID,
        workspace_id=None,
        status="active",
    )
    db = SimpleNamespace(
        execute=AsyncMock(return_value=SimpleNamespace(
            scalar_one_or_none=lambda: conversation,
        )),
        flush=AsyncMock(),
    )

    resolved = await _validate_conversation(
        db,
        subscription=subscription,
        owner_user_id="user-1",
        delivery=DeliveryPolicy.model_validate({
            "surface": {
                "kind": "conversation",
                "conversation_id": "conversation-1",
            },
            "notification": {
                "enabled": False,
                "channels": ["inapp"],
            },
        }),
        bind_personal=True,
    )

    assert resolved is conversation
    assert conversation.agent_id is None
    assert conversation.agent_subscription_id == subscription.id
    db.flush.assert_awaited_once()


def test_initiative_id_cannot_cross_conversation_or_subscription() -> None:
    context = _context(
        ChatSurface.AGENT_DM,
        RuntimeProfile.AGENT_DELEGATE,
    )
    subscription = SimpleNamespace(id="subscription-1", workspace_id=None)
    row = SimpleNamespace(
        agent_subscription_id="subscription-1",
        workspace_id=None,
        delivery_policy={
            "version": "1",
            "surface": {
                "kind": "conversation",
                "conversation_id": "conversation-1",
            },
        },
    )
    _assert_current_initiative_scope(
        row=row,
        subscription=subscription,
        context=context,
    )

    row.delivery_policy["surface"]["conversation_id"] = "conversation-2"
    with pytest.raises(AgentInitiativeServiceError):
        _assert_current_initiative_scope(
            row=row,
            subscription=subscription,
            context=context,
        )
    row.delivery_policy["surface"]["conversation_id"] = "conversation-1"
    row.agent_subscription_id = "subscription-2"
    with pytest.raises(AgentInitiativeServiceError):
        _assert_current_initiative_scope(
            row=row,
            subscription=subscription,
            context=context,
        )


@pytest.mark.asyncio
async def test_read_resolution_does_not_materialize_a_personal_subscription(
    monkeypatch,
) -> None:
    conversation = Conversation(
        id="conversation-1",
        entity_id="entity-1",
        user_id="user-1",
        agent_id="agent-1",
        workspace_id=None,
        status="active",
        scope="channel",
    )

    class Result:
        def __init__(self, value):
            self.value = value

        def scalar_one_or_none(self):
            return self.value

    class FakeDB:
        def __init__(self):
            self.values = iter((conversation, None))

        async def execute(self, _query):
            return Result(next(self.values))

    async def unexpected_materialization(*_args, **_kwargs):
        raise AssertionError("a read-only tool must not create a Subscription")

    monkeypatch.setattr(
        agent_initiative_tools,
        "ensure_personal_subscription",
        unexpected_materialization,
    )
    result = await _current_subscription(
        FakeDB(),
        context=_context(ChatSurface.AGENT_DM, RuntimeProfile.AGENT_DELEGATE),
        user=SimpleNamespace(id="user-1", entity_id="entity-1"),
    )
    assert result is None


def test_managed_driver_blocks_every_initiative_mutation() -> None:
    initiative = SimpleNamespace(
        id="initiative-1",
        name="Daily briefing",
        instruction="Check for relevant news",
        owner_user_id="user-1",
        revision=4,
        status="active",
        trigger_config={
            "version": "1",
            "kind": "interval",
            "every_seconds": 3600,
            "timezone": "UTC",
        },
        condition_config={"version": "1", "semantic_condition": "new items exist"},
        decision_policy={"version": "1", "mode": "on_change"},
        delivery_policy={
            "version": "1",
            "surface": {
                "kind": "conversation",
                "conversation_id": "conversation-1",
            },
        },
        interruption_policy={"version": "1", "quiet_hours": {"mode": "none"}},
    )
    subscription = SimpleNamespace(
        id="subscription-1",
        agent_id="agent-1",
        workspace_id=None,
    )
    fields = _driver_fields(initiative, subscription)
    blocked = set(fields["execution_target"]["blocked_tool_names"])
    assert _MUTATION_TOOLS.issubset(blocked)

    envelope = RuntimeEnvelope(
        surface=ChatSurface.SCHEDULED_AGENT_RUN,
        principal=resolve_runtime_principal(
            surface=ChatSurface.SCHEDULED_AGENT_RUN,
            entity_id="entity-1",
            user_id="user-1",
            agent_id="agent-1",
            system_worker=True,
        ),
        profile=RuntimeProfile.BACKGROUND_WORKER,
        entity_id="entity-1",
        user_id="user-1",
        agent_id="agent-1",
        allowed_tool_names=AGENT_INITIATIVE_TOOL_NAMES,
        blocked_tool_names=tuple(blocked),
    )
    for name in _MUTATION_TOOLS:
        decision = check_runtime_tool_policy(
            envelope=envelope,
            tool_name=name,
            arguments={},
        )
        assert not decision.allowed
        assert decision.code == RuntimeToolPolicyCode.BLOCKED_BY_RUNTIME_POLICY.value
