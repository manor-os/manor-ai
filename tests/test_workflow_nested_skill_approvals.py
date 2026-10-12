"""Workflow-owned nested Skill approvals preserve the complete execution tree."""
import copy
import json
from collections import Counter

import pytest

from packages.core.ai import agentic_loop as loops
from packages.core.ai.runtime import harness
from packages.core.ai.runtime.nested_agent import (
    runtime_checkpoint_leaf_call_id, runtime_nested_skill_scope,
    runtime_pending_checkpoint_leaf,
)


def tool(name):
    return {'type': 'function', 'function': {'name': name, 'parameters': {'type': 'object'}}}


@pytest.mark.asyncio
@pytest.mark.parametrize('depth', [1, 2])
async def test_workflow_nested_skill_resumes_leaf_without_replaying_completed_work(monkeypatch, depth):
    calls = Counter()
    approved = False

    async def completion(messages, tools, **kwargs):
        label = messages[0]['content'].splitlines()[0]
        results = [m for m in messages if m.get('role') == 'tool']
        if label.startswith('parent'):
            if not results:
                level = int(label.split('-')[1])
                return '', [{'id': 'child', 'name': 'invoke_skill', 'arguments': {'level': level + 1}}], {}
            return '{"saved":true}', [], {}
        if not results:
            return '', [{'id': 'before', 'name': 'write_note', 'arguments': {'text': 'before'}}], {}
        if len(results) == 1:
            return '', [{'id': 'gated', 'name': 'write_file', 'arguments': {'text': 'gated'}}], {}
        return 'Saved child file.', [], {}

    async def execute(name, args):
        if name == 'invoke_skill':
            level = args['level']
            with runtime_nested_skill_scope(f'skill-{level}') as frame:
                assert frame is not None, 'Workflow host must retain the nested Skill continuation'
                child = await loops.agentic_loop(
                    system_prompt=f'parent-{level}' if level < depth else 'leaf', user_message='Save once',
                    tools=[tool('invoke_skill')] if level < depth else [tool('write_note'), tool('write_file')],
                    tool_executor=execute, max_rounds=5, **frame.loop_kwargs(),
                )
                response = {'content': child.content, 'stop_reason': child.stop_reason, 'control': child.control}
                await frame.complete(response, child.messages)
                return json.dumps(response)
        if name == 'write_file' and not approved:
            return json.dumps({'__hitl__': True, 'error': 'approval_required', 'approval_token': 'request-1',
                              'hitl': {'id': 'request-1', 'type': 'approval', 'prompt': 'Approve?', 'tool': name}})
        calls[args['text']] += 1
        return '{"ok":true}'

    async def loop(**kwargs):
        return await loops.agentic_loop(
            system_prompt='parent-0', user_message='Save once', tools=[tool('invoke_skill')],
            tool_executor=execute, max_rounds=5, hitl_resume_checkpoint=True,
            resume_checkpoint=kwargs.get('runtime_checkpoint'),
        )

    monkeypatch.setattr(loops, 'runtime_execute_agentic_round_tool_completion', completion)
    monkeypatch.setattr(harness, 'runtime_execute_agentic_loop', loop)
    result = await harness.runtime_execute_workflow_agent_loop(hitl_resume_checkpoint=True)
    assert result.stop_reason == 'hitl_required'
    checkpoint = copy.deepcopy(result.control['checkpoint'])
    leaf = runtime_pending_checkpoint_leaf(checkpoint)
    assert leaf['pending_tool_call']['name'] == 'write_file'
    assert leaf['pending_tool_call']['id'] == 'gated'
    assert runtime_checkpoint_leaf_call_id(checkpoint).startswith('nested_')
    assert calls == {'before': 1}
    approved = True
    resumed = await harness.runtime_execute_workflow_agent_loop(
        hitl_resume_checkpoint=True, runtime_checkpoint=checkpoint,
    )
    assert resumed.stop_reason == 'completed'
    assert json.loads(resumed.content) == {'saved': True}
    assert calls == {'before': 1, 'gated': 1}


@pytest.mark.asyncio
@pytest.mark.parametrize('choice', ['approve', 'reject'])
async def test_nested_native_gate_binds_scoped_leaf_and_restores_only_its_token(client, db_session, monkeypatch, tmp_path, choice):
    from types import SimpleNamespace

    from packages.core.ai.runtime.surfaces import ChatSurface
    from packages.core.ai.runtime.workflow_tools import runtime_workflow_run_context
    from packages.core.models.hitl_request import HitlRequest
    from packages.core.models.workflow import WorkflowRun
    from packages.core.services import workflow_runtime_approvals as approvals
    from test_nested_skill_durability import checkpoint as make_checkpoint
    from test_workflow_native_approval_integration import approval_case, decide

    case = await approval_case(client, db_session, monkeypatch, tmp_path)
    await db_session.rollback()
    request = await db_session.get(HitlRequest, case.request_id)
    original = copy.deepcopy(request.context[approvals.CONTEXT_KEY]['payload'])
    leaf = make_checkpoint('read_file', 'child-read', original['call']['arguments'])
    root = make_checkpoint()
    root['tool_continuations'] = {'parent': {'nested_skill': {
        'version': 1, 'skill_id': 'skill-1', 'kind': 'pending', 'checkpoint': leaf,
    }}}
    scoped_id = runtime_checkpoint_leaf_call_id(root)
    request.context = {k: v for k, v in request.context.items() if k != approvals.CONTEXT_KEY}
    request.context = {**request.context, 'operation_id': f'runtime:{case.run_id}:call:{scoped_id}'}
    run = await db_session.get(WorkflowRun, case.run_id)
    run.status = 'running'
    context = runtime_workflow_run_context(run)
    context.update(workflow_run_id=run.id, workflow_step_id='read', workflow_execution_node_id='read')
    await db_session.flush()
    parked = await approvals.persist_workflow_runtime_approval_gate(
        db_session, step={'id': 'read'}, runtime_context=context,
        entity_id=run.entity_id, user_id=case.actor_id,
        control={'kind': 'approval_required', 'tool_call_id': 'parent', 'tool_name': 'read_file',
                 'checkpoint': root, 'blocking_result': json.dumps(original['hitl_data'])},
    )
    run.step_results = {**run.step_results, 'read': parked}
    run.status = 'paused'
    await db_session.commit()
    response = await decide(client, case, choice)
    assert response.status_code == 200, response.text
    await db_session.rollback()
    run = await db_session.get(WorkflowRun, case.run_id)
    await db_session.refresh(run)
    if choice == 'reject':
        assert run.status == 'failed'
        assert not case.wakes
        with pytest.raises(approvals.WorkflowRuntimeApprovalError, match='not granted'):
            await approvals.load_workflow_approval_resume(
                db_session, previous_result=run.step_results['read'], runtime_context=context,
                entity_id=run.entity_id, user_id=case.actor_id,
            )
        return
    run.status = 'running'
    await db_session.commit()
    resumed = await approvals.load_workflow_approval_resume(
        db_session, previous_result=run.step_results['read'], runtime_context=context,
        entity_id=run.entity_id, user_id=case.actor_id,
    )
    assert resumed['call']['id'] == scoped_id
    assert resumed['checkpoint']['pending_tool_call'] == root['pending_tool_call']
    restored_leaf = runtime_pending_checkpoint_leaf(resumed['checkpoint'])
    assert restored_leaf['pending_tool_call']['id'] == 'child-read'
    assert restored_leaf['pending_tool_call']['arguments']['approval_token'] == case.request_id
    assert 'approval_token' not in runtime_pending_checkpoint_leaf(root)['pending_tool_call']['arguments']
    arguments = {**resumed['call']['arguments'], '_workflow_run_id_from_context': run.id,
                 '_workflow_step_id_from_context': 'read', '_runtime_tool_call_id_from_context': scoped_id}
    request = await db_session.get(HitlRequest, case.request_id)
    envelope = SimpleNamespace(surface=ChatSurface.WORKFLOW_AGENT_STEP)
    await approvals.validate_workflow_approval_consumption(
        db_session, request=request, arguments=arguments, runtime_envelope=envelope,
    )
    with pytest.raises(approvals.WorkflowRuntimeApprovalError, match='another origin'):
        await approvals.validate_workflow_approval_consumption(
            db_session, request=request, arguments={**arguments, '_runtime_tool_call_id_from_context': 'parent'},
            runtime_envelope=envelope,
        )
