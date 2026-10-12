"""Rules Manor generates never gate reads; reads are allowed by every mode.

Only a person may put reads behind approval, with an explicit rule. Guards
the class: every generator that expands a rule over all approval categories.
"""
from pathlib import Path

import pytest

from packages.core.governance.approval_policy_migration import normalize_legacy_workspace_policy
from packages.core.governance.defaults import normalize_default_workspace_governance
from packages.core.governance.policy import WorkspacePolicy, policy_to_dict

ROOT = Path(__file__).resolve().parents[1]
READ = {'kind': 'category', 'category': 'read'}
# Every module that expands rules over all approval categories, each covered
# below; the cloud-only conversion is covered by its property tests in
# tests/test_approval_new_semantics.py and is absent from the OSS build.
CATEGORY_WIDE_GENERATORS = {
    'packages/core/governance/approval_policy_migration.py',
    'packages/core/governance/defaults.py',
    'packages/core/migrations/approval_new_semantics.py',
}


def _selectors(value):
    if isinstance(value, list):
        for item in value:
            yield from _selectors(item)
    elif isinstance(value, dict):
        if isinstance(value.get('selector'), dict):
            yield value['selector']
        for item in value.get('approval_selectors') or []:
            yield item
        for item in value.values():
            yield from _selectors(item)


@pytest.mark.parametrize('raw', [
    None,
    {'hitl_required_capabilities': ['external.email']},
    {'never_allow_actions': ['billing.*']},
    {'automated_runs_always_allow': True},
])
def test_blueprint_safe_import_review_never_covers_reads(raw):
    rules = normalize_legacy_workspace_policy(raw, safe_import=True)['approval']['rules']
    assert rules, 'the import still asks for review'
    assert READ not in list(_selectors(rules))


def test_template_risk_review_never_covers_reads():
    _, policy, _ = normalize_default_workspace_governance([], WorkspacePolicy(max_risk_level='low'))
    selectors = list(_selectors(policy_to_dict(policy)))
    assert selectors and READ not in selectors


def test_every_category_wide_generator_is_covered():
    found = {str(path.relative_to(ROOT)) for path in (ROOT / 'packages').rglob('*.py')
             if 'get_args(ApprovalCategory)' in path.read_text(encoding='utf-8')}
    assert found == {path for path in CATEGORY_WIDE_GENERATORS if (ROOT / path).exists()}, (
        'A module now expands rules over every approval category; exclude read '
        'and add it to the behaviour tests above.')
