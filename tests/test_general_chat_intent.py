import json
from types import SimpleNamespace

import pytest

from packages.core.ai.runtime.auto_route_classifier import (
    AutoRouteDecision,
    AutoRouteMode,
)
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
    TurnExecutionMode,
    TurnExecutionPlan,
    TurnToolCatalogMode,
    build_turn_execution_plan,
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
    _workspace_draft_ids,
    auto_chat_intent_routing_allowed,
    classify_chat_intent_routing,
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
            )
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
        return SimpleNamespace(content='{"kind":"workspace_recommendation"}')

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
        return SimpleNamespace(content='{"status":"authorized"}')

    monkeypatch.setattr(
        "packages.core.ai.runtime.workspace_creation_authorization."
        "runtime_execute_text_completion",
        fake_completion,
    )

    decision = await classify_workspace_creation_authorization(
        message_text="管理我的房产",
    )

    assert decision.status is WorkspaceCreationAuthorizationStatus.NOT_AUTHORIZED


def test_routing_metadata_keeps_recommendation_separate_from_agent_prompt():
    general_intent = GeneralChatIntentDecision.from_payload(
        _payload(),
        candidates=[_candidate()],
    )
    decision = ChatIntentRoutingDecision(
        auto_route=AutoRouteDecision(AutoRouteMode.GENERAL_CHAT, 0.96),
        general_intent=general_intent,
        workspace_creation_authorization=_not_authorized(),
        execution_plan=TurnExecutionPlan(
            mode=TurnExecutionMode.TOOL_CHAT,
            max_rounds=8,
        ),
    )

    metadata = decision.runtime_metadata(request="Run the launch every week")

    assert metadata["workspace_recommendation"]["workspace_id"] == (
        "01WORKSPACEEXACT0000000000"
    )
    assert metadata["workspace_recommendation"]["request"] == (
        "Run the launch every week"
    )
    response_guardrail = metadata["general_intent_prompt"]
    assert "A separate UI card may offer" not in response_guardrail
    assert "never mention or allude" in response_guardrail
    assert "Do not predict or announce that any interface element will appear" in (
        response_guardrail
    )
    assert "Do not tell the user to click, choose, accept, or wait" in response_guardrail
    assert "Do not start or modify a Workspace" in response_guardrail


def test_agent_chat_prompt_forbids_implicit_new_workspace_draft():
    decision = ChatIntentRoutingDecision(
        auto_route=AutoRouteDecision(AutoRouteMode.GENERAL_CHAT, 0.96),
        general_intent=GeneralChatIntentDecision.agent_chat(0.99),
        workspace_creation_authorization=_not_authorized(),
        execution_plan=TurnExecutionPlan(
            mode=TurnExecutionMode.TOOL_CHAT,
            max_rounds=8,
        ),
    )

    prompt = decision.runtime_metadata(request="管理我的房产")["general_intent_prompt"]

    assert "Do not start a new Workspace or a new Workspace draft" in prompt
    assert "existing active Workspace draft may still be updated" in prompt


def test_explicit_creation_authorization_allows_draft_but_not_final_workspace():
    decision = ChatIntentRoutingDecision(
        auto_route=AutoRouteDecision(AutoRouteMode.GENERAL_CHAT, 0.96),
        general_intent=GeneralChatIntentDecision.agent_chat(1.0),
        workspace_creation_authorization=(
            WorkspaceCreationAuthorizationDecision.from_payload({
                "status": "authorized",
                "confidence": 0.99,
            })
        ),
        execution_plan=TurnExecutionPlan(
            mode=TurnExecutionMode.TOOL_CHAT,
            max_rounds=8,
        ),
    )

    prompt = decision.runtime_metadata(request="Create a Workspace")["general_intent_prompt"]

    assert "explicitly authorized starting a new Workspace draft" in prompt
    assert "Do not create the final Workspace" in prompt


def test_auto_chat_intent_routing_is_only_for_plain_owner_auto_chat():
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
async def test_direct_chat_route_still_checks_workspace_recommendation(monkeypatch):
    from packages.core.services import chat_intent_routing as module

    calls: list[list[WorkspaceIntentCandidate]] = []

    async def fake_recent_context(*_args, **_kwargs):
        return "", False

    async def fake_auto_route(**_kwargs):
        return AutoRouteDecision(AutoRouteMode.DIRECT_CHAT, 0.98)

    async def fake_candidates(*_args, **_kwargs):
        return [_candidate()]

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

    monkeypatch.setattr(module, "_recent_chat_context", fake_recent_context)
    monkeypatch.setattr(module, "classify_auto_route", fake_auto_route)
    monkeypatch.setattr(module, "list_workspace_intent_candidates", fake_candidates)
    monkeypatch.setattr(module, "classify_general_chat_intent", fake_general_intent)
    monkeypatch.setattr(
        module,
        "classify_workspace_creation_authorization",
        fake_creation_authorization,
    )

    decision = await classify_chat_intent_routing(
        object(),
        user=SimpleNamespace(entity_id="entity-1", id="user-1"),
        message="How should I organize this eight-week launch?",
        conversation_id=None,
    )

    assert decision.auto_route.mode is AutoRouteMode.DIRECT_CHAT
    assert decision.general_intent.kind is GeneralChatIntentKind.WORKSPACE_RECOMMENDATION
    assert decision.execution_plan.mode is TurnExecutionMode.DIRECT_CHAT
    assert calls == [[_candidate()]]


@pytest.mark.asyncio
async def test_voice_greeting_skips_remote_routing_classifiers(monkeypatch):
    from packages.core.services import chat_intent_routing as module

    async def unexpected(*_args, **_kwargs):
        raise AssertionError("voice greeting should use the local direct-chat plan")

    monkeypatch.setattr(module, "_recent_chat_context", unexpected)
    monkeypatch.setattr(module, "classify_auto_route", unexpected)
    monkeypatch.setattr(module, "list_workspace_intent_candidates", unexpected)
    monkeypatch.setattr(module, "classify_general_chat_intent", unexpected)
    monkeypatch.setattr(
        module,
        "classify_workspace_creation_authorization",
        unexpected,
    )

    decision = await classify_chat_intent_routing(
        object(),
        user=SimpleNamespace(entity_id="entity-1", id="user-1"),
        message="Hi.",
        conversation_id="conversation-1",
        runtime_metadata={"voice_session_mode": "chat_gateway"},
    )

    assert decision.auto_route.mode is AutoRouteMode.DIRECT_CHAT
    assert decision.general_intent.kind is GeneralChatIntentKind.AGENT_CHAT
    assert (
        decision.workspace_creation_authorization.status
        is WorkspaceCreationAuthorizationStatus.NOT_AUTHORIZED
    )
    assert decision.execution_plan.mode is TurnExecutionMode.DIRECT_CHAT
    assert decision.execution_plan.disable_tools is True
    assert decision.execution_plan.use_fast_model is True


@pytest.mark.asyncio
async def test_unrelated_request_with_active_draft_still_checks_workspace_recommendation(monkeypatch):
    from packages.core.services import chat_intent_routing as module

    seen_context: list[str] = []

    async def fake_recent_context(*_args, **_kwargs):
        return (
            "[An active Workspace Draft exists in this conversation.]\n"
            "assistant: Property marketing draft",
            True,
        )

    async def fake_auto_route(**_kwargs):
        return AutoRouteDecision(AutoRouteMode.DIRECT_CHAT, 0.98)

    async def fake_candidates(*_args, **_kwargs):
        return [_candidate()]

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

    monkeypatch.setattr(module, "_recent_chat_context", fake_recent_context)
    monkeypatch.setattr(module, "classify_auto_route", fake_auto_route)
    monkeypatch.setattr(module, "list_workspace_intent_candidates", fake_candidates)
    monkeypatch.setattr(module, "classify_general_chat_intent", fake_general_intent)
    monkeypatch.setattr(
        module,
        "classify_workspace_creation_authorization",
        fake_creation_authorization,
    )

    decision = await classify_chat_intent_routing(
        object(),
        user=SimpleNamespace(entity_id="entity-1", id="user-1"),
        message="我想长期管理招聘流程",
        conversation_id="conversation-with-property-draft",
    )
    metadata = decision.runtime_metadata(request="我想长期管理招聘流程")

    assert decision.general_intent.kind is GeneralChatIntentKind.WORKSPACE_RECOMMENDATION
    assert metadata["workspace_recommendation"]["action"] == "create_new"
    assert seen_context and "Property marketing draft" in seen_context[0]


@pytest.mark.asyncio
async def test_user_opt_out_skips_suitability_but_keeps_creation_authorization(monkeypatch):
    from packages.core.services import chat_intent_routing as module

    async def fake_recent_context(*_args, **_kwargs):
        return "", False

    async def fake_auto_route(**_kwargs):
        return AutoRouteDecision(AutoRouteMode.DIRECT_CHAT, 0.98)

    authorization_calls: list[dict] = []

    async def unexpected_candidate_call(*_args, **_kwargs):
        raise AssertionError("workspace candidates must not be loaded after opt-out")

    async def unexpected_general_intent(**_kwargs):
        raise AssertionError("suitability must not run after recommendation opt-out")

    async def fake_creation_authorization(**kwargs):
        authorization_calls.append(kwargs)
        return _not_authorized()

    monkeypatch.setattr(module, "_recent_chat_context", fake_recent_context)
    monkeypatch.setattr(module, "classify_auto_route", fake_auto_route)
    monkeypatch.setattr(
        module,
        "list_workspace_intent_candidates",
        unexpected_candidate_call,
    )
    monkeypatch.setattr(
        module,
        "classify_general_chat_intent",
        unexpected_general_intent,
    )
    monkeypatch.setattr(
        module,
        "classify_workspace_creation_authorization",
        fake_creation_authorization,
    )

    decision = await classify_chat_intent_routing(
        object(),
        user=SimpleNamespace(
            entity_id="entity-1",
            id="user-1",
            preferences={"workspace_recommendations_enabled": False},
        ),
        message="How should I organize this eight-week launch?",
        conversation_id=None,
    )

    assert decision.auto_route.mode is AutoRouteMode.DIRECT_CHAT
    assert decision.general_intent.kind is GeneralChatIntentKind.AGENT_CHAT
    assert decision.general_intent.recommendation is None
    assert len(authorization_calls) == 1
    assert authorization_calls[0]["message_text"] == (
        "How should I organize this eight-week launch?"
    )


@pytest.mark.asyncio
async def test_user_opt_out_does_not_block_explicit_workspace_creation(monkeypatch):
    from packages.core.services import chat_intent_routing as module

    async def fake_recent_context(*_args, **_kwargs):
        return "", False

    async def fake_auto_route(**_kwargs):
        return AutoRouteDecision(AutoRouteMode.DIRECT_CHAT, 0.98)

    async def unexpected_candidate_call(*_args, **_kwargs):
        raise AssertionError("workspace candidates must not load after opt-out")

    async def unexpected_general_intent(**_kwargs):
        raise AssertionError("suitability must not run after opt-out")

    async def fake_creation_authorization(**_kwargs):
        return WorkspaceCreationAuthorizationDecision.from_payload({
            "status": "authorized",
            "confidence": 0.99,
        })

    monkeypatch.setattr(module, "_recent_chat_context", fake_recent_context)
    monkeypatch.setattr(module, "classify_auto_route", fake_auto_route)
    monkeypatch.setattr(
        module,
        "list_workspace_intent_candidates",
        unexpected_candidate_call,
    )
    monkeypatch.setattr(
        module,
        "classify_general_chat_intent",
        unexpected_general_intent,
    )
    monkeypatch.setattr(
        module,
        "classify_workspace_creation_authorization",
        fake_creation_authorization,
    )

    decision = await classify_chat_intent_routing(
        object(),
        user=SimpleNamespace(
            entity_id="entity-1",
            id="user-1",
            preferences={"workspace_recommendations_enabled": False},
        ),
        message="Create a Workspace for this launch",
        conversation_id=None,
    )
    metadata = decision.runtime_metadata(request="Create a Workspace for this launch")

    assert decision.auto_route.mode is AutoRouteMode.GENERAL_CHAT
    assert metadata["workspace_creation_authorization"]["status"] == "authorized"
    assert "workspace_recommendation" not in metadata
    assert "Start and save a guided Workspace draft" in metadata["general_intent_prompt"]


@pytest.mark.asyncio
async def test_explicit_create_suppresses_recommendation_and_authorizes_draft(monkeypatch):
    from packages.core.services import chat_intent_routing as module

    async def fake_recent_context(*_args, **_kwargs):
        return "", False

    async def fake_auto_route(**_kwargs):
        return AutoRouteDecision(AutoRouteMode.GENERAL_CHAT, 0.98)

    async def fake_candidates(*_args, **_kwargs):
        return [_candidate()]

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

    monkeypatch.setattr(module, "_recent_chat_context", fake_recent_context)
    monkeypatch.setattr(module, "classify_auto_route", fake_auto_route)
    monkeypatch.setattr(module, "list_workspace_intent_candidates", fake_candidates)
    monkeypatch.setattr(module, "classify_general_chat_intent", fake_general_intent)
    monkeypatch.setattr(
        module,
        "classify_workspace_creation_authorization",
        fake_creation_authorization,
    )

    decision = await classify_chat_intent_routing(
        object(),
        user=SimpleNamespace(entity_id="entity-1", id="user-1"),
        message="Create a Workspace for this launch",
        conversation_id=None,
    )
    metadata = decision.runtime_metadata(request="Create a Workspace for this launch")

    assert decision.auto_route.mode is AutoRouteMode.GENERAL_CHAT
    assert decision.execution_plan.mode is TurnExecutionMode.TOOL_CHAT
    assert decision.general_intent.kind is GeneralChatIntentKind.AGENT_CHAT
    assert "workspace_recommendation" not in metadata
    assert metadata["workspace_creation_authorization"]["status"] == "authorized"
    assert "Start and save a guided Workspace draft" in metadata["general_intent_prompt"]


def test_turn_execution_plan_factory_fails_closed_on_invalid_enum():
    plan = TurnExecutionPlan.from_metadata({"mode": "unknown", "disable_tools": True})

    assert plan.mode is TurnExecutionMode.LEGACY
    assert plan.disable_tools is False
    assert plan.reason == "invalid_turn_execution_plan_metadata"


def test_workspace_ledger_query_uses_the_read_only_ledger_catalog():
    plan = build_turn_execution_plan(
        enabled=True,
        surface=ChatSurface.WORKSPACE_CHAT,
        message=(
            "请查询 Finance Ledger，统计 sum_inflow_minor 和 "
            "sum_outflow_minor，并展示财务汇总。"
        ),
        message_text=(
            "请查询 Finance Ledger，统计 sum_inflow_minor 和 "
            "sum_outflow_minor，并展示财务汇总。"
        ),
        workspace_id="ws-finance",
    )

    assert plan.mode is TurnExecutionMode.WORKSPACE
    assert plan.tool_catalog_mode is TurnToolCatalogMode.LEDGER_QUERY
    assert runtime_turn_visible_tool_names(plan) == (
        "read_content_ledger",
        "read_finance_ledger",
        "read_recruiting_ledger",
        "read_relationship_ledger",
        "query_ledger",
        "manor",
    )


@pytest.mark.parametrize(
    "message",
    [
        "花了多少钱了",
        "这个月收入和支出是多少？",
        "How much have we spent this month?",
    ],
)
def test_workspace_natural_finance_query_uses_the_read_only_ledger_catalog(
    message: str,
):
    plan = build_turn_execution_plan(
        enabled=True,
        surface=ChatSurface.WORKSPACE_CHAT,
        message=message,
        message_text=message,
        workspace_id="ws-finance",
    )

    assert plan.mode is TurnExecutionMode.WORKSPACE
    assert plan.tool_catalog_mode is TurnToolCatalogMode.LEDGER_QUERY
    assert runtime_turn_visible_tool_names(plan) is not None


@pytest.mark.parametrize(
    "message",
    [
        "How much did this task spend in AI credits?",
        "这份方案花费了多少 tokens？",
    ],
)
def test_workspace_ai_usage_cost_question_keeps_the_full_catalog(message: str):
    plan = build_turn_execution_plan(
        enabled=True,
        surface=ChatSurface.WORKSPACE_CHAT,
        message=message,
        message_text=message,
        workspace_id="ws-finance",
    )

    assert plan.tool_catalog_mode is TurnToolCatalogMode.FULL
    assert runtime_turn_visible_tool_names(plan) is None


def test_workspace_natural_finance_query_falls_back_without_ledger_tools():
    message = "How much have we spent this month?"
    plan = build_turn_execution_plan(
        enabled=True,
        surface=ChatSurface.WORKSPACE_CHAT,
        message=message,
        message_text=message,
        workspace_id="ws-without-ledgers",
    )

    assert plan.tool_catalog_mode is TurnToolCatalogMode.LEDGER_QUERY
    assert runtime_turn_visible_tool_names(
        plan,
        available_tool_names=set(),
    ) is None


def test_workspace_ledger_write_intent_keeps_the_full_catalog():
    message = "请在 Finance Ledger 里新增一笔客户回款记录。"
    plan = build_turn_execution_plan(
        enabled=True,
        surface=ChatSurface.WORKSPACE_CHAT,
        message=message,
        message_text=message,
        workspace_id="ws-finance",
    )

    assert plan.mode is TurnExecutionMode.WORKSPACE
    assert plan.tool_catalog_mode is TurnToolCatalogMode.FULL
    assert runtime_turn_visible_tool_names(plan) is None


@pytest.mark.parametrize(
    "message",
    [
        "在 Finance Ledger 记一笔 100 美元的支出",
        "log expense to the Finance Ledger",
    ],
)
def test_workspace_common_ledger_write_intent_keeps_the_full_catalog(
    message: str,
):
    plan = build_turn_execution_plan(
        enabled=True,
        surface=ChatSurface.WORKSPACE_CHAT,
        message=message,
        message_text=message,
        workspace_id="ws-finance",
    )

    assert plan.mode is TurnExecutionMode.WORKSPACE
    assert plan.tool_catalog_mode is TurnToolCatalogMode.FULL
    assert runtime_turn_visible_tool_names(plan) is None


def test_workspace_non_ledger_research_keeps_the_full_catalog():
    message = "Search the latest market news for this Workspace."
    plan = build_turn_execution_plan(
        enabled=True,
        surface=ChatSurface.WORKSPACE_CHAT,
        message=message,
        message_text=message,
        workspace_id="ws-finance",
    )

    assert plan.mode is TurnExecutionMode.WORKSPACE
    assert plan.tool_catalog_mode is TurnToolCatalogMode.FULL
    assert runtime_turn_visible_tool_names(plan) is None


def test_workspace_non_ledger_count_keeps_the_full_catalog():
    message = "Count the open tasks in this Workspace."
    plan = build_turn_execution_plan(
        enabled=True,
        surface=ChatSurface.WORKSPACE_CHAT,
        message=message,
        message_text=message,
        workspace_id="ws-finance",
    )

    assert plan.mode is TurnExecutionMode.WORKSPACE
    assert plan.tool_catalog_mode is TurnToolCatalogMode.FULL
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
