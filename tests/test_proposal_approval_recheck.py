"""Offline checks of Proposal preflight ordering and the real policy evaluator."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from packages.core.governance import approval_policy_service, service as governance
from packages.core.governance.approval_policy import ApprovalScope
from packages.core.governance.policy import WorkspacePolicy
from packages.core.proposals import approval_guard, lifecycle
from packages.core.strategist import service


@pytest.mark.asyncio
async def test_locked_policy_boundary_refreshes_stale_workspace_identity():
    from sqlalchemy import JSON, MetaData, update
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
    from packages.core.models.governance import GovernancePolicy
    from packages.core.models.user import Entity
    from packages.core.models.workspace import Workspace
    # Reuse the existing SQLite JSONB adapter; no repository DB fixture runs.
    import test_approval_policy_settings  # noqa: F401

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as connection:
            for model in (Entity, GovernancePolicy):
                await connection.run_sync(model.__table__.create)
            workspace_table = Workspace.__table__.to_metadata(MetaData())
            workspace_table.c.attribute_tags.type = JSON()
            await connection.run_sync(workspace_table.create)
        async with AsyncSession(engine, expire_on_commit=False) as db:
            workspace = Workspace(id="workspace", entity_id="entity", name="Fixture", status="active")
            db.add_all([Entity(id="entity", name="Fixture", settings={
                "approval_defaults": approval_policy_service.account_defaults()}), workspace,
                GovernancePolicy(workspace_id="workspace", entity_id="entity", revision=1,
                    policy={"approval": {"root_entity_id": "entity", "policy": {"mode": None, "rules": []}}})])
            await db.commit()
            await db.execute(update(Workspace).where(Workspace.id == workspace.id).values(status="archived")
                .execution_options(synchronize_session=False))
            assert workspace.status == "active", "fixture must contain a stale identity-map object"
            with pytest.raises(approval_guard.ProposalApprovalBlocked, match="unavailable"):
                await approval_guard.lock_proposal_approval_boundary(db, entity_id="entity", workspace_id="workspace")
            assert workspace.status == "archived"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,action", [
    ("task", "workspace.proposal.task"), ("task", "workspace.proposal.task.external"),
    ("automation_change", "workspace.proposal.automation_change.pause"),
    ("workflow_change", "workspace.proposal.workflow_change.update"),
    ("goal_change", "workspace.proposal.goal_change.update_target"),
    ("experiment", "workspace.proposal.experiment"),
    ("workflow_run", "workspace.proposal.workflow_run"),
    ("workflow_run", "workspace.proposal.workflow_run.external"),
])
@pytest.mark.parametrize("boundary", ["ask", "allow", "root_deny", "local_deny", "budget", "risk", "permission"])
@pytest.mark.parametrize("actor_kind", ["user", "system"])
async def test_proposal_uses_current_policy_and_outer_limits(monkeypatch, kind, action, boundary, actor_kind):
    from packages.core import budget
    from packages.core.services import runtime_authorization

    record = SimpleNamespace(id="proposal", entity_id="entity", workspace_id="workspace")
    item = SimpleNamespace(id="item", entity_id="entity", workspace_id="workspace", proposal_id="proposal",
        kind=kind, action_key=action, risk_level="high", payload={})
    rule = {"id": "deny", "effect": "deny", "selector": {"kind": "action", "action_key": action}}
    chain = approval_policy_service.build_policy_chain(
        scope=ApprovalScope(kind="workspace", id="workspace", entity_id="entity"),
        root={"revision": 2, "policy": {"mode": "automatic" if boundary == "allow" else "confirm_changes", "rules": [rule] if boundary == "root_deny" else []}},
        local={"revision": 2, "root_entity_id": "entity", "policy": {
            "mode": None, "rules": [rule] if boundary == "local_deny" else []}})
    order = []

    async def lock(*args, **kwargs):
        order.append("lock")

    async def authority(*args, **kwargs):
        order.append("authority")
        assert kwargs["action_key"] == action
        if actor_kind == "user":
            assert kwargs["by_user_id"] == "actor"
        else:
            assert kwargs["principal_kind"] == "system_worker" and kwargs["user_id"] is None
        return SimpleNamespace(allowed=boundary != "permission", reason="permission denied")

    async def current_chain(*args, **kwargs):
        order.append("policy")
        return chain

    monkeypatch.setattr(approval_guard, "lock_proposal_approval_boundary", lock)
    monkeypatch.setattr(runtime_authorization, "authorize_hitl_action", authority)
    monkeypatch.setattr(runtime_authorization, "authorize_runtime_action", authority)
    monkeypatch.setattr(budget, "get_workspace_spent_credits_per_kind", AsyncMock(return_value={"action": 10}))
    monkeypatch.setattr(approval_policy_service, "load_policy_chain", current_chain)
    monkeypatch.setattr(governance, "get_policy", AsyncMock(return_value=WorkspacePolicy(
        max_risk_level="low" if boundary == "risk" else "high",
        budget_caps_per_kind={"action": 10} if boundary == "budget" else {})))
    db = SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(entity_id="entity")))
    call = approval_guard.require_current_proposal_policy(db, record=record, items=[item], actor_id="actor", actor_kind=actor_kind)
    if boundary == "allow" or (boundary == "ask" and actor_kind == "user"):
        await call
    else:
        with pytest.raises(approval_guard.ProposalApprovalBlocked):
            await call
    if actor_kind == "user":
        assert order[:2] == ["lock", "authority"]
    else:
        assert order[0] == "authority" and "lock" not in order


@pytest.mark.asyncio
async def test_proposal_preflight_denial_precedes_task_or_item_mutations(monkeypatch):
    from packages.core.proposals import service as proposals

    record = SimpleNamespace(id="proposal", entity_id="entity", workspace_id="workspace")
    task = SimpleNamespace(id="task", status="proposed")
    items = [SimpleNamespace(id="task-item", status="proposed", kind="task", payload={"task_id": "task"}),
             SimpleNamespace(id="change", status="proposed", kind="goal_change", payload={})]
    monkeypatch.setattr(service, "_find_proposed", AsyncMock(return_value=[task]))
    monkeypatch.setattr(service, "_selected_rows_with_required_dependencies", lambda rows, _: rows)
    monkeypatch.setattr(proposals, "get_proposal_for_review", AsyncMock(return_value=record))
    monkeypatch.setattr(proposals, "get_items_for_review", AsyncMock(return_value=items))
    gate = AsyncMock(side_effect=approval_guard.ProposalApprovalBlocked("new root deny"))
    monkeypatch.setattr(approval_guard, "require_current_proposal_policy", gate)
    mutations = [AsyncMock() for _ in range(3)]
    for name, mutation in zip(("_create_proposal_work_batch", "update_task", "_mirror_item_decisions"), mutations):
        monkeypatch.setattr(service, name, mutation)
    with pytest.raises(approval_guard.ProposalApprovalBlocked, match="new root deny"):
        await service.approve_proposal(object(), entity_id="entity", review_id="review", actor_id="actor")
    assert gate.await_args.kwargs["items"] == items
    for mutation in mutations:
        mutation.assert_not_awaited()
    assert task.status == "proposed" and all(item.status == "proposed" for item in items)


@pytest.mark.asyncio
@pytest.mark.parametrize("approved", [False, True])
async def test_non_task_denial_blocks_effect_but_rejection_remains_available(monkeypatch, approved):
    record = SimpleNamespace(id="proposal")
    item = SimpleNamespace(id="item", status="proposed")
    rows = SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [item]))
    db = SimpleNamespace(execute=AsyncMock(return_value=rows), flush=AsyncMock())
    gate = AsyncMock(side_effect=approval_guard.ProposalApprovalBlocked("current deny"))
    monkeypatch.setattr(approval_guard, "require_current_proposal_policy", gate)
    monkeypatch.setattr(lifecycle, "_live_approval_requests", AsyncMock(return_value=[]))
    monkeypatch.setattr(lifecycle, "resolve_proposal_if_settled", AsyncMock())
    effect = AsyncMock()
    call = lifecycle.apply_non_task_cohort_decision(db, record=record, kinds=["experiment"],
        approved=approved, actor_id="actor", on_approved=effect)
    if approved:
        with pytest.raises(approval_guard.ProposalApprovalBlocked):
            await call
        assert item.status == "proposed"
    else:
        await call
        gate.assert_not_awaited()
        assert item.status == "rejected"
    effect.assert_not_awaited()


@pytest.mark.asyncio
async def test_each_non_task_item_rechecks_policy_after_previous_dispatch(monkeypatch):
    record = SimpleNamespace(id="proposal")
    items = [SimpleNamespace(id=key, status="proposed") for key in ("first", "second")]
    rows = SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: items))
    db = SimpleNamespace(execute=AsyncMock(return_value=rows), flush=AsyncMock())
    events = []

    async def check(db, **kwargs):
        selected = [item.id for item in kwargs["items"]]
        events.append(("check", selected))
        if selected == ["second"]:
            raise approval_guard.ProposalApprovalBlocked("policy changed after first dispatch committed")

    async def effect(item):
        events.append(("effect", item.id))

    monkeypatch.setattr(approval_guard, "require_current_proposal_policy", check)
    monkeypatch.setattr(lifecycle, "_live_approval_requests", AsyncMock(return_value=[]))
    with pytest.raises(approval_guard.ProposalApprovalBlocked):
        await lifecycle.apply_non_task_cohort_decision(db, record=record, kinds=["workflow_run"],
            approved=True, actor_id="actor", on_approved=effect)
    assert events == [("check", ["first", "second"]), ("check", ["first"]),
        ("effect", "first"), ("check", ["second"])]
    assert items[1].status == "proposed"
