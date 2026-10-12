"""Standing consent covers every approved output while hard denials still win."""

from dataclasses import replace

import pytest

from packages.core.governance import WorkspacePolicy, get_policy, update_policy
from packages.core.governance.approvals import (
    ApprovalOrigin,
    ApprovalSubject,
    grant_approval,
    resolve_approval,
)
from packages.core.governance.approval_facts import action_facts
from packages.core.governance.approval_policy import ApprovalPolicyOverride, ApprovalScope, PolicyDraft, evaluate_policy
from packages.core.governance.approval_policy_service import account_defaults, build_policy_chain, read_approval_settings, update_approval_policy
from packages.core.models.base import generate_ulid
from packages.core.models.user import Entity, User, UserMembership
from packages.core.models.workspace import Workspace
from packages.core.services.runtime_authorization import AuthorizationRule, PermissionDecision


pytestmark = pytest.mark.oss_smoke


ACTION = "workspace.file.create"
RESOURCES = (
    "Workspaces/_by_id/media/images/hero.png",
    "Workspaces/_by_id/media/images/hero.webp",
    "images/hero.png",
    "images/hero.webp",
)


async def _workspace(db):
    entity, workspace, user = generate_ulid(), generate_ulid(), generate_ulid()
    db.add(Entity(id=entity, name="Media test", settings={"approval_defaults": account_defaults()}))
    db.add(User(
        id=user, entity_id=entity, email=f"{user}@example.com",
        password_hash="x", role="owner",
    ))
    db.add(UserMembership(user_id=user, entity_id=entity, role="owner", status="active"))
    db.add(Workspace(id=workspace, entity_id=entity, name="Media policy", operating_model={}))
    await db.flush()
    await update_policy(
        db, entity_id=entity, workspace_id=workspace,
        policy=WorkspacePolicy(approval=ApprovalPolicyOverride(mode="confirm_changes")), changed_by=user,
    )
    return ApprovalSubject(
        entity_id=entity, workspace_id=workspace, action_key=ACTION,
        capability_id="file.write", resource_kind="file", resource_id=RESOURCES[0],
        resource_ids=RESOURCES,
    ), user


async def _resolve(db, subject, *, context=None):
    return await resolve_approval(
        db, subject=subject,
        origin=ApprovalOrigin(kind="tool_call", operation_id=f"runtime:run:call:{generate_ulid()}", args_hash="frozen", context=context or {}),
        permission_decision=PermissionDecision.allow(AuthorizationRule.SYSTEM_WORKER),
    )


async def _save_rules(db, subject, user, rules):
    scope = ApprovalScope(kind="workspace", id=subject.workspace_id, entity_id=subject.entity_id)
    actor = await db.get(User, user)
    current = await read_approval_settings(db, scope, actor)
    return await update_approval_policy(db, scope, PolicyDraft(mode="confirm_changes", user_rules=rules), current["version"], actor)


def _resource_rule(effect, resources):
    return {"effect": effect, "selector": {"kind": "resource", "action_key": ACTION,
        "account_id": None, "resource_kind": "file", "resource_ids": list(resources)}}


@pytest.mark.asyncio
async def test_standing_media_approval_promotes_all_targets_and_preserves_denials(db_session):
    subject, user = await _workspace(db_session)
    decision = await _resolve(
        db_session, subject,
        context={"_approval_resource_ids": ["unapproved/*"], "display": "kept"},
    )
    assert decision.outcome == "needs_human"
    request = decision.request
    assert request.context["_approval_resource_ids"] == list(RESOURCES)
    assert request.context["display"] == "kept"

    await grant_approval(db_session, request, by_user_id=user, via="once")
    await _save_rules(db_session, subject, user, [_resource_rule("allow", RESOURCES)])
    repeated = await _resolve(db_session, subject)
    assert repeated.outcome == "allow"
    assert repeated.request is None
    await _save_rules(db_session, subject, user, [
        _resource_rule("allow", RESOURCES), _resource_rule("deny", [RESOURCES[1]]),
    ])
    denied = await _resolve(db_session, subject)
    assert denied.outcome == "deny"
    assert denied.request is None


@pytest.mark.asyncio
async def test_single_resource_mint_removes_forged_scope_and_saves_only_exact_rule(db_session):
    subject, user = await _workspace(db_session)
    subject = replace(subject, resource_ids=())
    decision = await _resolve(
        db_session, subject, context={"_approval_resource_ids": ["unapproved/*"]},
    )
    assert "_approval_resource_ids" not in decision.request.context
    await _save_rules(db_session, subject, user, [_resource_rule("allow", [subject.resource_id])])
    policy = await get_policy(db_session, subject.workspace_id)
    assert policy.approval.policy.rules[0].selector.resource_ids == [subject.resource_id]


@pytest.mark.asyncio
async def test_partial_standing_grant_cannot_skip_other_target_review(db_session):
    subject, user = await _workspace(db_session)
    await _save_rules(db_session, subject, user, [_resource_rule("allow", [RESOURCES[0]])])
    assert (await _resolve(db_session, subject)).outcome == "needs_human"


@pytest.mark.asyncio
async def test_malformed_persisted_scope_cannot_promote_a_broader_grant(db_session):
    subject, user = await _workspace(db_session)
    request = (await _resolve(db_session, subject)).request
    request.context = {"_approval_resource_ids": [None]}
    with pytest.raises(ValueError, match="versioned"):
        await grant_approval(db_session, request, by_user_id=user, via="always", standing=True)
    assert request.status == "pending"
    assert not (await get_policy(db_session, subject.workspace_id)).approval.policy.rules


@pytest.mark.asyncio
async def test_standing_media_resource_with_brackets_remains_literal(db_session):
    subject, user = await _workspace(db_session)
    resources = tuple(resource.replace("images/", "images/[ab]/") for resource in RESOURCES)
    subject = replace(subject, resource_id=resources[0], resource_ids=resources)
    request = (await _resolve(db_session, subject)).request
    await grant_approval(db_session, request, by_user_id=user, via="once")
    await _save_rules(db_session, subject, user, [_resource_rule("allow", resources)])
    assert (await _resolve(db_session, subject)).outcome == "allow"

    other_resources = tuple(resource.replace("[ab]", "a") for resource in resources)
    other = replace(subject, resource_id=other_resources[0], resource_ids=other_resources)
    assert (await _resolve(db_session, other)).outcome == "needs_human"


@pytest.mark.parametrize("resource, other", [
    ("images/[ab]/hero.png", "images/a/hero.png"),
    ("images/*.png", "images/private.png"),
    ("images/?.png", "images/a.png"),
    ("images/[?*]/hero.png", "images/?/hero.png"),
    ("images/[]abc]/hero.png", "images/a/hero.png"),
])
def test_literal_resource_selector_does_not_expand_glob(resource, other):
    chain = build_policy_chain(scope=ApprovalScope(kind="account", id="entity", entity_id="entity"),
        root={"revision": 1, "policy": {"mode": "confirm_changes", "rules": [
            {"id": "exact", **_resource_rule("allow", [resource])},
        ]}})
    def outcome(path):
        return evaluate_policy(chain, action_facts(action_key=ACTION, resource_kind="file", resource_id=path)).outcome
    assert outcome(resource) == "allow"
    assert outcome(other) == "ask"
