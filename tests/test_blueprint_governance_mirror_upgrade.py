"""Blueprint governance mirrors never become a second configuration writer."""
from copy import deepcopy

import pytest

from packages.core.blueprints import configuration_upgrade as upgrade
from packages.core.governance.approval_policy import ApprovalPolicyBinding
from packages.core.governance.approval_projection import policy_override
from packages.core.governance.policy import policy_to_dict
from packages.core.services.workspace_setup_service import _build_governance_policy_from_rules
from test_blueprint_unified_approval_upgrade import installed, manual_rule, payload


@pytest.mark.parametrize('mirror', [
    {'approval': {'mode': 'automatic', 'rules': []}, 'max_risk_level': 'low'},
    {'auto_approve_actions': ['workspace.file.modify'], 'max_risk_level': 'low'},
])
def test_governance_mirror_is_not_flattened_into_independent_write_targets(mirror):
    incoming = payload()
    incoming['recipe']['operating_model'].update(governance=deepcopy(mirror), business={'mode': 'kanban'})
    targets = upgrade._targets(incoming, 'workspace')
    assert 'governance:policy' in targets
    assert targets['workspace_configuration:operating_model/business/mode']['source_content'] == {'value': 'kanban'}
    assert not any(target.get('path', [])[:2] == ['operating_model', 'governance'] for target in targets.values())
    assert incoming['recipe']['operating_model']['governance'] == mirror


@pytest.mark.asyncio
@pytest.mark.parametrize('old_path', [
    ['operating_model', 'governance', 'approval'],
    ['operating_model', 'governance', 'auto_approve_actions'],
])
async def test_upgrade_and_revert_keep_bound_mirror_and_local_policy_with_historical_baseline(monkeypatch, old_path):
    db, workspace, row, baseline, _ = await installed(monkeypatch)
    local = policy_override(row.policy)
    local.mode = 'automatic'
    local.rules.append(manual_rule())
    row.policy['approval']['policy'] = local.model_dump(mode='json')
    row.revision += 1
    workspace.operating_model.update(governance=deepcopy(row.policy), business={'mode': 'kanban'})
    original = deepcopy(workspace.operating_model)
    prior_policy = deepcopy(row.policy)
    # Converted baselines can have one aggregate approval receipt; older
    # packages used individual legacy leaves. Neither owns a future write.
    key = 'workspace_configuration:' + '/'.join(old_path)
    historical = {'value': deepcopy(row.policy['approval'])} if old_path[-1] == 'approval' else {'value': []}
    baseline['entries'][key] = {'key': key, 'kind': 'workspace_configuration',
        'identity': '/'.join(old_path), 'path': old_path, 'source_content': deepcopy(historical),
        'installed_content': deepcopy(historical), 'blueprint_content': deepcopy(historical)}
    incoming = payload('communication')
    incoming['recipe']['operating_model'].update(
        governance={'approval': {'mode': None, 'rules': []}, 'max_risk_level': 'low'},
        business={'mode': 'table'},
    )
    incoming['policy']['governance'] = {'max_risk_level': 'high'}
    plan = await upgrade.plan_configuration(db, workspace=workspace, payload=incoming, baseline=baseline)
    assert not any(target.get('path', [])[:2] == ['operating_model', 'governance'] for target in plan['targets'].values())
    with pytest.raises(upgrade.ConfigurationUpgradeError, match='Review every configuration conflict'):
        await upgrade.apply_configuration(db, workspace=workspace, payload=incoming, plan=plan)
    result = await upgrade.apply_configuration(db, workspace=workspace, payload=incoming, plan=plan,
        choices={'workspace_configuration:operating_model/business/mode': 'use_blueprint'})
    ApprovalPolicyBinding.model_validate(workspace.operating_model['governance']['approval'])
    assert workspace.operating_model['governance'] == row.policy
    assert workspace.operating_model['governance'] != original['governance']
    assert workspace.operating_model['business'] == {'mode': 'table'}
    assert manual_rule() in policy_override(row.policy).rules
    assert row.policy['approval']['root_entity_id'] == workspace.entity_id
    rebuilt = _build_governance_policy_from_rules(workspace.operating_model['rules'], workspace.operating_model['governance'])
    assert upgrade._governance_content(policy_to_dict(rebuilt)) == upgrade._governance_content(row.policy)
    assert not any(entry.get('path', [])[:2] == ['operating_model', 'governance'] for entry in result['baseline']['entries'].values())
    again = await upgrade.plan_configuration(db, workspace=workspace, payload=incoming, baseline=result['baseline'])
    assert all(item['action'] == 'unchanged' for item in again['items'])
    await upgrade.revert_configuration(db, workspace=workspace, restore=result['restore'])
    assert {key: value for key, value in workspace.operating_model.items() if key != 'governance'} == {
        key: value for key, value in original.items() if key != 'governance'}
    assert workspace.operating_model['governance'] == row.policy
    assert row.policy['approval']['root_entity_id'] == workspace.entity_id
    assert upgrade._governance_content(workspace.operating_model['governance']) == upgrade._governance_content(original['governance'])
    assert upgrade._governance_content(row.policy) == upgrade._governance_content(prior_policy)
