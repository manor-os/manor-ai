"""Workspace HITL attachments survive API resolution and runtime resumption."""
import asyncio

import pytest
from sqlalchemy import select

from packages.core.ai.runtime.control import RuntimeAgentCheckpoint
from packages.core.constants.agents import MANOR_AGENT_ID
from packages.core.models.base import generate_ulid
from packages.core.models.document import Document, DocumentFolder, DocumentGroupMember
from packages.core.models.execution import ExecutionPlan, ExecutionStep
from packages.core.models.participant import HumanCommitment
from packages.core.models.task import Conversation, Message, Task
from packages.core.services.task_agent_loop_hitl import TASK_AGENT_LOOP_INPUT_DISPATCH
from packages.core.services.task_input_resume import TASK_INPUT_QUESTION_KEY

pytestmark = pytest.mark.asyncio


async def _fixture(client, db, monkeypatch, kind):
    from auth_helpers import register_user_and_get_token
    from packages.core.humans import open_commitment

    suffix = generate_ulid().lower()
    registered = await register_user_and_get_token(client, json={
        "username": f"attachment_{suffix}", "email": f"{suffix}@test.com",
        "password": "pass123", "entity_name": "Attachment fixtures",
    })
    assert registered.status_code == 200, registered.text
    headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}
    actor = (await client.get("/api/v1/auth/me", headers=headers)).json()
    workspace = (await client.post("/api/v1/workspaces", headers=headers,
                                  json={"name": "Input attachments"})).json()
    entity_id, workspace_id = actor["entity_id"], workspace["id"]
    conversation = Conversation(entity_id=entity_id, workspace_id=workspace_id,
                                scope="workspace_main", channel="workspace")
    db.add(conversation)
    await db.flush()
    action = {"kind": "human_input", "prompt": "Please upload the audio"}
    origin = None
    message = None
    if kind == "step":
        plan = ExecutionPlan(entity_id=entity_id, workspace_id=workspace_id,
                             status="running", plan_dag={})
        db.add(plan)
        await db.flush()
        origin = ExecutionStep(entity_id=entity_id, workspace_id=workspace_id,
                               plan_id=plan.id, step_key="audio", kind="subagent",
                               step_status="waiting_human", params={},
                               runtime_checkpoint=RuntimeAgentCheckpoint(
                                   messages=[], rounds=1, usage={}, tool_calls_made=[],
                                   pending_tool_call={"id": "ask", "name": "request_input", "arguments": {}},
                               ).to_dict())
        db.add(origin)
        await db.flush()
        action.update(step_id=origin.id, plan_id=plan.id)
    elif kind == "task":
        origin = Task(entity_id=entity_id, workspace_id=workspace_id,
                      title="Produce audio", status="waiting_on_customer",
                      agent_id=MANOR_AGENT_ID,
                      details={TASK_INPUT_QUESTION_KEY: {"question": "Upload audio"}})
        db.add(origin)
        await db.flush()
        action["task_id"] = origin.id

        # The Task's run is parked on this question: the answer (with its
        # canonical attachments) is handed to that run.
        async def parked_run_takes_answer(_db, *, task, payload, **kwargs):
            task.details = {**(task.details or {}), "_hitl_payload": dict(payload)}
            return {"resumed": True, "dispatch_agent_id": TASK_AGENT_LOOP_INPUT_DISPATCH}

        monkeypatch.setattr(
            "packages.core.services.task_agent_loop_hitl.apply_task_agent_loop_input_answer",
            parked_run_takes_answer,
        )
        monkeypatch.setattr("packages.core.services.task_input_resume.dispatch_task_input_resume", lambda *_: True)
        from packages.core.services.task_chat_hitl import ensure_task_input_hitl

        message = await ensure_task_input_hitl(db, origin, question={"question": "Upload audio"})
        assert message is not None
    else:
        origin = await open_commitment(
            db, entity_id=entity_id, workspace_id=workspace_id, request_kind="input",
            source_kind="chat", source_id="attachment-fixture", expected_input="Upload audio",
        )
        action["commitment_id"] = origin.id
        monkeypatch.setattr("packages.core.tasks.ai_tasks.run_strategist_review.apply_async", lambda *_args, **_kwargs: None)
    if message is None:
        message = Message(conversation_id=conversation.id, role="assistant", message_kind="hitl_request",
                          pending_action=action)
    folder = DocumentFolder(entity_id=entity_id, name="Original Knowledge", owner_id=actor["id"])
    db.add_all([message, folder])
    await db.flush()
    document = Document(entity_id=entity_id, owner_id=actor["id"], name="audio.wav",
                        fs_path="Original Knowledge/audio.wav", source="upload",
                        folder_id=folder.id, visibility="private")
    db.add(document)
    await db.commit()
    return headers, workspace_id, message, origin, document


@pytest.mark.parametrize("kind", ["step", "task", "commitment"])
async def test_attachment_only_answer_is_canonical_and_preserves_existing_document(
    client, db_session, monkeypatch, kind,
):
    headers, workspace_id, message, origin, document = await _fixture(
        client, db_session, monkeypatch, kind,
    )
    document_id, folder_id = document.id, document.folder_id
    response = await client.post(
        f"/api/v1/workspaces/{workspace_id}/chat/messages/{message.id}/resolve",
        headers=headers, json={"choice": "respond", "payload": {"attachments": [
            {"id": document_id, "name": "forged", "fs_path": "forged", "type": "file"},
            {"document_id": document_id},
        ]}},
    )
    assert response.status_code == 200, response.text
    canonical = response.json()["resolution"]["payload"]["attachments"]
    assert canonical == [{"id": document_id, "document_id": document_id, "name": "audio.wav",
                          "type": "knowledge", "fs_path": "Original Knowledge/audio.wav",
                          "viewer_url": f"/viewer/{document_id}"}]
    await db_session.refresh(document)
    assert document.folder_id == folder_id
    assert document.visibility == "private"
    assert document.fs_path == "Original Knowledge/audio.wav"
    assert not list((await db_session.scalars(select(DocumentGroupMember).where(
        DocumentGroupMember.document_id == document_id,
    ))).all())
    await db_session.refresh(origin)
    if kind == "step":
        assert origin.step_status == "pending"
        # The worker resumes the parked call from the kept checkpoint with this answer.
        assert origin.human_input_response["attachments"] == canonical
        assert RuntimeAgentCheckpoint.from_dict(origin.runtime_checkpoint).pending_tool_call["id"] == "ask"
    elif kind == "task":
        assert origin.details["_hitl_payload"]["attachments"] == canonical
    else:
        assert isinstance(origin, HumanCommitment)
        assert origin.status == "fulfilled"
        assert origin.response["attachments"] == canonical


@pytest.mark.parametrize("invalid_attachment", ["missing", "foreign", "malformed", "private"])
async def test_unavailable_attachment_does_not_consume_the_input_card(
    client, db_session, monkeypatch, invalid_attachment,
):
    headers, workspace_id, message, origin, document = await _fixture(
        client, db_session, monkeypatch, "step",
    )
    attachment = {"id": generate_ulid()}
    if invalid_attachment == "foreign":
        document.entity_id = generate_ulid()
        await db_session.commit()
        attachment = {"id": document.id}
    elif invalid_attachment == "malformed":
        attachment = {"name": "missing id"}
    elif invalid_attachment == "private":
        from packages.core.models.workspace import WorkspaceStaff
        from tests.test_document_permissions import _create_entity_user

        member = await _create_entity_user(document.entity_id, f"member_{generate_ulid().lower()}")
        db_session.add(WorkspaceStaff(workspace_id=workspace_id, user_id=member["id"],
                                     role="contributor", status="active"))
        await db_session.commit()
        headers = member["headers"]
        attachment = {"id": document.id}
    attachment["origin"] = "input_upload"  # The browser's intent marker cannot grant access.
    response = await client.post(
        f"/api/v1/workspaces/{workspace_id}/chat/messages/{message.id}/resolve",
        headers=headers, json={"choice": "respond", "payload": {"attachments": [attachment]}},
    )
    assert response.status_code == (422 if invalid_attachment == "malformed" else 404)
    await db_session.refresh(message)
    await db_session.refresh(origin)
    assert message.resolved_at is None
    assert origin.step_status == "waiting_human"


@pytest.mark.parametrize("different_workspace", [False, True])
async def test_two_cards_can_attach_the_same_upload_without_rehoming_or_duplicate_membership(
    client, db_session, monkeypatch, different_workspace,
):
    from packages.core.humans import open_commitment

    headers, workspace_id, first, _origin, document = await _fixture(
        client, db_session, monkeypatch, "commitment",
    )
    document.folder_id = None
    document.fs_path = "audio.wav"
    second_workspace_id = workspace_id
    second_conversation_id = first.conversation_id
    if different_workspace:
        second_workspace = (await client.post(
            "/api/v1/workspaces", headers=headers, json={"name": "Other input workspace"},
        )).json()
        second_workspace_id = second_workspace["id"]
        conversation = Conversation(entity_id=document.entity_id, workspace_id=second_workspace_id,
                                    scope="workspace_main", channel="workspace")
        db_session.add(conversation)
        await db_session.flush()
        second_conversation_id = conversation.id
    second_origin = await open_commitment(
        db_session, entity_id=document.entity_id, workspace_id=second_workspace_id,
        request_kind="input", source_kind="chat", source_id="second-attachment",
        expected_input="Upload the same audio",
    )
    second = Message(conversation_id=second_conversation_id, role="assistant",
                     message_kind="hitl_request", pending_action={
                         "kind": "human_input", "commitment_id": second_origin.id,
                     })
    db_session.add(second)
    await db_session.commit()
    responses = await asyncio.gather(*(
        client.post(
            f"/api/v1/workspaces/{target_workspace_id}/chat/messages/{message.id}/resolve",
            headers=headers, json={"choice": "respond", "payload": {
                "attachments": [{"id": document.id, "origin": "input_upload"}],
            }},
        ) for target_workspace_id, message in ((workspace_id, first), (second_workspace_id, second))
    ))
    assert [response.status_code for response in responses] == [200, 200], [
        response.text for response in responses
    ]
    assert all("origin" not in response.json()["resolution"]["payload"]["attachments"][0]
               for response in responses)
    await db_session.refresh(document)
    assert document.visibility == "workspace"
    assert document.metadata_["origin"]["workspace_id"] in {workspace_id, second_workspace_id}
    assert document.fs_path == "audio.wav"
    members = list((await db_session.scalars(select(DocumentGroupMember).where(
        DocumentGroupMember.document_id == document.id,
    ))).all())
    assert len(members) == 1


async def test_private_unfiled_knowledge_selection_is_not_treated_as_a_new_upload(
    client, db_session, monkeypatch,
):
    headers, workspace_id, message, _origin, document = await _fixture(
        client, db_session, monkeypatch, "commitment",
    )
    document.folder_id = None
    document.fs_path = "private-audio.wav"
    await db_session.commit()
    original_metadata = document.metadata_
    response = await client.post(
        f"/api/v1/workspaces/{workspace_id}/chat/messages/{message.id}/resolve",
        headers=headers, json={"choice": "respond", "payload": {
            "attachments": [{"id": document.id, "type": "knowledge"}],
        }},
    )
    assert response.status_code == 200, response.text
    await db_session.refresh(document)
    assert document.folder_id is None
    assert document.fs_path == "private-audio.wav"
    assert document.visibility == "private"
    assert document.metadata_ == original_metadata
    assert list((await db_session.scalars(select(DocumentGroupMember).where(
        DocumentGroupMember.document_id == document.id,
    ))).all()) == []
