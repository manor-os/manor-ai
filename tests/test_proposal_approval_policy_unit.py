"""No-network checks for the unified Proposal policy and card contract."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from packages.core.governance import approvals
from packages.core.governance.policy import PolicyDecision
from packages.core.governance.approval_facts import action_facts
from packages.core.governance.approval_policy import ApprovalScope, evaluate_policy
from packages.core.governance.approval_policy_service import build_policy_chain
from packages.core.governance import proposal_approval
from packages.core.proposals.constants import EXTERNAL_TASK_ACTION_KEY, TASK_ACTION_KEY
from packages.core.strategist import service
from packages.core.strategist.proposal import Proposal


class _Rows:
    def __init__(self, rows):
        self.rows = rows

    def scalars(self):
        return self

    def all(self):
        return self.rows

    def scalar_one_or_none(self):
        return self.rows[0] if self.rows else None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "external,grant,deny,automatic_mode,paused,outcome",
    [
        (False, None, False, False, False, "needs_human"),
        (False, TASK_ACTION_KEY, False, False, False, "allow"),
        (False, TASK_ACTION_KEY, True, False, False, "deny"),
        (True, TASK_ACTION_KEY, False, False, False, "needs_human"),
        (True, EXTERNAL_TASK_ACTION_KEY, False, False, False, "allow"),
        (True, EXTERNAL_TASK_ACTION_KEY, True, True, False, "deny"),
        (False, None, False, True, False, "allow"),
        (False, None, True, True, False, "deny"),
        (False, TASK_ACTION_KEY, False, False, True, "needs_human"),
    ],
)
async def test_cohort_uses_real_approval_core(
    monkeypatch, external, grant, deny, automatic_mode, paused, outcome,
):
    action = EXTERNAL_TASK_ACTION_KEY if external else TASK_ACTION_KEY
    rules = [{"id": "grant", "effect": "allow", "selector": {"kind": "action", "action_key": grant}}] if grant else []
    if deny:
        rules.append({"id": "deny", "effect": "deny", "selector": {"kind": "action", "action_key": action}})
    chain = build_policy_chain(scope=ApprovalScope(kind="account", id="entity", entity_id="entity"),
        root={"revision": 1, "policy": {"mode": "automatic" if automatic_mode else "risk_based", "rules": rules}})
    workspace = SimpleNamespace(id="workspace", entity_id="entity", status="paused" if paused else "active")
    proposed = SimpleNamespace(title="Draft", external_action={} if external else None)
    proposal = SimpleNamespace(
        summary="Draft", notes=None, tasks=[proposed], human_requests=[],
        experiments=[], workflow_runs=[], automation_changes=[], workflow_changes=[], goal_changes=[],
    )
    record = SimpleNamespace(id="proposal")
    item = SimpleNamespace(approval_request_id=None)
    db = SimpleNamespace(
        execute=AsyncMock(side_effect=[_Rows([SimpleNamespace(id="task")]), _Rows([])]),
        flush=AsyncMock(),
    )
    import packages.core.proposals as proposals
    monkeypatch.setattr(proposals, "create_proposal_with_items", AsyncMock(return_value=record))
    monkeypatch.setattr(proposals, "get_items_for_review", AsyncMock(return_value=[item]))
    monkeypatch.setattr(proposals, "validate_items", AsyncMock(return_value=[(item, None)]))
    monkeypatch.setattr(service, "_strategist_permission_decision", AsyncMock(return_value=SimpleNamespace(allowed=True)))
    for name in ("_execute_human_request_items", "_execute_experiment_items", "_execute_workflow_run_items", "_execute_change_items"):
        monkeypatch.setattr(service, name, AsyncMock(return_value=[]))

    async def policy_decision(_db, subject, _origin, **kwargs):
        result = evaluate_policy(chain, action_facts(action_key=subject.action_key,
            risk_level=subject.risk_level, category=subject.category, impact=subject.impact))
        return PolicyDecision(result.outcome == "allow", result.outcome == "ask",
            result.reason_code, result.rule_id, result.model_dump())

    monkeypatch.setattr(approvals, "_policy_decision", policy_decision)
    monkeypatch.setattr(approvals, "_find_open_request", AsyncMock(return_value=None))
    monkeypatch.setattr(approvals, "_find_operation_request", AsyncMock(return_value=None))
    mint = AsyncMock(return_value=SimpleNamespace(id="approval", reason="Review required", matched_rule="proposal.review"))
    monkeypatch.setattr(approvals, "_create_pending_request", mint)
    result = await service._wire_proposal_governance(
        db, workspace=workspace, review_id="review", proposal=proposal,
        new_task_ids=["task"],
    )
    assert result["outcome"] == outcome
    assert result["task_action_key"] == action
    assert bool(mint.await_count) is (outcome == "needs_human")
    assert item.approval_request_id == ("approval" if outcome == "needs_human" else None)
    if outcome == "needs_human":
        assert mint.await_args.kwargs["dedup_key"] == "operation:proposal:proposal"
    if outcome == "deny":
        approvals._find_operation_request.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "auto_approved,policy_denied,task_ids,expected",
    [(True, False, ["task"], "auto_approved"), (False, False, ["task"], "pending"),
     (False, True, ["task"], "rejected"), (False, False, [], "not_required")],
)
async def test_card_has_typed_approval_metadata(monkeypatch, auto_approved, policy_denied, task_ids, expected):
    monkeypatch.setattr(service, "_proposal_impact_goals", AsyncMock(return_value={}))
    monkeypatch.setattr(service, "_proposal_basis_reports", AsyncMock(return_value={}))
    post = AsyncMock(return_value=SimpleNamespace(id="message"))
    monkeypatch.setattr(service.chat_service, "post_message", post)
    await service._post_proposal_chat(
        SimpleNamespace(id="workspace", entity_id="entity"),
        Proposal(review_id="review", summary="Draft", tasks=[]), task_ids,
        db=object(), auto_approved=auto_approved, policy_denied=policy_denied,
        auto_approved_action_key=TASK_ACTION_KEY if auto_approved else None,
    )
    payload = post.await_args.kwargs
    assert payload["meta"]["proposal"]["approval"]["status"] == expected
    assert bool(payload["pending_action"]) is (expected == "pending")


@pytest.mark.asyncio
@pytest.mark.parametrize("denied", [False, True])
async def test_resolved_tasks_do_not_hide_pending_change_items(monkeypatch, denied):
    monkeypatch.setattr(service, "_proposal_impact_goals", AsyncMock(return_value={}))
    monkeypatch.setattr(service, "_proposal_basis_reports", AsyncMock(return_value={}))
    post = AsyncMock()
    monkeypatch.setattr(service.chat_service, "post_message", post)
    await service._post_proposal_chat(
        SimpleNamespace(id="workspace", entity_id="entity"),
        Proposal(review_id="review", summary="Draft", tasks=[]), ["already_approved"],
        db=object(), auto_approved=not denied, policy_denied=denied, auto_approved_action_key=TASK_ACTION_KEY,
        pending_items=[{"item_id": "change", "kind": "goal_change", "summary": "Raise target"}],
    )
    payload = post.await_args.kwargs
    assert payload["meta"]["proposal"]["approval"]["status"] == "pending"
    assert payload["pending_action"]["task_ids"] == []
    assert payload["pending_action"]["items"][0]["item_id"] == "change"


@pytest.mark.asyncio
async def test_retired_proposal_toggle_fails_before_database_mutation():
    db = SimpleNamespace(execute=AsyncMock(), flush=AsyncMock())
    with pytest.raises(ValueError, match="Execution & approvals"):
        await proposal_approval.set_proposal_approval_actions(
            db, entity_id="entity", workspace_id="workspace", action_keys=(TASK_ACTION_KEY,), enabled=False,
        )
    db.execute.assert_not_awaited()
    db.flush.assert_not_awaited()


@pytest.mark.parametrize("field", ["auto_approve_actions", "approval"])
def test_ordinary_governance_write_cannot_bypass_shared_policy_cas(field):
    from apps.api.routers import governance
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        governance.PolicyBody(**{field: []}, max_risk_level="medium")


@pytest.mark.asyncio
async def test_mixed_approval_notification_requests_review_without_hiding_started_tasks(monkeypatch):
    import packages.core.database as database
    import packages.core.permissions as permissions
    from packages.core.services import notification_service, workspace_access

    db = SimpleNamespace(
        execute=AsyncMock(return_value=_Rows([SimpleNamespace(id="user", role="owner")])),
        commit=AsyncMock(),
    )
    context = AsyncMock()
    context.__aenter__.return_value = db
    monkeypatch.setattr(database, "async_session", lambda: context)
    monkeypatch.setattr(permissions, "resolve_effective_user_role_name", AsyncMock(return_value="owner"))
    monkeypatch.setattr(workspace_access, "user_can_read_workspace_by_identity", AsyncMock(return_value=True))
    create = AsyncMock()
    monkeypatch.setattr(notification_service, "create_notification", create)
    await service._notify_proposal_users(
        SimpleNamespace(id="workspace", entity_id="entity", name="Workspace"),
        Proposal(review_id="review", summary="Draft and configure", tasks=[]),
        ["task"], items=[{"item_id": "change"}], auto_approved=True,
        auto_approved_action_key=TASK_ACTION_KEY,
    )
    create.assert_awaited_once()
    payload = create.await_args.kwargs
    assert payload["title"].startswith("Proposal needs review:")
    assert "1 task already approved and started" in payload["body"]
    assert "1 other item still requires approval" in payload["body"]
    assert "Review the remaining items" in payload["body"]


@pytest.mark.asyncio
@pytest.mark.parametrize("external", [False, True])
async def test_workflow_item_mints_and_authorizes_its_persisted_action(monkeypatch, external):
    import packages.core.proposals as proposals
    from packages.core.proposals.constants import WORKFLOW_RUN_ACTION_KEY, WORKFLOW_RUN_EXTERNAL_ACTION_KEY
    from packages.core.services import proposal_workflow_runs

    action_key = WORKFLOW_RUN_EXTERNAL_ACTION_KEY if external else WORKFLOW_RUN_ACTION_KEY
    workspace = SimpleNamespace(id="workspace", entity_id="entity", status="active")
    record = SimpleNamespace(id="proposal", entity_id="entity", workspace_id="workspace")
    item = SimpleNamespace(id="workflow-item", action_key=action_key,
        risk_level="high" if external else "medium", approval_request_id=None)
    proposed = SimpleNamespace(run_key="publish", workflow_ref=SimpleNamespace(model_dump=lambda **kw: {"workflow_slug": "flow"}))
    db = SimpleNamespace(flush=AsyncMock())
    permission = object()
    authorize = AsyncMock(return_value=permission)
    resolve = AsyncMock(return_value=SimpleNamespace(outcome="needs_human", request=SimpleNamespace(id="approval")))
    monkeypatch.setattr(proposals, "create_workflow_run_items", AsyncMock(return_value=[item]))
    monkeypatch.setattr(service, "_lock_proposal_workspace", AsyncMock(return_value=workspace))
    monkeypatch.setattr(service, "_strategist_permission_decision", authorize)
    monkeypatch.setattr(proposal_workflow_runs, "resolve_proposal_workflow_binding", AsyncMock())
    monkeypatch.setattr(approvals, "resolve_approval", resolve)

    result = await service._execute_workflow_run_items(db, workspace=workspace,
        record=record, review_id="review", proposal=SimpleNamespace(workflow_runs=[proposed]))

    authorize.assert_awaited_once_with(db, workspace=workspace,
        action_key=action_key, capability_id="workflow.run")
    resolve.assert_awaited_once()
    subject = resolve.await_args.kwargs["subject"]
    assert (subject.entity_id, subject.workspace_id, subject.resource_kind, subject.resource_id, subject.action_key) == (
        record.entity_id, record.workspace_id, "proposal_item", item.id, item.action_key)
    assert resolve.await_args.kwargs["permission_decision"] is permission
    assert item.approval_request_id == "approval"
    assert result[0]["action_key"] == action_key and result[0]["outcome"] == "needs_human"


@pytest.mark.asyncio
@pytest.mark.parametrize("external", [False, True])
@pytest.mark.parametrize("approved", [False, True])
async def test_task_cohort_settles_only_its_scoped_operation_request(monkeypatch, external, approved):
    from packages.core.proposals import service as proposal_service

    action = EXTERNAL_TASK_ACTION_KEY if external else TASK_ACTION_KEY
    record = SimpleNamespace(id="proposal", entity_id="entity", workspace_id="workspace")
    fields = dict(id="request", entity_id=record.entity_id, workspace_id=record.workspace_id,
        resource_kind="proposal", resource_id=record.id, action_key=action, status="pending")
    request = SimpleNamespace(**fields)
    unrelated = [SimpleNamespace(**{**fields, key: value}) for key, value in (
        ("entity_id", "other-entity"), ("workspace_id", "other-workspace"),
        ("resource_kind", "proposal_item"), ("resource_id", "other-proposal"),
        ("action_key", "workspace.proposal.experiment"), ("status", "denied"),
        ("status", "consumed"), ("status", "expired"),
    )]
    lookup = AsyncMock(return_value=[request, *unrelated])
    decide = AsyncMock()
    events = []

    async def grant(db, row, **kwargs):
        events.append(("grant", row, kwargs))
        row.status = "granted"

    async def consume(db, row):
        assert row.status == "granted"
        events.append(("consume", row, {}))
        row.status = "consumed"

    async def deny(db, row, **kwargs):
        events.append(("deny", row, kwargs))
        row.status = "denied"

    monkeypatch.setattr(proposal_service, "get_proposal_for_review", AsyncMock(return_value=record))
    monkeypatch.setattr(proposal_service, "decide_items", decide)
    monkeypatch.setattr(approvals, "find_requests_by_dedup", lookup)
    monkeypatch.setattr(approvals, "grant_approval", grant)
    monkeypatch.setattr(approvals, "consume_approval", consume)
    monkeypatch.setattr(approvals, "deny_approval", deny)
    for name in ("_mirror_experiment_items_on_cohort_decision", "_mirror_workflow_run_items_on_cohort_decision",
                 "_mirror_change_items_on_cohort_decision"):
        monkeypatch.setattr(service, name, AsyncMock())
    db = object()
    await service._mirror_item_decisions(db, review_id="review", task_ids=["task"],
        approved=approved, actor_id="owner", reason="operator decision")
    lookup.assert_awaited_once_with(db, entity_id="entity", dedup_key="operation:proposal:proposal")
    assert decide.await_args.kwargs["task_ids"] == ["task"]
    assert decide.await_args.kwargs["decision"] == ("approved" if approved else "rejected")
    assert [event[0] for event in events] == (["grant", "consume"] if approved else ["deny"])
    assert all(event[1] is request for event in events)
    assert events[0][2]["by_user_id"] == "owner" and events[0][2]["via"] == "chat_card"
    if approved:
        assert events[0][2]["authority_prechecked"] is False
    else:
        assert events[0][2]["reason"] == "operator decision"
    assert request.status == ("consumed" if approved else "denied")
    assert [row.status for row in unrelated] == ["pending"] * 5 + ["denied", "consumed", "expired"]
