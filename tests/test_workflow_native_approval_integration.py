"""Native Workflow approvals keep Runtime authority and resume the exact operation.

These tests use the real API, PostgreSQL, policy CAS, File Engine and runner.
The temporary storage root simulates a mount; broker delivery is captured so
each test explicitly delivers the persisted wake.
"""
from __future__ import annotations

import json
import os
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from approval_settings_test_utils import action_rule, set_workspace_test_approval
from packages.core.ai.workflow_runner import WorkflowRunner
from packages.core.blueprints.installer import _materialize_knowledge_pack_document
from packages.core.config import get_settings
from packages.core.models.document import DocumentGroup
from packages.core.models.hitl_request import HitlRequest
from packages.core.models.task import Conversation
from packages.core.models.user import User
from packages.core.models.workflow import WorkflowRun
from packages.core.models.workspace import Workspace
from test_workflows import _auth, _create_workspace


@dataclass
class ApprovalCase:
    headers: dict = field(repr=False)
    workspace_id: str
    actor_id: str
    workflow_id: str
    run_id: str
    request_id: str
    executions: Counter
    wakes: list
    body: str


def assert_public_only(payload):
    private_keys = {
        'workflow_runtime_approval_v1', 'continuation_token', 'checkpoint_signature',
        'resume_token', 'signed_continuation', 'agent_checkpoint', 'runtime_checkpoint',
        'pending_tool_call', 'checkpoint', 'signature',
    }

    def inspect(value):
        if isinstance(value, dict):
            assert private_keys.isdisjoint(value), private_keys.intersection(value)
            for child in value.values():
                inspect(child)
        elif isinstance(value, list):
            for child in value:
                inspect(child)

    inspect(payload)


async def approval_case(client, db, monkeypatch, tmp_path):
    settings = get_settings()
    monkeypatch.setattr(settings, 'MANOR_FS_ENABLED', True)
    monkeypatch.setattr(settings, 'MANOR_FS_ROOT', str(tmp_path))
    if settings.DEPLOYMENT_MODE == 'cloud':
        original_ismount = os.path.ismount
        monkeypatch.setattr(os.path, 'ismount', lambda path:
            os.path.abspath(path) == str(tmp_path) or original_ismount(path))
        (tmp_path / '.accesschk').touch()
    headers = await _auth(client, 'workflow_native_approval')
    workspace_id = await _create_workspace(client, headers, 'Approval integration')
    actor = (await db.execute(select(User).where(
        User.email == 'workflow_native_approval@test.com',
    ))).scalar_one()
    workspace = await db.get(Workspace, workspace_id)
    await set_workspace_test_approval(
        db, workspace_id=workspace_id, entity_id=actor.entity_id,
        actor_id=actor.id, mode='confirm_changes',
        user_rules=[action_rule('tool.read_file.read', effect='ask')],
    )
    group = DocumentGroup(entity_id=actor.entity_id, workspace_id=workspace_id,
                          name='Native approval source')
    db.add(group)
    await db.flush()
    body = 'Verified local source for the native approval integration test.\n'
    document, created = await _materialize_knowledge_pack_document(
        db, entity_id=actor.entity_id, workspace_id=workspace.id, group_id=group.id,
        knowledge_pack_slug='approval-integration',
        document={'key': 'source', 'path': 'approval-source.md', 'body_md': body},
    )
    assert document is not None and created
    document_id = document.id
    await db.commit()
    executions = Counter()
    wakes = []
    execute_step = WorkflowRunner._execute_step

    async def counted(self, step, run, db, *args, **kwargs):
        executions[step['id']] += 1
        return await execute_step(self, step, run, db, *args, **kwargs)

    def capture_wake(run_id, delay_seconds, *, continuation_token=None):
        wakes.append((run_id, continuation_token))
        return True

    monkeypatch.setattr(WorkflowRunner, '_execute_step', counted)
    monkeypatch.setattr(WorkflowRunner, 'enqueue_resume', staticmethod(capture_wake))
    response = await client.post('/api/v1/workflows', headers=headers, json={
        'name': 'Read a reviewed source',
        'steps': [
            {'id': 'trigger', 'type': 'trigger', 'config': {}, 'next': ['before']},
            {'id': 'before', 'type': 'transform',
             'config': {'set': {'prepared': True}}, 'next': ['read']},
            {'id': 'read', 'type': 'tool', 'name': 'Read source', 'config': {
                'tool': 'read_file', 'args': {'document_id': document_id},
                'output_var': 'source', 'output_format': 'json',
                'output_schema': {'type': 'object', 'required': ['content'],
                                  'properties': {'content': {'type': 'string'}}},
            }, 'next': ['end']},
            {'id': 'end', 'type': 'end', 'config': {}, 'next': []},
        ],
    })
    assert response.status_code == 201, response.text
    workflow_id = response.json()['id']
    response = await client.post('/api/v1/workflows/bindings', headers=headers, json={
        'workflow_id': workflow_id, 'workspace_id': workspace_id, 'trigger_type': 'manual',
        'config': {'workspace_attached': True, 'chat_entrypoint': {
            'enabled': True, 'title': 'Read source', 'wait_bridge': True,
            'projection': {'progress': True, 'step_outputs': 'explicit'},
        }},
    })
    assert response.status_code == 201, response.text
    binding_id = response.json()['id']
    response = await client.post(f'/api/v1/workflows/bindings/{binding_id}/run',
                                headers=headers, json={
                                    'execute': True,
                                    'trigger_data': {'workspace_manual': True},
                                })
    assert response.status_code == 201, response.text
    queued = response.json()
    assert queued['status'] in {'pending', 'running'} and not executions
    # Binding runs are queued; execute what the run_workflow worker would.
    await WorkflowRunner().run(queued['id'])
    response = await client.get(f"/api/v1/workflows/runs/{queued['id']}", headers=headers)
    assert response.status_code == 200, response.text
    run = response.json()
    assert run['status'] == 'paused', run
    assert run['current_step_id'] == 'read'
    paused = run['step_results']['read']
    assert paused['status'] == 'paused'
    assert paused['code'] == 'runtime_approval_required'
    assert paused['resume_strategy'] == 'blocked'
    assert 'source' not in run['variables'], 'approval envelopes are not tool outputs'
    request_id = paused['runtime_approval']['request_id']
    await db.rollback()
    request = await db.get(HitlRequest, request_id)
    assert request is not None and request.status == 'pending'
    assert request.workspace_id == workspace_id
    assert request.context.get('workflow_runtime_approval_v1')
    assert executions['before'] == 1 and executions['read'] == 1
    assert not wakes
    assert_public_only(run)
    return ApprovalCase(headers, workspace_id, actor.id, workflow_id, run['id'],
                        request_id, executions, wakes, body)


async def detail(client, case):
    response = await client.get(f'/api/v1/workflows/runs/{case.run_id}', headers=case.headers)
    assert response.status_code == 200, response.text
    return response.json()


async def decide(client, case, choice='approve', *, request_id=None, headers=None):
    return await client.post(
        f'/api/v1/workflows/runs/{case.run_id}/approvals/{request_id or case.request_id}/resolve',
        headers=headers or case.headers, json={'choice': choice},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name,deadline_state", [
    ("mcp__tiktok__post_video", "none"),
    ("mcp__tiktok__post_photo", "none"),
    ("mcp__tiktok__post_video", "future"),
    ("mcp__tiktok__post_video", "expired_on_open"),
    ("mcp__tiktok__post_photo", "expired_on_submit"),
])
async def test_scheduled_tiktok_review_uses_operator_conversation_and_resumes_once(
    client, db_session, monkeypatch, isolated_test_redis, tool_name, deadline_state,
):
    """A scheduler-run TikTok post retains its native review origin on resume."""
    from packages.core.ai.mcp import tiktok
    from packages.core.ai.runtime import approval_service as approvals
    from packages.core.ai.runtime.surfaces import ChatSurface
    from packages.core.ai.runtime.workflow_tools import (
        RuntimeWorkflowToolStepResult,
        runtime_workflow_tool_context_args,
        runtime_workflow_run_context,
    )
    from packages.core.models.scheduler import ScheduledJob
    from packages.core.services import tiktok_publish_review as review_service
    from packages.core.services.workflow_service import schedule_workflow
    from packages.core.tasks.scheduler_tasks import _dispatch_job
    from apps.api.routers.chat import refresh_tiktok_publish_review
    from tests.test_runtime_guard_unified_store import _runtime_envelope

    deadline_at = "2099-01-01T00:00:00Z"
    deadline_context = {} if deadline_state == "none" else {
        "workflow_effect_deadline_at": deadline_at,
        "workflow_effect_deadline_action_keys": ["social_post.publish"],
    }

    def expire_window():
        from packages.core.services import workflow_runtime_approvals

        class AfterDeadline(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2099, 1, 1, tzinfo=timezone.utc)

        monkeypatch.setattr(workflow_runtime_approvals, "datetime", AfterDeadline)

    headers = await _auth(client, "scheduled_tiktok_native")
    workspace_id = await _create_workspace(client, headers, "Scheduled TikTok review")
    photo = tool_name == "mcp__tiktok__post_photo"
    source_path = "uploads/chat/0123456789abcdef.jpg" if photo else "uploads/chat/0123456789abcdef.mp4"
    tool_args = {
        "photo_urls": [source_path],
        "title": "Scheduled post",
        "integration_account_id": "account-1",
    } if photo else {
        "video_url": source_path,
        "title": "Scheduled post",
        "integration_account_id": "account-1",
    }
    actor = (await db_session.execute(select(User).where(
        User.email == "scheduled_tiktok_native@test.com",
    ))).scalar_one()
    conversation = Conversation(
        entity_id=actor.entity_id,
        user_id=actor.id,
        workspace_id=workspace_id,
        scope="workspace_main",
        status="active",
    )
    db_session.add(conversation)
    await db_session.flush()
    await set_workspace_test_approval(
        db_session,
        workspace_id=workspace_id,
        entity_id=actor.entity_id,
        actor_id=actor.id,
        mode="confirm_changes",
        user_rules=[action_rule("social_post.publish", effect="allow")],
    )
    workflow_response = await client.post("/api/v1/workflows", headers=headers, json={
        "name": "Publish scheduled TikTok post",
        "steps": [
            {"id": "start", "type": "trigger", "config": {}, "next": ["publish" if deadline_state == "none" else "deadline"]},
            *([] if deadline_state == "none" else [{
                "id": "deadline", "type": "workflow_effect_deadline", "config": {
                    "deadline_at": deadline_at, "action_keys": ["social_post.publish"],
                }, "next": ["publish"],
            }]),
            {"id": "publish", "type": "tool", "name": "Publish TikTok", "config": {
                "tool": tool_name,
                "args": tool_args,
            }, "next": ["end"]},
            {"id": "end", "type": "end", "config": {}, "next": []},
        ],
    })
    assert workflow_response.status_code == 201, workflow_response.text
    workflow_id = workflow_response.json()["id"]
    job = await schedule_workflow(
        db_session,
        entity_id=actor.entity_id,
        workflow_id=workflow_id,
        workspace_id=workspace_id,
        cron="0 9 * * *",
        created_by=actor.id,
        conversation_id=conversation.id,
    )
    await db_session.commit()

    creator = {
        "creator_username": "manor",
        "creator_nickname": "Manor AI",
        "creator_avatar_url": "",
        "privacy_level_options": ["PUBLIC_TO_EVERYONE", "SELF_ONLY"],
        "comment_disabled": False,
        "duet_disabled": False,
        "stitch_disabled": False,
        "max_video_post_duration_sec": 300,
    }
    if photo:
        monkeypatch.setattr(
            review_service,
            "inspect_tiktok_photo_sources",
            AsyncMock(return_value=[{
                "source_kind": "chat_attachment",
                "rel_path": source_path,
                "source_sha256": "a" * 64,
                "size_bytes": 1024,
            }]),
        )
    else:
        monkeypatch.setattr(
            review_service,
            "inspect_chat_video",
            AsyncMock(return_value={
                "rel_path": source_path,
                "source_sha256": "a" * 64,
                "size_bytes": 1024,
                "duration_seconds": 45.25,
                "width": 1080,
                "height": 1920,
            }),
        )
    monkeypatch.setattr(
        review_service, "_creator_for_actor", AsyncMock(return_value=("account-1", creator)),
    )
    schema = next(item for item in tiktok.list_tools() if item["name"] == tool_name.removeprefix("mcp__tiktok__"))
    monkeypatch.setattr(
        "packages.core.ai.runtime.tool_registry.runtime_tool_schema_for_actor",
        AsyncMock(return_value=schema),
    )
    provider_calls = []
    blocked_calls = []

    async def guarded_tiktok_step(*, request, tool_name, arguments, workflow_context, **_kwargs):
        envelope = replace(
            _runtime_envelope(
                {
                    "entity_id": request.entity_id,
                    "user_id": request.user_id,
                    "conversation_id": request.conversation_id,
                    "workspace_id": request.workspace_id,
                },
                name=tool_name,
            ),
            surface=ChatSurface.WORKFLOW_AGENT_STEP,
        )
        run_id = workflow_context["workflow_run_id"]
        step_id = workflow_context["workflow_step_id"]
        runtime_arguments = {
            **arguments,
            **runtime_workflow_tool_context_args(workflow_context),
            "_runtime_run_id_from_context": f"workflow:{run_id}",
            "_runtime_tool_call_id_from_context": f"node:{step_id}",
        }
        gate = await approvals.guard_runtime_tool_action(
            name=tool_name,
            arguments=runtime_arguments,
            entity_id=request.entity_id,
            user_id=request.user_id,
            conversation_id=request.conversation_id,
            workspace_id=request.workspace_id,
            runtime_envelope=envelope,
            operation_id=f"runtime:workflow:{run_id}:call:node:{step_id}",
        )
        if gate is not None:
            if "approval_token" in runtime_arguments:
                raise AssertionError(gate)
            blocked_calls.append(gate)
            return RuntimeWorkflowToolStepResult(gate, None, envelope, {tool_name})
        provider_calls.append(dict(runtime_arguments))
        return RuntimeWorkflowToolStepResult(
            json.dumps({"data": {"publish_id": "provider-mocked"}}),
            None,
            envelope,
            {tool_name},
        )

    monkeypatch.setattr(
        "packages.core.ai.workflow_runner.runtime_execute_workflow_tool_step",
        guarded_tiktok_step,
    )
    wakes = []
    monkeypatch.setattr(
        WorkflowRunner,
        "enqueue_resume",
        staticmethod(lambda run_id, _delay, *, continuation_token=None: wakes.append((run_id, continuation_token)) or True),
    )

    job = await db_session.get(ScheduledJob, job.id)
    prepared = await _dispatch_job(
        db_session,
        job,
        datetime.now(timezone.utc),
        occurrence_key="scheduled-tiktok-native",
    )
    assert prepared is not None
    await db_session.commit()
    workflow_run_id = prepared[0]["args"][0]
    await WorkflowRunner().run(workflow_run_id)

    run = await db_session.get(WorkflowRun, workflow_run_id)
    assert run.trigger_source == "schedule"
    assert run.started_by == actor.id
    assert runtime_workflow_run_context(run)["conversation_id"] == conversation.id
    assert run.trigger_data["_workflow_runtime_context"] == {
        "workspace_id": workspace_id,
        "conversation_id": conversation.id,
        **deadline_context,
    }
    request_id = run.step_results["publish"]["runtime_approval"]["request_id"]
    request = await db_session.get(HitlRequest, request_id)
    assert request.origin_conversation_id == conversation.id
    assert request.context["requested_by"] == actor.id
    evidence = request.context["tiktok_publish_evidence"]
    assert (evidence["media_sources"][0]["source_sha256"] if photo else evidence["source_sha256"]) == "a" * 64
    assert provider_calls == []
    assert len(blocked_calls) == 1

    projected = await client.get(
        f"/api/v1/workflows/runs/{workflow_run_id}", headers=headers,
        params={"detail": "false"},
    )
    assert projected.status_code == 200, projected.text
    intervention = projected.json()["intervention"]
    assert intervention["runtime_approval_id"] == request_id
    assert intervention["tool"] == tool_name
    assert intervention["review"]["kind"] == "tiktok_publish"
    assert intervention["review_read_only"] is False

    missing_review = await client.post(
        f"/api/v1/workflows/runs/{workflow_run_id}/approvals/{request_id}/resolve",
        headers=headers, json={"choice": "approve"},
    )
    assert missing_review.status_code == 409, missing_review.text
    assert not wakes and provider_calls == []

    if deadline_state == "expired_on_open":
        expire_window()
    creator_reads = review_service._creator_for_actor.await_count
    refreshed_review = await refresh_tiktok_publish_review(
        request_id,
        user=actor,
        db=db_session,
    )
    await db_session.refresh(request)
    if deadline_state != "none":
        window = refreshed_review["publication_window"]
        assert window["deadline_at"] == deadline_at
        assert window["expired"] is (deadline_state == "expired_on_open")
        assert window["run_url"] == f"/flows/{workflow_id}/runs/{workflow_run_id}"
        assert "publication_window" not in request.context["review"]
    if deadline_state == "expired_on_open":
        assert "review_id" not in refreshed_review
        assert review_service._creator_for_actor.await_count == creator_reads
    submitted_review = {
        "kind": "tiktok_publish",
        "version": 2 if photo else 1,
        "review_id": request.context["review"]["review_id"],
        "caption": "Operator approved scheduled post",
        "privacy_level": "SELF_ONLY",
        "allow_comment": False,
        "allow_duet": True,
        "allow_stitch": False,
        "commercial_content": False,
        "brand_organic_toggle": False,
        "brand_content_toggle": False,
        "music_usage_confirmed": True,
        "branded_content_policy_confirmed": False,
        "consent_confirmed": True,
        **({"photo_cover_index": 0} if photo else {}),
    }
    await db_session.commit()
    if deadline_state.startswith("expired_"):
        expire_window()
        response = await client.post(
            f"/api/v1/workflows/runs/{workflow_run_id}/approvals/{request_id}/resolve",
            headers=headers, json={"choice": "approve", "review": submitted_review},
        )
        assert response.status_code == 409, response.text
        assert "publishing window ended" in response.json()["detail"]
        await db_session.refresh(request)
        await db_session.refresh(run)
        assert request.status == "pending"
        assert "tiktok_publish_consent" not in request.context
        assert run.status == "paused" and not wakes and not provider_calls
        # The ordinary cancellation remains available; expiry grants nothing.
        cancelled = await client.post(
            f"/api/v1/workflows/runs/{workflow_run_id}/approvals/{request_id}/resolve",
            headers=headers, json={"choice": "reject"},
        )
        assert cancelled.status_code == 200, cancelled.text
        await db_session.refresh(request)
        assert request.status == "denied" and not wakes and not provider_calls
        return
    changed_review = await client.post(
        f"/api/v1/workflows/runs/{workflow_run_id}/approvals/{request_id}/resolve",
        headers=headers,
        json={"choice": "approve", "review": {**submitted_review, "review_id": "changed"}},
    )
    assert changed_review.status_code == 409, changed_review.text
    assert not wakes and provider_calls == []
    response = await client.post(
        f"/api/v1/workflows/runs/{workflow_run_id}/approvals/{request_id}/resolve",
        headers=headers,
        json={"choice": "approve", "review": submitted_review},
    )
    assert response.status_code == 200, response.text
    assert len(wakes) == 1
    await db_session.refresh(request)
    from packages.core.ai.runtime.approval_messages import approval_args_hash

    resumed_arguments = {
        **request.context["continuation"]["arguments"],
        "_workflow_run_id_from_context": workflow_run_id,
        "_workflow_lineage_root_run_id_from_context": workflow_run_id,
        "_workflow_step_id_from_context": "publish",
        "_performance_run_id_from_context": workflow_run_id,
        "_runtime_run_id_from_context": f"workflow:{workflow_run_id}",
        "_runtime_tool_call_id_from_context": "node:publish",
    }
    assert request.context["args_hash"] == approval_args_hash(
        resumed_arguments, tool_name=tool_name,
    )
    await WorkflowRunner.resume(workflow_run_id, continuation_token=wakes[0][1])

    await db_session.refresh(run)
    await db_session.refresh(request)
    assert run.status == "completed", run.step_results["publish"]
    assert request.status == "consumed"
    expected_arguments = {
        **({"photo_urls": [source_path], "photo_cover_index": 0} if photo else {"video_url": source_path}),
        "title": "Operator approved scheduled post",
        "integration_account_id": "account-1",
        "privacy_level": "SELF_ONLY",
        "disable_comment": True,
        "disable_duet": False,
        "disable_stitch": True,
        "commercial_content": False,
        "brand_content_toggle": False,
        "brand_organic_toggle": False,
        "_workflow_run_id_from_context": workflow_run_id,
        "_workflow_lineage_root_run_id_from_context": workflow_run_id,
        "_workflow_step_id_from_context": "publish",
        "_performance_run_id_from_context": workflow_run_id,
        "_runtime_run_id_from_context": f"workflow:{workflow_run_id}",
        "_runtime_tool_call_id_from_context": "node:publish",
        "_workflow_effect_deadline_action_keys_from_context": deadline_context.get("workflow_effect_deadline_action_keys", []),
        **({"_workflow_effect_deadline_at_from_context": deadline_at} if deadline_context else {}),
    }
    assert provider_calls == [expected_arguments]
    await WorkflowRunner.resume(workflow_run_id, continuation_token=wakes[0][1])
    assert len(provider_calls) == 1


@pytest.mark.asyncio
async def test_app_projects_exact_native_request_and_generic_resume_cannot_bypass(
    client, db_session, monkeypatch, tmp_path,
):
    case = await approval_case(client, db_session, monkeypatch, tmp_path)
    run = await detail(client, case)
    action = run['resume_action']
    assert action['kind'] == 'workflow_approval'
    assert action['runtime_approval_id'] == case.request_id
    assert action['step_id'] == 'read'
    assert_public_only(run)
    compact = await client.get(f'/api/v1/workflows/runs/{case.run_id}',
                               headers=case.headers, params={'detail': 'false'})
    assert compact.status_code == 200, compact.text
    assert compact.json()['intervention']['runtime_approval_id'] == case.request_id
    assert_public_only(compact.json())
    response = await client.post(f'/api/v1/workflows/runs/{case.run_id}/resume',
                                 headers=case.headers, json={'choice': 'approve'})
    assert response.status_code == 409, response.text
    assert (await detail(client, case))['status'] == 'paused'
    assert not case.wakes
    await db_session.rollback()
    assert (await db_session.get(HitlRequest, case.request_id)).status == 'pending'


async def resume_wake(case):
    assert len(case.wakes) == 1
    run_id, token = case.wakes[0]
    assert run_id == case.run_id and token
    return await WorkflowRunner.resume(run_id, continuation_token=token)


@pytest.mark.asyncio
async def test_app_approval_consumes_same_request_and_resumes_only_blocked_step(
    client, db_session, monkeypatch, tmp_path,
):
    case = await approval_case(client, db_session, monkeypatch, tmp_path)
    response = await decide(client, case)
    assert response.status_code == 200, response.text
    assert_public_only(response.json())
    await db_session.rollback()
    request = await db_session.get(HitlRequest, case.request_id)
    assert request.status == 'granted'
    assert request.decided_by_user_id == case.actor_id
    run = await db_session.get(WorkflowRun, case.run_id)
    assert run.status == 'paused'
    assert run.continuation_token == case.wakes[0][1]
    assert run.continuation_due_at is not None
    await db_session.rollback()
    await resume_wake(case)
    completed = await detail(client, case)
    assert completed['status'] == 'completed', completed.get('step_results')
    assert completed['variables']['source']['content'] == case.body
    assert case.executions['before'] == 1 and case.executions['read'] == 2
    assert completed['resume_action'] is None
    assert_public_only(completed)
    await db_session.rollback()
    assert (await db_session.get(HitlRequest, case.request_id)).status == 'consumed'
    # An at-least-once broker delivery must not replay either operation.
    before = dict(case.executions)
    await WorkflowRunner.resume(case.run_id, continuation_token=case.wakes[0][1])
    assert dict(case.executions) == before
    repeated = await decide(client, case)
    assert repeated.status_code == 409, repeated.text
    assert len(case.wakes) == 1


@pytest.mark.asyncio
async def test_rejection_is_terminal_without_executing_the_blocked_tool(
    client, db_session, monkeypatch, tmp_path,
):
    case = await approval_case(client, db_session, monkeypatch, tmp_path)
    response = await decide(client, case, 'reject')
    assert response.status_code == 200, response.text
    terminal = await detail(client, case)
    assert terminal['status'] == 'failed'
    assert terminal['step_results']['read']['code'] == 'runtime_approval_rejected'
    assert 'source' not in terminal['variables']
    assert terminal['resume_action'] is None
    assert case.executions['read'] == 1 and not case.wakes
    await db_session.rollback()
    assert (await db_session.get(HitlRequest, case.request_id)).status == 'denied'
    repeated = await decide(client, case)
    assert repeated.status_code == 409
    assert not case.wakes


@pytest.mark.asyncio
async def test_native_card_in_workspace_chat_uses_the_same_canonical_request(
    client, db_session, monkeypatch, tmp_path,
):
    case = await approval_case(client, db_session, monkeypatch, tmp_path)
    response = await client.get(f'/api/v1/workspaces/{case.workspace_id}/chat/messages',
                                headers=case.headers)
    assert response.status_code == 200, response.text
    messages = response.json()
    cards = [message for message in messages if
             (message.get('meta') or {}).get('workflow_runtime_hitl')]
    assert len(cards) == 1
    card = cards[0]
    assert card['meta']['workflow_run_id'] == case.run_id
    assert card['meta']['workflow_step_id'] == 'read'
    assert case.request_id in json.dumps(card['hitl_requests'])
    assert_public_only(messages)
    # Workspace native cards send structured chat input, not pending_action
    # decisions. Exercise the same canonical resolver called before the model.
    from packages.core.services.chat_approvals import resolve_chat_approval_turn

    await db_session.rollback()
    request = await db_session.get(HitlRequest, case.request_id)
    result = await resolve_chat_approval_turn(
        db_session, conversation_id=request.origin_conversation_id,
        entity_id=request.entity_id, user_id=case.actor_id,
        message=json.dumps({'hitl_id': case.request_id, 'action': 'approve'}),
    )
    assert result[3]['approval_kind'] == 'workflow_runtime'
    assert not case.wakes, 'the broker must not observe an uncommitted grant'
    await db_session.commit()
    await resume_wake(case)
    assert (await detail(client, case))['status'] == 'completed'
    assert case.executions['before'] == 1
    await db_session.refresh(request)
    assert request.status == 'consumed'


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['request_id', 'execution_snapshot', 'scope', 'expired', 'signature'])
async def test_stale_or_changed_approval_cannot_grant_or_enqueue(
    client, db_session, monkeypatch, tmp_path, change,
):
    from copy import deepcopy

    from packages.core.models.base import generate_ulid
    case = await approval_case(client, db_session, monkeypatch, tmp_path)
    await db_session.rollback()
    request = await db_session.get(HitlRequest, case.request_id)
    chosen_id = case.request_id
    if change == 'request_id':
        chosen_id = generate_ulid()
    elif change == 'execution_snapshot':
        await change_saved_execution(db_session, case)
    elif change == 'scope':
        request.workspace_id = generate_ulid()
    elif change == 'expired':
        request.status = 'expired'
    elif change == 'signature':
        context = deepcopy(request.context)
        context['workflow_runtime_approval_v1']['signature'] = 'tampered'
        request.context = context
    await db_session.commit()
    response = await decide(client, case, request_id=chosen_id)
    assert response.status_code in {400, 403, 409}, response.text
    assert not case.wakes
    assert case.executions['read'] == 1
    await db_session.rollback()
    assert (await db_session.get(HitlRequest, case.request_id)).status != 'granted'
    assert 'source' not in (await db_session.get(WorkflowRun, case.run_id)).variables


@pytest.mark.asyncio
async def test_workspace_reviewer_can_view_but_cannot_grant_run_approval(
    client, db_session, monkeypatch, tmp_path,
):
    from packages.core.models.base import generate_ulid
    from packages.core.models.user import UserMembership
    from packages.core.models.workspace import WorkspaceStaff
    from packages.core.services.auth_service import create_access_token, hash_password

    case = await approval_case(client, db_session, monkeypatch, tmp_path)
    await db_session.rollback()
    owner = await db_session.get(User, case.actor_id)
    reviewer = User(id=generate_ulid(), entity_id=owner.entity_id,
                    email='native-workflow-reviewer@example.test',
                    password_hash=hash_password('pass123'), role='member', status='active')
    db_session.add(reviewer)
    await db_session.flush()
    db_session.add_all([
        UserMembership(user_id=reviewer.id, entity_id=owner.entity_id,
                       role='member', status='active', is_primary=True),
        WorkspaceStaff(workspace_id=case.workspace_id, user_id=reviewer.id,
                       role='viewer', status='active', added_by=owner.id),
    ])
    await db_session.commit()
    headers = {'Authorization': f'Bearer {create_access_token(reviewer.id, reviewer.entity_id, reviewer.role)}'}
    visible = await client.get(f'/api/v1/workflows/runs/{case.run_id}', headers=headers)
    assert visible.status_code == 200, visible.text
    assert visible.json()['capabilities']['can_control'] is False
    assert_public_only(visible.json())
    forbidden = await decide(client, case, headers=headers)
    assert forbidden.status_code == 403, forbidden.text
    assert not case.wakes
    await db_session.rollback()
    assert (await db_session.get(HitlRequest, case.request_id)).status == 'pending'


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['workspace_paused', 'actor_inactive', 'execution_snapshot'])
async def test_resume_rechecks_lifecycle_and_definition_after_grant(
    client, db_session, monkeypatch, tmp_path, change,
):
    case = await approval_case(client, db_session, monkeypatch, tmp_path)
    response = await decide(client, case)
    assert response.status_code == 200, response.text
    await db_session.rollback()
    if change == 'workspace_paused':
        workspace = await db_session.get(Workspace, case.workspace_id)
        workspace.status = 'paused'
    elif change == 'actor_inactive':
        actor = await db_session.get(User, case.actor_id)
        actor.status = 'inactive'
    else:
        await change_saved_execution(db_session, case)
    await db_session.commit()
    await resume_wake(case)
    await db_session.rollback()
    run = await db_session.get(WorkflowRun, case.run_id)
    request = await db_session.get(HitlRequest, case.request_id)
    assert case.executions['before'] == 1
    if change == 'workspace_paused':
        assert run.status == 'completed'
        assert 'source' in run.variables
        assert request.status == 'consumed'
        return
    assert run.status != 'completed'
    assert 'source' not in run.variables
    assert request.status != 'consumed', 'lost authority cannot spend approval'
    if change == 'execution_snapshot':
        assert run.step_results['read']['retry_blocked'] is True
        retry = await client.post(f'/api/v1/workflows/runs/{case.run_id}/retry',
                                  headers=case.headers, json={})
        assert retry.status_code == 409, retry.text
        assert case.executions['before'] == 1


@pytest.mark.asyncio
async def test_rolled_back_native_grant_never_enqueues_or_changes_the_request(
    client, db_session, monkeypatch, tmp_path,
):
    from packages.core.services.workflow_runtime_approvals import resolve_workflow_approval

    case = await approval_case(client, db_session, monkeypatch, tmp_path)
    await db_session.rollback()
    run = await db_session.get(WorkflowRun, case.run_id)
    resolution = await resolve_workflow_approval(
        db_session, run=run, request_id=case.request_id,
        user_id=case.actor_id, choice='approve',
    )
    assert resolution.runtime_metadata['approval_outcome'] == 'approved'
    assert not case.wakes
    await db_session.rollback()
    request = await db_session.get(HitlRequest, case.request_id)
    run = await db_session.get(WorkflowRun, case.run_id)
    assert request.status == 'pending'
    assert run.continuation_token is None
    assert run.step_results['read']['resume_strategy'] == 'blocked'
    assert not case.wakes


@pytest.mark.asyncio
async def test_broker_failure_keeps_durable_approval_wake_for_recovery(
    client, db_session, monkeypatch, tmp_path,
):
    case = await approval_case(client, db_session, monkeypatch, tmp_path)

    def unavailable_broker(run_id, delay_seconds, *, continuation_token=None):
        case.wakes.append((run_id, continuation_token))
        return False

    monkeypatch.setattr(WorkflowRunner, 'enqueue_resume', staticmethod(unavailable_broker))
    response = await decide(client, case)
    assert response.status_code == 200, response.text
    await db_session.rollback()
    run = await db_session.get(WorkflowRun, case.run_id)
    assert run.continuation_token == case.wakes[0][1]
    assert run.continuation_due_at is not None
    assert run.continuation_next_attempt_at is not None
    assert (await db_session.get(HitlRequest, case.request_id)).status == 'granted'
    await db_session.rollback()
    from packages.core.tasks.ai_tasks import _claim_due_workflow_continuations

    claimed = await _claim_due_workflow_continuations()
    assert claimed == case.wakes
    assert await _claim_due_workflow_continuations() == [], 'recovery leases one wake'
    await WorkflowRunner.resume(claimed[0][0], continuation_token=claimed[0][1])
    assert (await detail(client, case))['status'] == 'completed'
    assert case.executions['before'] == 1


async def change_saved_execution(db, case):
    from copy import deepcopy
    from types import SimpleNamespace

    from packages.core.services.workflow_run_trace import workflow_definition_fingerprint

    run = await db.get(WorkflowRun, case.run_id)
    snapshot = deepcopy(run.execution_snapshot)
    snapshot['steps'][2]['config']['args']['offset'] = 1
    snapshot['fingerprint'] = workflow_definition_fingerprint(SimpleNamespace(
        id=case.workflow_id, version=snapshot['version'], steps=snapshot['steps'],
    ))
    run.execution_snapshot = snapshot


@pytest.mark.asyncio
async def test_live_definition_edit_does_not_replace_the_frozen_approved_invocation(
    client, db_session, monkeypatch, tmp_path,
):
    from copy import deepcopy

    from packages.core.models.workflow import WorkflowDefinition

    case = await approval_case(client, db_session, monkeypatch, tmp_path)
    await db_session.rollback()
    definition = await db_session.get(WorkflowDefinition, case.workflow_id)
    changed = deepcopy(definition.steps)
    changed[2]['config']['args']['offset'] = 1
    definition.steps = changed
    await db_session.commit()
    response = await decide(client, case)
    assert response.status_code == 200, response.text
    await resume_wake(case)
    completed = await detail(client, case)
    assert completed['status'] == 'completed', completed
    assert completed['variables']['source']['content'] == case.body
    assert case.executions['before'] == 1


@pytest.mark.asyncio
async def test_native_approval_message_failure_preserves_gate_and_projection_retry(
    client, db_session, monkeypatch, tmp_path,
):
    from packages.core.models.task import Message
    from packages.core.services import workflow_chat_projection, workflow_runtime_approvals

    project = workflow_runtime_approvals.project_workflow_runtime_approval

    async def fail_after_message(db, *, run, step, result):
        await project(db, run=run, step=step, result=result)
        if run.status == 'paused' and isinstance(result, dict) and result.get('code') == 'runtime_approval_required':
            raise RuntimeError('Chat projection failed after inserting the approval message')

    monkeypatch.setattr(workflow_runtime_approvals, 'project_workflow_runtime_approval', fail_after_message)
    case = await approval_case(client, db_session, monkeypatch, tmp_path)
    event_id = f'workflow-runtime-hitl:{case.request_id}'
    messages = select(Message.id).where(Message.response_surface_event_id == event_id)
    assert (await db_session.execute(messages)).scalars().all() == []
    request = await db_session.get(HitlRequest, case.request_id)
    assert request.status == 'pending'
    assert request.context.get('workflow_runtime_approval_v1')
    run = await detail(client, case)
    assert run['status'] == 'paused'
    assert run['step_results']['read']['runtime_approval']['request_id'] == case.request_id
    assert not case.wakes

    monkeypatch.setattr(workflow_runtime_approvals, 'project_workflow_runtime_approval', project)
    await db_session.rollback()
    row = await db_session.get(WorkflowRun, case.run_id)
    for _ in range(2):
        await workflow_chat_projection.project_workflow_run_status(db_session, run=row)
        await db_session.commit()
    assert len((await db_session.execute(messages)).scalars().all()) == 1
    recovered = await detail(client, case)
    assert recovered['status'] == 'paused'
    assert recovered['resume_action']['runtime_approval_id'] == case.request_id
    assert case.executions['before'] == 1 and case.executions['read'] == 1
    assert not case.wakes


@pytest.mark.asyncio
@pytest.mark.parametrize('endpoint', ['stream', 'message'])
@pytest.mark.parametrize('choice', ['approve', 'reject'])
async def test_chat_http_native_workflow_decision_never_starts_a_model_turn(
    client, db_session, monkeypatch, tmp_path, endpoint, choice,
):
    from apps.api.routers import chat
    from packages.core.models.runtime_run import RuntimeRun

    case = await approval_case(client, db_session, monkeypatch, tmp_path)
    request = await db_session.get(HitlRequest, case.request_id)
    conversation_id = request.origin_conversation_id
    await db_session.rollback()

    async def unexpected_model_turn(*args, **kwargs):
        raise AssertionError('Workflow approval acknowledgement must not start Chat inference')

    monkeypatch.setattr(chat, 'create_runtime_run', unexpected_model_turn)
    monkeypatch.setattr(chat, 'runtime_run_chat_turn', unexpected_model_turn)
    response = await client.post(f'/api/v1/chat/{endpoint}', headers=case.headers, data={
        'message': json.dumps({'hitl_id': case.request_id, 'action': choice}),
        'conversation_id': conversation_id,
        'workspace_id': case.workspace_id,
        'workspace_context': 'true',
    })
    assert response.status_code == 200, response.text
    if endpoint == 'stream':
        assert 'event: stream_end' in response.text
        assert 'event: error' not in response.text
        terminal = next(frame for frame in response.text.split('\n\n') if frame.startswith('event: stream_end'))
        payload = json.loads(terminal.split('data: ', 1)[1])
        assert payload['rounds'] == 0
        assert payload['usage'] == {}
    else:
        assert response.json()['rounds'] == 0
        assert response.json()['usage'] == {}
    assert not (await db_session.scalars(select(RuntimeRun).where(
        RuntimeRun.conversation_id == conversation_id,
    ))).all()
    await db_session.rollback()
    if choice == 'approve':
        assert (await db_session.get(HitlRequest, case.request_id)).status == 'granted'
        await db_session.rollback()
        await resume_wake(case)
        assert (await detail(client, case))['status'] == 'completed'
        assert case.executions['before'] == 1
        assert case.executions['read'] == 2
    else:
        assert (await detail(client, case))['status'] == 'failed'
        assert case.executions['read'] == 1
        assert not case.wakes
