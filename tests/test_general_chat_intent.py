import json
from types import SimpleNamespace

import pytest

from packages.core.ai.runtime.general_chat_intent import (
    GeneralChatIntentDecision,
    GeneralChatIntentKind,
    WorkspaceIntentCandidate,
    WorkspaceRecommendationAction,
    classify_general_chat_intent,
    _classifier_messages,
)
from packages.core.ai.runtime.surfaces import ChatSurface
from packages.core.ai.runtime.turn_policy import (
    CHAT_MAX_ROUNDS,
    TurnExecutionMode,
    TurnExecutionPlan,
    TurnToolCatalogMode,
    build_turn_execution_plan,
    runtime_turn_max_rounds,
    runtime_turn_visible_tool_names,
)
from packages.core.ai.runtime.workspace_creation_authorization import (
    WorkspaceCreationAuthorizationDecision,
    WorkspaceCreationAuthorizationStatus,
    _authorization_messages,
    classify_workspace_creation_authorization,
)
from packages.core.services.chat_intent_routing import (
    ChatIntentRoutingDecision,
    DeferredChatIntentRequest,
    _workspace_draft_ids,
    auto_chat_intent_routing_allowed,
    classify_chat_intent_routing,
    classify_deferred_chat_intent,
    workspace_recommendations_enabled,
)


def _candidate(*, can_add: bool = True) -> WorkspaceIntentCandidate:
    return WorkspaceIntentCandidate(
        workspace_id="01WORKSPACEEXACT0000000000",
        name="Launch operations",
        summary="Launch planning and weekly delivery",
        can_add=can_add,
    )


def _payload(**overrides):
    value = {
        "kind": "workspace_recommendation",
        "confidence": 0.95,
        "reason": "This needs recurring delivery tracking.",
        "action": "add_to_existing",
        "workspace_id": "01WORKSPACEEXACT0000000000",
    }
    value.update(overrides)
    return value


def _not_authorized(
    confidence: float = 0.99,
) -> WorkspaceCreationAuthorizationDecision:
    return WorkspaceCreationAuthorizationDecision.not_authorized(confidence)


def test_recommendation_factory_requires_exact_candidate_id():
    with pytest.raises(ValueError, match="exact candidate id"):
        GeneralChatIntentDecision.from_payload(
            _payload(workspace_id="Launch operations"),
            candidates=[_candidate()],
        )


def test_recommendation_factory_rejects_add_for_read_only_workspace():
    with pytest.raises(ValueError, match="read-only"):
        GeneralChatIntentDecision.from_payload(
            _payload(),
            candidates=[_candidate(can_add=False)],
        )


def test_recommendation_factory_builds_typed_add_action():
    decision = GeneralChatIntentDecision.from_payload(
        _payload(),
        candidates=[_candidate()],
    )

    assert decision.kind is GeneralChatIntentKind.WORKSPACE_RECOMMENDATION
    assert decision.recommendation is not None
    assert decision.recommendation.action is WorkspaceRecommendationAction.ADD_TO_EXISTING
    assert decision.recommendation.workspace_id == "01WORKSPACEEXACT0000000000"
    assert decision.recommendation.workspace_name == "Launch operations"


def test_low_confidence_recommendation_falls_back_to_agent_chat():
    decision = GeneralChatIntentDecision.from_payload(
        _payload(confidence=0.7),
        candidates=[_candidate()],
    )

    assert decision.kind is GeneralChatIntentKind.AGENT_CHAT
    assert decision.recommendation is None


def test_agent_chat_payload_cannot_smuggle_workspace_action():
    with pytest.raises(ValueError, match="cannot include"):
        GeneralChatIntentDecision.from_payload(
            _payload(kind="agent_chat", reason="", confidence=0.99),
            candidates=[_candidate()],
        )


def test_suitability_prompt_only_compares_chat_with_workspace():
    messages = _classifier_messages(
        message_text="管理我的房产",
        recent_context_text="",
        candidates=[],
    )

    prompt = messages[0]["content"]
    assert "Decide only which operating surface is the best fit" in prompt
    assert "Do not decide whether the user authorized Workspace creation" in prompt
    assert '"kind":"agent_chat|workspace_recommendation"' in prompt
    assert "workspace_create" not in prompt


def test_suitability_prompt_understands_real_workspace_operating_boundary():
    messages = _classifier_messages(
        message_text="Run and measure this operation every week",
        recent_context_text="",
        candidates=[],
    )

    prompt = messages[0]["content"]
    assert "conversation-scoped assistant surface" in prompt
    assert "use tools, and create or edit artifacts" in prompt
    assert "durable named operating boundary" in prompt
    assert "shared Knowledge and artifacts" in prompt
    assert "goals, measurements, and operating rules" in prompt
    assert "Proposals, Tasks, Plans, Workflows, approvals, and governance" in prompt
    assert "scheduled automations or recurring Strategist reviews" in prompt
    assert "material to the requested outcome, not merely compatible" in prompt
    assert "long or complex one-off request" in prompt


@pytest.mark.asyncio
async def test_suitability_classifier_receives_latest_query_and_active_draft_context(
    monkeypatch,
):
    captured: dict[str, object] = {}

    async def fake_completion(messages, **_kwargs):
        captured["messages"] = messages
        return SimpleNamespace(
            content=json.dumps(
                _payload(
                    action="create_new",
                    workspace_id=None,
                    reason="Recruiting needs its own recurring operating system.",
                ),
                ensure_ascii=False,
            ),
            usage={},
        )

    monkeypatch.setattr(
        "packages.core.ai.runtime.general_chat_intent.runtime_execute_text_completion",
        fake_completion,
    )

    decision = await classify_general_chat_intent(
        message_text="我想长期管理招聘流程",
        recent_context_text=(
            "assistant: Property marketing Workspace draft\n"
            "[An active Workspace Draft exists in this conversation. Treat it as context "
            "only: determine whether the latest user request continues that Draft or is an "
            "independent request that may need a separate Workspace.]"
        ),
        candidates=[],
    )

    messages = captured["messages"]
    assert isinstance(messages, list)
    prompt = messages[0]["content"]
    request = json.loads(messages[1]["content"])
    assert "do not let unrelated history turn a bounded query into a Workspace" in prompt
    assert request["latest_user_message"] == "我想长期管理招聘流程"
    assert "Property marketing Workspace draft" in request["recent_conversation"]
    assert "independent request that may need a separate Workspace" in (
        request["recent_conversation"]
    )
    assert decision.kind is GeneralChatIntentKind.WORKSPACE_RECOMMENDATION


def test_creation_authorization_is_a_separate_typed_decision():
    decision = WorkspaceCreationAuthorizationDecision.from_payload({
        "status": "authorized",
        "confidence": 0.97,
    })

    assert decision.status is WorkspaceCreationAuthorizationStatus.AUTHORIZED
    assert decision.to_metadata() == {
        "status": "authorized",
        "confidence": 0.97,
    }


def test_low_confidence_creation_authorization_fails_closed():
    decision = WorkspaceCreationAuthorizationDecision.from_payload({
        "status": "authorized",
        "confidence": 0.84,
    })

    assert decision.status is WorkspaceCreationAuthorizationStatus.NOT_AUTHORIZED


def test_creation_authorization_prompt_does_not_classify_workspace_suitability():
    messages = _authorization_messages(
        message_text="管理我的房产",
        recent_context_text="",
    )

    prompt = messages[0]["content"]
    assert "explicitly authorizes starting and saving a new Workspace draft" in prompt
    assert "manage my properties" in prompt
    assert "not authorization" in prompt
    assert "best fit" not in prompt


@pytest.mark.asyncio
async def test_classifier_malformed_result_fails_closed_to_agent_chat(monkeypatch):
    async def fake_completion(*_args, **_kwargs):
        return SimpleNamespace(content='{"kind":"workspace_recommendation"}', usage={})

    monkeypatch.setattr(
        "packages.core.ai.runtime.general_chat_intent.runtime_execute_text_completion",
        fake_completion,
    )

    decision = await classify_general_chat_intent(
        message_text="Run a weekly launch program",
        candidates=[_candidate()],
    )

    assert decision.kind is GeneralChatIntentKind.AGENT_CHAT


@pytest.mark.asyncio
async def test_creation_authorization_malformed_result_fails_closed(monkeypatch):
    async def fake_completion(*_args, **_kwargs):
        return SimpleNamespace(content='{"status":"authorized"}', usage={})

    monkeypatch.setattr(
        "packages.core.ai.runtime.workspace_creation_authorization."
        "runtime_execute_text_completion",
        fake_completion,
    )

    decision = await classify_workspace_creation_authorization(
        message_text="管理我的房产",
    )

    assert decision.status is WorkspaceCreationAuthorizationStatus.NOT_AUTHORIZED


def _routing_decision() -> ChatIntentRoutingDecision:
    return ChatIntentRoutingDecision(
        execution_plan=TurnExecutionPlan(mode=TurnExecutionMode.TOOL_CHAT, max_rounds=8),
        deferred=DeferredChatIntentRequest(
            key="intent-1", message="Run the launch every week", recent_context="",
            entity_id="entity-1", user_id="user-1", conversation_id=None,
            recommendations_enabled=True,
        ),
    )


def test_recommendation_stays_out_of_the_agent_prompt():
    """A card may follow any reply, so the prompt never announces one and the
    recommendation itself is delivered beside the turn, not in its metadata."""
    metadata = _routing_decision().runtime_metadata(request="Run the launch every week")

    assert "workspace_recommendation" not in metadata
    response_guardrail = metadata["general_intent_prompt"]
    assert "A separate UI card may offer" not in response_guardrail
    assert "never mention or allude" in response_guardrail
    assert "do not predict or announce that any interface element will appear" in (
        response_guardrail
    )


def test_agent_chat_prompt_forbids_implicit_new_workspace_draft():
    prompt = _routing_decision().runtime_metadata(request="管理我的房产")["general_intent_prompt"]

    assert "Start a new Workspace draft only when the user explicitly asks" in prompt
    assert "existing active Workspace draft may still be updated" in prompt


def test_explicit_creation_allows_a_draft_but_not_the_final_workspace():
    prompt = _routing_decision().runtime_metadata(request="Create a Workspace")["general_intent_prompt"]

    assert "save a guided Workspace draft" in prompt
    assert "do not create the final Workspace" in prompt


def test_auto_chat_intent_routing_is_only_for_unencumbered_owner_auto_request():
    base = dict(
        surface=ChatSurface.GLOBAL_OWNER_CHAT,
        chat_mode="auto",
        agent_id=None,
        workspace_id=None,
        manual_skill_selected=False,
        editor_context=None,
        ephemeral=False,
        disable_tools=False,
        blocked_tools=False,
        approval_turn=False,
        has_forced_tool_calls=False,
        has_attachments=False,
    )

    assert auto_chat_intent_routing_allowed(**base)
    assert not auto_chat_intent_routing_allowed(**{**base, "workspace_id": "ws-1"})
    assert not auto_chat_intent_routing_allowed(**{**base, "has_attachments": True})
    assert not auto_chat_intent_routing_allowed(**{**base, "chat_mode": "research"})


def _forbid_planning_classifiers(monkeypatch, module) -> None:
    async def unexpected(**_kwargs):
        raise AssertionError("planning a turn must not wait on a model classifier")

    monkeypatch.setattr(module, "classify_general_chat_intent", unexpected)
    monkeypatch.setattr(module, "classify_workspace_creation_authorization", unexpected)


def _deferred_request(**overrides) -> DeferredChatIntentRequest:
    return DeferredChatIntentRequest(**{
        "key": "intent-1",
        "message": "How should I organize this eight-week launch?",
        "recent_context": "",
        "entity_id": "entity-1",
        "user_id": "user-1",
        "conversation_id": None,
        "recommendations_enabled": True,
        **overrides,
    })


@pytest.mark.asyncio
async def test_planning_a_turn_waits_on_no_classifier(monkeypatch):
    """Creation authorization and recommendation run beside the turn."""
    from packages.core.services import chat_intent_routing as module

    async def fake_recent_context(*_args, **_kwargs):
        return "assistant: earlier turn", False

    monkeypatch.setattr(module, "_recent_chat_context", fake_recent_context)
    _forbid_planning_classifiers(monkeypatch, module)

    decision = await classify_chat_intent_routing(
        object(),
        user=SimpleNamespace(entity_id="entity-1", id="user-1"),
        message="最近跑过吗？",
        conversation_id="initiative-chat",
        agent_subscription_id="subscription-1",
    )
    metadata = decision.runtime_metadata(request="最近跑过吗？")

    assert decision.execution_plan.mode is TurnExecutionMode.AGENT
    assert decision.execution_plan.disable_tools is False
    assert decision.execution_plan.tool_catalog_mode is TurnToolCatalogMode.FULL
    assert decision.deferred.message == "最近跑过吗？"
    assert decision.deferred.recent_context == "assistant: earlier turn"
    assert decision.deferred.recommendations_enabled is True
    assert metadata["workspace_creation_authorization"] == {
        "status": "deferred", "key": decision.deferred.key,
    }
    assert metadata["deferred_chat_intent_key"] == decision.deferred.key
    assert "workspace_recommendation" not in metadata
    assert "do not create the final Workspace" in metadata["general_intent_prompt"]


@pytest.mark.asyncio
async def test_voice_greeting_uses_the_same_routing_as_other_chat(monkeypatch):
    from packages.core.services import chat_intent_routing as module

    async def recent_context(*_args, **_kwargs):
        return "", False

    monkeypatch.setattr(module, "_recent_chat_context", recent_context)
    _forbid_planning_classifiers(monkeypatch, module)

    decision = await classify_chat_intent_routing(
        object(),
        user=SimpleNamespace(entity_id="entity-1", id="user-1", preferences={
            "workspace_recommendations_enabled": False,
        }),
        message="Hi.",
        conversation_id="conversation-1",
        runtime_metadata={"voice_session_mode": "chat_gateway"},
    )

    assert decision.execution_plan.mode is TurnExecutionMode.TOOL_CHAT
    assert decision.execution_plan.disable_tools is False
    assert decision.deferred.recommendations_enabled is False


def test_workspace_recommendations_default_on_and_respect_user_opt_out():
    assert workspace_recommendations_enabled(SimpleNamespace())
    assert workspace_recommendations_enabled(
        SimpleNamespace(preferences={"workspace_recommendations_enabled": True})
    )
    assert not workspace_recommendations_enabled(
        SimpleNamespace(preferences={"workspace_recommendations_enabled": False})
    )


def test_workspace_draft_ids_use_only_structured_artifact_ids():
    value = [
        {"artifact_kind": "workspace_draft", "draft_id": "draft-exact"},
        {"artifact_kind": "document", "draft_id": "not-a-workspace-draft"},
        '{"artifact_kind":"workspace_draft","draft_id":"draft-json"}',
    ]

    assert _workspace_draft_ids(value) == {"draft-exact", "draft-json"}


@pytest.mark.asyncio
async def test_deferred_intent_recommends_a_workspace(monkeypatch):
    from packages.core.services import chat_intent_routing as module

    calls: list[list[WorkspaceIntentCandidate]] = []

    async def fake_general_intent(*, candidates, **_kwargs):
        calls.append(candidates)
        return GeneralChatIntentDecision.from_payload(
            _payload(
                action="create_new",
                workspace_id=None,
                reason="Eight weeks of recurring tracking needs durable state.",
            ),
            candidates=candidates,
        )

    async def fake_creation_authorization(**_kwargs):
        return _not_authorized()

    monkeypatch.setattr(module, "classify_general_chat_intent", fake_general_intent)
    monkeypatch.setattr(module, "classify_workspace_creation_authorization", fake_creation_authorization)

    result = await classify_deferred_chat_intent(_deferred_request(), candidates=[_candidate()])
    metadata = result.to_metadata(request="How should I organize this eight-week launch?")

    assert result.general_intent.kind is GeneralChatIntentKind.WORKSPACE_RECOMMENDATION
    assert metadata["workspace_recommendation"]["action"] == "create_new"
    assert metadata["workspace_creation_authorization"]["status"] == "not_authorized"
    assert calls == [[_candidate()]]


@pytest.mark.asyncio
async def test_active_draft_context_still_reaches_the_recommendation(monkeypatch):
    from packages.core.services import chat_intent_routing as module

    seen_context: list[str] = []

    async def fake_general_intent(*, candidates, recent_context_text, **_kwargs):
        seen_context.append(recent_context_text)
        return GeneralChatIntentDecision.from_payload(
            _payload(
                action="create_new",
                workspace_id=None,
                reason="Recruiting needs its own recurring operating system.",
            ),
            candidates=candidates,
        )

    async def fake_creation_authorization(**_kwargs):
        return _not_authorized()

    monkeypatch.setattr(module, "classify_general_chat_intent", fake_general_intent)
    monkeypatch.setattr(module, "classify_workspace_creation_authorization", fake_creation_authorization)

    result = await classify_deferred_chat_intent(
        _deferred_request(
            message="我想长期管理招聘流程",
            recent_context=(
                "[An active Workspace Draft exists in this conversation.]\n"
                "assistant: Property marketing draft"
            ),
        ),
        candidates=[_candidate()],
    )

    assert result.general_intent.kind is GeneralChatIntentKind.WORKSPACE_RECOMMENDATION
    assert seen_context and "Property marketing draft" in seen_context[0]


@pytest.mark.asyncio
async def test_user_opt_out_skips_suitability_but_keeps_creation_authorization(monkeypatch):
    from packages.core.services import chat_intent_routing as module

    authorization_calls: list[dict] = []

    async def unexpected_general_intent(**_kwargs):
        raise AssertionError("suitability must not run after recommendation opt-out")

    async def fake_creation_authorization(**kwargs):
        authorization_calls.append(kwargs)
        return _not_authorized()

    monkeypatch.setattr(module, "classify_general_chat_intent", unexpected_general_intent)
    monkeypatch.setattr(module, "classify_workspace_creation_authorization", fake_creation_authorization)

    result = await classify_deferred_chat_intent(
        _deferred_request(recommendations_enabled=False), candidates=[],
    )

    assert result.general_intent.kind is GeneralChatIntentKind.AGENT_CHAT
    assert result.general_intent.recommendation is None
    assert len(authorization_calls) == 1
    assert authorization_calls[0]["message_text"] == (
        "How should I organize this eight-week launch?"
    )


@pytest.mark.asyncio
async def test_user_opt_out_does_not_block_explicit_workspace_creation(monkeypatch):
    from packages.core.services import chat_intent_routing as module

    async def unexpected_general_intent(**_kwargs):
        raise AssertionError("suitability must not run after opt-out")

    async def fake_creation_authorization(**_kwargs):
        return WorkspaceCreationAuthorizationDecision.from_payload({
            "status": "authorized",
            "confidence": 0.99,
        })

    monkeypatch.setattr(module, "classify_general_chat_intent", unexpected_general_intent)
    monkeypatch.setattr(module, "classify_workspace_creation_authorization", fake_creation_authorization)

    result = await classify_deferred_chat_intent(
        _deferred_request(message="Create a Workspace for this launch", recommendations_enabled=False),
        candidates=[],
    )
    metadata = result.to_metadata(request="Create a Workspace for this launch")

    assert metadata["workspace_creation_authorization"]["status"] == "authorized"
    assert metadata["workspace_recommendation"] is None


@pytest.mark.asyncio
async def test_explicit_create_suppresses_recommendation_and_authorizes_draft(monkeypatch):
    from packages.core.services import chat_intent_routing as module

    async def fake_general_intent(**_kwargs):
        return GeneralChatIntentDecision.from_payload(
            _payload(),
            candidates=[_candidate()],
        )

    async def fake_creation_authorization(**_kwargs):
        return WorkspaceCreationAuthorizationDecision.from_payload({
            "status": "authorized",
            "confidence": 0.99,
        })

    monkeypatch.setattr(module, "classify_general_chat_intent", fake_general_intent)
    monkeypatch.setattr(module, "classify_workspace_creation_authorization", fake_creation_authorization)

    result = await classify_deferred_chat_intent(
        _deferred_request(message="Create a Workspace for this launch"),
        candidates=[_candidate()],
    )
    metadata = result.to_metadata(request="Create a Workspace for this launch")

    assert result.general_intent.kind is GeneralChatIntentKind.AGENT_CHAT
    assert metadata["workspace_recommendation"] is None
    assert metadata["workspace_creation_authorization"]["status"] == "authorized"


@pytest.mark.asyncio
async def test_deferred_intent_is_published_and_read_back(monkeypatch):
    from packages.core.services import chat_intent_routing as module

    async def fake_classify(request, *, candidates):
        return module.DeferredChatIntent(
            workspace_creation_authorization=WorkspaceCreationAuthorizationDecision.from_payload({
                "status": "authorized", "confidence": 0.95,
            }),
            general_intent=GeneralChatIntentDecision.agent_chat(1.0),
        )

    monkeypatch.setattr(module, "classify_deferred_chat_intent", fake_classify)
    from packages.core.models.base import generate_ulid

    request = _deferred_request(key=generate_ulid(), recommendations_enabled=False)

    assert await module.wait_for_deferred_chat_intent(request.key, timeout=0) is None
    await module.publish_deferred_chat_intent(request)
    published = await module.wait_for_deferred_chat_intent(request.key, timeout=0)

    assert published["workspace_creation_authorization"]["status"] == "authorized"
    assert published["workspace_recommendation"] is None


@pytest.mark.asyncio
async def test_failed_deferred_classification_publishes_a_closed_default(monkeypatch):
    """The draft gate must not wait out its timeout when classification failed."""
    from packages.core.services import chat_intent_routing as module

    async def broken(request, *, candidates):
        raise RuntimeError("provider error")

    monkeypatch.setattr(module, "classify_deferred_chat_intent", broken)
    from packages.core.models.base import generate_ulid

    request = _deferred_request(key=generate_ulid(), recommendations_enabled=False)

    await module.publish_deferred_chat_intent(request)
    published = await module.wait_for_deferred_chat_intent(request.key, timeout=0)

    assert published["workspace_creation_authorization"]["status"] == "not_authorized"
    assert published["workspace_recommendation"] is None


def test_turn_execution_plan_factory_fails_closed_on_invalid_enum():
    plan = TurnExecutionPlan.from_metadata({"mode": "unknown", "disable_tools": True})

    assert plan.mode is TurnExecutionMode.LEGACY
    assert plan.disable_tools is False
    assert plan.reason == "invalid_turn_execution_plan_metadata"


@pytest.mark.parametrize(
    ("message", "message_text", "metadata"),
    [
        ("你去做", "你去做", {}),
        (
            "Fill the attached document and export a PDF.",
            "Fill the attached document and export a PDF.", {},
        ),
        (
            "暂停这个 Chat 里的 AMDL 价格与新闻监控。",
            "暂停这个 Chat 里的 AMDL 价格与新闻监控。", {},
        ),
        ("你去做", "你去做", {"extra_tool_names": ["generate_file"]}),
        (
            [
                {"type": "text", "text": "Inspect this image."},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,eA=="}},
            ],
            "Inspect this image.", {},
        ),
    ],
    ids=["continuation", "document", "action", "explicit-tool", "multimodal"],
)
def test_owner_tool_chat_has_room_to_complete_multi_step_work(
    message, message_text, metadata,
):
    plan = build_turn_execution_plan(
        enabled=True,
        surface=ChatSurface.GLOBAL_OWNER_CHAT,
        message=message,
        message_text=message_text,
        runtime_metadata=metadata,
    )

    assert plan.mode is TurnExecutionMode.TOOL_CHAT
    assert plan.disable_tools is False
    assert plan.max_rounds == CHAT_MAX_ROUNDS
    # Chat persists the plan before the execution worker consumes its budget.
    persisted = TurnExecutionPlan.from_metadata(plan.to_metadata())
    assert runtime_turn_max_rounds(SimpleNamespace(turn_execution_plan=persisted)) == CHAT_MAX_ROUNDS


@pytest.mark.parametrize(
    ("message", "overrides", "expected_budget"),
    [
        ("Why is the sky blue?", {}, CHAT_MAX_ROUNDS),
        ("你去做", {"disable_tools": True}, 1),
        (
            "Run this", {"runtime_metadata": {"forced_tool_calls": [{"name": "read_file"}]}}, CHAT_MAX_ROUNDS,
        ),
        (
            "制作完整视频",
            {"runtime_metadata": {"forced_tool_calls": [
                {"name": "invoke_skill", "arguments": {"skill": "video-production"}}
            ]}},
            CHAT_MAX_ROUNDS,
        ),
        ("Create a PowerPoint presentation", {}, CHAT_MAX_ROUNDS),
        ("你去做", {"runtime_metadata": {"chat_mode": "document"}}, CHAT_MAX_ROUNDS),
        (
            "你去做", {"runtime_metadata": {"continuation_context_text": "Fill the attached DOCX."}}, CHAT_MAX_ROUNDS,
        ),
        ("查一下 AMDL 当前价格和最新新闻。", {"manual_skill_selected": True}, CHAT_MAX_ROUNDS),
        ("你去做", {"agent_id": "agent-1"}, CHAT_MAX_ROUNDS),
        ("你去做", {"surface": ChatSurface.WORKSPACE_CHAT, "workspace_id": "ws-1"}, CHAT_MAX_ROUNDS),
    ],
    ids=["direct", "tools-disabled", "forced", "video-production", "slides", "document-mode",
         "generation-continuation", "skill-research", "bound-agent", "workspace"],
)
def test_every_tool_using_chat_turn_shares_one_round_budget(message, overrides, expected_budget):
    plan = build_turn_execution_plan(**{
        "enabled": True,
        "surface": ChatSurface.GLOBAL_OWNER_CHAT,
        "message": message,
        "message_text": message,
        **overrides,
    })

    assert plan.max_rounds == expected_budget


@pytest.mark.asyncio
@pytest.mark.parametrize(("metadata", "expected_budget"), [({}, 1000), ({"chat_mode": "document"}, 1000)])
async def test_owner_turn_budget_reaches_loop_without_changing_its_default(
    monkeypatch, metadata, expected_budget,
):
    import importlib
    from unittest.mock import AsyncMock, Mock

    from packages.core.ai.runtime.harness import runtime_execute_agentic_loop
    from packages.core.ai.runtime.response_language import RuntimeResponseLanguage

    loop_module = importlib.import_module("packages.core.ai.agentic_loop")
    seen = {}

    async def fake_loop(*, max_rounds=loop_module.DEFAULT_MAX_ROUNDS, **kwargs):
        seen["max_rounds"] = max_rounds
        return SimpleNamespace(content="done", usage={}, rounds=1, tool_calls_made=[])

    billing = Mock()
    monkeypatch.setattr(loop_module, "agentic_loop", fake_loop)
    monkeypatch.setattr(
        "packages.core.ai.runtime.completions.runtime_resolve_text_completion_route",
        AsyncMock(return_value=("test-model", {}, False)),
    )
    monkeypatch.setattr(
        "packages.core.ai.runtime.response_language.resolve_runtime_response_language",
        AsyncMock(return_value=RuntimeResponseLanguage("entity-1", "user-1")),
    )
    monkeypatch.setattr("packages.core.ai.runtime.billing.runtime_ensure_billing_context", billing)
    plan = build_turn_execution_plan(
        enabled=True,
        surface=ChatSurface.GLOBAL_OWNER_CHAT,
        message="你去做",
        message_text="你去做",
        runtime_metadata=metadata,
    )
    ctx = SimpleNamespace(turn_execution_plan=plan.to_metadata())

    result = await runtime_execute_agentic_loop(
        runtime_envelope=None,
        system_prompt="system",
        user_message="你去做",
        tools=[],
        entity_id="entity-1",
        agent_id=None,
        user_id="user-1",
        conversation_id="conversation-1",
        billing_source="chat",
        max_rounds=runtime_turn_max_rounds(ctx),
    )

    assert result.content == "done"
    assert seen["max_rounds"] == expected_budget
    assert loop_module.DEFAULT_MAX_ROUNDS == 200
    billing.assert_called_once_with(
        "entity-1", source="chat", user_id="user-1", agent_id=None,
        workspace_id=None, conversation_id="conversation-1", task_id=None, byok=False,
    )


@pytest.mark.asyncio
async def test_owner_tool_chat_can_finish_after_nine_tool_rounds(monkeypatch):
    import importlib
    from unittest.mock import AsyncMock

    loop_module = importlib.import_module("packages.core.ai.agentic_loop")
    model_calls = 0
    tool_calls = []

    async def completion(messages, tools, **kwargs):
        nonlocal model_calls
        model_calls += 1
        usage = {"prompt": 10, "completion": 1, "total": 11}
        if model_calls == 10:
            return "Verified output is ready.", [], usage
        return "", [{
            "id": f"call-{model_calls}",
            "name": "read_file",
            "arguments": {"path": f"evidence-{model_calls}.txt"},
        }], usage

    async def execute(name, args):
        tool_calls.append((name, args))
        return json.dumps({"content": "verified"})

    final_summary = AsyncMock(side_effect=AssertionError("The work must finish before its budget."))
    monkeypatch.setattr(loop_module, "runtime_execute_agentic_round_tool_completion", completion)
    monkeypatch.setattr(loop_module, "runtime_execute_agentic_final_completion", final_summary)
    plan = build_turn_execution_plan(
        enabled=True,
        surface=ChatSurface.GLOBAL_OWNER_CHAT,
        message="你去做",
        message_text="你去做",
    )

    result = await loop_module.agentic_loop(
        system_prompt="Complete and verify the requested work.",
        user_message="你去做",
        tools=[{"type": "function", "function": {
            "name": "read_file", "parameters": {"type": "object"},
        }}],
        tool_executor=execute,
        max_rounds=runtime_turn_max_rounds(SimpleNamespace(turn_execution_plan=plan)),
    )

    assert result.stop_reason == "completed"
    assert result.content == "Verified output is ready."
    assert len(tool_calls) == 9
    assert model_calls == result.rounds == 10
    final_summary.assert_not_awaited()


@pytest.mark.parametrize(
    "message",
    [
        "暂停这个 Chat 里的 AMDL 价格与新闻监控。",
        "Resume my daily briefing monitor.",
        "请只使用给出的材料回答。",
        "What is the current price of AMDL?",
        "Hello",
    ],
)
@pytest.mark.parametrize(
    ("surface", "workspace_id", "agent_id", "expected_mode"),
    [
        (ChatSurface.GLOBAL_OWNER_CHAT, None, None, TurnExecutionMode.TOOL_CHAT),
        (ChatSurface.WORKSPACE_CHAT, "ws-1", None, TurnExecutionMode.WORKSPACE),
        (ChatSurface.AGENT_DM, None, "agent-1", TurnExecutionMode.AGENT),
    ],
)
def test_chat_wording_never_narrows_authorized_tools(
    message, surface, workspace_id, agent_id, expected_mode,
):
    plan = build_turn_execution_plan(
        enabled=True,
        surface=surface,
        message=message,
        message_text=message,
        workspace_id=workspace_id,
        agent_id=agent_id,
    )

    assert plan.mode is expected_mode
    assert plan.disable_tools is False
    assert plan.tool_catalog_mode is TurnToolCatalogMode.FULL
    assert plan.research_strategy.value == "none"
    assert runtime_turn_visible_tool_names(plan) is None


@pytest.mark.asyncio
async def test_runtime_context_applies_direct_chat_plan(monkeypatch):
    from packages.core.ai.runtime.prompt_adapter import ChatContext
    from packages.core.ai.runtime.turn_policy import TurnPromptMode
    from packages.core.services import runtime_chat_context as module

    captured: dict = {}
    model_roles: list[str] = []

    async def fake_workspace_runtime(*_args, **_kwargs):
        return SimpleNamespace(
            workspace_id=None,
            tool_profile=None,
            is_master=True,
            task_id=None,
            thread_ref_kind=None,
            thread_ref_id=None,
            bound_tool_names=set(),
            mcp_allowed_names=set(),
            extra_context=None,
        )

    async def fake_assemble(db, *, request, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            context=ChatContext(
                db=db,
                entity_id=request.entity_id,
                user_id=request.user_id,
                runtime_envelope=SimpleNamespace(metadata=request.metadata),
            ),
            tool_schemas=[],
            prompt="base prompt",
        )

    async def fake_resolve_model(role, **_kwargs):
        model_roles.append(role)
        return "worker-model"

    async def fake_resolve_metadata(role, **_kwargs):
        model_roles.append(f"metadata:{role}")
        return None

    monkeypatch.setattr(
        "packages.core.services.workspace_runtime.resolve_workspace_runtime",
        fake_workspace_runtime,
    )
    monkeypatch.setattr(module, "runtime_assemble_prompt_for_turn", fake_assemble)
    monkeypatch.setattr(
        "packages.core.services.model_resolver.resolve_model_for_user",
        fake_resolve_model,
    )
    monkeypatch.setattr(
        "packages.core.services.model_resolver.resolve_llm_metadata_for_user",
        fake_resolve_metadata,
    )

    plan = TurnExecutionPlan(
        mode=TurnExecutionMode.DIRECT_CHAT,
        disable_tools=True,
        use_fast_model=True,
        prompt_mode=TurnPromptMode.MINIMAL,
        max_rounds=1,
        history_token_budget=8_000,
        reason="test_direct",
        confidence=0.99,
    )
    prompt, tools, _history, ctx = await module.resolve_runtime_chat_context(
        object(),
        "Explain this briefly",
        entity_id="entity-1",
        user_id="user-1",
        runtime_metadata={
            "turn_execution_plan": plan.to_metadata(),
            "general_intent_prompt": "Keep this turn read-only.",
        },
    )

    assert tools == []
    assert captured["mode"] == "minimal"
    assert captured["disable_tools"] is True
    assert captured["tool_schemas"] == []
    assert captured["allowed_tool_names"] == set()
    assert ctx.turn_execution_plan == plan
    assert ctx.auto_forced_tool_calls == []
    assert ctx.model == "worker-model"
    assert model_roles == ["worker", "metadata:worker"]
    assert prompt.startswith("Keep this turn read-only.\n\n")
