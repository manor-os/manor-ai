"""Initialize explicit test fixtures and edit policy through the real CAS service."""
from packages.core.governance.approval_policy import ApprovalScope, PolicyDraft
from packages.core.governance.approval_policy_service import (
    account_defaults, initialize_personal_approval, initialize_workspace_approval,
    read_approval_settings, update_approval_policy,
)
from packages.core.models.user import Entity, User


async def initialize_test_approval_owners(db, workspace, *, user_id):
    """Creation-only fixture setup; an existing account keeps its defaults."""
    entity = await db.get(Entity, workspace.entity_id)
    if entity is None:
        db.add(Entity(id=workspace.entity_id, name="Approval test account",
            settings={"approval_defaults": account_defaults()}))
        await db.flush()
    elif "approval_defaults" not in (entity.settings or {}):
        entity.settings = {**(entity.settings or {}), "approval_defaults": account_defaults()}
        await db.flush()
    await initialize_personal_approval(db, user_id=user_id, entity_id=workspace.entity_id)
    await initialize_workspace_approval(db, workspace)


async def set_workspace_test_approval(db, *, workspace_id, entity_id, actor_id,
                                      mode=None, user_rules=()):
    """No blind grants: use the persisted actor and the current root/local version."""
    actor = await db.get(User, actor_id)
    assert actor is not None and actor.entity_id == entity_id
    scope = ApprovalScope(kind="workspace", id=workspace_id, entity_id=entity_id)
    before = await read_approval_settings(db, scope, actor)
    return await update_approval_policy(db, scope,
        PolicyDraft(mode=mode, user_rules=list(user_rules)), before["version"], actor)


def action_rule(action_key, *, effect="allow"):
    return {"effect": effect, "selector": {"kind": "action", "action_key": action_key}}


def room_reply_rule(room_id):
    return {"effect": "allow", "selector": {
        "kind": "resource", "action_key": "external_message.send",
        "resource_kind": "resource", "resource_ids": [room_id], "account_id": None,
    }}


async def set_personal_test_approval(db, *, entity_id, actor_id, mode=None, user_rules=()):
    """Edit the actor's existing personal branch through its current CAS token."""
    actor = await db.get(User, actor_id)
    assert actor is not None and actor.entity_id == entity_id
    scope = ApprovalScope(kind="personal", id=actor_id, entity_id=entity_id)
    before = await read_approval_settings(db, scope, actor)
    return await update_approval_policy(db, scope,
        PolicyDraft(mode=mode, user_rules=list(user_rules)), before["version"], actor)


async def initialize_test_task_proposal(db, workspace, *, review_id, actor_id=None):
    """Creation-only fixture for hand-authored Tasks entering the Proposal API."""
    from sqlalchemy import select
    from packages.core.models.base import generate_ulid
    from packages.core.models.proposal import ProposalItemRecord, ProposalRecord
    from packages.core.models.task import Task
    from packages.core.models.user import UserMembership
    from packages.core.proposals.constants import EXTERNAL_TASK_ACTION_KEY, TASK_ACTION_KEY

    actor_id = actor_id or workspace.entity_id
    if await db.get(User, actor_id) is None:
        db.add(User(id=actor_id, entity_id=workspace.entity_id, email=f"{actor_id}@example.test",
            password_hash="fixture", role="owner", status="active"))
        db.add(UserMembership(user_id=actor_id, entity_id=workspace.entity_id,
            role="owner", status="active", is_primary=True))
    await db.flush()
    await initialize_test_approval_owners(db, workspace, user_id=actor_id)
    record = ProposalRecord(id=generate_ulid(), entity_id=workspace.entity_id,
        workspace_id=workspace.id, review_id=review_id, summary="Fixture proposal", status="open")
    db.add(record)
    tasks = (await db.execute(select(Task).where(Task.entity_id == workspace.entity_id,
        Task.workspace_id == workspace.id, Task.status == "proposed"))).scalars().all()
    for task in tasks:
        details = dict(task.details or {})
        if details.get("strategist_review_id") != review_id:
            continue
        external = bool(details.get("external_action"))
        item = ProposalItemRecord(id=generate_ulid(), proposal_id=record.id, entity_id=record.entity_id,
            workspace_id=workspace.id, item_key=task.id, kind="task", payload={"task_id": task.id},
            action_key=EXTERNAL_TASK_ACTION_KEY if external else TASK_ACTION_KEY,
            risk_level="high" if external else "low", status="proposed")
        db.add(item)
        task.details = {**details, "strategist_proposal_id": record.id, "strategist_proposal_item_id": item.id}
    await db.flush()
    return actor_id


async def initialize_test_account_approval(db, *, entity_id, user_id=None, user_rules=()):
    """Create a fixture account and grant only its declared worker actions via CAS."""
    from packages.core.models.base import generate_ulid
    from packages.core.models.user import UserMembership

    user_id = user_id or generate_ulid()
    entity = await db.get(Entity, entity_id)
    if entity is None:
        db.add(Entity(id=entity_id, name="Worker approval test account",
                      settings={"approval_defaults": account_defaults()}))
    elif "approval_defaults" not in (entity.settings or {}):
        entity.settings = {**(entity.settings or {}), "approval_defaults": account_defaults()}
    actor = await db.get(User, user_id)
    if actor is None:
        actor = User(id=user_id, entity_id=entity_id, email=f"{user_id}@example.test",
                     password_hash="fixture", role="owner", status="active")
        db.add_all([actor, UserMembership(user_id=user_id, entity_id=entity_id,
                                         role="owner", status="active")])
    await db.flush()
    await initialize_personal_approval(db, user_id=user_id, entity_id=entity_id)
    scope = ApprovalScope(kind="account", id=entity_id, entity_id=entity_id)
    before = await read_approval_settings(db, scope, actor)
    await update_approval_policy(db, scope,
        PolicyDraft(mode=before["policy"]["mode"], user_rules=list(user_rules)),
        before["version"], actor)
    return actor
