"""Atomic one-time approval plus a server-derived local policy rule.

Only ordinary Task-runtime requests with a signed durable checkpoint expose
this command. Other origins keep their existing one-time decision transport.
"""
from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.governance.approval_policy import ActionSelector, ApprovalScope, ApprovalTarget, PolicyDraft, ResourceSelector, RuleDraft
from packages.core.governance import approval_policy_service as policies
from packages.core.models.audit import AuditLog
from packages.core.models.hitl_request import HitlRequest
from packages.core.models.task import Conversation
from packages.core.models.user import User
from packages.core.services.runtime_authorization import authorize_hitl_resolution
from packages.core.services.task_runtime_approvals import (
    TASK_RUNTIME_CHECKPOINT_CONTEXT_KEY,
    ensure_task_runtime_approval_resolvable,
    lock_task_runtime_approval_boundary,
    stage_task_runtime_approval_resolution,
    task_runtime_approval_binding_digest,
)


class ApprovalCommandError(ValueError):
    pass


class ApprovalCommandConflict(ApprovalCommandError):
    pass


class HitlRequestRef(BaseModel):
    model_config = ConfigDict(extra='forbid')
    kind: Literal['hitl']
    id: str = Field(min_length=1, max_length=128)


class ApproveAndSaveRuleCommand(BaseModel):
    model_config = ConfigDict(extra='forbid', populate_by_name=True)
    request: HitlRequestRef
    choice: Literal['approve_and_save_rule']
    expected_digest: str = Field(alias='expectedDigest', min_length=1, max_length=128)
    idempotency_key: str = Field(alias='idempotencyKey', min_length=8, max_length=128)
    expected_policy_version: str = Field(alias='expectedPolicyVersion', min_length=1, max_length=128)
    option_id: str = Field(alias='optionId', min_length=1, max_length=128)


def request_digest(request: HitlRequest) -> str:
    binding = task_runtime_approval_binding_digest(request)
    context = request.context or {}
    payload = [binding, context.get('operation_id'), context.get('approval_requirements'),
        context.get('_approval_resource_ids'), context.get('approval_account_id'),
        (context.get('policy_evaluation') or {}).get('target_decisions')]
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _ordinary_task_request(request: HitlRequest) -> bool:
    context = request.context or {}
    return bool(request.origin_kind == 'tool_call' and request.origin_task_id
        and request.origin_conversation_id and request.hitl_type == 'authorize'
        and isinstance(context.get(TASK_RUNTIME_CHECKPOINT_CONTEXT_KEY), dict)
        and context.get('operation_id') and isinstance(context.get('approval_requirements'), list)
        and context.get('kind') != 'provider'
        and not any('coordinator' in key or 'task_comment' in key for key in context)
        and isinstance(context.get('continuation'), dict)
        and request.action_key)


def _stored_targets(request: HitlRequest) -> list[ApprovalTarget]:
    """Use the complete server evaluation, never one display resource as a set."""
    context = request.context or {}
    decisions = (context.get('policy_evaluation') or {}).get('target_decisions')
    if not isinstance(decisions, list) or not decisions:
        return []
    try:
        targets = [ApprovalTarget.model_validate(item['target']) for item in decisions]
    except (ValueError, KeyError, TypeError):
        return []
    declared = context.get('_approval_resource_ids')
    if declared and set(declared) != {target.resource_id for target in targets}:
        return []
    return targets


def _rule_options(request: HitlRequest) -> list[dict]:
    if not _ordinary_task_request(request):
        return []
    try:
        selectors = [ActionSelector(action_key=request.action_key)]
        targets = _stored_targets(request)
        identities = {(target.account_id, target.resource_kind) for target in targets}
        if targets and len(identities) == 1:
            account_id, resource_kind = next(iter(identities))
            if account_id or resource_kind in ('file', 'knowledge_file'):
                selectors.insert(0, ResourceSelector(action_key=request.action_key, account_id=account_id,
                    resource_kind=resource_kind, resource_ids=[target.resource_id for target in targets]))
    except ValueError:
        return []
    options = []
    for selector in selectors:
        rule = RuleDraft(effect='allow', selector=selector).model_dump(exclude_none=True)
        option_id = hashlib.sha256(json.dumps(rule, sort_keys=True).encode()).hexdigest()
        options.append({'id': option_id, 'rule': rule,
            'label': f'Only these {selector.resource_kind} targets: {", ".join(selector.resource_ids)}' if selector.kind == 'resource' else f'All targets for {request.action_key}',
            'coverage': 'exact_resource' if selector.kind == 'resource' else 'all_targets_for_action',
            'action_key': request.action_key})
    return options


async def _request(db, request_id, actor, *, lock=False):
    query = select(HitlRequest).where(HitlRequest.id == request_id, HitlRequest.entity_id == actor.entity_id)
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    request = (await db.execute(query)).scalar_one_or_none()
    if request is None:
        raise policies.ApprovalPolicyAccessError('Approval request is unavailable')
    authority = await authorize_hitl_resolution(db, request=request, by_user_id=actor.id, standing=False)
    if not authority.allowed:
        raise policies.ApprovalPolicyAccessError(authority.reason or 'You cannot decide this approval')
    return request


async def _local_scope(db, request: HitlRequest, actor: User) -> ApprovalScope:
    if request.workspace_id:
        return await policies.resolve_scope(db, entity_id=request.entity_id, workspace_id=request.workspace_id)
    conversation = (await db.execute(select(Conversation.user_id, Conversation.entity_id).where(
        Conversation.id == request.origin_conversation_id))).one_or_none()
    if not conversation or conversation.entity_id != request.entity_id or conversation.user_id != actor.id:
        raise policies.ApprovalPolicyAccessError('Personal approval settings belong to the conversation owner')
    return ApprovalScope(kind='personal', id=conversation.user_id, entity_id=conversation.entity_id)


async def project_approval_command(db: AsyncSession, *, request_id: str, actor: User) -> dict:
    request = await _request(db, request_id, actor)
    result = {'request_ref': {'kind': 'hitl', 'id': request.id}, 'status': request.status, 'digest': request_digest(request),
        'available_decisions': ['approve_once', 'reject'] if request.status == 'pending' else [],
        'grant_options': [], 'rule_options': [], 'policy_versions': [],
        'capabilities': {'can_approve_once': request.status == 'pending', 'can_save_rule': False, 'can_grant_run': False}}
    if request.status != 'pending' or not _ordinary_task_request(request):
        return result
    scope = await _local_scope(db, request, actor)
    snapshot = await policies.read_approval_settings(db, scope, actor)
    result.update(scope=scope.model_dump(), policy_versions=snapshot['policy_versions'], policy_version=snapshot['version'])
    if not snapshot['capabilities']['can_save_rule']:
        return result
    authority = await authorize_hitl_resolution(db, request=request, by_user_id=actor.id, standing=True)
    if not authority.allowed:
        return result
    try:
        if not await ensure_task_runtime_approval_resolvable(db, request=request):
            return result
    except RuntimeError:
        return result
    options = _rule_options(request)
    if options:
        result['available_decisions'].append('approve_and_save_rule')
        result['rule_options'] = options
        result['capabilities']['can_save_rule'] = True
    return result


def _command_fingerprint(command: ApproveAndSaveRuleCommand) -> str:
    payload = command.model_dump(exclude={'idempotency_key'})
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


async def approve_and_save_rule(db: AsyncSession, *, command: ApproveAndSaveRuleCommand, actor: User) -> dict:
    initial = await _request(db, command.request.id, actor)
    if not _ordinary_task_request(initial):
        raise ApprovalCommandError('This origin does not support atomic approval and rule saving')
    # Match Task cancellation/resume lock order before locking policy owners.
    await lock_task_runtime_approval_boundary(db, task_id=initial.origin_task_id)
    scope = await _local_scope(db, initial, actor)
    async with db.begin_nested():
        await policies._load(db, scope, lock=True)
        request = await _request(db, command.request.id, actor, lock=True)
        if await _local_scope(db, request, actor) != scope:
            raise ApprovalCommandConflict('Approval owner changed')
        fingerprint = _command_fingerprint(command)
        previous = (request.context or {}).get('approval_command')
        if previous:
            if previous.get('idempotency_key') != command.idempotency_key or previous.get('fingerprint') != fingerprint or previous.get('actor_id') != actor.id:
                raise ApprovalCommandConflict('This approval already has a different decision')
            return {**previous['result'], 'policy': await policies.read_approval_settings(db, scope, actor), 'idempotent': True}
        if request.status != 'pending' or request_digest(request) != command.expected_digest:
            raise ApprovalCommandConflict('The approval changed; review it again')
        if not _ordinary_task_request(request):
            raise ApprovalCommandConflict('The exact approval continuation is no longer available')
        authority = await authorize_hitl_resolution(db, request=request, by_user_id=actor.id, standing=True)
        if not authority.allowed:
            raise policies.ApprovalPolicyAccessError(authority.reason or 'You cannot save a standing approval rule')
        option = next((item for item in _rule_options(request) if item['id'] == command.option_id), None)
        if option is None:
            raise ApprovalCommandConflict('The offered approval rule changed')
        if not await ensure_task_runtime_approval_resolvable(db, request=request):
            raise ApprovalCommandError('The origin has no durable resume transaction')
        from packages.core.ai.runtime.approval_messages import runtime_approval_runtime_metadata
        continuation = request.context['continuation']
        metadata = runtime_approval_runtime_metadata({'tool': request.context.get('tool'), 'continuation': continuation}, request.id)
        if metadata is None:
            raise ApprovalCommandError('Exact tool continuation is unavailable')
        current = await policies.read_approval_settings(db, scope, actor)
        rules = [RuleDraft(id=rule['id'], effect=rule['effect'], selector=rule['selector'])
            for rule in current['policy']['rules'] if rule['source_kind'] == 'user']
        selected = RuleDraft.model_validate(option['rule'])
        if not any(rule.effect == selected.effect and rule.selector == selected.selector for rule in rules):
            rules.append(selected)
        saved = await policies.update_approval_policy(db, scope, PolicyDraft(mode=current['policy']['mode'], user_rules=rules), command.expected_policy_version, actor)
        # A local allow cannot override a live root/local deny.
        from packages.core.governance.approval_facts import action_facts
        from packages.core.governance.approval_policy import evaluate_policy
        chain, _ = await policies._load(db, scope)
        facts = action_facts(action_key=request.action_key, capability_id=request.capability_id,
            resource_kind=request.resource_kind, resource_id=request.resource_id,
            account_id=request.context.get('approval_account_id'), risk_level=request.risk_level,
            resource_ids=tuple(request.context.get('_approval_resource_ids') or ()))
        targets = _stored_targets(request)
        if targets:
            facts = facts.model_copy(update={'targets': targets})
        if evaluate_policy(chain, facts).outcome == 'deny':
            raise ApprovalCommandConflict('A current policy prohibits this action')
        from packages.core.governance.approvals import grant_approval
        await grant_approval(db, request, by_user_id=actor.id, via='approval_and_save_rule', standing=False)
        resume = await stage_task_runtime_approval_resolution(db, request=request, choice='approve', runtime_metadata=metadata)
        if not resume:
            raise ApprovalCommandError('The origin has no durable resume transaction')
        from packages.core.ai.runtime.approval_store import mark_runtime_hitl_request_resolved
        await mark_runtime_hitl_request_resolved(db, conversation_id=request.origin_conversation_id, hitl_id=request.id, choice='approve')
        result = {'request_ref': {'kind': 'hitl', 'id': request.id}, 'status': request.status,
            'digest': command.expected_digest, 'changed_scope': scope.model_dump(),
            'policy_versions': saved['policy_versions'], 'policy_version': saved['version'], **resume}
        request.context = {**request.context, 'approval_command': {'idempotency_key': command.idempotency_key,
            'fingerprint': fingerprint, 'actor_id': actor.id, 'result': result}}
        db.add(AuditLog(entity_id=scope.entity_id, user_id=actor.id, action='approval.approved_and_rule_saved',
            resource_type='hitl_request', resource_id=request.id, details={'scope': scope.model_dump(),
                'request_digest': command.expected_digest, 'option_id': command.option_id, 'idempotency_key': command.idempotency_key}))
        await db.flush()
        return {**result, 'policy': saved, 'idempotent': False}
