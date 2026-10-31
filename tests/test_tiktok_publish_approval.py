"""An editable TikTok review is consumed once through the runtime approval lease."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from packages.core.ai.runtime import approval_service as approvals
from packages.core.ai.runtime.approval_classifier import classify_runtime_tool_action
from packages.core.ai.runtime.authorization_receipts import (
    TikTokPublishConsent,
    RuntimeToolAuthorizationLease,
    RuntimeToolAuthorizationReceipt,
    runtime_authorization_arguments_hash,
    runtime_begin_tool_authorization_scope,
    runtime_capture_tiktok_publish_consent,
    runtime_claim_tiktok_publish_consent,
    runtime_current_tool_authorization_receipt,
    runtime_end_tool_authorization_scope,
    runtime_record_tiktok_publish_consent,
)
from packages.core.services.runtime_authorization import AuthorizationRule, PermissionDecision
from packages.core.services.runtime_authorization.domain import RuntimeAuthorizationAccess

TOOL = "mcp__tiktok__post_video"
ARGS = {
    "video_url": "uploads/chat/0123456789abcdef.mp4",
    "title": "Original title",
    "integration_account_id": "account-1",
}


def _receipt(consent):
    return RuntimeToolAuthorizationReceipt(
        tool_name=TOOL,
        entity_id="entity-1",
        user_id="user-1",
        workspace_id=None,
        conversation_id="conversation-1",
        task_id=None,
        action_key="social_post.publish",
        capability_id="external.social",
        access=RuntimeAuthorizationAccess.ACTION,
        arguments_hash=runtime_authorization_arguments_hash(ARGS, public_argument_keys=frozenset(ARGS)),
        resource_kind="integration",
        file_resources=(),
        public_argument_keys=frozenset(ARGS),
        tiktok_publish_consent=consent,
    )


def test_consent_is_bound_to_active_exact_once_lease_and_does_not_leak():
    consent = TikTokPublishConsent("review-1", '{"source_sha256":"hash"}', json.dumps(ARGS))
    runtime_record_tiktok_publish_consent(consent)  # No active capture cannot leak to a later call.
    with runtime_capture_tiktok_publish_consent() as captured:
        assert captured == []
        runtime_record_tiktok_publish_consent(consent)
        with runtime_capture_tiktok_publish_consent() as child:
            assert child == []
        assert captured == [consent]
    with runtime_capture_tiktok_publish_consent() as later:
        assert later == []
    lease = RuntimeToolAuthorizationLease(_receipt(consent))
    assert runtime_claim_tiktok_publish_consent(consent) is False
    scope = runtime_begin_tool_authorization_scope(lease, arguments=dict(ARGS))
    try:
        assert scope.valid
        assert runtime_current_tool_authorization_receipt().tiktok_publish_consent is consent
        assert runtime_claim_tiktok_publish_consent(replace(consent)) is False
        assert runtime_claim_tiktok_publish_consent(consent) is True
        assert runtime_claim_tiktok_publish_consent(consent) is False
        changed = consent.fields
        changed["title"] = "Cannot mutate the frozen consent"
        assert consent.fields == ARGS
    finally:
        runtime_end_tool_authorization_scope(scope)
    assert runtime_current_tool_authorization_receipt() is None
    assert runtime_claim_tiktok_publish_consent(consent) is False
    repeated = runtime_begin_tool_authorization_scope(lease, arguments=dict(ARGS))
    try:
        assert repeated.valid is False
    finally:
        runtime_end_tool_authorization_scope(repeated)


@pytest.mark.parametrize(
    "changed",
    [{"title": "Different"}, {"integration_account_id": "other"}, {"video_url": "uploads/chat/1111111111111111.mp4"}],
)
def test_changed_post_cannot_activate_the_consumed_approval_receipt(changed):
    consent = TikTokPublishConsent("review-1", "{}", json.dumps(ARGS))
    scope = runtime_begin_tool_authorization_scope(
        RuntimeToolAuthorizationLease(_receipt(consent)),
        arguments={**ARGS, **changed},
    )
    try:
        assert scope.valid is False
        assert runtime_current_tool_authorization_receipt() is None
        assert runtime_claim_tiktok_publish_consent(consent) is False
    finally:
        runtime_end_tool_authorization_scope(scope)


def test_review_survives_live_card_without_private_evidence():
    from packages.core.services.hitl_requests import hitl_requests_from_data

    review = {"kind": "tiktok_publish", "review_id": "fresh-review", "title": "Reviewed"}
    payload = json.loads(
        approvals._runtime_hitl_payload(
            request_id="approval-1",
            action=classify_runtime_tool_action(TOOL, ARGS),
            tool_name=TOOL,
            arguments=ARGS,
            matched_rule=None,
            render={"review": review, "tiktok_publish_evidence": {"private": "secret-hash"}},
        )
    )
    assert payload["hitl"]["options"] == ["approve", "reject"]
    assert hitl_requests_from_data(payload)[0]["review"] == review
    assert "secret-hash" not in json.dumps(payload)


@pytest.mark.asyncio
@pytest.mark.parametrize("workspace_id", [None, "workspace-1"])
async def test_standing_consent_still_enters_explicit_review_gate(monkeypatch, workspace_id):
    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def commit(self):
            pass

    monkeypatch.setattr("packages.core.database.async_session", Session)
    monkeypatch.setattr(
        "packages.core.services.runtime_authorization.authorize_runtime_action",
        AsyncMock(return_value=PermissionDecision.allow(AuthorizationRule.SYSTEM_WORKER)),
    )
    monkeypatch.setattr("packages.core.budget.get_workspace_spent_credits_per_kind", AsyncMock(return_value={}))
    request = SimpleNamespace(id="approval-1", origin_task_id=None, context={"review": {"kind": "tiktok_publish"}})
    resolve = AsyncMock(
        return_value=SimpleNamespace(
            outcome="needs_human",
            request=request,
            matched_rule="standing_would_allow",
            reason=None,
        )
    )
    monkeypatch.setattr("packages.core.governance.approvals.resolve_approval", resolve)
    result = await approvals.guard_runtime_tool_action(
        name=TOOL,
        arguments=dict(ARGS),
        entity_id="entity-1",
        user_id="user-1",
        conversation_id="conversation-1",
        workspace_id=workspace_id,
        operation_id="runtime:tiktok-test:call:publish",
    )
    assert json.loads(result)["__hitl__"] is True
    assert resolve.await_args.kwargs["require_explicit_approval"] is True


@pytest.mark.asyncio
async def test_chat_transports_review_without_coercing_submitted_values(monkeypatch):
    from packages.core.services.chat_approvals import resolve_chat_approval_turn

    submitted = {"review_id": "r1", "title": "User edited", "disable_duet": False, "video_cover_timestamp_ms": 0}
    resolve = AsyncMock(return_value=approvals.RuntimeApprovalResolution("Still pending"))
    monkeypatch.setattr(approvals, "resolve_runtime_approval_turn", resolve)
    monkeypatch.setattr(
        "packages.core.services.ai_file_permissions.resolve_file_approval_message", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        "packages.core.services.task_comment_approval_origin.lock_task_comment_turn_boundary", AsyncMock()
    )
    await resolve_chat_approval_turn(
        None,
        conversation_id="conversation-1",
        entity_id="entity-1",
        user_id="user-1",
        message=json.dumps({"hitl_id": "approval-1", "action": "approve", "payload": {"review": submitted}}),
    )
    assert resolve.await_args.kwargs["submitted_review"] == submitted


@pytest.mark.asyncio
@pytest.mark.parametrize("issued_count", [0, 1, 2])
async def test_prepared_tiktok_execution_requires_one_server_issued_consent(monkeypatch, issued_count):
    from packages.core.ai.runtime.approval_classifier import classify_runtime_tool
    from packages.core.ai.runtime.approvals import RuntimeApprovalDecision, RuntimeApprovalMiddleware
    from packages.core.ai.runtime.tool_availability import RuntimeMCPCallPreflightResult
    from packages.core.ai.runtime.tool_execution import runtime_prepare_tool_execution

    consent = TikTokPublishConsent("review-1", "{}", json.dumps(ARGS))

    async def allowed_gate(_self, request):
        for _ in range(issued_count):
            runtime_record_tiktok_publish_consent(consent)
        return RuntimeApprovalDecision.allow(classify_runtime_tool(TOOL, request.arguments), request)

    monkeypatch.setattr(RuntimeApprovalMiddleware, "guard_request", allowed_gate)
    monkeypatch.setattr(
        "packages.core.ai.runtime.tool_execution.runtime_preflight_mcp_call",
        AsyncMock(return_value=RuntimeMCPCallPreflightResult()),
    )
    prepared = await runtime_prepare_tool_execution(
        tool_name=TOOL,
        arguments={**ARGS, "_tiktok_publish_consent_from_context": {"approved": True}},
        entity_id="entity-1",
        user_id="user-1",
        conversation_id="conversation-1",
    )
    if issued_count == 1:
        assert prepared.blocked_result is None
        assert prepared.authorization_receipt.tiktok_publish_consent is consent
    else:
        assert json.loads(prepared.blocked_result)["error"] == "tiktok_publish_review_required"
        assert prepared.authorization_receipt is None


@pytest.mark.asyncio
@pytest.mark.parametrize(("status", "matched_rule", "error"), [
    ("expired", "approval.operation_terminal", "blocked_by_governance"),
    ("consumed", "approval.operation_terminal", "blocked_by_governance"),
    ("denied", "approval.rule_denied", "blocked_by_governance"),
    (None, "permission.denied", "blocked_by_permission"),
])
async def test_denied_revalidation_never_projects_an_actionable_card(
    monkeypatch, status, matched_rule, error,
):
    from packages.core.constants.approvals import ApprovalOutcome

    request = SimpleNamespace(id="old-approval", status=status) if status else None
    resolve = AsyncMock(return_value=SimpleNamespace(
        outcome=ApprovalOutcome.DENY, request=request,
        matched_rule=matched_rule, reason="The operation cannot execute.",
    ))
    monkeypatch.setattr("packages.core.governance.approvals.resolve_approval", resolve)
    rendered = json.loads(await approvals._runtime_block_payload(
        None, conversation_id="conversation-1", entity_id="entity-1", user_id="user-1",
        workspace_id=None, tool_name=TOOL, arguments=dict(ARGS),
        action=classify_runtime_tool_action(TOOL, ARGS),
        permission_decision=PermissionDecision.allow(AuthorizationRule.SYSTEM_WORKER),
        operation_id="runtime:old:call:publish", reason="Review this post again.",
        matched_rule_hint="tiktok_publish_review",
    ))
    assert rendered["error"] == error
    assert rendered["matched_rule"] == matched_rule
    assert rendered["action_executed"] is False
    assert "__hitl__" not in rendered and "hitl" not in rendered
    assert rendered["message"] == "The operation cannot execute."
    if request is not None:
        assert rendered["approval_token"] == request.id
        assert rendered["status"] == status
        assert request.status == status
    else:
        assert "approval_token" not in rendered
    resolve.assert_awaited_once()


@pytest.fixture
async def persisted_review(db_session, monkeypatch):
    return await _persisted_review(db_session, monkeypatch)


async def _persisted_review(db_session, monkeypatch, *, workflow_origin=None):
    from packages.core.ai.mcp import tiktok
    from packages.core.models.base import generate_ulid
    from packages.core.models.hitl_request import HitlRequest
    from packages.core.models.task import Conversation
    from packages.core.models.user import User, UserMembership
    from packages.core.services import tiktok_publish_review as review_service

    entity_id, user_id, conversation_id = (generate_ulid() for _ in range(3))
    db_session.add(
        User(
            id=user_id,
            entity_id=entity_id,
            email=f"{user_id}@example.com",
            password_hash="unused",
            role="owner",
            status="active",
        )
    )
    db_session.add(
        Conversation(id=conversation_id, entity_id=entity_id, user_id=user_id, status="active", scope="channel")
    )
    db_session.add(UserMembership(user_id=user_id, entity_id=entity_id,
        role="owner", status="active", is_primary=True))
    await db_session.flush()
    from tests.approval_settings_test_utils import initialize_test_account_approval
    await initialize_test_account_approval(db_session, entity_id=entity_id, user_id=user_id)

    class Session:
        async def __aenter__(self):
            return db_session

        async def __aexit__(self, *_args):
            return False

    monkeypatch.setattr("packages.core.database.async_session", Session)
    monkeypatch.setattr(
        review_service,
        "inspect_chat_video",
        AsyncMock(
            return_value={
                "rel_path": ARGS["video_url"],
                "source_sha256": "a" * 64,
                "size_bytes": 1024,
                "duration_seconds": 45.25,
                "width": 1080,
                "height": 1920,
            }
        ),
    )
    creator = {
        "creator_username": "manor",
        "creator_nickname": "Manor AI",
        "creator_avatar_url": "",
        "privacy_level_options": ["PUBLIC_TO_EVERYONE", "SELF_ONLY"],
        "comment_disabled": False,
        "duet_disabled": False,
        "stitch_disabled": False,
        "max_video_post_duration_sec": 300,
    }
    monkeypatch.setattr(review_service, "_creator_for_actor", AsyncMock(return_value=("account-1", creator)))
    schema = next(item for item in tiktok.list_tools() if item["name"] == "post_video")
    monkeypatch.setattr(
        "packages.core.ai.runtime.tool_registry.runtime_tool_schema_for_actor", AsyncMock(return_value=schema)
    )
    scope = dict(entity_id=entity_id, user_id=user_id, conversation_id=conversation_id)
    runtime_scope = {"workspace_id": None}
    arguments = dict(ARGS)
    if workflow_origin is not None:
        from packages.core.ai.runtime.surfaces import ChatSurface
        from packages.core.governance import update_policy
        from packages.core.governance.approval_policy import ApprovalPolicyOverride
        from packages.core.governance.approval_policy_service import initialize_workspace_approval
        from packages.core.governance.policy import WorkspacePolicy
        from packages.core.models.workflow import WorkflowRun
        from packages.core.models.workspace import Workspace
        from tests.test_runtime_guard_unified_store import _runtime_envelope

        workspace = Workspace(id=generate_ulid(), entity_id=entity_id, name="Review workflow", status="active", operating_model={})
        db_session.add(workspace)
        await db_session.flush()
        await initialize_workspace_approval(db_session, workspace)
        await update_policy(db_session, entity_id=entity_id, workspace_id=workspace.id,
            changed_by=user_id, policy=WorkspacePolicy(approval=ApprovalPolicyOverride(mode="confirm_changes")))
        conversation = await db_session.get(Conversation, conversation_id)
        conversation.workspace_id = workspace.id
        run = WorkflowRun(id=generate_ulid(), workflow_id=generate_ulid(), entity_id=entity_id,
            workspace_id=workspace.id, started_by=user_id, status="running", variables={},
            step_results={}, trigger_data={}, execution_trace=[])
        db_session.add(run)
        await db_session.flush()
        runtime_scope = {"workspace_id": workspace.id, "runtime_envelope": replace(
            _runtime_envelope({**scope, "workspace_id": workspace.id}, name=TOOL),
            surface=ChatSurface.WORKFLOW_AGENT_STEP)}
        arguments["_workflow_run_id_from_context"] = run.id
        workflow_origin.update(run=run, **runtime_scope)
    payload = json.loads(
        await approvals.guard_runtime_tool_action(
            name=TOOL,
            arguments=arguments,
            operation_id=f"test:tiktok:{conversation_id}:publish",
            **runtime_scope,
            **scope,
        )
    )
    assert payload.get("__hitl__") is True, payload
    request = await db_session.get(HitlRequest, payload["approval_token"])
    request.context = {**request.context, "tiktok_review_refreshed": True}
    await db_session.flush()
    submitted = {
        "kind": "tiktok_publish",
        "version": 1,
        "review_id": request.context["review"]["review_id"],
        "caption": "Operator edited caption",
        "privacy_level": "SELF_ONLY",
        "allow_comment": False,
        "allow_duet": True,
        "allow_stitch": False,
        "commercial_content": False,
        "brand_organic_toggle": False,
        "brand_content_toggle": False,
        "music_usage_confirmed": True,
        "branded_content_policy_confirmed": False,
        "consent_confirmed": True,
    }
    return request, scope, submitted


@pytest.mark.asyncio
@pytest.mark.parametrize("with_token", [True, False])
async def test_workflow_review_is_consumed_once_without_a_second_policy_approval(db_session, monkeypatch, with_token):
    origin = {}
    request, scope, submitted = await _persisted_review(db_session, monkeypatch, workflow_origin=origin)
    approved = await approvals.resolve_runtime_approval_turn(db_session, hitl_id=request.id,
        action="approve", submitted_review=submitted, **scope)
    assert request.status == "granted"
    arguments = {**approved.runtime_metadata["forced_tool_calls"][0]["arguments"],
        "_workflow_run_id_from_context": origin["run"].id}
    if not with_token:
        arguments.pop("approval_token", None)
    kwargs = {**scope, "workspace_id": origin["workspace_id"], "name": TOOL,
        "runtime_envelope": origin["runtime_envelope"], "operation_id": request.context["operation_id"]}
    with runtime_capture_tiktok_publish_consent() as captured:
        result = await approvals.guard_runtime_tool_action(**kwargs, arguments=dict(arguments))
    assert result is None, result
    assert request.status == "consumed"
    assert len(captured) == 1 and captured[0].approval_id == request.id
    repeated = await approvals.guard_runtime_tool_action(**kwargs, arguments=dict(arguments))
    assert repeated is not None
    assert request.status == "consumed"


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["paused", "cancelled", "deny", "reject", "pending", "changed_payload", "missing_consent"])
async def test_workflow_review_continuation_keeps_hard_and_exact_consent_boundaries(db_session, monkeypatch, change):
    origin = {}
    request, scope, submitted = await _persisted_review(db_session, monkeypatch, workflow_origin=origin)
    if change == "pending":
        arguments = dict(ARGS)
    else:
        resolution = await approvals.resolve_runtime_approval_turn(db_session, hitl_id=request.id,
            action="reject" if change == "reject" else "approve", submitted_review=submitted, **scope)
        arguments = dict(ARGS) if change == "reject" else dict(resolution.runtime_metadata["forced_tool_calls"][0]["arguments"])
    arguments.update(approval_token=request.id, _workflow_run_id_from_context=origin["run"].id)
    if change in {"paused", "cancelled"}:
        origin["run"].status = change
        await db_session.commit()
    elif change == "deny":
        from tests.approval_settings_test_utils import action_rule, set_workspace_test_approval
        await set_workspace_test_approval(db_session, workspace_id=origin["workspace_id"],
            entity_id=scope["entity_id"], actor_id=scope["user_id"], mode="confirm_changes",
            user_rules=[action_rule("social_post.publish", effect="deny")])
    elif change == "changed_payload":
        arguments["title"] = "Unreviewed caption"
    elif change == "missing_consent":
        request.context = {key: value for key, value in request.context.items() if key != "tiktok_publish_consent"}
        await db_session.flush()
    with runtime_capture_tiktok_publish_consent() as captured:
        result = await approvals.guard_runtime_tool_action(**scope, name=TOOL, arguments=arguments,
            workspace_id=origin["workspace_id"], runtime_envelope=origin["runtime_envelope"],
            operation_id=request.context["operation_id"])
    assert result is not None
    assert request.status != "consumed"
    assert captured == []


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["paused", "cancelled", "chat_surface"])
async def test_workflow_origin_is_rechecked_at_token_consumption(db_session, monkeypatch, change):
    from packages.core.ai.runtime.surfaces import ChatSurface

    origin = {}
    request, scope, submitted = await _persisted_review(db_session, monkeypatch, workflow_origin=origin)
    approved = await approvals.resolve_runtime_approval_turn(db_session, hitl_id=request.id,
        action="approve", submitted_review=submitted, **scope)
    original_consume = approvals.consume_runtime_approval

    async def changed_after_preflight(db, **kwargs):
        assert kwargs["workflow_run_id"] == origin["run"].id
        if change == "chat_surface":
            kwargs["runtime_envelope"] = replace(kwargs["runtime_envelope"], surface=ChatSurface.WORKSPACE_CHAT)
        else:
            origin["run"].status = change
            await db.commit()
        return await original_consume(db, **kwargs)

    monkeypatch.setattr(approvals, "consume_runtime_approval", changed_after_preflight)
    with runtime_capture_tiktok_publish_consent() as captured:
        result = await approvals.guard_runtime_tool_action(**scope, name=TOOL,
            arguments={**approved.runtime_metadata["forced_tool_calls"][0]["arguments"],
                "_workflow_run_id_from_context": origin["run"].id},
            workspace_id=origin["workspace_id"], runtime_envelope=origin["runtime_envelope"],
            operation_id=request.context["operation_id"])
    assert json.loads(result)["error"] == "workflow_origin_not_executable"
    assert request.status == "granted"
    assert captured == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid",
    [
        "bare_approve",
        "not_refreshed",
        "always",
        "unknown_field",
        "changed_media",
        "changed_account",
        "stale_review",
        "string_boolean",
    ],
)
async def test_invalid_or_bare_confirmation_keeps_original_request_pending(
    db_session,
    persisted_review,
    invalid,
):
    request, scope, submitted = persisted_review
    action = "approve"
    if invalid == "bare_approve":
        submitted = None
    elif invalid == "not_refreshed":
        request.context = {**request.context, "tiktok_review_refreshed": False}
    elif invalid == "always":
        action = "always_approve"
    else:
        submitted = {
            **submitted,
            **{
                "unknown_field": {"auto_approve": True},
                "changed_media": {"video_url": "uploads/chat/1111111111111111.mp4"},
                "changed_account": {"integration_account_id": "account-2"},
                "stale_review": {"review_id": "old-review"},
                "string_boolean": {"allow_duet": "true"},
            }[invalid],
        }
    await db_session.flush()
    resolution = await approvals.resolve_runtime_approval_turn(
        db_session,
        hitl_id=request.id,
        action=action,
        submitted_review=submitted,
        **scope,
    )
    assert request.status == "pending"
    assert "tiktok_publish_consent" not in request.context
    assert resolution.runtime_metadata["approval_outcome"] == "pending"
    assert "forced_tool_calls" not in resolution.runtime_metadata


@pytest.mark.asyncio
async def test_edited_review_preserves_types_and_is_granted_and_consumed_once(db_session, persisted_review):
    request, scope, submitted = persisted_review
    approved = await approvals.resolve_runtime_approval_turn(
        db_session,
        hitl_id=request.id,
        action="approve",
        submitted_review=submitted,
        **scope,
    )
    assert request.status == "granted"
    assert type(request.context["tiktok_publish_consent"]["schema_version"]) is int
    assert request.context["tiktok_publish_consent"]["schema_version"] == 1
    assert request.context["tiktok_publish_consent"]["submitted_review"] == submitted
    forced = approved.runtime_metadata["forced_tool_calls"]
    assert len(forced) == 1 and forced[0]["disable_followup_tools"] is True
    final = forced[0]["arguments"]
    assert final["title"] == "Operator edited caption"
    assert final["disable_comment"] is True and final["disable_duet"] is False
    assert final["brand_content_toggle"] is False
    assert final["video_url"] == ARGS["video_url"]
    assert final["integration_account_id"] == ARGS["integration_account_id"]
    repeated = await approvals.resolve_runtime_approval_turn(
        db_session,
        hitl_id=request.id,
        action="approve",
        submitted_review=submitted,
        **scope,
    )
    assert "already approved" in repeated.message and not repeated.runtime_metadata
    action = classify_runtime_tool_action(TOOL, final)
    with runtime_capture_tiktok_publish_consent() as captured:
        result = await approvals.consume_runtime_approval(
            db_session,
            hitl_id=request.id,
            tool_name=TOOL,
            arguments=dict(final),
            action=action,
            permission_decision=PermissionDecision.allow(AuthorizationRule.SYSTEM_WORKER),
            **scope,
        )
    assert result is None and request.status == "consumed"
    assert len(captured) == 1 and captured[0].approval_id == request.id
    assert captured[0].fields == {key: value for key, value in final.items() if key != "approval_token"}
    with runtime_capture_tiktok_publish_consent() as second_capture:
        duplicate = await approvals.consume_runtime_approval(
            db_session,
            hitl_id=request.id,
            tool_name=TOOL,
            arguments=dict(final),
            action=action,
            permission_decision=PermissionDecision.allow(AuthorizationRule.SYSTEM_WORKER),
            **scope,
        )
    assert json.loads(duplicate)["status"] == "consumed" and second_capture == []


@pytest.mark.asyncio
@pytest.mark.parametrize("tamper", ["missing", "consent", "music", "brand", "caption", "review_id", "schema_version"])
async def test_missing_or_changed_declarations_cannot_issue_execution_consent(
    db_session,
    persisted_review,
    tamper,
):
    request, scope, submitted = persisted_review
    approved = await approvals.resolve_runtime_approval_turn(
        db_session,
        hitl_id=request.id,
        action="approve",
        submitted_review=submitted,
        **scope,
    )
    final = approved.runtime_metadata["forced_tool_calls"][0]["arguments"]
    context = dict(request.context)
    consent = {**context["tiktok_publish_consent"]}
    if tamper == "missing":
        consent.pop("submitted_review")
    elif tamper == "schema_version":
        consent["schema_version"] = True
    else:
        consent["submitted_review"] = {
            **submitted,
            **{
                "consent": {"consent_confirmed": False},
                "music": {"music_usage_confirmed": False},
                "brand": {
                    "commercial_content": True,
                    "brand_content_toggle": True,
                    "privacy_level": "PUBLIC_TO_EVERYONE",
                    "branded_content_policy_confirmed": False,
                },
                "caption": {"caption": "Not the approved caption"},
                "review_id": {"review_id": "other-review"},
            }[tamper],
        }
    request.context = {**context, "tiktok_publish_consent": consent}
    # JSON equality treats True == 1. Force the intentionally malformed value
    # to disk, then verify consumption reads the tampered stored evidence.
    from sqlalchemy.orm.attributes import flag_modified

    flag_modified(request, "context")
    await db_session.flush()
    await db_session.refresh(request, ["context"])
    if tamper == "schema_version":
        assert request.context["tiktok_publish_consent"]["schema_version"] is True
    tampered_context = deepcopy(request.context)
    with runtime_capture_tiktok_publish_consent() as captured:
        blocked = await approvals.consume_runtime_approval(
            db_session,
            hitl_id=request.id,
            tool_name=TOOL,
            arguments=dict(final),
            action=classify_runtime_tool_action(TOOL, final),
            permission_decision=PermissionDecision.allow(AuthorizationRule.SYSTEM_WORKER),
            **scope,
        )
    assert captured == []
    assert request.status == "expired"
    assert request.consumed_at is None
    assert request.resolved_reason == "tiktok_publish_consent_missing"
    assert request.context == tampered_context
    payload = json.loads(blocked)
    assert payload["error"] == "blocked_by_governance"
    assert payload["matched_rule"] == "approval.operation_terminal"
    assert payload["status"] == "expired" and payload["action_executed"] is False
    assert payload["approval_token"] == request.id
    assert "__hitl__" not in payload and "hitl" not in payload

    # Damaged declarations terminate this operation; neither another card click
    # nor a replay may turn its old consent into a fresh executable request.
    repeated = await approvals.resolve_runtime_approval_turn(
        db_session, hitl_id=request.id, action="approve", submitted_review=submitted, **scope,
    )
    assert "already expired" in repeated.message
    assert not repeated.runtime_metadata
    with runtime_capture_tiktok_publish_consent() as replay_capture:
        replay = json.loads(await approvals.consume_runtime_approval(
            db_session, hitl_id=request.id, tool_name=TOOL, arguments=dict(final),
            action=classify_runtime_tool_action(TOOL, final),
            permission_decision=PermissionDecision.allow(AuthorizationRule.SYSTEM_WORKER),
            **scope,
        ))
    assert replay_capture == []
    assert replay["error"] == "approval_not_granted" and replay["status"] == "expired"
    assert request.status == "expired" and request.consumed_at is None
    assert request.context == tampered_context
    from sqlalchemy import select
    from packages.core.models.hitl_request import HitlRequest

    ids = (await db_session.scalars(select(HitlRequest.id).where(
        HitlRequest.entity_id == scope["entity_id"],
        HitlRequest.origin_conversation_id == scope["conversation_id"],
    ))).all()
    assert ids == [request.id]
