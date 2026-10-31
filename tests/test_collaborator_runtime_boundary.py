"""An external collaborator's agent runs reach only their live-granted Workspace."""

import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from test_workspace_collaboration import accepted, minted
from packages.core.ai.runtime.approval_classifier import RUNTIME_LOOP_CONTROL_TOOL_NAMES, classify_runtime_tool
from packages.core.ai.runtime.composite_tools import SandboxToolAction, WorkspaceToolAction
from packages.core.ai.runtime.crm_actions import runtime_crm_action
from packages.core.ai.runtime.tool_registry import runtime_registered_tool_names
from packages.core.ai.runtime.workspace_operation_actions import WORKSPACE_OPERATION_ACTIONS
from packages.core.ai.tools.manor_tool import get_manor_action_names
from packages.core.models.document import Document, DocumentGroup, DocumentGroupMember
from packages.core.models.permission import GrantStatus, ResourceGrant, SubjectType
from packages.core.models.people import Client
from packages.core.models.task import Task
from packages.core.models.user import UserMembership
from packages.core.models.workspace import Workspace
from packages.core.services.runtime_authorization import authorize_hitl_action, authorize_runtime_action
from packages.core.services.runtime_authorization.collaborator import COLLABORATOR_RUNTIME_SUBJECTS


def _catalog():
    """Every runtime tool call shape: registered tools and every dispatcher action."""
    dispatch = {
        "sandbox": [{"action": action} for action in SandboxToolAction.values()],
        "workspace_operation": [{"action": action} for action in WORKSPACE_OPERATION_ACTIONS],
        "workspace_agent": [
            {"action": "operation", "params": {"action": operation}} for operation in WORKSPACE_OPERATION_ACTIONS
        ] + [{"action": action, "params": {}} for action in WorkspaceToolAction.values() if action != "operation"],
        "manor": [{"action": action, "params": {}} for action in sorted({*get_manor_action_names(), "search"})],
    }
    # Loop-armed control tools are created per run, outside the registry.
    names = {*runtime_registered_tool_names(include_undiscoverable=True), *RUNTIME_LOOP_CONTROL_TOOL_NAMES}
    for name in sorted(names):
        if name.startswith("mcp__"):
            continue
        for args in dispatch.get(name, [{}]):
            authorization = classify_runtime_tool(name, args).authorization
            if authorization is not None:
                yield name, args, authorization
    # Connected-account tools classify from the provider's declared effect.
    for name in ("mcp__gmail__send_message", "mcp__gmail__search_threads"):
        yield name, {}, classify_runtime_tool(name, {}, declared_effect="write").authorization


# Emitted outside the registered tool catalog: file persistence re-authorizes
# path writes as workspace.file.write, and channel replies come from inbound turns.
_EMITTED_OUTSIDE_CATALOG = {("workspace.file.write", "file.write"), ("channel.reply", "")}


async def _authorize(db, user_id, entity_id, workspace_id, name, authorization):
    return await authorize_runtime_action(
        db,
        entity_id=entity_id,
        user_id=user_id,
        workspace_id=workspace_id,
        action_key=authorization.action_key,
        capability_id=authorization.capability_id,
        access=authorization.access,
        tool_name=name,
    )


def _action(args) -> str:
    params = args.get("params") if isinstance(args.get("params"), dict) else {}
    action = str(args.get("action") or "")
    return f"{action}/{params['action']}" if params.get("action") else action


async def _inside(client, p):
    # A scoped session minted from the provider's own sign-in, as the UI does.
    return await minted(client, p.provider_headers, p.source, p.delivery_id)


@asynccontextmanager
async def _gated(db, entity_id, *user_ids):
    """The scope a tool call runs in once the runtime gate (or the HTTP boundary) admitted it.

    Handlers read who is a collaborator from this context only, so a direct
    handler call is wrapped the way the gate wraps it: the collaborators among
    ``user_ids`` (the run's user, the Task's steering collaborators) act.
    """
    from packages.core.services.collaborator_steering import collaborator_steering_scope, collaborators_acting

    ids = sorted({str(user_id) for user_id in user_ids if user_id})
    collaborators = (await db.scalars(select(UserMembership.user_id).where(
        UserMembership.entity_id == entity_id,
        UserMembership.user_id.in_(ids),
        UserMembership.role == "collaborator",
    ))).all() if ids else []
    with collaborator_steering_scope():
        collaborators_acting(entity_id, collaborators)
        yield


async def _trial(client, db):
    ph, p, rh, r, cid, source, target, delivery, token = await accepted(client, db)
    customer = (await db.get(Workspace, target)).entity_id
    other = await client.post(
        "/api/v1/workspaces", headers=rh, json={"name": "Customer only", "kind": "campaign", "primary_work": "Ops"}
    )
    assert other.status_code == 201, other.text
    p.provider_headers, p.customer_headers = ph, rh
    p.source, p.delivery_id = source, delivery["id"]
    return p, r, customer, target, other.json()["id"]


async def _issuer_leaves(db, issuer_id, customer):
    # Nothing re-derives the collaborator's membership or seat afterwards.
    issuer = await db.scalar(
        select(UserMembership).where(UserMembership.user_id == issuer_id, UserMembership.entity_id == customer)
    )
    issuer.status = "inactive"
    await db.commit()


# Every (tool, action) a collaborator's run may call in their granted Workspace.
# A new admission fails here until it is reviewed: add it with the reference
# check or handler scoping that pins it to the Workspace.
ADMITTED = {
    ("ai_edit_workflow", ""), ("align_subtitles", ""), ("analyze_audio", ""), ("answer_task_blocker", ""),
    ("archive_file_collection", ""), ("browse_web", ""), ("build_narration_timeline", ""), ("call_tool", ""),
    ("cancel_scheduled_job", ""), ("cancel_workflow_run", ""), ("complete_task", ""),
    ("compose_creative_deck", ""), ("compose_creative_dub", ""), ("compose_creative_publication", ""),
    ("compose_creative_video", ""),
    ("compose_video_timeline", ""),
    ("create_goal", ""), ("create_scheduled_job", ""), ("create_workflow", ""), ("delegate_to_agent", ""),
    ("delete_file", ""), ("delete_workflow", ""),
    # Same scoping as still_to_video: its input must already be a file of the granted Workspace, and its
    # crop is written back into that Workspace only.
    ("derive_image_aspect", ""),
    ("generate_file", ""), ("generate_image", ""),
    ("generate_video", ""), ("get_current_time", ""), ("get_task_details", ""), ("get_workflow", ""),
    ("glob_files", ""), ("grep_files", ""), ("import_free_music", ""), ("invoke_skill", ""),
    ("list_documents", ""), ("list_files", ""), ("list_scheduled_jobs", ""), ("list_workspace_flows", ""),
    ("merge_videos", ""), ("normalize_audio_loudness", ""), ("patch_file", ""),
    ("prepare_creative_studio", ""), ("prepare_narration_timeline", ""), ("prepare_recording", ""),
    ("probe_media", ""), ("query_ledger", ""), ("query_scheduled_jobs", ""), ("rag", ""),
    ("read_content_ledger", ""), ("read_file", ""), ("read_finance_ledger", ""),
    ("read_recruiting_ledger", ""), ("read_relationship_ledger", ""), ("read_stickman_topic_ledger", ""),
    ("record_content_ledger", ""), ("record_finance_ledger", ""), ("record_recruiting_ledger", ""),
    ("record_relationship_ledger", ""), ("record_stickman_topic_ledger", ""),
    ("record_youtube_publication", ""), ("record_youtube_workspace_metrics", ""), ("render_frame_samples", ""),
    ("render_recording", ""), ("render_response_surface", ""), ("replace_file", ""), ("request_input", ""),
    ("resume_workflow_run", ""), ("run_scheduled_job_now", ""), ("sandbox_cancel", ""), ("sandbox_create", ""),
    ("sandbox_destroy", ""), ("sandbox_exec", ""), ("sandbox_read_file", ""), ("sandbox_save_result", ""),
    ("sandbox_status", ""), ("sandbox_write_file", ""), ("search_documents", ""), ("search_tasks", ""),
    ("search_tools", ""), ("start_workspace_flow", ""), ("still_to_video", ""),
    ("test_workflow", ""), ("test_workflow_node", ""), ("toggle_scheduled_job", ""), ("transcribe_video", ""),
    ("update_goal_value", ""), ("update_task", ""), ("update_task_plan", ""), ("update_workflow", ""),
    ("validate_workflow", ""), ("verify_creative_studio", ""), ("video_edit", ""),
    ("visualize_workspace_ledgers", ""), ("wait_media_jobs", ""), ("weather_search", ""),
    ("web_event_search", ""), ("web_fetch", ""), ("web_search", ""), ("website_content", ""),
    ("workspace_add_knowledge_documents", ""), ("workspace_add_rule", ""),
    ("workspace_create_knowledge_folder", ""), ("workspace_create_task", ""), ("workspace_list_knowledge", ""),
    ("workspace_remove_knowledge_document", ""), ("workspace_request_strategist_review", ""),
    ("workspace_resolve_hitl", ""), ("workspace_search", ""), ("workspace_update_knowledge_policy", ""),
    ("workspace_update_task_runtime", ""),
    ("manor", "add_task_comment"), ("manor", "assign_task"), ("manor", "cancel_scheduled_job"),
    ("manor", "create_scheduled_job"), ("manor", "create_task"), ("manor", "delete_document"),
    ("manor", "add_assessed_metrics"), ("manor", "invoke_skill"), ("manor", "record_stat_assessment"), ("manor", "run_scheduled_job_now"),
    ("manor", "toggle_scheduled_job"), ("manor", "update_goal_value"), ("manor", "update_task"),
    ("sandbox", "cancel"), ("sandbox", "create"), ("sandbox", "destroy"), ("sandbox", "exec"),
    ("sandbox", "read_file"), ("sandbox", "save_result"), ("sandbox", "status"), ("sandbox", "write_file"),
    ("workspace_agent", "add_assessed_metrics"), ("workspace_agent", "add_knowledge_documents"),
    ("workspace_agent", "add_rule"),
    ("workspace_agent", "answer_task_blocker"), ("workspace_agent", "create_knowledge_folder"),
    ("workspace_agent", "create_task"), ("workspace_agent", "delegate_service"),
    ("workspace_agent", "get_goal_status"), ("workspace_agent", "get_stat_status"),
    # Only adds reviews to the caller's own conversation.
    ("workspace_agent", "hold_for_approval"),
    ("workspace_agent", "list_knowledge"), ("workspace_agent", "operation/apply_draft"),
    ("workspace_agent", "operation/create_draft"), ("workspace_agent", "operation/design_app"),
    ("workspace_agent", "operation/discard_draft"),
    ("workspace_agent", "operation/get_current"), ("workspace_agent", "operation/patch_draft"),
    ("workspace_agent", "operation/preview_diff"), ("workspace_agent", "operation/validate_draft"),
    ("workspace_agent", "read_delegation_result"), ("workspace_agent", "record_stat_assessment"),
    ("workspace_agent", "remove_knowledge_document"), ("workspace_agent", "request_strategist_review"),
    ("workspace_agent", "resolve_hitl"), ("workspace_agent", "search"),
    ("workspace_agent", "start_task"),
    ("workspace_agent", "update_goal_value"), ("workspace_agent", "update_knowledge_policy"),
    ("workspace_agent", "update_task_runtime"), ("workspace_agent", "visualize_ledgers"),
    ("workspace_operation", "apply_draft"), ("workspace_operation", "create_draft"),
    ("workspace_operation", "design_app"),
    ("workspace_operation", "discard_draft"), ("workspace_operation", "get_current"),
    ("workspace_operation", "patch_draft"), ("workspace_operation", "preview_diff"),
    ("workspace_operation", "validate_draft"),
}


@pytest.mark.asyncio
async def test_collaborator_runtime_admits_exactly_the_reviewed_subjects(client, db_session):
    # Catches: any new subject admitted for a collaborator's run without
    # review (CRM, people, integrations, company settings, Workspace creation,
    # shell, unbound connected accounts); a reviewed Workspace subject denied
    # in the granted Workspace (the company-permission step still applied);
    # any subject admitted in another customer Workspace or with no Workspace;
    # an unknown subject admitted; an allowlist entry no tool emits; the
    # derived seat standing in for the grant.
    p, r, customer, target, other = await _trial(client, db_session)
    catalog = list(_catalog())
    emitted = {(a.action_key, a.capability_id or "") for _, _, a in catalog}
    for group in COLLABORATOR_RUNTIME_SUBJECTS:
        assert group.subjects - emitted - _EMITTED_OUTSIDE_CATALOG == set(), group.reason

    admitted = set()
    for name, args, authorization in catalog:
        decision = await _authorize(db_session, p.id, customer, target, name, authorization)
        if not decision.allowed:
            assert decision.matched_rule == "permission.collaborator.subject", (name, args, decision)
            continue
        admitted.add((name, _action(args)))
        assert decision.collaborator_workspace_ids == frozenset({target})
        for workspace_id in (other, None):
            outside = await _authorize(db_session, p.id, customer, workspace_id, name, authorization)
            assert outside.matched_rule == "permission.collaborator.workspace", (name, args, workspace_id)
    assert admitted == ADMITTED
    unknown = await authorize_runtime_action(
        db_session, entity_id=customer, user_id=p.id, workspace_id=target,
        action_key="tool.not_yet_written.read", capability_id=None, access="read", tool_name="not_yet_written",
    )
    assert unknown.matched_rule == "permission.collaborator.subject"

    # A customer member keeps the ordinary decision; an admin is not narrowed.
    crm = classify_runtime_tool("manor", {"action": "list_clients"}).authorization
    assert (await _authorize(db_session, r.id, customer, target, "manor", crm)).allowed

    # A user-less background call (dispatcher, strategist) acts for its Task's
    # requester, and a run may not carry one identity while executing as another.
    task = classify_runtime_tool("workspace_agent", {"action": "create_task", "params": {}}).authorization
    requested = Task(entity_id=customer, workspace_id=target, title="Trial work", creator_id=p.id)
    db_session.add(requested)
    await db_session.commit()

    async def background(authorization, **identity):
        return await authorize_runtime_action(
            db_session, entity_id=customer, workspace_id=target, action_key=authorization.action_key,
            capability_id=authorization.capability_id, access=authorization.access, **identity,
        )

    worker = {"user_id": None, "principal_kind": "system_worker", "task_id": requested.id}
    assert (await background(task, **worker)).allowed
    assert (await background(crm, **worker)).matched_rule == "permission.collaborator.subject"
    assert (await background(crm, user_id=None, principal_kind="system_worker")).allowed
    mismatch = await background(task, user_id=r.id, principal_execution_user_id=p.id, tool_name="workspace_agent")
    assert mismatch.matched_rule == "permission.runtime.identity_mismatch"

    # An ended collaboration never falls back to company-wide rules, here the
    # system worker's allow-all.
    membership = await db_session.scalar(
        select(UserMembership).where(UserMembership.user_id == p.id, UserMembership.entity_id == customer)
    )
    membership.status = "inactive"
    await db_session.commit()
    deactivated = await background(task, user_id=p.id, principal_kind="system_worker")
    assert deactivated.matched_rule == "permission.collaborator.workspace"
    membership.status = "active"
    await db_session.commit()

    await _issuer_leaves(db_session, r.id, customer)
    ended = await _authorize(db_session, p.id, customer, target, "workspace_agent", task)
    assert not ended.allowed and ended.matched_rule == "permission.collaborator.workspace"


@pytest.mark.asyncio
async def test_a_task_a_collaborator_steered_stays_in_their_workspace_whoever_runs_it(client, db_session):
    # Reproduces the review finding: a collaborator edits a customer's Task (or
    # approves a system Task's card) and the Task's later run - as the customer,
    # or user-less - read the customer CRM. Catches: steering not recorded on
    # the HTTP edit or the approval; the gate ignoring recorded steering; a
    # customer-only Task narrowed; a steered Task still running after the
    # collaborator's authority lapsed.
    from packages.core.models.hitl_request import HitlRequest
    from packages.core.services.collaborator_steering import collaborator_steering_scope

    p, r, customer, target, other = await _trial(client, db_session)
    inside = await _inside(client, p)
    customer_task = Task(entity_id=customer, workspace_id=target, title="Customer task", creator_id=r.id)
    system_task = Task(entity_id=customer, workspace_id=target, title="Strategist task")
    db_session.add_all([customer_task, system_task])
    await db_session.flush()
    card = HitlRequest(
        entity_id=customer, workspace_id=target, origin_kind="task", origin_task_id=system_task.id,
        dedup_key=f"steer:{system_task.id}", action_key="workspace.task.create", capability_id="workspace.task",
    )
    db_session.add(card)
    await db_session.commit()
    crm = classify_runtime_tool("manor", {"action": "list_clients"}).authorization
    work = classify_runtime_tool("workspace_agent", {"action": "create_task", "params": {}}).authorization

    async def as_customer(task, authorization):
        return await authorize_runtime_action(
            db_session, entity_id=customer, user_id=r.id, workspace_id=target, task_id=task.id,
            action_key=authorization.action_key, capability_id=authorization.capability_id,
            access=authorization.access,
        )

    async def user_less(task, authorization):
        return await authorize_runtime_action(
            db_session, entity_id=customer, user_id=None, workspace_id=target, task_id=task.id,
            principal_kind="system_worker", action_key=authorization.action_key,
            capability_id=authorization.capability_id, access=authorization.access,
        )

    assert (await as_customer(customer_task, crm)).allowed

    edited = await client.put(f"/api/v1/tasks/{customer_task.id}", headers=inside, json={"title": "Edited by provider"})
    assert edited.status_code == 200, edited.text
    await db_session.refresh(customer_task)
    assert customer_task.collaborator_ids == [p.id]
    # The request's marking ended with the request.
    later = Task(entity_id=customer, workspace_id=target, title="Written after the request", creator_id=r.id)
    db_session.add(later)
    await db_session.commit()
    assert later.collaborator_ids is None
    assert (await as_customer(customer_task, crm)).matched_rule == "permission.collaborator.subject"
    assert (await as_customer(customer_task, work)).allowed

    # The collaborator's run resolves the system Task's card: the gate admits
    # the call and every write it makes is the collaborator's steering.
    resolve = classify_runtime_tool("workspace_agent", {"action": "resolve_hitl", "params": {}}).authorization
    with collaborator_steering_scope():
        assert (await _authorize(db_session, p.id, customer, target, "workspace_agent", resolve)).allowed
        card.status = "granted"
        await db_session.commit()
    await db_session.refresh(system_task)
    assert system_task.collaborator_ids == [p.id]
    assert (await user_less(system_task, crm)).matched_rule == "permission.collaborator.subject"
    assert (await user_less(system_task, work)).allowed

    # A member's save of the content takes the Task over; a later
    # collaborator edit steers it again.
    customer_headers = p.customer_headers
    resaved = await client.put(f"/api/v1/tasks/{customer_task.id}", headers=customer_headers, json={"title": "Reviewed"})
    assert resaved.status_code == 200 and resaved.json()["steered_by_collaborator"] is False, resaved.text
    await db_session.refresh(customer_task)
    assert customer_task.collaborator_ids is None
    assert (await as_customer(customer_task, crm)).allowed
    again = await client.put(f"/api/v1/tasks/{customer_task.id}", headers=inside, json={"description": "Provider notes"})
    assert again.json()["steered_by_collaborator"] is True, again.text
    await db_session.refresh(customer_task)
    assert customer_task.collaborator_ids == [p.id]

    # The collaboration ends: the steered Task stops with a reason a member can
    # act on, at the gate and where its run resolves who executes it.
    from packages.core.services.task_requester_identity import (
        TASK_STEERING_ENDED_MESSAGE,
        TaskSteeringEndedError,
        resolve_task_execution_user_id,
    )
    from test_workspace_collaboration import trial_grant

    grant = await trial_grant(db_session, target)
    grant.status = "revoked"
    await db_session.commit()
    lapsed = await as_customer(customer_task, work)
    assert lapsed.matched_rule == "permission.collaborator.steering_ended"
    assert lapsed.reason == TASK_STEERING_ENDED_MESSAGE
    with pytest.raises(TaskSteeringEndedError) as stopped:
        await resolve_task_execution_user_id(db_session, customer_task)
    assert str(stopped.value) == TASK_STEERING_ENDED_MESSAGE
    reviewed = await client.put(f"/api/v1/tasks/{customer_task.id}", headers=customer_headers, json={"title": "Ours now"})
    assert reviewed.status_code == 200, reviewed.text
    await db_session.refresh(customer_task)
    assert (await as_customer(customer_task, crm)).allowed
    assert await resolve_task_execution_user_id(db_session, customer_task) == r.id


@pytest.mark.asyncio
async def test_collaborator_uses_only_integrations_bound_to_the_granted_workspace(client, db_session):
    # Catches: a connected-account tool admitted for a collaborator without a
    # binding on the granted Workspace (company-level MCP) or with one bound to
    # another Workspace; a collaborator binding a company MCP server (the
    # operation validation admits an account-free server); a member's binding
    # refused; a collaborator's own binding outliving their connection.
    from packages.core.models.mcp import MCPServer
    from packages.core.models.user import OAuthAccount
    from packages.core.services.collaborator_steering import CollaboratorBindingRejected, collaborator_steering_scope

    p, r, customer, target, other = await _trial(client, db_session)
    inside = await _inside(client, p)
    p_id, customer_headers = p.id, p.customer_headers  # the test rolls back below
    db_session.add(MCPServer(server_key="acme_crm", name="Acme CRM", transport="builtin", auth_type="none"))
    other_workspace = await db_session.get(Workspace, other)
    other_workspace.operating_model = {**(other_workspace.operating_model or {}), "capability_bindings": [
        {"capability_type": "mcp", "mcp_server_key": "acme_crm", "allowed_tools": ["delete_record"]},
    ]}
    await db_session.commit()

    def mcp(provider, tool="send_message"):
        return {
            "binding_key": f"{provider}-{tool}", "owner_scope": "workspace_agent", "capability_type": "mcp",
            "capability": provider, "integration_key": provider, "allowed_tools": [tool],
        }

    async def bind(headers, *bindings):
        current = (await db_session.get(Workspace, target, populate_existing=True)).operating_model or {}
        await db_session.commit()
        return await client.put(
            f"/api/v1/workspaces/{target}/operating-model", headers=headers,
            json={**current, "capability_bindings": [*current.get("capability_bindings", []), *bindings]},
        )

    async def call(name, workspace_id=target):
        authorization = classify_runtime_tool(name, {}, declared_effect="write").authorization
        return await authorize_runtime_action(
            db_session, entity_id=customer, user_id=p_id, workspace_id=workspace_id,
            action_key=authorization.action_key, capability_id=authorization.capability_id,
            access=authorization.access, tool_name=name, bound_tool_names={name},
        )

    refused = await bind(inside, mcp("acme_crm"))
    assert refused.status_code == 403 and "company integration" in refused.text, refused.text
    assert (await call("mcp__acme_crm__send_message")).matched_rule == "permission.collaborator.subject"

    bound = await bind(customer_headers, mcp("acme_crm"))
    assert bound.status_code == 200, bound.text
    assert (await call("mcp__acme_crm__send_message")).allowed
    assert (await call("mcp__acme_crm__delete_record")).matched_rule == "permission.collaborator.subject"
    assert (await call("mcp__acme_crm__delete_record", other)).matched_rule == "permission.collaborator.workspace"

    # A collaborator's own connection: their binding carries their provenance
    # and counts only while they hold the connection.
    own = OAuthAccount(user_id=p_id, provider="slack", provider_user_id="provider-slack")
    db_session.add(own)
    await db_session.commit()
    workspace = await db_session.get(Workspace, target)
    current = dict(workspace.operating_model or {})
    with collaborator_steering_scope():
        assert (await call("mcp__acme_crm__send_message")).allowed  # the gate marks p as acting
        workspace.operating_model = {**current, "capability_bindings": [*current["capability_bindings"], mcp("slack")]}
        await db_session.commit()
        # Re-saving it unchanged cannot strip that provenance.
        workspace.operating_model = {**current, "capability_bindings": [*current["capability_bindings"], mcp("slack")]}
        await db_session.commit()
        workspace.operating_model = {**current, "capability_bindings": [mcp("gmail")]}
        with pytest.raises(CollaboratorBindingRejected, match="company integration"):
            await db_session.flush()
        await db_session.rollback()
    stored = (await db_session.get(Workspace, target, populate_existing=True)).operating_model["capability_bindings"]
    assert [entry.get("added_by_collaborator") for entry in stored] == [None, p_id]
    assert (await call("mcp__slack__send_message")).allowed
    await db_session.delete(own)
    await db_session.commit()
    assert (await call("mcp__slack__send_message")).matched_rule == "permission.collaborator.subject"


@pytest.mark.asyncio
async def test_a_collaborator_with_a_staff_row_is_still_a_collaborator(client, db_session):
    # Catches: the runtime gate (or HITL resolution) deciding collaborator
    # status from the resolved role -- here a member StaffRole that grants the
    # call -- instead of the membership role, letting a collaborator reach the
    # customer's CRM and company-wide scope.
    from packages.core.models.staff import Staff, StaffRole
    from packages.core.permissions import _get_role_permissions
    from packages.core.services.resource_access import resolve_user_role

    p, r, customer, target, other = await _trial(client, db_session)
    role = StaffRole(entity_id=customer, name="member",
                     permissions=sorted(permission.value for permission in _get_role_permissions("member")))
    db_session.add(role)
    await db_session.flush()
    db_session.add(Staff(entity_id=customer, user_id=p.id, name="Provider", status="active", role_id=role.id))
    await db_session.commit()
    assert await resolve_user_role(db_session, user_id=p.id, entity_id=customer) == "member"

    crm = classify_runtime_tool("manor", {"action": "list_clients"}).authorization
    for workspace_id, rule in ((None, "permission.collaborator.workspace"), (other, "permission.collaborator.workspace"),
                               (target, "permission.collaborator.subject")):
        decision = await _authorize(db_session, p.id, customer, workspace_id, "manor", crm)
        assert not decision.allowed and decision.matched_rule == rule, (workspace_id, decision)
    for workspace_id in (other, None):
        card = await authorize_hitl_action(
            db_session, entity_id=customer, workspace_id=workspace_id, by_user_id=p.id,
            action_key="workspace.task.create", capability_id="workspace.task", hitl_type="authorize",
        )
        assert card.matched_rule == "permission.collaborator.workspace", (workspace_id, card)
    # A customer member keeps the company-wide call.
    assert (await _authorize(db_session, r.id, customer, None, "manor", crm)).allowed


@pytest.mark.asyncio
async def test_a_preset_collaborator_scope_on_a_request_is_ignored(client, db_session):
    # Catches: the service trusting a caller-built request's collaborator scope,
    # which would replace a member's company-permission check with the
    # collaborator's Workspace grant.
    from dataclasses import replace

    from packages.core.services.runtime_authorization import get_authorization_service
    from packages.core.services.runtime_authorization.domain import RuntimeAuthorizationRequest

    p, r, customer, target, other = await _trial(client, db_session)
    crm = classify_runtime_tool("manor", {"action": "list_clients"}).authorization
    request = replace(RuntimeAuthorizationRequest.create(
        entity_id=customer, user_id=r.id, workspace_id=target, action_key=crm.action_key,
        capability_id=crm.capability_id, access=crm.access, tool_name="manor",
    ), collaborator_workspace_ids=frozenset({target}))
    decision = await get_authorization_service().authorize_runtime(db_session, request)
    assert decision.allowed and decision.collaborator_workspace_ids is None
    assert decision.matched_rule != "permission.collaborator.workspace"


@pytest.mark.asyncio
async def test_collaborator_run_cannot_read_the_customer_crm_or_name_other_workspace_resources(
    client, db_session, monkeypatch
):
    # Reproduces the probe: a collaborator chatting with the Workspace agent
    # asked it for the customer's clients. Catches: the tool gate admitting the
    # CRM action; the CRM actor admitting a collaborator membership; the Manor
    # gateway running a company-wide action for a collaborator; a Workspace
    # call naming another Workspace's Task or Workspace; a gateway (call_tool)
    # reaching a tool denied by name; Workspace work being refused.
    from packages.core import database
    from packages.core.ai.runtime.approval_service import guard_runtime_tool_action
    from packages.core.ai.tools.manor_tool import _manor_handler

    p, r, customer, target, other = await _trial(client, db_session)
    db_session.add(Client(entity_id=customer, name="Customer secret account"))
    in_workspace = Task(entity_id=customer, workspace_id=target, title="Trial task")
    elsewhere = Task(entity_id=customer, workspace_id=other, title="Customer-only task")
    db_session.add_all([in_workspace, elsewhere])
    await db_session.commit()
    monkeypatch.setattr(database, "async_session", async_sessionmaker(db_session.bind, expire_on_commit=False))

    async def guard(name, arguments):
        result = await guard_runtime_tool_action(
            name=name, arguments=arguments, entity_id=customer, user_id=p.id, workspace_id=target,
            conversation_id=None, operation_id="op-1",
        )
        return json.loads(result) if result else None

    blocked = await guard("manor", {"action": "list_clients", "params": {}})
    assert blocked["error"] == "blocked_by_permission"
    assert blocked["matched_rule"] == "permission.collaborator.subject"
    async with AsyncSession(bind=db_session.bind, expire_on_commit=False) as tool_db:
        direct = json.loads(await runtime_crm_action(
            tool_db, action="list_clients", params={}, entity_id=customer, user_id=p.id,
        ))
    assert not direct["ok"] and direct["error"]["status"] == 403
    assert "Customer secret account" not in json.dumps(direct)
    # A collaborator's run is always in their Workspace; the gateway refuses
    # a company-wide action there even for an action the gate admitted.
    async with _gated(db_session, customer, p.id):
        gateway = json.loads(await _manor_handler(
            entity_id=customer, action="list_staff", params={}, _user_id_from_context=p.id, workspace_id=target,
        ))
    assert gateway["error"] == "collaborator_action_unavailable"

    refused = await guard("update_task", {"task_id": elsewhere.id, "status": "completed"})
    assert refused["matched_rule"] == "permission.collaborator.subject"
    assert refused["message"] == "The call names a resource outside this Workspace."
    named = await guard("workspace_agent", {"action": "search", "params": {"query": "x", "workspace_id": other}})
    assert named["message"] == "The call names a resource outside this Workspace."
    shell = await guard("call_tool", {"name": "bash", "arguments": {"command": "cat Knowledge/plan.md > out.md"}})
    assert shell["error"] == "blocked_by_permission"

    assert await guard("workspace_agent", {"action": "search", "params": {"query": "trial"}}) is None
    own = await guard("update_task", {"task_id": in_workspace.id, "status": "completed"})
    assert (own or {}).get("error") != "blocked_by_permission", own


@pytest.mark.asyncio
async def test_collaborator_knowledge_is_the_granted_workspace_only(client, db_session, monkeypatch):
    # Catches: a company document the customer shared with the collaborator by
    # grant, or one linked only to another Workspace, appearing in a
    # collaborator's Knowledge reads, search listing or rag tool results
    # (single and batched evaluators, model-chosen Knowledge Nets, user-less
    # turns); the
    # Workspace's own documents missing; the derived seat
    # keeping Workspace documents readable after the grant's authority lapsed.
    from packages.core import database
    from packages.core.ai.runtime.rag import runtime_rag_action
    from packages.core.services.document_access import (
        list_visible_documents,
        unreadable_document_paths,
        user_can_read_document,
    )

    p, r, customer, target, other = await _trial(client, db_session)
    docs = {
        name: Document(entity_id=customer, name=f"{name} plan.md", file_type="md", source="upload",
                       fs_path=f"Knowledge/{name}.md", visibility=visibility, owner_id=r.id)
        for name, visibility in (("trial", "workspace"), ("company", "entity"), ("elsewhere", "workspace"))
    }
    db_session.add_all(docs.values())
    groups = {workspace_id: DocumentGroup(entity_id=customer, name="Knowledge", workspace_id=workspace_id)
              for workspace_id in (target, other)}
    db_session.add_all(groups.values())
    await db_session.flush()
    db_session.add_all([
        DocumentGroupMember(document_id=docs["trial"].id, group_id=groups[target].id),
        DocumentGroupMember(document_id=docs["elsewhere"].id, group_id=groups[other].id),
        ResourceGrant(entity_id=customer, resource_type="document", resource_id=docs["company"].id,
                      subject_type=SubjectType.USER, subject_id=p.id, capabilities=["view"],
                      status=GrantStatus.ACTIVE, granted_at=datetime.now(timezone.utc)),
    ])
    await db_session.commit()
    monkeypatch.setattr(database, "async_session", async_sessionmaker(db_session.bind, expire_on_commit=False))

    async def searched(user_id, *, steered_by=()):
        # The rag tool as a run calls it: run Workspace from context, Knowledge
        # Nets chosen by the model; the gate marks the collaborators behind
        # the call (a Task they steered, a run they started).
        from packages.core.services.collaborator_steering import collaborator_steering_scope, collaborators_acting

        result = {}
        for net_ids in ([], [groups[target].id, groups[other].id]):
            with collaborator_steering_scope():
                collaborators_acting(customer, steered_by)
                payload = json.loads(await runtime_rag_action(
                    entity_id=customer, user_id=user_id, workspace_id=target, trusted_workspace_id=target,
                    params={"question": "plan", "net_ids": net_ids},
                ))
            result[bool(net_ids)] = {source["name"] for source in payload["sources"]}
        return result

    async def readable():
        single = {
            name for name, doc in docs.items()
            if await user_can_read_document(db_session, doc, entity_id=customer, user_id=p.id, actor_type="agent")
        }
        listed, _ = await list_visible_documents(db_session, customer, user_id=p.id, actor_type="agent")
        blocked = await unreadable_document_paths(
            db_session, entity_id=customer, rel_paths=[doc.fs_path for doc in docs.values()],
            user_id=p.id, workspace_id=target, actor_type="agent",
        )
        batched = {name for name, doc in docs.items() if doc.fs_path not in blocked}
        assert {doc.id for doc in listed} == {docs[name].id for name in single}
        assert batched == single
        return single

    assert await readable() == {"trial"}
    assert await searched(p.id) == {False: {"trial plan.md"}, True: {"trial plan.md"}}
    # The same search names another Workspace's Knowledge Net, which a member
    # reads; a user-less turn of a collaborator-steered run stays in its
    # Workspace, while a customer-only user-less turn is not narrowed.
    assert "elsewhere plan.md" in (await searched(r.id))[True]
    assert await searched(None, steered_by=[p.id]) == {False: {"trial plan.md"}, True: {"trial plan.md"}}
    assert "elsewhere plan.md" in (await searched(None))[True]
    # Without indexed content the search falls back to document metadata,
    # which a user-less turn may not list at all.
    from packages.core.services import embedding_service

    async def nothing_indexed(*args, **kwargs):
        return []

    with monkeypatch.context() as patched:
        patched.setattr(embedding_service, "hybrid_search", nothing_indexed)
        assert await searched(None, steered_by=[p.id]) == {False: set(), True: set()}
        assert (await searched(p.id))[True] == {"trial plan.md"}
    owner_view = await user_can_read_document(db_session, docs["company"], entity_id=customer, user_id=r.id)
    assert owner_view
    await _issuer_leaves(db_session, r.id, customer)
    assert await readable() == set()
    assert await searched(p.id) == {False: set(), True: set()}


@pytest.mark.asyncio
async def test_collaborator_workflows_call_only_their_workspace_workflows_and_agents(
    client, db_session, monkeypatch
):
    # Catches: a collaborator saving (directly or through generate_workflow) a
    # Workflow whose subworkflow step names a company Workflow by id or name,
    # or whose agent step names another Workspace's Agent; a run the
    # collaborator started, authored or steered calling them anyway (a child
    # run inherits the Workspace, so the company Workflow would run and be
    # readable there); a customer's own Workflow being refused.
    from packages.core.ai.tools import workflow_tools
    from packages.core.ai.workflow_runner import WorkflowRunner
    from packages.core.models.workflow import WorkflowDefinition, WorkflowRun
    from packages.core.models.workspace import Agent
    from packages.core.services import workflow_generator
    from packages.core.services.collaborator_steering import collaborator_steering_scope, collaborators_acting

    p, r, customer, target, other = await _trial(client, db_session)
    company = WorkflowDefinition(entity_id=customer, workspace_id=other, name="Company payroll", steps=[], created_by=r.id)
    own = WorkflowDefinition(entity_id=customer, workspace_id=target, name="Trial flow", steps=[], created_by=r.id)
    foreign_agent = Agent(entity_id=customer, workspace_id=other, name="Payroll agent")
    db_session.add_all([company, own, foreign_agent])
    await db_session.commit()
    monkeypatch.setattr(workflow_tools, "async_session", async_sessionmaker(db_session.bind, expire_on_commit=False))

    def calls(step):
        return [{"id": "call", "type": step[0], "name": "Call", "config": step[1]}]

    subflow = ("subworkflow", {"workflow_id": company.id})
    by_name = ("subworkflow", {"workflow_id": "Company payroll"})
    agent = ("agent", {"agent_id": foreign_agent.id, "prompt": "Run payroll"})

    async def created(steps=None, prompt=None):
        with collaborator_steering_scope():
            collaborators_acting(customer, [p.id])
            return json.loads(await workflow_tools._create_workflow(
                entity_id=customer, name="Collaborator flow", steps=steps, prompt=prompt, allow_invalid=True,
                user_id=p.id, _user_id_from_context=p.id, workspace_id=target,
            ))

    for step in (subflow, by_name, agent):
        assert "outside this Workspace" in (await created(calls(step)))["error"], step

    async def generated(*_args, **_kwargs):
        return {"name": "Generated", "steps": calls(subflow), "variables": {}}

    monkeypatch.setattr(workflow_generator, "generate_workflow", generated)
    assert "outside this Workspace" in (await created(prompt="Pay everyone"))["error"]
    saved = await created(calls(("subworkflow", {"workflow_id": own.id})))
    assert saved["ok"], saved

    runner = WorkflowRunner()

    async def executed(step, *, started_by, author=None, steered=(), variables=None):
        definition = WorkflowDefinition(entity_id=customer, workspace_id=target, name="Run", steps=[], created_by=author or started_by)
        task = Task(entity_id=customer, workspace_id=target, title="Steered", collaborator_ids=list(steered))
        db_session.add_all([definition, task])
        await db_session.commit()
        run = WorkflowRun(
            entity_id=customer, workflow_id=definition.id, workspace_id=target, started_by=started_by,
            variables=variables or {}, step_results={},
            trigger_data={"_workflow_runtime_context": {"workspace_id": target, "task_id": task.id}},
        )
        return await runner._execute_step(calls(step)[0], run, db_session)

    refused = "collaborator_reference_outside_workspace"
    for case in (
        {"started_by": p.id},
        {"started_by": r.id, "author": p.id},
        {"started_by": r.id, "steered": [p.id]},
    ):
        assert (await executed(subflow, **case)).get("code") == refused, case
    assert (await executed(agent, started_by=p.id)).get("code") == refused
    templated = ("subworkflow", {"workflow_id": "{{target}}"})
    assert (await executed(templated, started_by=p.id, variables={"target": company.id})).get("code") == refused
    # The customer's own run of the same steps resolves and calls the company Workflow.
    customer_run = await executed(subflow, started_by=r.id)
    assert customer_run.get("code") != refused
    assert customer_run["error"] == "subworkflow requires an explicit trigger or webhook entry node"
    assert (await executed(templated, started_by=r.id, variables={"target": company.id})).get("code") != refused


@pytest.mark.asyncio
async def test_workflow_runs_resolve_their_collaborators_once_and_only_where_granted(client, db_session, monkeypatch):
    # Catches: the runner looking up who is behind a run (definition, Task,
    # memberships) on every reference step, or at all for a run in a Workspace
    # no collaboration grant ever covered; a cached answer leaking across runs.
    from packages.core.ai.workflow_runner import WorkflowRunner
    from packages.core.models.workflow import WorkflowDefinition, WorkflowRun
    from packages.core.services.runtime_authorization import collaborator

    p, r, customer, target, other = await _trial(client, db_session)
    company = WorkflowDefinition(entity_id=customer, workspace_id=other, name="Company payroll", steps=[], created_by=r.id)
    db_session.add(company)
    await db_session.commit()
    lookups, checks = [], []
    real_lookup, real_check = collaborator.workflow_run_collaborators, collaborator.collaboration_access_granted

    async def lookup(db, run):
        lookups.append(run.id)
        return await real_lookup(db, run)

    async def check(db, **kwargs):
        checks.append(kwargs["workspace_id"])
        return await real_check(db, **kwargs)

    monkeypatch.setattr(collaborator, "workflow_run_collaborators", lookup)
    monkeypatch.setattr(collaborator, "collaboration_access_granted", check)
    step = {"id": "call", "type": "subworkflow", "name": "Call", "config": {"workflow_id": company.id}}

    async def steps(workspace_id, started_by, count=3):
        definition = WorkflowDefinition(entity_id=customer, workspace_id=workspace_id, name="Run", steps=[],
                                        created_by=started_by)
        db_session.add(definition)
        await db_session.flush()
        run = WorkflowRun(entity_id=customer, workflow_id=definition.id, workspace_id=workspace_id,
                          started_by=started_by, variables={}, step_results={})
        db_session.add(run)
        await db_session.commit()
        runner = WorkflowRunner()
        return [(await runner._execute_step(step, run, db_session)).get("code") for _ in range(count)]

    refused = "collaborator_reference_outside_workspace"
    assert refused not in await steps(other, r.id)
    assert (checks, lookups) == ([other], [])
    assert await steps(target, p.id) == [refused] * 3
    assert checks == [other, target] and len(lookups) == 1
    assert refused not in await steps(target, r.id)
    assert len(lookups) == 2


@pytest.mark.asyncio
async def test_collaborator_discovers_only_skills_invocable_in_their_workspace(client, db_session, monkeypatch):
    # Catches: tool discovery offering a collaborator a company Skill of
    # another Workspace (which the gate refuses to invoke) while their
    # Workspace's own Skill is offered; a customer member losing either.
    from packages.core import database
    from packages.core.ai.runtime import tool_search
    from packages.core.ai.runtime.skills import SkillDescriptor
    from packages.core.models.skill import Skill

    p, r, customer, target, other = await _trial(client, db_session)
    skills = {
        workspace_id: Skill(entity_id=customer, workspace_id=workspace_id, name=f"ledger {workspace_id}",
                            slug=f"ledger-{workspace_id.lower()}", system_prompt="Read the ledger.")
        for workspace_id in (target, other)
    }
    db_session.add_all(skills.values())
    await db_session.commit()
    monkeypatch.setattr(database, "async_session", async_sessionmaker(db_session.bind, expire_on_commit=False))

    async def available(matches, *_args):
        return [{**match, "available": True} for match in matches]

    async def descriptors():
        return [
            SkillDescriptor(id=skill.id, slug=skill.slug, name=skill.name, description="Finance ledger guide",
                            metadata={"capability_companion": {"tool_names": ["read_finance_ledger"]}})
            for skill in skills.values()
        ]

    monkeypatch.setattr(tool_search, "runtime_annotate_tool_availability", available)

    async def offered(user_id):
        async with _gated(db_session, customer, user_id):
            payload = json.loads(await tool_search.runtime_execute_search_tools_handler(
                arguments={"mode": "exact", "exact_names": ["read_finance_ledger"], "max_results": 1,
                           "_user_id_from_context": user_id, "workspace_id": target},
                entity_id=customer, user_id=user_id,
                tool_schemas=[("read_finance_ledger", {"type": "function", "function": {
                    "name": "read_finance_ledger", "description": "Read the finance ledger",
                    "parameters": {"type": "object", "properties": {}},
                }})],
                available_tool_names=("read_finance_ledger",), skill_descriptor_loader=descriptors,
            ))
        return {match["skill_id"] for match in payload["matches"] if match.get("kind") == "skill"}

    assert await offered(p.id) == {skills[target].id}
    assert await offered(r.id) == {skills[target].id, skills[other].id}


@pytest.mark.asyncio
async def test_collaborator_runs_never_receive_customer_skill_credentials(client, db_session, monkeypatch):
    # Reproduces the finding: a collaborator invokes a customer sandbox Skill
    # and reads its injected credentials with `sandbox exec env`. Catches: the
    # Skill's stored credentials entering the sandbox of a collaborator's run
    # or of a run a collaborator steered; a member's run, or the collaborator's
    # own Skill, losing them.
    from packages.core import database
    from packages.core.config import get_settings
    from packages.core.services import skill_file_storage, skill_service
    from packages.core.services.sandbox_session_service import SandboxSessionError

    p, r, customer, target, other = await _trial(client, db_session)
    monkeypatch.setattr(database, "async_session", async_sessionmaker(db_session.bind, expire_on_commit=False))
    monkeypatch.setattr(get_settings(), "SANDBOX_SERVICE_URL", "http://sandbox-service")
    for loader, value in (
        ("load_skill_prompt", "Post the report."), ("load_skill_scripts", {"run.py": "print(1)"}),
        ("load_skill_requirements", ""), ("load_skill_extra_files", {}),
        ("load_skill_credentials", {"API_KEY": "customer-secret"}),
    ):
        monkeypatch.setattr(skill_file_storage, loader, lambda *_a, _v=value, **_k: _v)
    bundles = []

    owners = []

    async def admitted(*, bundle, binding, **_kwargs):
        bundles.append(dict(bundle.env))
        owners.append(binding.credentials_owner_user_id)
        return SandboxSessionError.of("create_failed", "stopped by the test")

    monkeypatch.setattr(skill_service, "acquire_sandbox_session", admitted)

    async def env(user_id, *, owner=r.id, steered_by=()):
        skill = SimpleNamespace(id="skill-1", entity_id=customer, owner_user_id=owner, name="Reporter",
                                slug="reporter", system_prompt="Post the report.", config={})
        async with _gated(db_session, customer, user_id, *steered_by):
            await skill_service._invoke_sandbox_skill(skill, customer, user_id, "go", conversation_id="conv-1")
        return bundles.pop()

    assert await env(p.id) == {}
    assert await env(r.id, steered_by=[p.id]) == {}
    assert await env(r.id) == {"API_KEY": "customer-secret"}
    assert await env(p.id, owner=p.id) == {"API_KEY": "customer-secret"}
    # The binding carries who owns the injected credentials (provenance).
    assert owners == [None, None, r.id, p.id]


@pytest.mark.asyncio
async def test_a_steered_run_never_enters_a_sandbox_holding_customer_credentials(client, db_session, monkeypatch):
    # Reproduces the H3 residual: a customer's Task run opens a credentialed
    # skill sandbox; a collaborator then steers the Task, and the steered run
    # names that sandbox to exec `env`. Catches: credential provenance not
    # recorded on the sandbox context (or dropped when a plain skill is loaded
    # into the same sandbox later); any sandbox tool entering it while a
    # collaborator acts; the customer's own continued use refused; a sandbox
    # holding only the collaborator's own skill credentials refused to them.
    from packages.core import database
    from packages.core.ai.runtime.sandbox import runtime_delete_sandbox_binding, runtime_sandbox_scope
    from packages.core.ai.tools import bash_tool, sandbox_tools
    from packages.core.services.collaborator_steering import collaborator_steering_scope, collaborators_acting
    from packages.core.services.sandbox_session_service import (
        SandboxBinding,
        SandboxBundle,
        SandboxOwner,
        SandboxSession,
        _bind_sandbox,
    )

    p, r, customer, target, other = await _trial(client, db_session)
    monkeypatch.setattr(database, "async_session", async_sessionmaker(db_session.bind, expire_on_commit=False))
    task_scope = "task-binding:steered-task"

    def scope_of(user_id):
        return runtime_sandbox_scope(entity_id=customer, user_id=user_id, binding_key=task_scope)

    async def opened(user_id, *, holds, owner, skill_id="skill-reporter"):
        bound = await _bind_sandbox(
            owner=SandboxOwner(entity_id=customer, user_id=user_id),
            session=SandboxSession(sandbox_id="sbx-1", base_url="http://runner", created_at=0.0,
                                   last_acquired_at=0.0, holds_credentials=holds),
            bundle=SandboxBundle(skill_name=skill_id, files={}),
            binding=SandboxBinding(conversation_id=task_scope, skill_id=skill_id, credentials_owner_user_id=owner),
        )
        assert bound is None

    async def entered(user_id, *, steered_by=()):
        with collaborator_steering_scope():
            collaborators_acting(customer, steered_by)
            access = await sandbox_tools._sandbox_instance_access_error(sandbox_id="sbx-1", scope=scope_of(user_id))
            if access is not None:
                # The exec tool itself refuses before reaching the runner.
                refused = await sandbox_tools._sandbox_exec(
                    entity_id=customer, user_id=user_id, sandbox_id="sbx-1", command="env | base64",
                    conversation_id=task_scope,
                )
                assert "stored credentials" in refused, refused
            # bash shares the owner's sandbox and applies the same rule.
            gated, _refusal = await bash_tool._bash_sandbox_credentials_gate(scope_of(user_id), "sbx-1")
            assert (gated is None) == (access is not None)
        return access is None

    await opened(r.id, holds=True, owner=r.id)
    # A plain skill loaded later into the same sandbox does not clear provenance.
    await opened(r.id, holds=False, owner=None, skill_id="skill-plain")
    assert not await entered(r.id, steered_by=[p.id])
    assert await entered(r.id)
    await runtime_delete_sandbox_binding(scope_of(r.id), "sbx-1")

    await opened(p.id, holds=True, owner=p.id)
    assert await entered(p.id)
    await runtime_delete_sandbox_binding(scope_of(p.id), "sbx-1")


@pytest.mark.asyncio
async def test_collaborator_assigns_and_comments_only_within_their_workspace(client, db_session):
    # Catches: Manor's assign_task/create_task/update_task resolving a name
    # across the whole company (people outside the Workspace, their emails,
    # company Agents) for a collaborator; a Workspace Task assigned at runtime
    # to an Agent not subscribed to its Workspace (for anyone, as over HTTP);
    # a comment authored as whoever params.user_id names.
    from packages.core.ai.runtime.manor_actions import (
        runtime_manor_add_task_comment,
        runtime_manor_assign_task,
        runtime_manor_create_task,
    )
    from packages.core.models.staff import Staff
    from packages.core.models.task import TaskLog
    from packages.core.models.workspace import Agent, AgentSubscription

    p, r, customer, target, other = await _trial(client, db_session)
    clerk = Staff(entity_id=customer, name="Payroll Clerk", email="clerk@customer.test", status="active")
    company_agent = Agent(entity_id=customer, workspace_id=other, name="Payroll agent", status="active")
    trial_agent = Agent(entity_id=customer, workspace_id=target, name="Trial agent", status="active")
    task = Task(entity_id=customer, workspace_id=target, title="Trial task")
    db_session.add_all([clerk, company_agent, trial_agent, task])
    await db_session.flush()
    db_session.add(AgentSubscription(entity_id=customer, agent_id=trial_agent.id, workspace_id=target, service_key="trial"))
    await db_session.commit()

    async def assign(user_id, **params):
        async with _gated(db_session, customer, user_id):
            return json.loads(await runtime_manor_assign_task(
                db_session, entity_id=customer, params={"task_id": task.id, **params}, workspace_id=target,
                user_id=user_id,
            ))

    for params in ({"assignee_name": "Payroll"}, {"assignee_email": "clerk@customer.test"},
                   {"assignee_id": clerk.id}, {"assignee_name": "Payroll agent"}, {"agent_id": company_agent.id}):
        refused = await assign(p.id, **params)
        assert refused.get("error") in {"assignee_not_found", "agent_not_found"}, (params, refused)
        assert "assignee_email" in params or "clerk@customer.test" not in json.dumps(refused)
    seated = await assign(p.id, assignee_id=r.id)
    assert seated["assignee_id"] == r.id and "assignee_email" not in seated, seated
    assert (await assign(p.id, assignee_name="Trial agent"))["agent_id"] == trial_agent.id
    # A member keeps company-wide lookup, but a Workspace Task still runs
    # only through an Agent subscribed to its Workspace.
    assert (await assign(r.id, assignee_name="Payroll"))["assignee_email"] == "clerk@customer.test"
    assert (await assign(r.id, agent_id=company_agent.id))["error"] == "agent_not_in_workspace"
    async with _gated(db_session, customer, p.id):
        created = json.loads(await runtime_manor_create_task(
            db_session, entity_id=customer, params={"title": "New", "workspace_id": target, "assignee_name": "Payroll"},
            workspace_id=target, user_id=p.id,
        ))
    assert created.get("error") == "assignee_not_found", created

    await runtime_manor_add_task_comment(
        db_session, entity_id=customer, params={"task_id": task.id, "comment": "Done", "user_id": r.id}, user_id=p.id,
    )
    log = await db_session.scalar(select(TaskLog).where(TaskLog.task_id == task.id, TaskLog.content == "Done"))
    assert log.created_by == p.id


@pytest.mark.asyncio
async def test_collaborator_lists_only_their_workspace_scheduled_jobs(client, db_session, monkeypatch):
    # Catches: list_scheduled_jobs / query_scheduled_jobs (and Manor's
    # list_scheduled_jobs) showing a collaborator's run the company's
    # entity-level Jobs (workspace_id IS NULL) or another Workspace's; a
    # member losing them.
    from packages.core import database
    from packages.core.ai.runtime.scheduling import (
        runtime_list_scheduled_jobs_action,
        runtime_query_scheduled_jobs_action,
    )
    from packages.core.models.scheduler import ScheduledJob
    from packages.core.services.collaborator_steering import collaborator_steering_scope, collaborators_acting

    p, r, customer, target, other = await _trial(client, db_session)
    db_session.add_all([
        ScheduledJob(job_id=f"{name}-job", name=name, entity_id=customer, workspace_id=workspace_id,
                     execution_type="agent", enabled=True, schedule_kind="every", every_seconds=3600)
        for name, workspace_id in (("trial", target), ("elsewhere", other), ("company", None))
    ])
    await db_session.commit()
    monkeypatch.setattr(database, "async_session", async_sessionmaker(db_session.bind, expire_on_commit=False))

    async def listed(user_id, *, steered_by=()):
        async with _gated(db_session, customer, user_id, *steered_by):
            text = await runtime_list_scheduled_jobs_action(entity_id=customer, user_id=user_id, workspace_id=target)
            query = json.loads(await runtime_query_scheduled_jobs_action(
                entity_id=customer, user_id=user_id, runtime_workspace_id=target,
            ))
        names = {name for name in ("trial", "elsewhere", "company") if f"[{name}-job]" in text}
        assert names == {row["job_id"].removesuffix("-job") for row in query["automations"]}, (names, query)
        return names

    assert await listed(p.id) == {"trial"}
    assert await listed(r.id, steered_by=[p.id]) == {"trial"}
    assert {"trial", "company"} <= await listed(r.id)
    # The service itself lists only live-granted Workspaces while a
    # collaborator acts, whichever surface asks (HTTP, Manor, a worker).
    from packages.core.services.scheduler_service import list_scheduled_jobs

    with collaborator_steering_scope():
        collaborators_acting(customer, [p.id])
        jobs, _total = await list_scheduled_jobs(db_session, customer)
    assert {job.job_id for job in jobs} == {"trial-job"}


@pytest.mark.asyncio
async def test_learning_a_collaborator_applies_stays_in_their_workspace(client, db_session):
    # Catches: a collaborator applying a Skill learning candidate creating a
    # company-wide Skill, or binding it to a company Agent subscribed to their
    # Workspace; a member's apply changing.
    from packages.core.models.runtime_learning import AgentLearningCandidate
    from packages.core.models.skill import AgentSkillBinding, Skill
    from packages.core.models.workspace import Agent
    from packages.core.services.runtime_learning import apply_queued_learning_candidate, queue_learning_candidate_apply

    p, r, customer, target, other = await _trial(client, db_session)
    own = Agent(entity_id=customer, workspace_id=target, name="Trial agent", status="active")
    company = Agent(entity_id=customer, name="Company agent", status="active")
    db_session.add_all([own, company])
    await db_session.flush()

    own_id, company_id, p_id, r_id = own.id, company.id, p.id, r.id

    async def applied(agent_id, user_id):
        candidate = AgentLearningCandidate(
            entity_id=customer, workspace_id=target, agent_id=agent_id, candidate_type="skill",
            title=f"Learned {agent_id}", summary="Summarize the weekly report.", status="accepted",
            payload={"seed_prompt": "Summarize the weekly report."},
        )
        db_session.add(candidate)
        await db_session.commit()
        # Queued in the accepting person's request or run, applied by the worker outside it.
        async with _gated(db_session, customer, user_id):
            await queue_learning_candidate_apply(
                db_session, entity_id=customer, candidate_id=candidate.id, user_id=user_id,
            )
        await db_session.commit()
        await apply_queued_learning_candidate(db_session, entity_id=customer, candidate_id=candidate.id, user_id=user_id)
        skill_id = candidate.resolution["applied_result"]["skill_id"]
        bound = await db_session.scalar(select(AgentSkillBinding.agent_id).where(AgentSkillBinding.skill_id == skill_id))
        skill = await db_session.get(Skill, skill_id)
        return skill.workspace_id, skill.owner_user_id, bound

    assert await applied(own_id, p_id) == (target, p_id, own_id)
    with pytest.raises(ValueError, match="this Workspace's own Agents"):
        await applied(company_id, p_id)
    await db_session.rollback()
    workspace_id, _owner, bound = await applied(company_id, r_id)
    assert (workspace_id, bound) == (None, company_id)


# Resource-looking argument keys of admitted tools that the reference check
# does not resolve, and what keeps each inside the run Workspace.
_UNCHECKED_ARGUMENT_KEYS = {
    "agent": "delegate_to_agent names a service; the handler resolves it among the run Workspace's subscriptions",
    "assignee_id": "a person; collaborator_assignee_error admits only people seated in the Task's Workspace",
    "assignee_ids": "a search_tasks filter over the run Workspace's Tasks",
    "category_id": "a company Task category label; no company data",
    "category_ids": "a search_tasks filter over the run Workspace's Tasks",
    "contract_id": "a Ledger contract resolved inside the run Workspace",
    "expected_channel_id": "an external YouTube channel id the public video must match",
    "expected_video_id": "an external YouTube video id",
    "folder_ids": "Knowledge folders; every listed document passes document_access",
    "force_normalize_segment_ids": "segments of the call's own narration timeline",
    "reservation_id": "an entry of the run Workspace's Ledger",
    "reverses_entry_id": "an entry of the run Workspace's Ledger",
    "run_id": "a Workflow run; _workflow_outside requires the run Workspace's",
    "session_id": "the run's own video edit session",
    "shape_id": "a shape inside the file being patched (file path checks apply)",
    "source_record_id": "an entry of the run Workspace's Ledger",
    "supersedes_entry_id": "an entry of the run Workspace's Ledger",
    "template_id": "a response surface template",
    "track_id": "a public free-music catalog track",
    "workflow": "a Workflow id or name; _workflow_outside requires the run Workspace's",
}


def test_every_resource_argument_of_an_admitted_tool_is_checked_or_reviewed():
    # Catches (guard the class): an admitted tool gaining an argument that
    # names a company resource by id, alias or name (the `skill` alias of
    # invoke_skill was one) without the collaborator reference check knowing
    # the key; a reviewed exemption no admitted tool uses any more.
    import re

    from packages.core.ai.runtime.tool_registry import (
        runtime_ensure_tool_registry_initialized,
        runtime_registered_tool_schemas,
    )
    from packages.core.services.collaborator_scope import _RUNTIME_REFERENCE_KEYS

    runtime_ensure_tool_registry_initialized()
    admitted = {name for name, _ in ADMITTED}
    checked = {key for keys in _RUNTIME_REFERENCE_KEYS.values() for key in keys}
    resource_key = re.compile(r"(_ids?|Ids?)$|^(skill|agent|workflow)$")
    found: dict[str, set[str]] = {}

    def visit(schema, tool):
        if isinstance(schema, list):
            for item in schema:
                visit(item, tool)
        elif isinstance(schema, dict):
            for key, value in (schema.get("properties") or {}).items():
                if resource_key.search(key):
                    found.setdefault(key, set()).add(tool)
                visit(value, tool)
            for key in ("items", "anyOf", "oneOf", "allOf", "additionalProperties"):
                visit(schema.get(key), tool)

    for name, schema in runtime_registered_tool_schemas():
        if name in admitted:
            visit((schema.get("function") or schema).get("parameters") or {}, name)
    unreviewed = {key: sorted(tools) for key, tools in found.items() if key not in checked | set(_UNCHECKED_ARGUMENT_KEYS)}
    assert not unreviewed, (
        "Add each key to collaborator_scope._RUNTIME_REFERENCE_KEYS, or to _UNCHECKED_ARGUMENT_KEYS "
        f"with what keeps it inside the Workspace: {unreviewed}"
    )
    assert set(_UNCHECKED_ARGUMENT_KEYS) <= set(found)


@pytest.mark.asyncio
async def test_collaborator_invokes_skills_by_alias_only_inside_their_workspace(client, db_session, monkeypatch):
    # Catches: invoke_skill's legacy `skill` alias (an id or slug) naming
    # another Workspace's Skill past the reference check; a slug of the
    # Workspace's own Skill refused.
    from packages.core import database
    from packages.core.ai.runtime.approval_service import guard_runtime_tool_action
    from packages.core.models.skill import Skill

    p, r, customer, target, other = await _trial(client, db_session)
    elsewhere = Skill(entity_id=customer, workspace_id=other, name="Payroll", slug="payroll-run", system_prompt="Pay.")
    own = Skill(entity_id=customer, workspace_id=target, name="Report", slug="trial-report", system_prompt="Report.")
    db_session.add_all([elsewhere, own])
    await db_session.commit()
    monkeypatch.setattr(database, "async_session", async_sessionmaker(db_session.bind, expire_on_commit=False))

    async def guard(arguments):
        result = await guard_runtime_tool_action(
            name="invoke_skill", arguments=arguments, entity_id=customer, user_id=p.id, workspace_id=target,
            conversation_id=None, operation_id="op-1",
        )
        return json.loads(result)["message"] if result else None

    outside = "The call names a resource outside this Workspace."
    for arguments in ({"skill": elsewhere.id}, {"skill": "payroll-run"}, {"skill_id": "payroll-run"}):
        assert await guard(arguments) == outside, arguments
    for arguments in ({"skill": "trial-report"}, {"skill_id": own.id}):
        assert await guard(arguments) != outside, arguments


@pytest.mark.asyncio
async def test_collaborator_lists_the_flows_of_their_run_workspace(client, db_session, monkeypatch):
    # Catches: list_workspace_flows returning nothing for a collaborator
    # (their persisted company is their own, not the customer's) or listing
    # another customer Workspace's flows; a member's listing changing.
    from packages.core.ai.tools import workflow_tools
    from packages.core.models.workflow import WorkflowBinding, WorkflowDefinition

    p, r, customer, target, other = await _trial(client, db_session)
    for workspace_id, name in ((target, "Trial flow"), (other, "Payroll flow")):
        flow = WorkflowDefinition(entity_id=customer, workspace_id=workspace_id, name=name, steps=[], created_by=r.id,
                                  is_active=True, status="active")
        db_session.add(flow)
        await db_session.flush()
        db_session.add(WorkflowBinding(entity_id=customer, workspace_id=workspace_id, workflow_id=flow.id, name=name,
                                       enabled=True, status="active", config={"chat_entrypoint": {"enabled": True}}))
    await db_session.commit()
    monkeypatch.setattr(workflow_tools, "async_session", async_sessionmaker(db_session.bind, expire_on_commit=False))

    async def flows(user_id):
        async with _gated(db_session, customer, user_id):
            payload = json.loads(await workflow_tools._list_workspace_flows(
                entity_id=customer, user_id=user_id, workspace_id=target, _user_id_from_context=user_id,
            ))
        return {flow["title"] for flow in payload["flows"]}

    assert await flows(p.id) == {"Trial flow"}
    assert await flows(r.id) == {"Trial flow", "Payroll flow"}


@pytest.mark.asyncio
async def test_collaborator_resolves_approval_cards_only_in_the_granted_workspace(client, db_session):
    # Catches: a collaborator refused a card in their Workspace (response or
    # governance); a card in another customer Workspace, or a company-level
    # card, resolvable by the collaborator's seat or membership; a lapsed grant
    # still resolving cards through the derived seat.
    p, r, customer, target, other = await _trial(client, db_session)

    async def resolve(workspace_id, hitl_type):
        return await authorize_hitl_action(
            db_session, entity_id=customer, workspace_id=workspace_id, by_user_id=p.id,
            action_key="workspace.task.create", capability_id="workspace.task", hitl_type=hitl_type,
        )

    for hitl_type in ("authorize", "input"):
        assert (await resolve(target, hitl_type)).allowed, hitl_type
        for workspace_id in (other, None):
            assert not (await resolve(workspace_id, hitl_type)).allowed, (workspace_id, hitl_type)
    await _issuer_leaves(db_session, r.id, customer)
    assert not (await resolve(target, "authorize")).allowed


@pytest.mark.asyncio
async def test_collaborator_operation_drafts_name_only_their_workspace_agents(client, db_session):
    # Catches: a collaborator's operation draft (operating model, goals,
    # rules, runtime operation tools all apply one) subscribing a company Agent
    # by agent_id, recommended_agent_id or agent_slug, or assigning an
    # immediate Task to one; a draft written earlier by a member being applied
    # by the collaborator without the apply-time recheck; the Workspace's own
    # Agent, or a member's draft, being refused.
    from packages.core.models.workspace import Agent, AgentSubscription
    from packages.core.services import workspace_operation_service as operations

    p, r, customer, target, other = await _trial(client, db_session)
    company = Agent(
        entity_id=customer, workspace_id=other, name="Payroll agent", slug="payroll-agent", status="active",
        source="auto_workspace_setup",
    )
    own = Agent(entity_id=customer, workspace_id=target, name="Trial agent", status="active")
    db_session.add_all([company, own])
    await db_session.commit()
    refused = "collaborator_reference_outside_workspace"

    def mapped(**mapping):
        return [
            {"op": "service_role.upsert", "payload": {"key": "payroll", "name": "Payroll"}},
            {"op": "agent_mapping.upsert", "payload": {"mapping": {"service_key": "payroll", **mapping}}},
        ]

    async def drafted(user_id, patches):
        async with _gated(db_session, customer, user_id):
            draft = await operations.create_operation_draft(
                db_session, target, customer, user_id=user_id, initial_patches=patches,
            )
        return draft, {error.get("code") for error in draft.validation["errors"]}

    custom = {"agent_name": "Payroll", "system_prompt": "Run payroll.", "agent_slug": "payroll-agent"}
    for patches in (
        mapped(agent_id=company.id),
        mapped(recommended_agent_id=company.id),
        mapped(strategy="create_custom", create_agent_draft=custom),
        [{"op": "immediate_task.add", "payload": {"title": "Pay", "agent_id": company.id}}],
    ):
        assert refused in (await drafted(p.id, patches))[1], patches
    assert refused not in (await drafted(p.id, mapped(agent_id=own.id)))[1]

    # A name-only custom mapping provisions a new Agent rather than reusing
    # the company Agent of that name.
    by_name, codes = await drafted(p.id, mapped(strategy="create_custom", create_agent_draft={"agent_name": "Payroll agent"}))
    assert not by_name.validation["errors"], by_name.validation
    async with _gated(db_session, customer, p.id):
        await operations.apply_operation_draft(
            db_session, by_name.id, customer, target, user_id=p.id, user_confirmation=True,
        )
    subscribed = await db_session.scalar(select(AgentSubscription.agent_id).where(
        AgentSubscription.workspace_id == target, AgentSubscription.service_key == "payroll",
        AgentSubscription.status == "active",
    ))
    assert subscribed and subscribed != company.id

    member_draft, codes = await drafted(r.id, mapped(agent_id=company.id))
    assert refused not in codes
    with pytest.raises(operations.OperationValidationError) as applied:
        async with _gated(db_session, customer, p.id):
            await operations.apply_operation_draft(
                db_session, member_draft.id, customer, target, user_id=p.id, user_confirmation=True,
            )
    assert refused in {error.get("code") for error in applied.value.validation["errors"]}
