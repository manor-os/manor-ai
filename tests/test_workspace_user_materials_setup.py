"""Generic WS-03 workspace_user_materials checks and setup Need Input injection."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from packages.core.models.base import generate_ulid
from packages.core.models.document import Document, DocumentGroup, DocumentGroupMember
from packages.core.models.participant import HumanCommitment
from packages.core.models.workspace import Workspace
from packages.core.services.workspace_readiness import evaluate_workspace_blocking_setup
from packages.core.strategist.proposal import ProposedHumanRequest
from packages.core.strategist.service import (
    _ensure_blocking_setup_human_request,
    _restrict_proposal_to_setup_work,
)


def _materials_check(**overrides) -> dict:
    check = {
        "key": "required_materials",
        "kind": "workspace_user_materials",
        "blocking": True,
        "setup_task_key": "required_materials",
        "question": "Upload the required source file, or say the notes are skipped.",
        "all_of": [
            {
                "documents": {
                    "mime_prefixes": ["text/"],
                    "suffixes": [".txt", ".md"],
                }
            },
            {
                "any_of": [
                    {
                        "documents": {
                            "name_regex": "brief|notes",
                            "suffixes": [".txt", ".md"],
                        }
                    },
                    {
                        "fulfilled_input_regex": r"skip-notes|no notes"
                    },
                ]
            },
        ],
    }
    check.update(overrides)
    return check


def _workspace_with_materials_gate(**check_overrides) -> Workspace:
    return Workspace(
        id=generate_ulid(),
        entity_id=generate_ulid(),
        name="Materials gate",
        status="active",
        settings={
            "blocking_setup": {
                "ready_when": "all",
                "allowed_setup_task_keys": ["required_materials"],
                "checks": [_materials_check(**check_overrides)],
            }
        },
    )


async def _add_knowledge_document(
    db_session,
    workspace: Workspace,
    *,
    name: str,
    mime_type: str = "",
    source: str = "upload",
    is_trashed: bool = False,
    group: DocumentGroup | None = None,
) -> Document:
    if group is None:
        group = DocumentGroup(
            entity_id=workspace.entity_id,
            workspace_id=workspace.id,
            name="Workspace knowledge",
        )
        db_session.add(group)
        await db_session.flush()
    document = Document(
        entity_id=workspace.entity_id,
        name=name,
        mime_type=mime_type,
        source=source,
        is_trashed=is_trashed,
    )
    db_session.add(document)
    await db_session.flush()
    db_session.add(DocumentGroupMember(document_id=document.id, group_id=group.id))
    await db_session.flush()
    return document


def _proposal(*, task_key: str = "normal_work", request_key: str | None = None):
    human_requests = []
    if request_key:
        human_requests = [
            ProposedHumanRequest(
                request_key=request_key,
                request_kind="input",
                question="Please provide the missing setup materials now.",
            )
        ]
    return SimpleNamespace(
        tasks=[SimpleNamespace(task_key=task_key, depends_on_task_keys=[], external_action=None)],
        human_requests=human_requests,
        experiments=[object()],
        workflow_runs=[object()],
        automation_changes=[object()],
        workflow_changes=[object()],
        goal_changes=[object()],
    )


@pytest.mark.asyncio
async def test_user_materials_check_fails_closed_until_required_documents_exist(
    db_session,
) -> None:
    workspace = _workspace_with_materials_gate()
    db_session.add(workspace)
    await db_session.flush()

    blocked = await evaluate_workspace_blocking_setup(db_session, workspace)
    assert blocked is not None
    assert blocked.blocks_work
    assert blocked.details["allowed_setup_task_keys"] == ["required_materials"]

    await _add_knowledge_document(
        db_session,
        workspace,
        name="source.txt",
        mime_type="text/plain",
    )
    still_blocked = await evaluate_workspace_blocking_setup(db_session, workspace)
    assert still_blocked is not None
    assert still_blocked.blocks_work

    await _add_knowledge_document(
        db_session,
        workspace,
        name="project-brief.md",
        mime_type="text/markdown",
    )
    ready = await evaluate_workspace_blocking_setup(db_session, workspace)
    assert ready is not None
    assert not ready.blocks_work


@pytest.mark.asyncio
async def test_user_materials_check_ignores_blueprint_and_trashed_documents(
    db_session,
) -> None:
    workspace = _workspace_with_materials_gate()
    db_session.add(workspace)
    await db_session.flush()
    group = DocumentGroup(
        entity_id=workspace.entity_id,
        workspace_id=workspace.id,
        name="Workspace knowledge",
    )
    db_session.add(group)
    await db_session.flush()
    await _add_knowledge_document(
        db_session,
        workspace,
        name="source.txt",
        mime_type="text/plain",
        source="blueprint",
        group=group,
    )
    await _add_knowledge_document(
        db_session,
        workspace,
        name="project-brief.md",
        mime_type="text/markdown",
        is_trashed=True,
        group=group,
    )
    outsider = Workspace(
        id=generate_ulid(),
        entity_id=workspace.entity_id,
        name="Other workspace",
        status="active",
        settings={},
    )
    db_session.add(outsider)
    await db_session.flush()
    await _add_knowledge_document(
        db_session,
        outsider,
        name="source.txt",
        mime_type="text/plain",
    )
    await _add_knowledge_document(
        db_session,
        outsider,
        name="project-brief.md",
        mime_type="text/markdown",
    )

    blocked = await evaluate_workspace_blocking_setup(db_session, workspace)
    assert blocked is not None
    assert blocked.blocks_work


@pytest.mark.asyncio
async def test_user_materials_check_accepts_fulfilled_input_or_attachments(
    db_session,
) -> None:
    workspace = _workspace_with_materials_gate()
    db_session.add(workspace)
    await db_session.flush()
    await _add_knowledge_document(
        db_session,
        workspace,
        name="source.txt",
        mime_type="text/plain",
    )
    db_session.add(HumanCommitment(
        entity_id=workspace.entity_id,
        workspace_id=workspace.id,
        request_kind="input",
        source_kind="proposal_item",
        source_id=generate_ulid(),
        expected_input="Upload the required source file, or say the notes are skipped.",
        status="fulfilled",
        response={"text": "Please skip-notes for this run."},
    ))
    await db_session.flush()

    ready = await evaluate_workspace_blocking_setup(db_session, workspace)
    assert ready is not None
    assert not ready.blocks_work

    other = _workspace_with_materials_gate()
    db_session.add(other)
    await db_session.flush()
    db_session.add(HumanCommitment(
        entity_id=other.entity_id,
        workspace_id=other.id,
        request_kind="input",
        source_kind="proposal_item",
        source_id=generate_ulid(),
        expected_input="Upload the required source file, or say the notes are skipped.",
        status="fulfilled",
        response={
            "text": "files attached",
            "attachments": [
                {"name": "source.txt", "mime_type": "text/plain"},
                {"name": "project-brief.md", "mime_type": "text/markdown"},
            ],
        },
    ))
    await db_session.flush()
    attached = await evaluate_workspace_blocking_setup(db_session, other)
    assert attached is not None
    assert not attached.blocks_work


@pytest.mark.asyncio
async def test_setup_input_answer_only_counts_for_its_declared_check(db_session) -> None:
    workspace = _workspace_with_materials_gate()
    db_session.add(workspace)
    await db_session.flush()
    await _add_knowledge_document(
        db_session, workspace, name="source.txt", mime_type="text/plain",
    )
    db_session.add(HumanCommitment(
        entity_id=workspace.entity_id,
        workspace_id=workspace.id,
        request_kind="input",
        source_kind="workspace_setup",
        source_id=generate_ulid(),
        status="fulfilled",
        response={"text": "skip-notes", "setup_check_key": "another_check"},
    ))
    await db_session.flush()
    blocked = await evaluate_workspace_blocking_setup(db_session, workspace)
    assert blocked is not None and blocked.blocks_work

    db_session.add(HumanCommitment(
        entity_id=workspace.entity_id,
        workspace_id=workspace.id,
        request_kind="input",
        source_kind="workspace_setup",
        source_id=generate_ulid(),
        status="fulfilled",
        response={"text": "skip-notes", "setup_check_key": "required_materials"},
    ))
    await db_session.flush()
    ready = await evaluate_workspace_blocking_setup(db_session, workspace)
    assert ready is not None and not ready.blocks_work


@pytest.mark.asyncio
async def test_setup_gate_injects_need_input_until_waiting_card_exists(
    db_session,
) -> None:
    workspace = _workspace_with_materials_gate()
    db_session.add(workspace)
    await db_session.flush()
    setup_status = await evaluate_workspace_blocking_setup(db_session, workspace)
    assert setup_status is not None and setup_status.blocks_work

    proposal = _proposal()
    assert _restrict_proposal_to_setup_work(proposal, setup_status) is True
    await _ensure_blocking_setup_human_request(
        db_session,
        workspace,
        proposal,
        setup_status,
    )
    assert proposal.tasks == []
    assert len(proposal.human_requests) == 1
    request = proposal.human_requests[0]
    assert request.request_key == "required_materials"
    assert request.request_kind == "input"
    assert "Upload the required source file" in request.question

    db_session.add(HumanCommitment(
        entity_id=workspace.entity_id,
        workspace_id=workspace.id,
        request_kind="input",
        source_kind="proposal_item",
        source_id=generate_ulid(),
        expected_input=request.question,
        status="waiting",
    ))
    await db_session.flush()

    duplicate = _proposal(request_key="required_materials")
    _restrict_proposal_to_setup_work(duplicate, setup_status)
    await _ensure_blocking_setup_human_request(
        db_session,
        workspace,
        duplicate,
        setup_status,
    )
    assert duplicate.human_requests == []


@pytest.mark.asyncio
async def test_setup_gate_keeps_existing_allowlisted_request_and_passes_when_ready(
    db_session,
) -> None:
    workspace = _workspace_with_materials_gate()
    db_session.add(workspace)
    await db_session.flush()
    await _add_knowledge_document(
        db_session,
        workspace,
        name="source.txt",
        mime_type="text/plain",
    )
    await _add_knowledge_document(
        db_session,
        workspace,
        name="project-brief.md",
        mime_type="text/markdown",
    )
    ready = await evaluate_workspace_blocking_setup(db_session, workspace)
    assert ready is not None
    assert not ready.blocks_work

    proposal = _proposal()
    original_tasks = list(proposal.tasks)
    assert _restrict_proposal_to_setup_work(proposal, ready) is False
    await _ensure_blocking_setup_human_request(db_session, workspace, proposal, ready)
    assert proposal.tasks == original_tasks
    assert proposal.human_requests == []

    blocked_workspace = _workspace_with_materials_gate()
    db_session.add(blocked_workspace)
    await db_session.flush()
    blocked = await evaluate_workspace_blocking_setup(db_session, blocked_workspace)
    existing = _proposal(request_key="required_materials")
    _restrict_proposal_to_setup_work(existing, blocked)
    await _ensure_blocking_setup_human_request(
        db_session,
        blocked_workspace,
        existing,
        blocked,
    )
    assert [item.request_key for item in existing.human_requests] == ["required_materials"]
