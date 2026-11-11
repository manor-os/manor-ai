# FROZEN copy from commit d52a5cc75 (plan mode new_semantics_conservative_v2).
# Test fixture only: reproduces exactly what the 20261007 corrections wrote in production. Never edit.
"""Explicitly accepted conservative cutover to the new approval semantics.

This module only prepares replacements. The ordinary source-fenced preflight
and atomic migration remain the only database writers. It is never an online
policy fallback and never tries to reproduce unrepresentable legacy grants.

v2 (``new_semantics_conservative_v2``) corrects the over-conservative v1 plan:

* A missing legacy policy, or one that is exactly the old system default
  grants (``DEFAULT_POLICY`` / the 20260617_04/_05 backfills), is not a user
  choice: no local override, the Workspace follows its account (Q4).
  Likewise a person who never saved a personal file permission gets no
  ``migration-personal-*`` rules; a saved permission keeps its rules.
* An ask/deny fallback never names the ``read`` category (Q5). This does not
  yet cover read-impact actions classified in another category (for example
  a scheduled-job query under ``automation``); keeping every impact=read
  action out of category fallbacks is an evaluator rule (effect contract).
* ``file.write`` maps asymmetrically, never looser and never wider than the
  legacy selector: a grant becomes the ``files`` category only, while a
  restriction also covers ``deletion`` (legacy file.write covered deletes).
* Only an unrepresentable *restriction* (ask/deny) keeps a scope-wide
  fallback and the ``confirm_changes`` escalation. An unrepresentable *grant*
  is dropped (the action follows the user's own mode) and reported for
  review. The old evaluator allowed by default, so dropping a grant is never
  stricter than the user's own mode.
"""
from __future__ import annotations

from copy import deepcopy
from typing import get_args

from packages.core.governance.approval_policy import (
    ActionSelector, ApprovalCategory, ApprovalPolicy, ApprovalPolicyBinding,
    ApprovalPolicyOverride, ApprovalRule, RuleDraft,
)
from packages.core.governance.approval_policy_migration import LEGACY_POLICY_KEYS, source_digest
from packages.core.governance.policy import policy_from_dict, policy_to_dict
from packages.core.migrations.unified_approval import (
    _EXPIRE_FAILED_TASK_REQUEST, _EXPIRE_UNVERIFIABLE_REQUEST,
    _EXPIRE_UNIDENTIFIED_FAILED_STEP_REQUEST, _EXPIRE_LEGACY_CHROME_GRANT,
    _INVALIDATE_UNVERIFIABLE_ROOM_REQUEST, _LEGACY_RULE_KEYS, _SOURCE_CONFIGURATION_KEYS, _personal_override,
    preflight_sources,
)

PLAN_MODE = 'new_semantics_conservative_v2'
V1_PLAN_MODE = 'new_semantics_conservative_v1'
_CATEGORIES = get_args(ApprovalCategory)
# Reads are allowed by every mode; a review gate never covers them.
_FALLBACK_CATEGORIES = tuple(category for category in _CATEGORIES if category != 'read')
_READ_SELECTOR = {'kind': 'category', 'category': 'read'}
# Legacy BusinessCapability ids with a current selector per direction. Legacy
# file.write covered every workspace.file.* action, including delete, which is
# now the ``deletion`` category. A grant maps to the narrower ``files``
# category (deletion follows the mode); a restriction keeps covering deletion.
_CAPABILITY_SELECTORS = {
    'file.write': {
        'allow': ({'kind': 'category', 'category': 'files'},),
        'restrict': ({'kind': 'category', 'category': 'files'}, {'kind': 'category', 'category': 'deletion'}),
    },
}
# The system grants a fresh Workspace got (``DEFAULT_POLICY``), also written
# into existing rows by migrations 20260617_04 and 20260617_05.
SYSTEM_DEFAULT_GRANTS = frozenset({'file.write', 'manor.composite'})
# Capabilities known to need the effect declaration table before they can be
# mapped per action. Any other unmapped capability is unrepresentable too.
UNREPRESENTABLE_CAPABILITIES = {
    'manor.composite': 'per-action mapping needs the effect declaration table',
}
_EFFECTS = {
    'deny': 'deny', 'never_allow': 'deny', 'block': 'deny', 'draft_only': 'deny',
    'approval_required': 'ask', 'hitl_required': 'ask', 'require_approval': 'ask', 'review_required': 'ask',
    'auto_approve': 'allow', 'allow': 'allow', 'exception': 'allow',
}
_FIELDS = {f'{prefix}_{axis}': effect for prefix, effect in (
    ('never_allow', 'deny'), ('hitl_required', 'ask'), ('auto_approve', 'allow'),
) for axis in ('actions', 'capabilities')}


def _id(*parts):
    return 'new-semantics-' + source_digest(parts)[:32]


def _maximum(*effects):
    return max(effects, key=lambda item: {None: 0, 'ask': 1, 'deny': 2}[item])


def _exact_action(value):
    try:
        return ActionSelector(action_key=value).model_dump()
    except ValueError:
        return None


def _capability_selectors(value, effect):
    entry = _CAPABILITY_SELECTORS.get(value) if isinstance(value, str) else None
    return [deepcopy(item) for item in entry['allow' if effect == 'allow' else 'restrict']] if entry else None


def _representable(field, value):
    if 'capabilities' in field:
        return isinstance(value, str) and value in _CAPABILITY_SELECTORS
    return _exact_action(value) is not None


def is_system_default(raw) -> bool:
    """Exactly the old system default grants and nothing else that approves."""
    if not isinstance(raw, dict) or 'approval' in raw:
        return False
    legacy = {key: value for key, value in raw.items() if key in LEGACY_POLICY_KEYS and value not in (None, [], False)}
    grants = legacy.get('auto_approve_capabilities')
    return (set(legacy) == {'auto_approve_capabilities'} and isinstance(grants, list)
            and len(grants) == len(SYSTEM_DEFAULT_GRANTS) and set(grants) == SYSTEM_DEFAULT_GRANTS)


def _without_system_default(raw):
    return {key: deepcopy(value) for key, value in raw.items() if key not in LEGACY_POLICY_KEYS} if is_system_default(raw) else raw


def _restriction(effect):
    """Fallback strength for an unrepresentable selector; a grant needs none."""
    return None if effect == 'allow' else 'deny' if effect == 'deny' else 'ask'


def _source_selectors(rule, effect):
    if 'approval_selectors' in rule:
        selectors = rule['approval_selectors']
        if not isinstance(selectors, list):
            return [], True
        try:
            typed = [RuleDraft(effect=effect, selector=item).selector.model_dump() for item in selectors]
        except ValueError:
            return [], True
        return typed, any(rule.get(key) for key in _LEGACY_RULE_KEYS)
    actions = rule.get('action_patterns') or rule.get('actions') or rule.get('patterns') or []
    capabilities = rule.get('capability_patterns') or rule.get('capabilities') or rule.get('capability_ids') or []
    actions = [actions] if isinstance(actions, str) else actions
    capabilities = [capabilities] if isinstance(capabilities, str) else capabilities
    if not isinstance(actions, list) or not isinstance(capabilities, list):
        return [], True
    selectors = [_exact_action(item) for item in actions]
    unsupported = any(item is None for item in selectors)
    selectors = [item for item in selectors if item is not None]
    for capability in capabilities:
        mapped = _capability_selectors(capability, effect)
        unsupported = unsupported or mapped is None
        selectors.extend(mapped or [])
    return selectors, unsupported


def _severity(value):
    """Find the strongest fallback needed by an entire owner configuration."""
    if isinstance(value, list):
        return _maximum(None, *(_severity(item) for item in value))
    if not isinstance(value, dict):
        return None
    result = None
    for field, effect in _FIELDS.items():
        values = value.get(field) or []
        unsupported = not isinstance(values, list) or any(not _representable(field, item) for item in values)
        if unsupported:
            result = _maximum(result, _restriction(effect))
    if value.get('automated_runs_always_allow'):
        result = _maximum(result, 'ask')  # Retained v1 behaviour (Q6).
    if 'rule_type' in value:
        effect = _EFFECTS.get(str(value.get('rule_type') or '').strip().lower())
        if effect:
            _, unsupported = _source_selectors(value, effect)
            if unsupported:
                result = _maximum(result, _restriction(effect))
    return _maximum(result, *(_severity(value.get(key))
        for key in ('rules', 'governance', 'governance_policy', *_SOURCE_CONFIGURATION_KEYS)))


def review_reasons(value):
    """Legacy selectors the conversion could not represent exactly.

    Walks the same declared paths as ``_severity``. A dropped grant follows
    the user's own mode; an unrepresentable restriction keeps a scope-wide
    fallback. Both are reported so the owner can replace them with exact rules.
    """
    reasons = []

    def visit(item):
        if isinstance(item, list):
            for child in item:
                visit(child)
            return
        if not isinstance(item, dict):
            return
        for field, effect in _FIELDS.items():
            values = item.get(field) or []
            for value in values if isinstance(values, list) else [values]:
                if not isinstance(values, list) or not _representable(field, value):
                    reasons.append({'code': 'legacy_selector_unrepresentable', 'field': field, 'selector': str(value),
                        'handling': 'follows_mode' if effect == 'allow' else 'scope_fallback'})
        if item.get('automated_runs_always_allow'):
            reasons.append({'code': 'legacy_consent_requires_review', 'field': 'automated_runs_always_allow',
                'handling': 'scope_fallback'})
        if 'rule_type' in item:
            effect = _EFFECTS.get(str(item.get('rule_type') or '').strip().lower())
            if effect and _source_selectors(item, effect)[1]:
                reasons.append({'code': 'legacy_rule_unrepresentable', 'rule_key': str(item.get('rule_key') or ''),
                    'handling': 'follows_mode' if effect == 'allow' else 'scope_fallback'})
        for key in ('rules', 'governance', 'governance_policy', *_SOURCE_CONFIGURATION_KEYS):
            visit(item.get(key))

    visit(value)
    return list({source_digest(reason): reason for reason in reasons}.values())


def approval_review(reasons):
    """The Workspace settings marker (``settings.approval_review``) for the owner."""
    return {'required': True, 'reasons': deepcopy(reasons), 'plan_mode': PLAN_MODE}


def _entry_severity(target, contents):
    path = target.get('path') or []
    if target.get('kind') == 'governance':
        return _severity(contents)
    if target.get('kind') != 'workspace_configuration':
        return None
    values = [content.get('value') for content in contents if isinstance(content, dict)]
    if path in (['operating_model'], ['operating_model', 'rules'], ['operating_model', 'governance']):
        return _severity(values)
    if _flattened_approval(target):
        return _severity([{path[2]: value} for value in values])
    return None


def _baseline_severity(baseline):
    if not isinstance(baseline, dict):
        return None
    configuration = baseline.get('configuration', baseline)
    return _maximum(None, *(_entry_severity(entry, [entry.get(name)
        for name in ('source_content', 'installed_content', 'blueprint_content')])
        for entry in (configuration.get('entries') or {}).values()))


def _restore_severity(point):
    if not isinstance(point, dict):
        return None
    return _maximum(_baseline_severity(point.get('baseline_before')), *(
        _entry_severity(entry.get('target') or {}, [entry.get('before'), entry.get('after'), *(
            (entry.get('target') or {}).get(name) for name in ('source_content', 'installed_content', 'blueprint_content'))])
        for entry in point.get('configuration') or []))


def _fallback_selectors(severity, inherited):
    if severity is None:
        return []
    selectors = [{'kind': 'category', 'category': category} for category in _FALLBACK_CATEGORIES]
    if severity == 'ask':
        # Specificity outranks locality. Mirror current root grants as local
        # asks so an action/resource allow cannot defeat the scope review gate.
        # Reading is never gated, so a root read-category grant is not mirrored.
        selectors.extend(deepcopy(rule['selector']) for rule in inherited
            if rule['effect'] == 'allow' and rule['selector'] != _READ_SELECTOR)
    return list({source_digest(selector): selector for selector in selectors}.values())


def _policy(raw, *, severity=None, inherited=(), root=None):
    # No legacy row was the old *system* default, not a user choice: no local
    # override, so the owner follows its account (Q4).
    raw = deepcopy(raw if raw is not None else {})
    if not isinstance(raw, dict):
        raise ValueError('Approval source policy must be an object')
    severity = _maximum(severity, _severity(raw))
    rules, mode = [], None
    if raw.get('approval') is not None:
        approval = raw['approval']
        if 'root_entity_id' in approval:
            binding = ApprovalPolicyBinding.model_validate(approval)
            if root is not None and binding.root_entity_id != root:
                raise ValueError('Approval source root does not match owner')
            root = root or binding.root_entity_id
            override = binding.policy
        else:
            override = ApprovalPolicyOverride.model_validate(approval)
        mode = override.mode
        rules = [rule.model_dump() for rule in override.rules]
    for field, effect in _FIELDS.items():
        values = raw.get(field) or []
        if not isinstance(values, list):
            continue
        for value in values:
            if 'capabilities' in field:
                rules.extend({'id': _id(field, value, selector), 'effect': effect, 'selector': selector,
                    'source_kind': 'user'} for selector in _capability_selectors(value, effect) or [])
                continue
            selector = _exact_action(value)
            if selector is not None:
                rules.append({'id': _id(field, value), 'effect': effect, 'selector': selector, 'source_kind': 'user'})
    if severity == 'ask':
        rules = [{**rule, 'effect': 'ask' if rule['effect'] == 'allow' else rule['effect']} for rule in rules]
        mode = 'confirm_changes'
    rules.extend({'id': _id('fallback', severity, selector), 'effect': severity, 'selector': selector, 'source_kind': 'user'}
        for selector in _fallback_selectors(severity, inherited))
    rules = list({rule['id']: rule for rule in rules}.values())
    override = ApprovalPolicyOverride(mode=mode, rules=[ApprovalRule.model_validate(rule) for rule in rules])
    approval = ApprovalPolicyBinding(root_entity_id=root, policy=override).model_dump() if root else override.model_dump()
    return policy_to_dict(policy_from_dict({
        **{key: value for key, value in raw.items() if key not in LEGACY_POLICY_KEYS and key != 'approval'},
        'approval': approval,
    }))


def _rules(rules, *, severity=None, inherited=(), append_fallback=True):
    if not isinstance(rules, list):
        raise ValueError('Approval source rules must be a list')
    result = []
    for value in rules:
        if not isinstance(value, dict):
            result.append(deepcopy(value))
            continue
        rule = deepcopy(value)
        effect = _EFFECTS.get(str(rule.get('rule_type') or '').strip().lower())
        if effect is None:
            result.append(rule)
            continue
        selectors, unsupported = _source_selectors(rule, effect)
        if unsupported and effect != 'allow':
            # An unrepresentable grant keeps only its exact selectors.
            selectors.extend(_fallback_selectors(effect, inherited))
        if effect == 'allow' and severity == 'ask':
            effect = 'ask'
        rule['rule_type'] = {'allow': 'auto_approve', 'ask': 'approval_required', 'deny': 'deny'}[effect]
        rule['approval_selectors'] = list({source_digest(item): item for item in selectors}.values())
        for key in _LEGACY_RULE_KEYS:
            rule.pop(key, None)
        result.append(rule)
    if severity is not None and append_fallback:
        fallback = {'rule_key': _id('source-fallback', severity),
            'rule_type': 'deny' if severity == 'deny' else 'approval_required',
            'description': 'Reviewed migration: unrepresentable legacy approval rules require scope-wide ' + ('blocking.' if severity == 'deny' else 'human review.'),
            'approval_selectors': _fallback_selectors(severity, inherited)}
        if not any(item == fallback for item in result):
            if any(isinstance(item, dict) and item.get('rule_key') == fallback['rule_key'] for item in result):
                raise ValueError('Migration source rule identity conflicts with an existing rule')
            result.append(fallback)
    return result


def _tree(value, *, severity=None, inherited=(), confirm=False):
    """Convert declared Workspace/Draft sources, never arbitrary business JSON."""
    if not isinstance(value, dict):
        return deepcopy(value)
    result = deepcopy(value)
    result.pop('automated_runs_always_allow', None)
    for key in ('governance', 'governance_policy'):
        if isinstance(value.get(key), dict):
            result[key] = _policy(value[key], severity=severity, inherited=inherited)
    for key in _SOURCE_CONFIGURATION_KEYS:
        if isinstance(value.get(key), dict):
            result[key] = _tree(value[key], severity=severity, inherited=inherited, confirm=confirm)
    if isinstance(value.get('rules'), list):
        result['rules'] = _rules(value['rules'], severity=severity, inherited=inherited)
    if isinstance(result.get('rules'), list):
        from packages.core.services.workspace_setup_service import workspace_rule_fingerprint
        confirmations = dict(value.get('_explicit_hard_block_rules') or {})
        for rule in result['rules']:
            if not isinstance(rule, dict) or not rule.get('rule_key'):
                continue
            key = str(rule['rule_key'])
            old = next((item for item in value['rules'] if isinstance(item, dict) and str(item.get('rule_key')) == key), None)
            previously_confirmed = old is not None and confirmations.get(key) == workspace_rule_fingerprint(old)
            if previously_confirmed or (confirm and rule.get('rule_type') == 'deny'):
                confirmations[key] = workspace_rule_fingerprint(rule)
        if confirmations:
            result['_explicit_hard_block_rules'] = confirmations
    return result


def _flattened_approval(entry):
    path = entry.get('path') or []
    return (entry.get('kind') == 'workspace_configuration' and len(path) == 3
        and path[:2] == ['operating_model', 'governance'] and path[2] in LEGACY_POLICY_KEYS)


def _approval_target():
    identity = 'operating_model/governance/approval'
    return {'key': 'workspace_configuration:' + identity, 'kind': 'workspace_configuration',
        'identity': identity, 'path': ['operating_model', 'governance', 'approval']}


def _baseline(value, *, severity, inherited, root=None):
    if not isinstance(value, dict):
        return deepcopy(value)
    result = deepcopy(value)
    configuration = result.get('configuration', result)
    entries = configuration.get('entries', {}) if isinstance(configuration, dict) else {}
    flattened = {key: entry for key, entry in entries.items() if _flattened_approval(entry)}
    if flattened:
        target = _approval_target()
        if target['key'] in entries:
            raise ValueError('Blueprint baseline mixes legacy and current approval ownership')
        for name in ('source_content', 'installed_content', 'blueprint_content'):
            if not any(name in entry for entry in flattened.values()):
                continue
            raw = {}
            for entry in flattened.values():
                content = entry.get(name)
                if content is not None:
                    if not isinstance(content, dict) or 'value' not in content:
                        raise ValueError('Flattened Blueprint policy content must carry a value')
                    raw[entry['path'][2]] = deepcopy(content['value'])
            target[name] = {'value': _policy(raw, severity=severity, inherited=inherited,
                root=root if name == 'installed_content' else None)['approval']} if raw else None
        for key in flattened:
            entries.pop(key)
        entries[target['key']] = target
    for entry in entries.values():
        path = entry.get('path') or []
        if any(key in LEGACY_POLICY_KEYS for key in path):
            raise ValueError('Flattened legacy governance baseline needs a complete policy projection')
        for name in ('source_content', 'installed_content', 'blueprint_content'):
            content = entry.get(name)
            if entry.get('kind') == 'governance' and isinstance(content, dict):
                from packages.core.blueprints.configuration_upgrade import _governance_content
                entry[name] = _governance_content(_policy(content, severity=severity, inherited=inherited, root=root))
            elif path == ['operating_model'] and isinstance(content, dict) and isinstance(content.get('value'), dict):
                from packages.core.blueprints.configuration_upgrade import _governance_content
                converted = _tree(content['value'], severity=severity, inherited=inherited, confirm=True)
                if isinstance(content['value'].get('governance'), dict):
                    policy = _policy(content['value']['governance'], severity=severity, inherited=inherited, root=root)
                    converted['governance'] = policy if name == 'installed_content' else _governance_content(policy)
                entry[name] = {**content, 'value': converted}
            elif path == ['operating_model', 'governance'] and isinstance(content, dict) and isinstance(content.get('value'), dict):
                from packages.core.blueprints.configuration_upgrade import _governance_content
                policy = _policy(content['value'], severity=severity, inherited=inherited, root=root)
                entry[name] = {**content, 'value': policy if name == 'installed_content' else _governance_content(policy)}
            elif path == ['operating_model', 'governance', 'approval'] and isinstance(content, dict) and isinstance(content.get('value'), dict):
                from packages.core.blueprints.configuration_upgrade import _governance_content
                policy = _policy({'approval': content['value']}, severity=severity, inherited=inherited, root=root)
                entry[name] = {**content, 'value': (policy if name == 'installed_content' else _governance_content(policy))['approval']}
            elif path[:2] == ['operating_model', 'rules'] and isinstance(content, dict) and isinstance(content.get('value'), list):
                entry[name] = {**content, 'value': _rules(content['value'], severity=severity, inherited=inherited)}
    return result


def _path_content(workspace, path):
    value = workspace
    for key in path:
        if isinstance(value, dict) and key in value:
            value = value[key]
        elif isinstance(value, list) and isinstance(key, int) and 0 <= key < len(value):
            value = value[key]
        else:
            return None
    return {'value': deepcopy(value)}


def _restore(point, *, before_workspace, after_workspace, before_policy, after_policy, severity, inherited):
    if point is None:
        return None
    from packages.core.blueprints.configuration_upgrade import _governance_content, configuration_fingerprint
    result = deepcopy(point)
    if not isinstance(result, dict):
        raise ValueError('Blueprint restore receipt must be an object')
    if 'baseline_before' in result:
        result['baseline_before'] = _baseline(result['baseline_before'], severity=severity,
            inherited=inherited, root=before_workspace['entity_id'])
    flattened = [entry for entry in result.get('configuration') or [] if _flattened_approval(entry.get('target') or {})]
    if flattened:
        target = _approval_target()
        if any(entry.get('target', {}).get('key') == target['key'] for entry in result['configuration']):
            raise ValueError('Blueprint restore mixes legacy and current approval ownership')
        old_governance = deepcopy((before_workspace.get('operating_model') or {}).get('governance') or {})
        for entry in flattened:
            path = entry['target']['path']
            old_content = _path_content(before_workspace, path)
            if entry.get('expected_fingerprint') != configuration_fingerprint({'content': old_content, 'revision': None}):
                raise ValueError('Blueprint restore receipt is stale; cannot rebind it during approval migration')
            before = entry.get('before')
            if before is None:
                old_governance.pop(path[2], None)
            elif isinstance(before, dict) and 'value' in before:
                old_governance[path[2]] = deepcopy(before['value'])
            else:
                raise ValueError('Flattened Blueprint restore content must carry a value')
        restored = _policy(old_governance, severity=severity, inherited=inherited, root=before_workspace['entity_id'])
        new_content = _path_content(after_workspace, target['path'])
        if new_content is None:
            raise ValueError('Converted Blueprint approval restore target is missing')
        result['configuration'] = [entry for entry in result['configuration'] if entry not in flattened]
        # The aggregate receipt is created only after every original leaf
        # receipt proved ownership of its exact current preimage above.
        merged_receipt = {'target': target, 'before': {'value': restored['approval']}, 'after': new_content,
            'expected_fingerprint': configuration_fingerprint({'content': new_content, 'revision': None})}
    else:
        merged_receipt = None
    for entry in result.get('configuration') or []:
        target = entry.get('target') or {}
        kind, path = target.get('kind'), target.get('path') or []
        if kind == 'governance':
            if before_policy is None:
                raise ValueError('Blueprint governance restore has no existing policy owner')
            old_raw = before_policy['policy']
            if 'approval' in old_raw:
                old_content = _governance_content(old_raw)
            else:
                old_content = {key: deepcopy(old_raw.get(key) or []) for key in _FIELDS}
                old_content.update(max_risk_level=old_raw.get('max_risk_level', 'high'), budget_caps_per_kind=old_raw.get('budget_caps_per_kind', {}))
            new_content = _governance_content(after_policy['policy'])
            old_revision, new_revision = before_policy['revision'], after_policy['revision']
            def convert(content):
                return _governance_content(_policy(content, severity=severity, inherited=inherited,
                    root=before_workspace['entity_id'])) if content is not None else None
        elif kind == 'workspace_configuration' and (path == ['operating_model']
                or path[:2] in (['operating_model', 'rules'], ['operating_model', 'governance'])):
            old_content, new_content = _path_content(before_workspace, path), _path_content(after_workspace, path)
            old_revision = new_revision = None
            def convert(content):
                if content is None:
                    return None
                value = content.get('value')
                if path == ['operating_model'] and isinstance(value, dict):
                    converted = _tree(value, severity=severity, inherited=inherited, confirm=True)
                    if isinstance(value.get('governance'), dict):
                        converted['governance'] = _policy(value['governance'], severity=severity,
                            inherited=inherited, root=before_workspace['entity_id'])
                elif path == ['operating_model', 'governance'] and isinstance(value, dict):
                    converted = _policy(value, severity=severity, inherited=inherited, root=before_workspace['entity_id'])
                elif path == ['operating_model', 'governance', 'approval'] and isinstance(value, dict):
                    converted = _policy({'approval': value}, severity=severity, inherited=inherited,
                        root=before_workspace['entity_id'])['approval']
                elif path[:2] == ['operating_model', 'rules'] and isinstance(value, list):
                    converted = _rules(value, severity=severity, inherited=inherited)
                else:
                    converted = _tree(value, severity=severity, inherited=inherited, confirm=True)
                return {**content, 'value': converted}
        else:
            if _severity(entry) or any(key in LEGACY_POLICY_KEYS for key in path):
                raise ValueError('Unsupported legacy Blueprint restore target')
            continue
        if entry.get('expected_fingerprint') != configuration_fingerprint({'content': old_content, 'revision': old_revision}):
            raise ValueError('Blueprint restore receipt is stale; cannot rebind it during approval migration')
        for name in ('before', 'after'):
            if name in entry:
                entry[name] = convert(entry[name])
        for name in ('source_content', 'installed_content', 'blueprint_content'):
            if name in target:
                target[name] = convert(target[name])
        entry['after'] = deepcopy(new_content)
        entry['expected_fingerprint'] = configuration_fingerprint({'content': new_content, 'revision': new_revision})
    if merged_receipt is not None:
        result['configuration'].append(merged_receipt)
    # Published Blueprint fingerprints and unrelated component journals are
    # immutable provenance; only configuration evidence above is rebound.
    return result


_PERSONAL_FILE_PERMISSION_KEYS = ('ai_file_permission', 'file_permission', 'file_permissions')


def _personal(preferences, inherited):
    runtime = preferences.get('runtime_approval_policy') or {}
    chosen = {key: value for key, value in preferences.items() if key != 'runtime_approval_policy'}
    # Without a saved file permission the old personal gate used its system
    # default (ask). That is not the person's choice: they follow their mode
    # like a missing Workspace policy follows the account (Q4).
    base = (_personal_override(chosen) if any(chosen.get(key) is not None for key in _PERSONAL_FILE_PERMISSION_KEYS)
            else ApprovalPolicyOverride(mode=None, rules=[]).model_dump())
    raw, severity = {}, None
    modes = {'always_approve': 'allow', 'always approve': 'allow', 'always_allow': 'allow', 'always allow': 'allow', 'allow': 'allow',
        'approval': 'ask', 'approve': 'ask', 'ask': 'ask', 'ask_each_time': 'ask', 'manual': 'ask',
        'deny': 'deny', 'denied': 'deny', 'never': 'deny', 'block': 'deny', 'blocked': 'deny'}
    if not isinstance(runtime, dict) or set(runtime) - {'global'}:
        raise ValueError('Unknown personal runtime policy structure')
    scoped = runtime.get('global') or {}
    if not isinstance(scoped, dict) or set(scoped) - {'actions', 'capabilities'}:
        raise ValueError('Unknown personal approval scope structure')
    for axis, values in scoped.items():
        if not isinstance(values, dict):
            raise ValueError('Personal approval selectors must map to modes')
        for selector, mode in values.items():
            effect = modes.get(str(mode).strip().lower())
            if effect is None:
                severity = _maximum(severity, 'ask')
                continue
            field = {'deny': 'never_allow', 'ask': 'hitl_required', 'allow': 'auto_approve'}[effect] + '_' + axis
            raw.setdefault(field, []).append(selector)
    raw['approval'] = base
    severity = _maximum(severity, _severity(raw))
    return _policy(raw, severity=severity, inherited=inherited)['approval']


def generate_resolutions(source: dict, *, accept_new_semantics: bool = False, legacy_chrome_grant_ids: list[str] | None = None) -> dict:
    if accept_new_semantics is not True:
        raise ValueError('Explicit acceptance of the conservative new approval semantics is required')
    resolutions = {}
    approved_ids = [] if legacy_chrome_grant_ids is None else legacy_chrome_grant_ids
    if (not isinstance(approved_ids, list) or any(not isinstance(value, str) or not value for value in approved_ids)
            or len(set(approved_ids)) != len(approved_ids)):
        raise ValueError('Legacy Chrome grant IDs must be a unique list of nonempty strings')
    if set(approved_ids) - {row['id'] for row in source.get('hitl_requests', [])}:
        raise ValueError('Approved legacy Chrome grant is absent from the current source')
    entities = {row['id']: row for row in source['entities']}
    def inherited(entity_id):
        defaults = (entities[entity_id].get('settings') or {}).get('approval_defaults')
        return [rule.model_dump() for rule in ApprovalPolicy.model_validate(defaults['policy']).rules] if defaults else []
    policies = {row['workspace_id']: row for row in source['governance_policies']}
    workspaces = {row['id']: row for row in source['workspaces']}
    workspace_levels = {}
    for workspace in source['workspaces']:
        raw = _without_system_default(policies.get(workspace['id'], {}).get('policy'))
        operating = workspace.get('operating_model') or {}
        severity = _severity([raw, operating])
        reasons = review_reasons([raw, operating])
        settings = workspace.get('settings') or {}
        # Retained v1 behaviour (Q6): legacy standing consents need review.
        if (settings.get('_blueprint') or {}).get('governance_preset') == 'aggressive':
            severity = _maximum(severity, 'ask')
            reasons.append({'code': 'legacy_consent_requires_review', 'field': 'governance_preset', 'handling': 'scope_fallback'})
        if (settings.get('strategist') or {}).get('auto_approve_proposals') is True:
            severity = _maximum(severity, 'ask')
            reasons.append({'code': 'legacy_consent_requires_review', 'field': 'auto_approve_proposals', 'handling': 'scope_fallback'})
        workspace_levels[workspace['id']] = severity
        root_rules = inherited(workspace['entity_id'])
        converted = _tree(operating, severity=severity, inherited=root_rules)
        if isinstance(operating.get('governance'), dict):
            converted['governance'] = _policy(operating['governance'], severity=severity,
                inherited=root_rules, root=workspace['entity_id'])
        if severity is not None and 'rules' not in converted:
            converted['rules'] = _rules([], severity=severity, inherited=root_rules)
        resolutions[f"workspace:{workspace['id']}"] = {
            'policy': _policy(raw, severity=severity, inherited=root_rules, root=workspace['entity_id']),
            'operating_model': converted, 'reconcile_history': True,
        }
        if reasons:
            resolutions[f"workspace:{workspace['id']}"]['approval_review'] = approval_review(reasons)
    for user in source['users']:
        preferences = user.get('preferences') or {}
        roots = {user['entity_id']} | {row['entity_id'] for row in source['user_memberships'] if row['user_id'] == user['id']}
        for root in sorted(roots):
            if root not in (preferences.get('approval_policy') or {}):
                resolutions[f"personal:{user['id']}:{root}"] = _personal(preferences, inherited(root))
    for room in source['chat_rooms']:
        if not room.get('approval_policy') and room.get('ai_file_actions_direct_enabled'):
            owner = room.get('owner_entity_id') or next((row['entity_id'] for row in source['users'] if row['id'] == room.get('owner_user_id')), None)
            resolutions[f"room:{room['id']}"] = _policy({}, severity='ask', inherited=inherited(owner))['approval']
    for task in source.get('tasks', []):
        details = task.get('details') or {}
        runtime = details.get('runtime_context') or {}
        if 'rules' in runtime:
            active_rules = [rule for rule in runtime['rules'] if not isinstance(rule, dict) or rule.get('enabled') is not False]
            resolutions[f"task:{task['id']}"] = {**deepcopy(details), 'runtime_context': {
                **deepcopy(runtime), 'rules': _rules(runtime['rules'], severity=_severity(active_rules))}}
    for draft in source.get('workspace_drafts', []):
        if draft['status'] in ('active', 'ready'):
            fields = draft.get('fields') or {}
            severity = _severity(fields)
            converted = _tree(fields, severity=severity, inherited=inherited(draft['entity_id']), confirm=True)
            if severity is not None and 'rules' not in converted:
                converted = _tree({**converted, 'rules': []}, severity=severity, inherited=inherited(draft['entity_id']), confirm=True)
            resolutions[f"draft:{draft['id']}"] = converted
    for request in source.get('hitl_requests', []):
        context = request.get('context') or {}
        scope = f"request:{request['id']}"
        if request['id'] in approved_ids:
            # The formal preflight validates ownership/effect evidence, including
            # changed or consumed records. Never silently skip an approved ID.
            resolutions[scope] = dict(_EXPIRE_LEGACY_CHROME_GRANT)
            continue
        if request.get('hitl_type') != 'authorize' or request.get('consumed_at'):
            continue
        if request['status'] == 'granted' and not isinstance(context.get('approval_requirements'), list):
            resolutions[scope] = dict(_EXPIRE_UNVERIFIABLE_REQUEST)
        elif request['status'] == 'pending' and not context.get('operation_id') and context.get('kind') != 'provider':
            if request.get('origin_kind') in ('tool_call', 'operation'):
                resolutions[scope] = dict(_EXPIRE_UNVERIFIABLE_REQUEST)
            elif request.get('origin_kind') == 'step' and not context.get('args_hash'):
                resolutions[scope] = dict(_EXPIRE_UNIDENTIFIED_FAILED_STEP_REQUEST
                    if request.get('action_key') in (None, '') else _EXPIRE_FAILED_TASK_REQUEST)
    for request in source.get('chat_room_tool_approvals', []):
        if request['status'] in ('pending', 'approved') and not isinstance(request.get('approval_requirements'), list):
            resolutions[f"room_request:{request['id']}"] = dict(_INVALIDATE_UNVERIFIABLE_ROOM_REQUEST)
    for conversation in source.get('conversations', []):
        for key, receipt in ((conversation.get('metadata') or {}).get('file_approvals') or {}).items():
            if (isinstance(receipt, dict) and receipt.get('status') in ('pending', 'approved')
                    and (not receipt.get('operation_id') or not receipt.get('approval_action_key') or not isinstance(receipt.get('approval_requirements'), list))):
                resolutions[f"file_request:{conversation['id']}:{key}"] = dict(_EXPIRE_UNVERIFIABLE_REQUEST)
    # Compute the actual post-migration policy revision before re-binding an
    # existing configuration restore receipt to the converted current owner.
    interim = preflight_sources(source, resolutions=resolutions)
    projected = deepcopy(source)
    for operation in interim['operations']:
        table = projected[operation['table']]
        row = next((row for row in table if row[operation['key']] == operation['id']), None)
        if row is None:
            row = {operation['key']: operation['id']}
            table.append(row)
        row.update(deepcopy(operation['values']))
    for state in source.get('workspace_blueprint_upgrade_states', []):
        workspace = workspaces[state['workspace_id']]
        if state['entity_id'] != workspace['entity_id']:
            raise ValueError('Blueprint upgrade state owner does not match its Workspace')
        root_rules = inherited(state['entity_id'])
        severity = _maximum(workspace_levels[state['workspace_id']], _baseline_severity(state['baseline']), _restore_severity(state['restore_point']))
        after_workspace = next(row for row in projected['workspaces'] if row['id'] == workspace['id'])
        after_policy = next(row for row in projected['governance_policies'] if row['workspace_id'] == workspace['id'])
        resolutions[f"upgrade_state:{state['workspace_id']}"] = {
            'baseline': _baseline(state['baseline'], severity=severity, inherited=root_rules, root=workspace['entity_id']),
            'restore_point': _restore(state['restore_point'], before_workspace=workspace, after_workspace=after_workspace,
                before_policy=policies.get(workspace['id']), after_policy=after_policy, severity=severity, inherited=root_rules),
        }
    return resolutions


def build_plan(source: dict, *, accept_new_semantics: bool = False, legacy_chrome_grant_ids: list[str] | None = None) -> dict:
    resolutions = generate_resolutions(source, accept_new_semantics=accept_new_semantics,
        legacy_chrome_grant_ids=legacy_chrome_grant_ids)
    return {**preflight_sources(source, resolutions=resolutions), 'plan_mode': PLAN_MODE}
