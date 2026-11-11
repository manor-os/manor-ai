"""One-shot, source-fenced approval data migration; never an online fallback."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os

from sqlalchemy import MetaData, Table, inspect, select, update

from packages.core.governance.approval_policy import ApprovalPolicy, ApprovalPolicyBinding, ApprovalPolicyOverride
from packages.core.governance.approval_policy_migration import (
    LEGACY_POLICY_KEYS, LegacyApprovalConversionError, normalize_legacy_workspace_policy, source_digest,
)
from packages.core.governance.policy import policy_from_dict
from packages.core.models.base import generate_ulid

_COLUMNS = {
    'entities': ['id', 'settings'],
    'users': ['id', 'entity_id', 'preferences'],
    'user_memberships': ['user_id', 'entity_id'],
    'workspaces': ['id', 'entity_id', 'settings', 'operating_model'],
    'governance_policies': ['workspace_id', 'entity_id', 'policy', 'revision'],
    'governance_revisions': ['id', 'workspace_id', 'revision'],
    'chat_rooms': ['id', 'owner_entity_id', 'owner_user_id', 'approval_policy', 'ai_file_actions_direct_enabled'],
    'chat_room_tool_approvals': ['id', 'room_id', 'run_id', 'requester_user_id', 'binding_id', 'audience_fingerprint', 'ai_tool_execution_revision', 'tool_call_id', 'tool_name', 'arguments_hash', 'status', 'approval_requirements', 'decided_by_user_id', 'decided_at', 'claimed_at', 'finished_at', 'result_document_id', 'result_media_job_id'],
    'hitl_requests': ['id', 'entity_id', 'workspace_id', 'action_key', 'capability_id', 'resource_kind', 'resource_id', 'risk_level', 'origin_kind', 'origin_step_id', 'origin_plan_id', 'origin_task_id', 'origin_conversation_id', 'origin_message_id', 'status', 'hitl_type', 'dedup_key', 'context', 'payload', 'reason', 'matched_rule', 'resolved_reason', 'decided_by_user_id', 'decided_at', 'decided_via', 'consumed_at'],
    'execution_steps': ['id', 'entity_id', 'workspace_id', 'plan_id', 'action_key', 'params', 'approval_operation_revision', 'step_status'],
    'execution_plans': ['id', 'entity_id', 'workspace_id', 'task_id', 'status'],
    'tasks': ['id', 'entity_id', 'workspace_id', 'status', 'details'],
    'conversations': ['id', 'entity_id', 'workspace_id', 'scope', 'metadata'],
    'workspace_drafts': ['id', 'entity_id', 'status', 'fields'],
    'workspace_blueprint_upgrade_states': ['workspace_id', 'entity_id', 'revision', 'baseline', 'restore_point'],
}
_OLD_PREFS = ('runtime_approval_policy', 'ai_file_permission', 'file_permission', 'file_permissions')
_LEGACY_RULE_KEYS = ('action_patterns', 'actions', 'patterns', 'capability_patterns', 'capabilities', 'capability_ids')
_SOURCE_CONFIGURATION_KEYS = ('operating_model', '_blueprint_operating_model')
_APPROVAL_RULE_TYPES = frozenset({'auto_approve', 'allow', 'exception', 'approval_required', 'hitl_required',
    'require_approval', 'review_required', 'deny', 'never_allow', 'block', 'draft_only'})
_EXPIRE_UNVERIFIABLE_REQUEST = {'status': 'expired', 'resolved_reason': 'approval_migration_unverifiable'}
_EXPIRE_FAILED_TASK_REQUEST = {'status': 'expired', 'resolved_reason': 'approval_migration_failed_task_request'}
_EXPIRE_UNIDENTIFIED_FAILED_STEP_REQUEST = {'status': 'expired', 'resolved_reason': 'approval_migration_unidentified_failed_step'}
_EXPIRE_LEGACY_CHROME_GRANT = {'status': 'expired', 'resolved_reason': 'approval_migration_provider_outcome_unverified'}
_INVALIDATE_UNVERIFIABLE_ROOM_REQUEST = {'status': 'invalidated', 'resolved_reason': 'approval_migration_unverifiable'}
NEW_SEMANTICS_PLAN_MODES = ('new_semantics_conservative_v1', 'new_semantics_conservative_v2',
                            'new_semantics_conservative_v3')


def _can_retire_legacy_chrome_grant(request, source):
    """Revoke old consent only; missing receipts never prove no external effect."""
    context = request.get('context') or {}
    if (not isinstance(context, dict) or context.get('kind') != 'provider'
            or context.get('provider') != 'chrome' or request.get('status') != 'granted'
            or request.get('hitl_type') != 'authorize' or request.get('origin_kind') != 'tool_call'
            or request.get('consumed_at') or any(request.get(key) for key in ('origin_task_id', 'origin_plan_id', 'origin_step_id'))
            or any(key in context for key in ('provider_action_claim', 'provider_confirmation_receipt', 'approval_requirements', 'operation_id'))):
        return False
    provider_id = context.get('provider_approval_id')
    if not isinstance(provider_id, str) or not provider_id or request.get('dedup_key') != f'provider:chrome:{provider_id}':
        return False
    entity = request.get('entity_id')
    actor = context.get('requested_by')
    if not entity or not actor or not any(row['id'] == entity for row in source['entities']):
        return False
    owner = next((row for row in source['users'] if row['id'] == actor), None)
    workspace = request.get('workspace_id')
    return bool(workspace and any(row['id'] == workspace and row['entity_id'] == entity for row in source['workspaces'])
        and owner and (owner.get('entity_id') == entity or any(
        row['user_id'] == actor and row['entity_id'] == entity for row in source['user_memberships']))
        and any(row['id'] == request.get('origin_conversation_id') and row['entity_id'] == entity
                and row.get('workspace_id') == workspace and row.get('scope') == 'workspace_main'
                for row in source.get('conversations', [])))


def read_sources(connection) -> dict:
    metadata = MetaData()
    available = set(inspect(connection).get_table_names())
    result = {}
    for name, wanted in _COLUMNS.items():
        if name not in available:
            result[name] = []
            continue
        table = Table(name, metadata, autoload_with=connection)
        rows = connection.execute(select(*(table.c[key] for key in wanted if key in table.c))).mappings().all()
        result[name] = sorted(({key: (0 if name == 'execution_steps' and key == 'approval_operation_revision' and key not in table.c else deepcopy(row.get(key))) for key in wanted} for row in rows), key=lambda row: str(row.get('id') or row.get('workspace_id') or (row.get('user_id'), row.get('entity_id'))))
    return result


def _personal_override(preferences: dict) -> dict:
    if preferences.get('runtime_approval_policy'):
        raise LegacyApprovalConversionError([{'reason': 'personal runtime approval rules require exact action/account conversion'}])
    values = [preferences[key] for key in ('ai_file_permission', 'file_permission', 'file_permissions') if preferences.get(key) is not None]
    if len(values) > 1:
        raise LegacyApprovalConversionError([{'reason': 'multiple personal file permission sources require explicit resolution'}])
    value = values[0] if values else 'approval'
    if isinstance(value, dict):
        value = value.get('mode') or value.get('policy')
    normalized = str(value).strip().lower().replace('-', '_')
    effect = {'approval': 'ask', 'approve': 'ask', 'ask': 'ask', 'ask_each_time': 'ask', 'manual': 'ask',
        'always_approve': 'allow', 'always approve': 'allow', 'always_approval': 'allow', 'allow': 'allow', 'allowed': 'allow',
        'deny': 'deny', 'denied': 'deny', 'never': 'deny', 'block': 'deny', 'blocked': 'deny'}.get(normalized)
    if effect is None:
        raise LegacyApprovalConversionError([{'reason': 'unknown personal file permission mode'}])
    # The old personal file gate covered file mutations, including deletion.
    return ApprovalPolicyOverride.model_validate({'mode': None, 'rules': [
        {'id': f'migration-personal-{category}', 'effect': effect, 'selector': {'kind': 'category', 'category': category}, 'source_kind': 'user'}
        for category in ('files', 'deletion')]}).model_dump()


def _valid_approval_review(value) -> bool:
    return (isinstance(value, dict) and set(value) == {'required', 'reasons', 'plan_mode'}
            and type(value['required']) is bool and isinstance(value['reasons'], list)
            and all(isinstance(reason, dict) for reason in value['reasons'])
            and isinstance(value['plan_mode'], str) and bool(value['plan_mode']))


def _has_legacy_approval(value) -> bool:
    if isinstance(value, dict):
        return bool(set(value) & LEGACY_POLICY_KEYS) or any(_has_legacy_approval(item) for item in value.values())
    return isinstance(value, list) and any(_has_legacy_approval(item) for item in value)


def _require_typed_rules(rules):
    for rule in rules or []:
        if (isinstance(rule, dict) and (not rule.get('rule_type')
                or str(rule.get('rule_type') or '').strip().lower() in _APPROVAL_RULE_TYPES)
                and any(rule.get(key) for key in _LEGACY_RULE_KEYS)):
            raise LegacyApprovalConversionError([{'reason': 'source rule requires approval_selectors', 'rule_key': rule.get('rule_key')}])


def _require_typed_source_rules(value):
    if isinstance(value, dict):
        if isinstance(value.get('rules'), list):
            _require_typed_rules(value['rules'])
        for key in _SOURCE_CONFIGURATION_KEYS:
            if isinstance(value.get(key), dict):
                _require_typed_source_rules(value[key])


def _require_typed_restore_rules(restore):
    if not isinstance(restore, dict):
        return
    _require_typed_source_rules(restore)
    baseline = restore.get('baseline_before') or {}
    configuration = baseline.get('configuration', baseline) if isinstance(baseline, dict) else {}
    for entry in (configuration.get('entries') or {}).values():
        if entry.get('kind') == 'workspace_configuration' and entry.get('path') == ['operating_model']:
            for name in ('source_content', 'installed_content', 'blueprint_content'):
                content = entry.get(name)
                if isinstance(content, dict):
                    _require_typed_source_rules(content.get('value'))
        if entry.get('kind') == 'workspace_configuration' and (entry.get('path') or [])[:2] == ['operating_model', 'rules']:
            for name in ('source_content', 'installed_content', 'blueprint_content'):
                content = entry.get(name)
                if isinstance(content, dict):
                    _require_typed_rules(content.get('value'))
    for entry in restore.get('configuration') or []:
        target = entry.get('target') or {}
        if target.get('kind') == 'workspace_configuration' and target.get('path') == ['operating_model']:
            for content in [entry.get('before'), entry.get('after'), *(target.get(name) for name in ('source_content', 'installed_content', 'blueprint_content'))]:
                if isinstance(content, dict):
                    _require_typed_source_rules(content.get('value'))
        if target.get('kind') == 'workspace_configuration' and (target.get('path') or [])[:2] == ['operating_model', 'rules']:
            for content in [entry.get('before'), entry.get('after'), *(target.get(name) for name in ('source_content', 'installed_content', 'blueprint_content'))]:
                if isinstance(content, dict):
                    _require_typed_rules(content.get('value'))


def _convert_task_rules(rules):
    if not isinstance(rules, list):
        raise LegacyApprovalConversionError([{'reason': 'Source approval rules must be a list'}])
    result = deepcopy(rules)
    for rule in result:
        if not isinstance(rule, dict):
            continue
        rule_type = str(rule.get('rule_type') or '').strip().lower()
        effect = ('never_allow' if rule_type in ('deny', 'never_allow', 'block', 'draft_only') else
            'hitl_required' if rule_type in ('approval_required', 'hitl_required', 'require_approval', 'review_required') else
            'auto_approve' if rule_type in ('auto_approve', 'allow', 'exception') else None)
        if effect is None:
            continue  # Guidance and other Task metadata are not consent rules.
        if 'approval_selectors' in rule:
            _require_typed_rules([rule])
            from packages.core.governance.approval_policy import RuleDraft
            if not isinstance(rule['approval_selectors'], list):
                raise LegacyApprovalConversionError([{'reason': 'approval_selectors must be a list', 'rule_key': rule.get('rule_key')}])
            for selector in rule['approval_selectors']:
                RuleDraft(effect={'never_allow': 'deny', 'hitl_required': 'ask', 'auto_approve': 'allow'}[effect], selector=selector)
            continue
        for aliases in (_LEGACY_RULE_KEYS[:3], _LEGACY_RULE_KEYS[3:]):
            populated = [[rule[key]] if isinstance(rule[key], str) else rule[key] for key in aliases if rule.get(key)]
            if populated and any(value != populated[0] for value in populated[1:]):
                raise LegacyApprovalConversionError([{'reason': 'conflicting source aliases require approval_selectors', 'rule_key': rule.get('rule_key')}])
        actions = rule.get('action_patterns') or rule.get('actions') or rule.get('patterns') or []
        capabilities = rule.get('capability_patterns') or rule.get('capabilities') or rule.get('capability_ids') or []
        try:
            converted = normalize_legacy_workspace_policy({
                f'{effect}_actions': [actions] if isinstance(actions, str) else actions,
                f'{effect}_capabilities': [capabilities] if isinstance(capabilities, str) else capabilities,
            })
        except LegacyApprovalConversionError as exc:
            raise LegacyApprovalConversionError([{**issue, 'rule_key': rule.get('rule_key'),
                'reason': 'source rule requires approval_selectors: ' + issue['reason']} for issue in exc.blockers]) from exc
        rule['approval_selectors'] = [item['selector'] for item in converted['approval']['rules']]
        for key in _LEGACY_RULE_KEYS:
            rule.pop(key, None)
    return result


def _convert_source_rules(value):
    """Replace exact aliases only in declared rules lists, keeping their effects.

    compile_workspace_rules already accepts these exact action aliases and
    effects. The typed representation yields the same rule IDs, effects and
    selectors; capability/glob conversions still fail closed. Do not rewrite
    arbitrary rule-shaped objects or historical restore-point fingerprints.
    """
    if isinstance(value, dict):
        result = deepcopy(value)
        if isinstance(value.get('rules'), list):
            result['rules'] = _convert_task_rules(value['rules'])
        for key in _SOURCE_CONFIGURATION_KEYS:
            if isinstance(value.get(key), dict):
                result[key] = _convert_source_rules(value[key])
        confirmations = value.get('_explicit_hard_block_rules')
        if isinstance(value.get('rules'), list) and isinstance(confirmations, dict):
            from packages.core.services.workspace_setup_service import workspace_rule_fingerprint
            for before, after in zip(value['rules'], result['rules']):
                if (isinstance(before, dict) and before.get('rule_key')
                        and confirmations.get(str(before['rule_key'])) == workspace_rule_fingerprint(before)):
                    result['_explicit_hard_block_rules'][str(before['rule_key'])] = workspace_rule_fingerprint(after)
        return result
    return deepcopy(value)


def _convert_baseline(value):
    result = deepcopy(value)
    if not isinstance(result, dict):
        return result
    # Upgrade-state baseline is an envelope. A standalone configuration
    # baseline is retained for historical records and reviewed replacements.
    configuration = result.get('configuration', result)
    if isinstance(configuration, dict):
        for entry in (configuration.get('entries') or {}).values():
            if not isinstance(entry, dict):
                continue
            path = entry.get('path') or []
            if entry.get('kind') == 'workspace_configuration' and path == ['operating_model']:
                for key in ('source_content', 'installed_content', 'blueprint_content'):
                    if isinstance(entry.get(key), dict):
                        entry[key] = {**entry[key], 'value': _convert_source_rules(entry[key].get('value'))}
                        _require_typed_source_rules(entry[key]['value'])
            if entry.get('kind') == 'workspace_configuration' and path[:2] == ['operating_model', 'rules']:
                for key in ('source_content', 'installed_content', 'blueprint_content'):
                    if isinstance(entry.get(key), dict) and isinstance(entry[key].get('value'), list):
                        entry[key] = {**entry[key], 'value': _convert_task_rules(entry[key]['value'])}
                    _require_typed_rules((entry.get(key) or {}).get('value'))
            if any(key in LEGACY_POLICY_KEYS for key in path):
                raise LegacyApprovalConversionError([{'reason': 'flattened configuration approval projection requires a reviewed replacement', 'entry': entry.get('key')}])
            if entry.get('kind') != 'governance':
                continue
            for key in ('source_content', 'installed_content', 'blueprint_content'):
                if isinstance(entry.get(key), dict):
                    entry[key] = normalize_legacy_workspace_policy(entry[key])
    # Current configuration baselines store content, not cached fingerprints.
    # plan_configuration derives content/revision fingerprints each time; the
    # state revision is bumped below to invalidate any pre-migration review.
    result = _convert_source_rules(result)
    _require_typed_source_rules(result)
    if _has_legacy_approval(result):
        raise LegacyApprovalConversionError([{'reason': 'configuration baseline contains an unrecognized legacy approval projection'}])
    return result


def preflight_sources(source: dict, *, resolutions: dict | None = None) -> dict:
    """Deterministic conversion, shared by dry-run and apply.

    Resolutions are reviewable replacements keyed ``personal:user:entity``,
    ``workspace:id`` or ``room:id``. Source changes always require a new plan.
    A Workspace replacement contains current ``policy`` and ``operating_model``
    so unresolved source rules cannot return on their next rebuild.
    Its optional ``reconcile_history: true`` advances above the historical
    maximum without rewriting history. ``request:id`` accepts only an explicit
    ``{status: expired, resolved_reason: approval_migration_unverifiable}``
    replacement for a grant without historical requirements, or a pending
    tool/operation authorization without a stable operation binding, or a standalone
    Step authorization with a verified origin but no argument digest. It never
    resumes execution or changes the original decision/continuation.
    A separate ``approval_migration_failed_task_request`` reason explicitly
    retires an unbound pending Step request only when its complete Task/Plan/
    Step ownership matches and their states are failed/running/waiting_human.
    Task failure alone is never permission to change a request.
    ``approval_migration_unidentified_failed_step`` is a separate reviewed
    expiry for that same ownership/lifecycle tuple when both the request and
    its Step have no action key. It cannot create an action or resume a Step.
    ``room_request:id`` supports explicit ``invalidated`` with the
    ``approval_migration_unverifiable`` reason
    for an active Room receipt missing historical requirements; the reason is
    audited because Room receipts have no resolution-reason column.
    ``file_request:conversation:id`` supports explicit expiry for an active file
    receipt missing operation/action/requirements. Verified receipts cannot be
    retired through these migration resolutions.
    ``approval_migration_provider_outcome_unverified`` separately revokes an
    unconsumed legacy Workspace main-Chat Chrome grant with verified ownership and no
    operation, claim, confirmation or requirements receipt. It preserves the
    original decision and continuation without claiming the action did not run.
    """
    resolutions = {} if resolutions is None else resolutions
    if not isinstance(resolutions, dict):
        raise ValueError('Approval migration resolutions must be an object')
    blockers, changes, operations = [], [], []
    entities = {row['id']: row for row in source['entities']}
    users = {row['id']: row for row in source['users']}
    policies = {row['workspace_id']: row for row in source['governance_policies']}
    history_revisions = {}
    for row in source.get('governance_revisions', []):
        workspace_id = row['workspace_id']
        history_revisions[workspace_id] = max(history_revisions.get(workspace_id, 0), int(row['revision'] or 0))

    def block(scope, issues):
        blockers.extend({'scope': scope, **issue} for issue in issues)

    known_resolutions = {f"{kind}:{row[key]}" for kind, table, key in (
        ('workspace', 'workspaces', 'id'), ('room', 'chat_rooms', 'id'), ('draft', 'workspace_drafts', 'id'),
        ('task', 'tasks', 'id'), ('upgrade_state', 'workspace_blueprint_upgrade_states', 'workspace_id'),
        ('request', 'hitl_requests', 'id'),
        ('room_request', 'chat_room_tool_approvals', 'id'),
    ) for row in source.get(table, [])}

    for entity in source['entities']:
        settings = dict(entity['settings'] or {})
        if 'approval_defaults' in settings:
            try:
                root = settings['approval_defaults']
                ApprovalPolicy.model_validate(root['policy'])
                if type(root.get('revision')) is not int or root['revision'] < 1:
                    raise ValueError('root approval revision must be a positive integer')
            except (ValueError, KeyError, TypeError) as exc:
                block(f"account:{entity['id']}", [{'reason': str(exc)}])
            continue
        settings['approval_defaults'] = {'revision': 1, 'policy': ApprovalPolicy().model_dump()}
        operations.append({'table': 'entities', 'key': 'id', 'id': entity['id'], 'values': {'settings': settings}})

    memberships = {}
    for row in source['user_memberships']:
        memberships.setdefault(row['user_id'], set()).add(row['entity_id'])
    for user in source['users']:
        prefs = dict(user['preferences'] or {})
        branches = dict(prefs.get('approval_policy') or {})
        roots = memberships.get(user['id'], set()) | {user['entity_id']} | set(branches)
        failed = False
        for entity_id in sorted(roots):
            scope = f"personal:{user['id']}:{entity_id}"
            known_resolutions.add(scope)
            if entity_id not in entities:
                block(scope, [{'reason': 'personal root Entity is missing'}])
                failed = True
                continue
            if entity_id in branches:
                try:
                    binding = ApprovalPolicyBinding.model_validate({key: branches[entity_id][key] for key in ('root_entity_id', 'policy')})
                    if binding.root_entity_id != entity_id:
                        raise ValueError('personal root binding does not match its Entity branch')
                    if type(branches[entity_id].get('revision')) is not int or branches[entity_id]['revision'] < 1:
                        raise ValueError('personal approval revision must be a positive integer')
                except (ValueError, KeyError, TypeError) as exc:
                    block(scope, [{'reason': str(exc)}])
                    failed = True
                continue
            try:
                override = ApprovalPolicyOverride.model_validate(resolutions[scope]).model_dump() if scope in resolutions else _personal_override(prefs)
            except (ValueError, LegacyApprovalConversionError) as exc:
                block(scope, getattr(exc, 'blockers', [{'reason': str(exc)}]))
                failed = True
                continue
            branches[entity_id] = {'revision': 1, 'root_entity_id': entity_id, 'policy': override}
            changes.append({'scope': scope, 'change': 'Personal file approval is retained as local file/deletion rules; account mode is inherited'})
        if not failed:
            clean = {key: value for key, value in prefs.items() if key not in _OLD_PREFS}
            clean['approval_policy'] = branches
            if clean != prefs:
                operations.append({'table': 'users', 'key': 'id', 'id': user['id'], 'values': {'preferences': clean}})

    for workspace in source['workspaces']:
        scope = f"workspace:{workspace['id']}"
        old = policies.get(workspace['id'])
        raw = deepcopy(old['policy']) if old else None
        operating = deepcopy(workspace['operating_model'] or {})
        settings = deepcopy(workspace['settings'] or {})
        try:
            if workspace['entity_id'] not in entities or (old and old['entity_id'] != workspace['entity_id']):
                raise ValueError('Workspace policy owner does not match its existing root Entity')
            replacement = resolutions.get(scope)
            if scope in resolutions:
                if not isinstance(replacement, dict) or set(replacement) - {'policy', 'operating_model', 'reconcile_history', 'approval_review'}:
                    raise ValueError('Workspace resolution has unsupported fields')
                if 'reconcile_history' in replacement and type(replacement['reconcile_history']) is not bool:
                    raise ValueError('reconcile_history must be an explicit boolean')
                if 'approval_review' in replacement and not _valid_approval_review(replacement['approval_review']):
                    raise ValueError('approval_review must be {required, reasons, plan_mode}')
            live_revision = int(old['revision'] or 0) if old else 0
            history_revision = history_revisions.get(workspace['id'], 0)
            reconcile_history = bool(replacement and replacement.get('reconcile_history') is True)
            if history_revision > live_revision and not reconcile_history:
                raise ValueError('Governance history revision is ahead of the live policy; reconcile before upgrade')
            if scope in resolutions:
                converted = deepcopy(replacement['policy'])
                binding = ApprovalPolicyBinding.model_validate(converted['approval'])
                if binding.root_entity_id != workspace['entity_id']:
                    raise ValueError('replacement root does not match Workspace owner')
                operating = deepcopy(replacement['operating_model'])
            else:
                converted = normalize_legacy_workspace_policy(raw, root_entity_id=workspace['entity_id'])
            # Reviewed replacements must satisfy the same complete contract as
            # online reads, including retired fields and independent limits.
            policy_from_dict(converted)
            if not isinstance(operating, dict):
                raise ValueError('Workspace operating model must be an object')
            if operating.get('governance') is not None and not isinstance(operating['governance'], dict):
                raise ValueError('Workspace operating governance policy must be an object')
            if isinstance(operating.get('governance'), dict):
                operating['governance'] = normalize_legacy_workspace_policy(operating['governance'], root_entity_id=workspace['entity_id'])
                policy_from_dict(operating['governance'])
            # Source rules must be converted too; a stale source could restore
            # old glob semantics at the next rules.replace operation.
            operating = _convert_source_rules(operating)
            _require_typed_source_rules(operating)
            blueprint = dict(settings.get('_blueprint') or {})
            preset = blueprint.get('governance_preset')
            if preset in ('safe', 'standard', 'no_default_grants'):
                blueprint['governance_preset'] = {'safe': 'confirm_changes', 'standard': 'follow_default', 'no_default_grants': 'follow_default'}[preset]
                settings['_blueprint'] = blueprint
            elif preset == 'aggressive':
                if scope not in resolutions:
                    raise LegacyApprovalConversionError([{'reason': 'aggressive Blueprint preset requires explicit policy resolution'}])
                blueprint['governance_preset'] = 'follow_default'
                settings['_blueprint'] = blueprint
            strategist = dict(settings.get('strategist') or {})
            for key in ('auto_approve_proposals', 'auto_approve_proposals_source', 'proposal_task_approval_source'):
                if key in strategist:
                    if strategist[key] is True and scope not in resolutions:
                        raise LegacyApprovalConversionError([{'reason': 'legacy Proposal consent requires explicit action rules'}])
                    strategist.pop(key, None)
            if 'strategist' in settings:
                settings['strategist'] = strategist
            if replacement and 'approval_review' in replacement:
                # Owner-visible marker for conversions that still need review.
                settings['approval_review'] = deepcopy(replacement['approval_review'])
        except (ValueError, KeyError, LegacyApprovalConversionError) as exc:
            block(scope, getattr(exc, 'blockers', [{'reason': str(exc)}]))
            continue
        revision = max(live_revision, history_revision) + 1
        if raw != converted or history_revision > live_revision:
            operation = {'table': 'governance_policies', 'key': 'workspace_id', 'id': workspace['id'], 'insert': old is None,
                'values': {'entity_id': workspace['entity_id'], 'policy': converted, 'revision': revision}}
            if history_revision > live_revision:
                operation['reconciliation'] = {'reconcile_history': True, 'live_revision': live_revision, 'history_revision': history_revision}
            operations.append(operation)
        if operating != workspace['operating_model'] or settings != (workspace['settings'] or {}):
            operations.append({'table': 'workspaces', 'key': 'id', 'id': workspace['id'], 'values': {'operating_model': operating, 'settings': settings}})

    for room in source['chat_rooms']:
        scope = f"room:{room['id']}"
        entity_id = room['owner_entity_id'] or (users.get(room['owner_user_id']) or {}).get('entity_id')
        if entity_id not in entities:
            block(scope, [{'reason': 'Room persistent owner root is missing'}])
            continue
        if room.get('approval_policy'):
            try:
                binding = ApprovalPolicyBinding.model_validate({key: room['approval_policy'][key] for key in ('root_entity_id', 'policy')})
                if binding.root_entity_id != entity_id:
                    raise ValueError('Room persistent root binding changed')
                if type(room['approval_policy'].get('revision')) is not int or room['approval_policy']['revision'] < 1:
                    raise ValueError('Room approval revision must be a positive integer')
            except (ValueError, KeyError, TypeError) as exc:
                block(scope, [{'reason': str(exc)}])
            continue
        if scope in resolutions:
            override = ApprovalPolicyOverride.model_validate(resolutions[scope]).model_dump()
        elif room.get('ai_file_actions_direct_enabled'):
            block(scope, [{'reason': 'room direct-file consent requires explicit current action/category rules'}])
            continue
        else:
            # Historical room mutations asked each time. Keep that behavior
            # as an explicit local mode, without changing capability revision.
            override = ApprovalPolicyOverride(mode='confirm_changes').model_dump()
            changes.append({'scope': scope, 'change': 'Existing room mutation approval retained as confirm_changes'})
        operations.append({'table': 'chat_rooms', 'key': 'id', 'id': room['id'], 'values': {
            'approval_policy': {'revision': 1, 'root_entity_id': entity_id, 'policy': override}, 'ai_file_actions_direct_enabled': False}})

    for draft in source.get('workspace_drafts', []):
        if draft['status'] not in ('active', 'ready'):
            continue
        scope = f"draft:{draft['id']}"
        fields = deepcopy(resolutions.get(scope, draft['fields']) or {})
        try:
            for key in ('governance_policy', 'governance'):
                if isinstance(fields.get(key), dict):
                    fields[key] = normalize_legacy_workspace_policy(fields[key])
            fields = _convert_source_rules(fields)
            _require_typed_source_rules(fields)
            if _has_legacy_approval(fields):
                raise LegacyApprovalConversionError([{'reason': 'active draft contains an unrecognized legacy approval projection'}])
        except (ValueError, LegacyApprovalConversionError) as exc:
            block(scope, getattr(exc, 'blockers', [{'reason': str(exc)}]))
            continue
        if fields != draft['fields']:
            operations.append({'table': 'workspace_drafts', 'key': 'id', 'id': draft['id'], 'values': {'fields': fields}})

    for state in source.get('workspace_blueprint_upgrade_states', []):
        scope = f"upgrade_state:{state['workspace_id']}"
        replacement = resolutions.get(scope, {})
        baseline = deepcopy(replacement.get('baseline', state['baseline']))
        restore = deepcopy(replacement.get('restore_point', state['restore_point']))
        try:
            if set(replacement) - {'baseline', 'restore_point'}:
                raise ValueError('Upgrade-state resolution has unsupported fields')
            baseline = _convert_baseline(baseline)
            _require_typed_restore_rules(restore)
            if _has_legacy_approval(restore):
                raise LegacyApprovalConversionError([{'reason': 'configuration restore point needs a reviewed replacement; its historical approval fingerprint cannot be guessed'}])
        except (ValueError, LegacyApprovalConversionError) as exc:
            block(scope, getattr(exc, 'blockers', [{'reason': str(exc)}]))
            continue
        if baseline != state['baseline'] or restore != state['restore_point']:
            operations.append({'table': 'workspace_blueprint_upgrade_states', 'key': 'workspace_id', 'id': state['workspace_id'],
                'values': {'baseline': baseline, 'restore_point': restore, 'revision': int(state['revision'] or 0) + 1}})

    for task in source.get('tasks', []):
        scope = f"task:{task['id']}"
        details = deepcopy(resolutions.get(scope, task['details']) or {})
        try:
            if not isinstance(details, dict):
                raise ValueError('Task details must be an object')
            runtime = details.get('runtime_context') or {}
            if not isinstance(runtime, dict):
                raise ValueError('Task runtime context must be an object')
            if 'rules' in runtime:
                runtime['rules'] = _convert_task_rules(runtime['rules'])
                details['runtime_context'] = runtime
        except (ValueError, TypeError, LegacyApprovalConversionError) as exc:
            block(scope, getattr(exc, 'blockers', [{'reason': str(exc)}]))
            continue
        if details != task['details']:
            operations.append({'table': 'tasks', 'key': 'id', 'id': task['id'], 'values': {'details': details}})

    steps = {row['id']: row for row in source.get('execution_steps', [])}
    plans = {row['id']: row for row in source.get('execution_plans', [])}
    tasks = {row['id']: row for row in source.get('tasks', [])}
    seen_operations = set()
    for request in source.get('hitl_requests', []):
        scope = f"request:{request['id']}"
        context = request['context'] if isinstance(request.get('context'), dict) else {}
        step = steps.get(request.get('origin_step_id')) if request.get('origin_kind') == 'step' else None
        matching_step = bool(step and step.get('workspace_id') == request.get('workspace_id')
            and step.get('action_key') == request.get('action_key')
            and all(step.get(key) is not None and step[key] == request.get(request_key)
            for key, request_key in (('entity_id', 'entity_id'), ('plan_id', 'origin_plan_id'))))
        step_operation_id = None
        if matching_step:
            digest = hashlib.sha256(json.dumps(step['params'] or {}, sort_keys=True, default=str).encode()).hexdigest()
            if context.get('args_hash') == digest:
                step_operation_id = f"step:{step['plan_id']}:{step['id']}"
                if step.get('approval_operation_revision'):
                    step_operation_id += f":intent:{step['approval_operation_revision']}"
        if scope in resolutions:
            resolution = resolutions[scope]
            if not isinstance(resolution, dict) or resolution not in (_EXPIRE_UNVERIFIABLE_REQUEST,
                    _EXPIRE_FAILED_TASK_REQUEST, _EXPIRE_UNIDENTIFIED_FAILED_STEP_REQUEST, _EXPIRE_LEGACY_CHROME_GRANT):
                block(scope, [{'reason': 'Request resolution only supports explicit expiry of an unverifiable authorization'}])
                continue
            if request['status'] == 'expired' and request.get('resolved_reason') == resolution['resolved_reason']:
                continue  # Replaying a successfully applied reviewed plan is a no-op.
            if resolution == _EXPIRE_LEGACY_CHROME_GRANT:
                if not _can_retire_legacy_chrome_grant(request, source):
                    block(scope, [{'reason': 'Legacy Chrome grant retirement requires matching ownership and no execution, confirmation or modern approval receipt'}])
                    continue
                operations.append({'table': 'hitl_requests', 'key': 'id', 'id': request['id'],
                    'values': dict(resolution), 'reconciliation': dict(resolution)})
                changes.append({'scope': scope, 'change': 'Retire legacy Chrome consent; external outcome remains unverified, preserve historical decision and continuation, never replay'})
                continue
            if context.get('kind') == 'provider' or 'provider_action_claim' in context:
                block(scope, [{'reason': 'Provider authorization or effect claim requires its existing reconciliation path'}])
                continue
            unverifiable_grant = (request['status'] == 'granted' and request['hitl_type'] == 'authorize'
                and not request.get('consumed_at') and not isinstance(context.get('approval_requirements'), list))
            # A supplied but different digest is evidence of divergent intent,
            # not merely missing history. It needs explicit reconciliation.
            # Task-linked Steps use their dedicated lifecycle reconciliation;
            # the generic legacy resolution must not bypass that contract.
            task_linked = request.get('origin_task_id') or (plans.get(request.get('origin_plan_id')) or {}).get('task_id')
            unbound_step = matching_step and not task_linked and not context.get('args_hash') and not step_operation_id
            unbound_pending = (request['status'] == 'pending' and request['hitl_type'] == 'authorize'
                and (request.get('origin_kind') in ('tool_call', 'operation') or unbound_step)
                and not request.get('consumed_at') and not context.get('operation_id') and context.get('kind') != 'provider')
            if resolution in (_EXPIRE_FAILED_TASK_REQUEST, _EXPIRE_UNIDENTIFIED_FAILED_STEP_REQUEST):
                step = steps.get(request.get('origin_step_id'))
                plan = plans.get(request.get('origin_plan_id'))
                task = tasks.get(request.get('origin_task_id'))
                if resolution == _EXPIRE_UNIDENTIFIED_FAILED_STEP_REQUEST:
                    action_matches = (step and request.get('action_key') in (None, '') and step.get('action_key') in (None, ''))
                else:
                    action_matches = (step and request.get('action_key') and step.get('action_key') == request['action_key'])
                failed_task_request = (request['status'] == 'pending' and request['hitl_type'] == 'authorize'
                    and request.get('origin_kind') == 'step' and not context.get('operation_id')
                    and not context.get('args_hash') and not request.get('consumed_at') and context.get('kind') != 'provider'
                    and step and plan and task and task.get('status') == 'failed'
                    and plan.get('status') == 'running' and step.get('step_status') == 'waiting_human'
                    and plan.get('task_id') == task['id'] and step.get('plan_id') == plan['id']
                    and request.get('entity_id') and action_matches
                    and all(row.get('entity_id') == request['entity_id'] and row.get('workspace_id') == request.get('workspace_id')
                        for row in (task, plan, step)))
                if not failed_task_request:
                    block(scope, [{'reason': 'Failed-Task expiry requires an unbound pending Step authorization with matching ownership, reviewed action identity and failed/running/waiting_human Task/Plan/Step states'}])
                    continue
            elif not (unverifiable_grant or unbound_pending):
                block(scope, [{'reason': 'Only an unverifiable granted authorization or an unbound pending tool/operation authorization may be expired by this resolution'}])
                continue
            operations.append({'table': 'hitl_requests', 'key': 'id', 'id': request['id'],
                'values': dict(resolution), 'reconciliation': dict(resolution)})
            changes.append({'scope': scope, 'change': 'Explicitly expire the unverifiable old authorization; preserve its decision and continuation, without resuming execution'})
            continue
        if request['status'] not in ('pending', 'granted') or request['hitl_type'] in ('input', 'choice', 'error'):
            continue
        context = dict(request['context'] or {})
        if request['status'] == 'granted' and not isinstance(context.get('approval_requirements'), list):
            block(scope, [{'reason': 'active grant has no historical approval requirements; reconcile or review before upgrade'}])
            continue
        operation_id = context.get('operation_id')
        if not operation_id:
            operation_id = step_operation_id
        provider_dedup = None
        if context.get('kind') == 'provider':
            if not context.get('provider') or not context.get('provider_approval_id'):
                block(scope, [{'reason': 'provider approval has no exact provider transaction identity'}])
                continue
            provider_dedup = f"provider:{context['provider']}:{context['provider_approval_id']}"
            if request['dedup_key'] != provider_dedup or operation_id not in (None, provider_dedup):
                block(scope, [{'reason': 'provider approval transaction binding changed'}])
                continue
            operation_id = provider_dedup
        if not operation_id:
            block(scope, [{'reason': 'active approval has no verifiable stable operation/semantic binding; finish or explicitly reconcile its existing continuation before upgrade'}])
            continue
        dedup_key = provider_dedup or f'operation:{operation_id}'
        if not provider_dedup and len(dedup_key) > 200:
            digest = hashlib.sha256(dedup_key.encode()).hexdigest()
            dedup_key = f'{dedup_key[:135]}:{digest}'
        identity = (request['entity_id'], dedup_key)
        if identity in seen_operations:
            block(scope, [{'reason': 'multiple active approval records resolve to one operation; reconcile before upgrade'}])
            continue
        seen_operations.add(identity)
        context['operation_id'] = operation_id
        if context != request['context'] or dedup_key != request['dedup_key']:
            operations.append({'table': 'hitl_requests', 'key': 'id', 'id': request['id'], 'values': {'context': context, 'dedup_key': dedup_key}})

    for request in source.get('chat_room_tool_approvals', []):
        scope = f"room_request:{request['id']}"
        unverifiable = request['status'] in ('pending', 'approved') and not isinstance(request.get('approval_requirements'), list)
        if scope in resolutions:
            resolution = resolutions[scope]
            if resolution != _INVALIDATE_UNVERIFIABLE_ROOM_REQUEST:
                block(scope, [{'reason': 'Room resolution only supports explicit invalidation of an unverifiable receipt'}])
            elif request['status'] == 'invalidated':
                continue
            elif not unverifiable or any(request.get(key) for key in ('claimed_at', 'finished_at', 'result_document_id', 'result_media_job_id')):
                block(scope, [{'reason': 'Only an active, unclaimed Room receipt without historical requirements may be invalidated by this resolution'}])
            else:
                operations.append({'table': 'chat_room_tool_approvals', 'key': 'id', 'id': request['id'],
                    'values': {'status': 'invalidated'}, 'reconciliation': dict(resolution)})
                changes.append({'scope': scope, 'change': 'Explicitly invalidate the unverifiable old Room receipt; preserve its original decision without resuming execution'})
        elif unverifiable:
            block(scope, [{'reason': 'active Room approval has no historical approval requirements; reconcile or review before upgrade'}])

    for conversation in source.get('conversations', []):
        metadata = deepcopy(conversation.get('metadata') or {})
        approvals = metadata.get('file_approvals') or {}
        if not isinstance(approvals, dict):
            block(f"conversation:{conversation['id']}", [{'reason': 'file approval receipts are malformed'}])
            continue
        seen_file_operations = set()
        reconciliations = {}
        for request_id, request in approvals.items():
            scope = f"file_request:{conversation['id']}:{request_id}"
            known_resolutions.add(scope)
            if not isinstance(request, dict):
                if scope in resolutions:
                    block(scope, [{'reason': 'File approval receipt must be an object before reconciliation'}])
                continue
            operation_id = request.get('operation_id')
            unverifiable = (not isinstance(operation_id, str) or not operation_id
                    or not isinstance(request.get('approval_action_key'), str) or not request['approval_action_key']
                    or not isinstance(request.get('approval_requirements'), list))
            if scope in resolutions:
                resolution = resolutions[scope]
                if resolution != _EXPIRE_UNVERIFIABLE_REQUEST:
                    block(scope, [{'reason': 'File resolution only supports explicit expiry of an unverifiable receipt'}])
                elif request.get('status') == 'expired' and request.get('resolved_reason') == resolution['resolved_reason']:
                    continue
                elif request.get('status') not in ('pending', 'approved') or not unverifiable or request.get('consumed_at'):
                    block(scope, [{'reason': 'Only an active, unconsumed file receipt without exact operation/action/historical requirements may be expired by this resolution'}])
                else:
                    request.update(resolution)
                    reconciliations[scope] = dict(resolution)
                    changes.append({'scope': scope, 'change': 'Explicitly expire the unverifiable old file receipt; preserve its original decision and continuation without resuming execution'})
                continue
            if request.get('status') not in ('pending', 'approved'):
                continue
            if unverifiable:
                block(scope, [{'reason': 'active file approval has no exact operation/action/historical requirements; reconcile or review before upgrade'}])
                continue
            if operation_id in seen_file_operations:
                block(scope, [{'reason': 'multiple active file approvals resolve to one operation; reconcile before upgrade'}])
            seen_file_operations.add(operation_id)
        if reconciliations:
            operations.append({'table': 'conversations', 'key': 'id', 'id': conversation['id'],
                'values': {'metadata': metadata}, 'reconciliation': reconciliations})

    for scope in resolutions:
        if scope not in known_resolutions:
            block(str(scope), [{'reason': 'Unknown approval migration resolution scope'}])

    return {'source_digest': source_digest(source), 'blockers': blockers, 'changes': changes, 'operations': operations,
        'counts': {key: len(value) for key, value in source.items()}, 'resolutions': resolutions}


def preflight(connection, *, resolutions: dict | None = None) -> dict:
    return preflight_sources(read_sources(connection), resolutions=resolutions)


def apply(connection, reviewed_plan: dict) -> dict:
    """Apply a reviewed plan atomically. Caller keeps the whole release offline."""
    plan_mode = reviewed_plan.get('plan_mode')
    if plan_mode not in (None, *NEW_SEMANTICS_PLAN_MODES):
        raise ValueError('Unknown approval migration plan mode')
    if plan_mode == 'new_semantics_conservative_v1' and os.environ.get('MANOR_APPROVAL_MIGRATION_ALLOW_V1_PLAN') != 'true':
        # v1 gated reads and escalated on system-default grants. Regenerate the
        # reviewed plan with the current generator instead of replaying it.
        raise ValueError('Reviewed plan mode new_semantics_conservative_v1 is retired; regenerate the plan '
                         '(set MANOR_APPROVAL_MIGRATION_ALLOW_V1_PLAN=true only to replay archived evidence)')
    if connection.dialect.name == 'postgresql':
        # Maintenance excludes workers and clients; this also fences an
        # accidental configuration write while checking source fingerprints.
        from sqlalchemy import text
        existing_tables = set(inspect(connection).get_table_names())
        for name in _COLUMNS:
            if name in existing_tables:
                connection.execute(text(f'LOCK TABLE {name} IN SHARE ROW EXCLUSIVE MODE'))
    source = read_sources(connection)
    if source_digest(source) != reviewed_plan.get('source_digest'):
        fresh = preflight_sources(source, resolutions=reviewed_plan.get('resolutions'))
        def already_written(operation):
            row = next((item for item in source[operation['table']] if item.get(operation['key']) == operation['id']), None)
            return row is not None and all(row.get(key) == value for key, value in operation['values'].items())
        if not fresh['blockers'] and not fresh['operations'] and all(already_written(item) for item in reviewed_plan.get('operations', [])):
            return {'source_digest': reviewed_plan['source_digest'], 'applied': 0}
        raise ValueError('Approval source changed after preflight; regenerate and review the plan')
    plan = preflight_sources(source, resolutions=reviewed_plan.get('resolutions'))
    if plan['blockers']:
        raise LegacyApprovalConversionError(plan['blockers'])
    if plan['operations'] != reviewed_plan.get('operations'):
        raise ValueError('Reviewed approval operations do not match their source/resolutions')
    metadata = MetaData()
    tables = {name: Table(name, metadata, autoload_with=connection) for name in {item['table'] for item in plan['operations']}}
    audit = Table('audit_log', metadata, autoload_with=connection)
    history = Table('governance_revisions', metadata, autoload_with=connection)
    for operation in plan['operations']:
        table = tables[operation['table']]
        values = operation['values']
        original = next((row for row in source[operation['table']] if row.get(operation['key']) == operation['id']), None)
        if operation.get('reconciliation'):
            # Reconciliation receipts preserve the complete durable row,
            # including the original approver/decision and exact continuation.
            stored = connection.execute(select(table).where(table.c[operation['key']] == operation['id'])).mappings().one_or_none()
            original = dict(stored) if stored is not None else None
        if operation.get('insert'):
            connection.execute(table.insert().values(**{operation['key']: operation['id'], **values}))
        else:
            connection.execute(update(table).where(table.c[operation['key']] == operation['id']).values(**values))
        details = {'source_digest': plan['source_digest'], 'source': original, 'after': values}
        if plan_mode is not None:
            details['plan_mode'] = plan_mode
        if operation.get('reconciliation'):
            persisted = connection.execute(select(table).where(table.c[operation['key']] == operation['id'])).mappings().one()
            details.update(reconciliation=operation['reconciliation'], after=dict(persisted))
        connection.execute(audit.insert().values(id=generate_ulid(), action='approval_policy.migrated', resource_type=operation['table'],
            resource_id=operation['id'], details=json.loads(json.dumps(details, default=str)),
            created_at=datetime.now(timezone.utc)))
        if operation['table'] == 'governance_policies':
            connection.execute(history.insert().values(id=generate_ulid(), workspace_id=operation['id'], revision=values['revision'],
                policy=values['policy'], change_summary='Unified approval migration; source preserved in audit', created_at=datetime.now(timezone.utc)))
    return {'source_digest': plan['source_digest'], 'applied': len(plan['operations'])}
