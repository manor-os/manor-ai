"""Approval owner storage, inheritance, permission checks and transactional CAS.

The caller owns commit. Reads use authoritative rows, never the auth object's
primary-Entity overlay or a cache of an expanded inherited policy.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from typing import Any
from types import SimpleNamespace

from sqlalchemy import event, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.governance.approval_policy import (
    ACTION_CATALOG, ActionFacts, ApprovalPolicy, ApprovalPolicyBinding,
    ApprovalPolicyOverride, ApprovalRule, ApprovalScope, PolicyDraft,
    PolicySnapshot, PolicyVersion, ResolvedPolicyChain, evaluate_policy,
)
from packages.core.models.audit import AuditLog
from packages.core.models.base import generate_ulid
from packages.core.models.chat_room import ChatRoom, ChatRoomMember
from packages.core.models.governance import GovernancePolicy, GovernanceRevision
from packages.core.models.user import Entity, User, UserMembership
from packages.core.models.workspace import Workspace


class ApprovalPolicyIntegrityError(ValueError):
    """Missing/misbound policies must be repaired by migration, not at runtime."""


class ApprovalPolicyAccessError(PermissionError):
    pass


class ApprovalPolicyConflict(ValueError):
    def __init__(self, current: dict):
        self.current = current
        super().__init__('Approval settings changed; review the current policy')


class ProtectedApprovalSettingsError(ValueError):
    pass


LEGACY_APPROVAL_PREFERENCES = frozenset({'runtime_approval_policy', 'ai_file_permission', 'file_permission', 'file_permissions'})


def account_defaults() -> dict:
    return {'revision': 1, 'policy': ApprovalPolicy().model_dump()}


def local_defaults(entity_id: str) -> dict:
    return {'revision': 1, **ApprovalPolicyBinding(root_entity_id=entity_id).model_dump()}


def _version(scope: ApprovalScope, root_revision: int, revision: int) -> str:
    raw = [scope.model_dump(), root_revision, revision]
    return hashlib.sha256(json.dumps(raw, sort_keys=True).encode()).hexdigest()


def build_policy_chain(*, scope: ApprovalScope, root: dict, local: dict | None = None,
                       local_revision: int | None = None) -> ResolvedPolicyChain:
    """Shared decoding used by storage and tests; rejects incomplete migration."""
    try:
        root_policy = ApprovalPolicy.model_validate(root['policy'])
        root_revision = int(root['revision'])
        root_scope = ApprovalScope(kind='account', id=scope.entity_id, entity_id=scope.entity_id)
        root_snapshot = PolicySnapshot(scope=root_scope, revision=root_revision,
            version=_version(root_scope, root_revision, root_revision), policy=root_policy,
            effective_policy=root_policy, policy_versions=[PolicyVersion(scope=root_scope, revision=root_revision)], inheritance='root')
        if scope.kind == 'account':
            return ResolvedPolicyChain(root=root_snapshot)
        if not isinstance(local, dict):
            raise ValueError('local approval policy is missing')
        binding = ApprovalPolicyBinding.model_validate({key: local[key] for key in ('root_entity_id', 'policy')})
        if binding.root_entity_id != scope.entity_id:
            raise ValueError('local approval policy has a different owner')
        revision = local_revision if local_revision is not None else int(local['revision'])
        mode = binding.policy.mode or root_policy.mode
        snapshot = PolicySnapshot(scope=scope, revision=revision,
            version=_version(scope, root_revision, revision), policy=binding.policy,
            effective_policy=ApprovalPolicy(mode=mode, rules=[*root_policy.rules, *binding.policy.rules]),
            root_scope=root_scope, policy_versions=[*root_snapshot.policy_versions, PolicyVersion(scope=scope, revision=revision)],
            inheritance='custom' if binding.policy.mode is not None or binding.policy.rules else 'following')
        return ResolvedPolicyChain(root=root_snapshot, local=snapshot)
    except (KeyError, TypeError, ValueError) as exc:
        raise ApprovalPolicyIntegrityError(f'Approval configuration is incomplete: {exc}') from exc


async def resolve_scope(db: AsyncSession, *, entity_id: str, workspace_id: str | None = None,
                        user_id: str | None = None, room_id: str | None = None) -> ApprovalScope:
    if room_id:
        row = (await db.execute(select(ChatRoom.owner_entity_id, ChatRoom.owner_user_id, ChatRoom.approval_policy)
            .where(ChatRoom.id == room_id, ChatRoom.status == 'active'))).one_or_none()
        if row is None:
            raise ApprovalPolicyAccessError('Room is unavailable')
        root_id = row.owner_entity_id
        if not root_id:
            # Query the persisted column; get_current_user overlays User.entity_id.
            root_id = (await db.execute(select(User.entity_id).where(User.id == row.owner_user_id,
                User.deleted_at.is_(None), User.status == 'active'))).scalar_one_or_none()
        if not root_id or not isinstance(row.approval_policy, dict) or row.approval_policy.get('root_entity_id') != root_id:
            raise ApprovalPolicyIntegrityError('Room approval owner binding is missing or changed')
        return ApprovalScope(kind='room', id=room_id, entity_id=root_id)
    if workspace_id:
        root_id = (await db.execute(select(Workspace.entity_id).where(Workspace.id == workspace_id,
            Workspace.deleted_at.is_(None)))).scalar_one_or_none()
        if root_id != entity_id:
            raise ApprovalPolicyAccessError('Workspace is unavailable in this account')
        return ApprovalScope(kind='workspace', id=workspace_id, entity_id=root_id)
    return ApprovalScope(kind='personal' if user_id else 'account', id=user_id or entity_id, entity_id=entity_id)


async def _load(db: AsyncSession, scope: ApprovalScope, *, lock: bool = False) -> tuple[ResolvedPolicyChain, Any]:
    root_query = select(Entity.settings).where(Entity.id == scope.entity_id, Entity.deleted_at.is_(None))
    if lock:
        root_query = root_query.with_for_update()
    settings = (await db.execute(root_query)).scalar_one_or_none()
    if not isinstance(settings, dict) or not isinstance(settings.get('approval_defaults'), dict):
        raise ApprovalPolicyIntegrityError('Account approval defaults are missing')
    local = None
    revision = None
    owner = settings
    if scope.kind == 'personal':
        query = select(User.preferences).where(User.id == scope.id, User.deleted_at.is_(None))
        if lock:
            query = query.with_for_update()
        owner = (await db.execute(query)).scalar_one_or_none()
        local = (owner or {}).get('approval_policy', {}).get(scope.entity_id)
    elif scope.kind == 'room':
        query = select(ChatRoom.owner_entity_id, ChatRoom.owner_user_id, ChatRoom.approval_policy).where(
            ChatRoom.id == scope.id, ChatRoom.status == 'active')
        row = (await db.execute(query)).one_or_none()
        if row is None:
            raise ApprovalPolicyAccessError('Room is unavailable')
        owner_identity = (row.owner_entity_id, row.owner_user_id)
        current_root = row.owner_entity_id
        if not current_root:
            owner_query = select(User.entity_id).where(User.id == row.owner_user_id,
                User.deleted_at.is_(None), User.status == 'active')
            if lock:
                # Match Room file mutations: Entity -> owner User -> Room.
                # Hold the primary Entity binding stable until the save commits.
                owner_query = owner_query.with_for_update(read=True)
            current_root = (await db.execute(owner_query)).scalar_one_or_none()
        if lock:
            row = (await db.execute(query.with_for_update())).one_or_none()
            if row is None or (row.owner_entity_id, row.owner_user_id) != owner_identity:
                raise ApprovalPolicyIntegrityError('Room approval owner changed while loading settings')
        if current_root != scope.entity_id or not isinstance(row.approval_policy, dict) or row.approval_policy.get('root_entity_id') != current_root:
            raise ApprovalPolicyIntegrityError('Room approval owner binding is missing or changed')
        owner = local = row.approval_policy
    elif scope.kind == 'workspace':
        query = select(Workspace).where(Workspace.id == scope.id, Workspace.entity_id == scope.entity_id, Workspace.deleted_at.is_(None))
        if lock:
            query = query.with_for_update().execution_options(populate_existing=True)
        if (await db.execute(query)).scalar_one_or_none() is None:
            raise ApprovalPolicyAccessError('Workspace is unavailable')
        query = select(GovernancePolicy).where(GovernancePolicy.workspace_id == scope.id, GovernancePolicy.entity_id == scope.entity_id)
        if lock:
            query = query.with_for_update().execution_options(populate_existing=True)
        owner = (await db.execute(query)).scalar_one_or_none()
        local = (owner.policy or {}).get('approval') if owner else None
        revision = owner.revision if owner else None
    return build_policy_chain(scope=scope, root=settings['approval_defaults'], local=local, local_revision=revision), owner


async def load_policy_chain(db: AsyncSession, *, entity_id: str, workspace_id: str | None = None,
                            user_id: str | None = None, room_id: str | None = None) -> ResolvedPolicyChain:
    scope = await resolve_scope(db, entity_id=entity_id, workspace_id=workspace_id, user_id=user_id, room_id=room_id)
    chain, _ = await _load(db, scope)
    return chain


async def _capabilities(db: AsyncSession, scope: ApprovalScope, actor: User, *, lock: bool = False) -> dict:
    from packages.core.permissions import user_is_effective_entity_admin
    from packages.core.services.workspace_access import user_can_manage_workspace, user_can_read_workspace

    membership_query = select(UserMembership).where(UserMembership.user_id == actor.id,
        UserMembership.entity_id == scope.entity_id, UserMembership.status == 'active', UserMembership.deleted_at.is_(None)).execution_options(populate_existing=True)
    if lock:
        membership_query = membership_query.with_for_update(read=True)
    membership = (await db.execute(membership_query)).scalar_one_or_none()
    same_account = actor.entity_id == scope.entity_id and membership is not None
    scoped_actor = SimpleNamespace(id=actor.id, entity_id=scope.entity_id, role=membership.role if membership else None)
    root_edit = same_account and await user_is_effective_entity_admin(db, scoped_actor)
    local_edit = False
    can_view = same_account
    if scope.kind == 'personal':
        can_view = same_account and scope.id == actor.id
        local_edit = can_view
    elif scope.kind == 'workspace':
        workspace = (await db.execute(select(Workspace).where(Workspace.id == scope.id,
            Workspace.entity_id == scope.entity_id, Workspace.deleted_at.is_(None)))).scalar_one_or_none()
        can_view = bool(same_account and workspace and await user_can_read_workspace(db, workspace=workspace, user=scoped_actor))
        local_edit = can_view and await user_can_manage_workspace(db, workspace_id=scope.id, user_id=actor.id, entity_role=scoped_actor.role)
    elif scope.kind == 'room':
        query = select(ChatRoom, ChatRoomMember).join(ChatRoomMember, ChatRoomMember.room_id == ChatRoom.id).where(
            ChatRoom.id == scope.id, ChatRoom.status == 'active', ChatRoomMember.user_id == actor.id, ChatRoomMember.status == 'active').execution_options(populate_existing=True)
        if lock:
            query = query.with_for_update(read=True)
        row = (await db.execute(query)).one_or_none()
        can_view = row is not None
        if row:
            room, member = row
            local_edit = room.kind == 'group' and member.role == 'owner' and (
                room.owner_entity_id == actor.entity_id if room.owner_entity_id else room.owner_user_id == actor.id)
    if not can_view:
        raise ApprovalPolicyAccessError('Approval settings are unavailable')
    return {'can_view': True, 'can_edit_root': bool(root_edit), 'can_edit_local': bool(local_edit),
        'can_approve_once': False, 'can_grant_run': False, 'can_save_rule': bool(root_edit if scope.kind == 'account' else local_edit)}


def _response(chain: ResolvedPolicyChain, capabilities: dict) -> dict:
    snapshot = chain.local or chain.root
    def rows(owner, inherited=False):
        return [{**rule.model_dump(), 'scope': owner.scope.model_dump(), 'editable': not inherited and rule.source_kind == 'user',
                 'deletable': not inherited and rule.source_kind == 'user',
                 'source_link': f'/workspaces/{owner.scope.id}?tab=rules' if rule.source_kind == 'workspace_rule' else None}
                for rule in owner.policy.rules]
    return {**snapshot.model_dump(), 'capabilities': capabilities, 'action_catalog': ACTION_CATALOG,
        'inherited_mode': chain.root.policy.mode if chain.local else None,
        'saved_rules': rows(snapshot), 'inherited_rules': rows(chain.root, True) if chain.local else [],
        'rule_sources': [*rows(chain.root, bool(chain.local)), *(rows(chain.local) if chain.local else [])],
        'effective_rows': [{**item, 'decision': evaluate_policy(chain, ActionFacts(**{key: item[key] for key in ('action_key', 'category', 'impact')})).model_dump()} for item in ACTION_CATALOG]}


async def _approval_review(db: AsyncSession, scope: ApprovalScope) -> dict | None:
    """The approval conversion's review marker (``settings.approval_review``), if any."""
    if scope.kind != 'workspace':
        return None
    settings = (await db.execute(select(Workspace.settings).where(
        Workspace.id == scope.id, Workspace.entity_id == scope.entity_id))).scalar_one_or_none()
    review = (settings or {}).get('approval_review') if isinstance(settings, dict) else None
    return review if isinstance(review, dict) and review.get('required') is True else None


async def _clear_approval_review(db: AsyncSession, scope: ApprovalScope) -> None:
    """An explicit save of this Workspace's approval settings is the owner's review."""
    if scope.kind != 'workspace':
        return
    # The Workspace row is already locked by ``_load(lock=True)``.
    settings = (await db.execute(select(Workspace.settings).where(
        Workspace.id == scope.id, Workspace.entity_id == scope.entity_id))).scalar_one_or_none()
    if isinstance(settings, dict) and 'approval_review' in settings:
        await db.execute(update(Workspace).where(Workspace.id == scope.id, Workspace.entity_id == scope.entity_id)
            .values(settings={key: value for key, value in settings.items() if key != 'approval_review'})
            .execution_options(synchronize_session='fetch'))


async def read_approval_settings(db: AsyncSession, scope: ApprovalScope, actor: User) -> dict:
    capabilities = await _capabilities(db, scope, actor)
    chain, _ = await _load(db, scope)
    return {**_response(chain, capabilities), 'approval_review': await _approval_review(db, scope)}


def apply_draft(chain: ResolvedPolicyChain, draft: PolicyDraft) -> ResolvedPolicyChain:
    snapshot = chain.local or chain.root
    if snapshot.scope.kind == 'account' and draft.mode is None:
        raise ValueError('Account defaults must specify an approval mode')
    existing = {rule.id: rule for rule in snapshot.policy.rules}
    seen = set()
    rules = [rule for rule in snapshot.policy.rules if rule.source_kind != 'user']
    for item in draft.user_rules:
        if item.id:
            if item.id in seen or item.id not in existing or existing[item.id].source_kind != 'user':
                raise ValueError('Rule ID is not an editable rule in this scope')
            seen.add(item.id)
        rules.append(ApprovalRule(id=item.id or generate_ulid(), effect=item.effect, selector=item.selector))
    changed = snapshot.model_copy(deep=True)
    changed.policy = (ApprovalPolicy if snapshot.scope.kind == 'account' else ApprovalPolicyOverride)(mode=draft.mode, rules=rules)
    if chain.local:
        changed.effective_policy = ApprovalPolicy(mode=draft.mode or chain.root.policy.mode, rules=[*chain.root.policy.rules, *rules])
        changed.inheritance = 'custom' if draft.mode is not None or rules else 'following'
        return ResolvedPolicyChain(root=chain.root, local=changed)
    changed.effective_policy = changed.policy
    return ResolvedPolicyChain(root=changed)


async def preview_approval_policy(db: AsyncSession, scope: ApprovalScope, draft: PolicyDraft,
                                   expected_version: str, actor: User) -> dict:
    capabilities = await _capabilities(db, scope, actor)
    if not capabilities['can_save_rule']:
        raise ApprovalPolicyAccessError('You cannot edit these approval settings')
    chain, _ = await _load(db, scope)
    current = _response(chain, capabilities)
    if expected_version != current['version']:
        raise ApprovalPolicyConflict(current)
    proposed = apply_draft(chain, draft)
    before_rules = {rule.id for rule in (chain.local or chain.root).policy.rules}
    after_rules = {rule.id for rule in (proposed.local or proposed.root).policy.rules}
    return {'before': current, 'after': _response(proposed, capabilities),
        'rules_added': len(after_rules - before_rules), 'rules_removed': len(before_rules - after_rules),
        'representative_only': True, 'samples': [{**item,
            'before': evaluate_policy(chain, ActionFacts(**{key: item[key] for key in ('action_key', 'category', 'impact')})).model_dump(),
            'after': evaluate_policy(proposed, ActionFacts(**{key: item[key] for key in ('action_key', 'category', 'impact')})).model_dump()}
            for item in ACTION_CATALOG]}


async def update_approval_policy(db: AsyncSession, scope: ApprovalScope, draft: PolicyDraft,
                                  expected_version: str, actor: User) -> dict:
    # Fixed order: root Entity, local owner, current membership/authority.
    chain, owner = await _load(db, scope, lock=True)
    capabilities = await _capabilities(db, scope, actor, lock=True)
    if not capabilities['can_save_rule']:
        raise ApprovalPolicyAccessError('You cannot edit these approval settings')
    current = _response(chain, capabilities)
    if expected_version != current['version']:
        raise ApprovalPolicyConflict(current)
    proposed = apply_draft(chain, draft)
    before = chain.local or chain.root
    after = proposed.local or proposed.root
    await _clear_approval_review(db, scope)
    if before.policy == after.policy:
        return {**current, 'approval_review': None}
    revision = before.revision + 1
    policy = after.policy.model_dump()
    if scope.kind == 'account':
        await db.execute(update(Entity).where(Entity.id == scope.id).values(settings={**owner, 'approval_defaults': {'revision': revision, 'policy': policy}}))
    elif scope.kind == 'personal':
        branches = {**(owner or {}).get('approval_policy', {}), scope.entity_id: {'revision': revision, 'root_entity_id': scope.entity_id, 'policy': policy}}
        await db.execute(update(User).where(User.id == scope.id).values(preferences={**(owner or {}), 'approval_policy': branches}).execution_options(synchronize_session='fetch'))
    elif scope.kind == 'room':
        await db.execute(update(ChatRoom).where(ChatRoom.id == scope.id).values(approval_policy={'revision': revision, 'root_entity_id': scope.entity_id, 'policy': policy}))
    else:
        from packages.core.governance.policy import policy_from_dict
        from packages.core.governance.service import update_policy
        persisted = {**owner.policy, 'approval': {'root_entity_id': scope.entity_id, 'policy': policy}}
        saved = await update_policy(db, entity_id=scope.entity_id, workspace_id=scope.id,
            policy=policy_from_dict(persisted), changed_by=actor.id,
            change_summary='Updated execution and approval settings')
        revision = saved.revision
    db.add(AuditLog(entity_id=scope.entity_id, user_id=actor.id, action='approval_policy.updated', resource_type=scope.kind,
        resource_id=scope.id, details={'before': before.policy.model_dump(), 'after': policy,
            'previous_version': before.version, 'revision': revision, 'root_revision': chain.root.revision}))
    await db.flush()
    if scope.kind == 'account':
        _queue_root_changed(db, scope.entity_id, revision)
    updated, _ = await _load(db, scope)
    return {**_response(updated, capabilities), 'approval_review': await _approval_review(db, scope)}


async def initialize_workspace_approval(db: AsyncSession, workspace: Workspace) -> None:
    """Only called from creation transactions, never by execution/read paths."""
    row = (await db.execute(select(GovernancePolicy).where(GovernancePolicy.workspace_id == workspace.id))).scalar_one_or_none()
    if row is None:
        binding = ApprovalPolicyBinding(root_entity_id=workspace.entity_id).model_dump()
        db.add(GovernancePolicy(workspace_id=workspace.id, entity_id=workspace.entity_id, revision=1, policy={'approval': binding}))
        db.add(GovernanceRevision(workspace_id=workspace.id, revision=1, policy={'approval': binding}, change_summary='Follow account approval defaults'))
        await db.flush()
    elif not isinstance((row.policy or {}).get('approval'), dict):
        raise ApprovalPolicyIntegrityError('New workspace contains an obsolete approval policy')


async def initialize_personal_approval(db: AsyncSession, *, user_id: str, entity_id: str) -> None:
    # Creation only: root must already be initialized by account creation.
    root_query = select(Entity.settings).where(Entity.id == entity_id).with_for_update()
    root = (await db.execute(root_query)).scalar_one_or_none()
    if not isinstance(root, dict) or 'approval_defaults' not in root:
        raise ApprovalPolicyIntegrityError('Account approval defaults are missing')
    prefs = (await db.execute(select(User.preferences).where(User.id == user_id).with_for_update())).scalar_one_or_none() or {}
    branches = dict(prefs.get('approval_policy') or {})
    if entity_id not in branches:
        branches[entity_id] = local_defaults(entity_id)
        await db.execute(update(User).where(User.id == user_id).values(preferences={**prefs, 'approval_policy': branches}).execution_options(synchronize_session='fetch'))
        await db.flush()


async def initialize_room_approval(db: AsyncSession, room: ChatRoom) -> None:
    root_id = room.owner_entity_id
    if not root_id:
        root_id = (await db.execute(select(User.entity_id).where(User.id == room.owner_user_id,
            User.deleted_at.is_(None)))).scalar_one_or_none()
    if not root_id:
        raise ApprovalPolicyIntegrityError('Room owner is unavailable')
    room.approval_policy = local_defaults(root_id)


async def list_approval_overrides(db: AsyncSession, *, entity_id: str, actor: User, limit: int = 50, offset: int = 0) -> dict:
    root_scope = ApprovalScope(kind='account', id=entity_id, entity_id=entity_id)
    await _capabilities(db, root_scope, actor)
    candidates = [(ApprovalScope(kind='personal', id=actor.id, entity_id=entity_id), 'Personal chat')]
    workspaces = (await db.execute(select(Workspace).where(Workspace.entity_id == entity_id, Workspace.deleted_at.is_(None)))).scalars().all()
    candidates.extend((ApprovalScope(kind='workspace', id=row.id, entity_id=entity_id), row.name) for row in workspaces)
    rooms = (await db.execute(select(ChatRoom).join(ChatRoomMember, ChatRoomMember.room_id == ChatRoom.id).where(
        ChatRoom.status == 'active', ChatRoom.kind == 'group', ChatRoomMember.user_id == actor.id, ChatRoomMember.status == 'active'))).scalars().all()
    candidates.extend((ApprovalScope(kind='room', id=row.id, entity_id=entity_id), row.title or 'Group chat')
        for row in rooms if isinstance(row.approval_policy, dict) and row.approval_policy.get('root_entity_id') == entity_id)
    items = []
    following = 0
    for scope, label in candidates:
        try:
            snapshot = await read_approval_settings(db, scope, actor)
        except ApprovalPolicyAccessError:
            continue
        if snapshot['inheritance'] == 'custom':
            items.append({'scope': scope.model_dump(), 'label': label, 'snapshot': snapshot})
        else:
            following += 1
    return {'items': items[offset:offset + limit], 'total': len(items), 'limit': limit, 'offset': offset,
        'following_count': following, 'custom_count': len(items)}


_ROOT_EVENTS = 'approval_root_events'
_ROOT_LISTENERS = 'approval_root_event_listeners'
_EVENT_TASKS: set[asyncio.Task] = set()


async def _publish_root_changed(entity_id: str, revision: int) -> None:
    from packages.core.cache import cache
    from packages.core.services.realtime import _broadcast
    try:
        await cache.delete(f'entity:{entity_id}')
        await cache.delete(f'settings:{entity_id}')
        await _broadcast(entity_id, 'approval_defaults_changed', {'entity_id': entity_id, 'revision': revision})
    except Exception:
        logging.getLogger(__name__).exception('Approval defaults notification failed')


def _publish_root_events(session) -> None:
    if session.in_nested_transaction():
        return
    for loop, entity_id, revision, _owner in session.info.pop(_ROOT_EVENTS, []):
        if not loop.is_closed():
            task = loop.create_task(_publish_root_changed(entity_id, revision))
            _EVENT_TASKS.add(task)
            task.add_done_callback(_EVENT_TASKS.discard)


def _discard_root_events(session) -> None:
    if not session.in_nested_transaction():
        session.info.pop(_ROOT_EVENTS, None)


def _discard_nested_root_events(session, transaction) -> None:
    if not transaction.nested:
        return
    def belongs(owner):
        while owner is not None:
            if owner is transaction:
                return True
            owner = owner.parent
        return False
    session.info[_ROOT_EVENTS] = [item for item in session.info.get(_ROOT_EVENTS, []) if not belongs(item[3])]


def _queue_root_changed(db: AsyncSession, entity_id: str, revision: int) -> None:
    session = db.sync_session
    session.info.setdefault(_ROOT_EVENTS, []).append((asyncio.get_running_loop(), entity_id, revision, session.get_nested_transaction()))
    if not session.info.get(_ROOT_LISTENERS):
        event.listen(session, 'after_commit', _publish_root_events)
        event.listen(session, 'after_rollback', _discard_root_events)
        event.listen(session, 'after_soft_rollback', _discard_nested_root_events)
        session.info[_ROOT_LISTENERS] = True
