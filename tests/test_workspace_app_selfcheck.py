"""The App self-check: a generated plan, reads on the real routes, and what happens to a page that fails them.

Everything holds on two unrelated Workspaces: a client article studio (approval
wait, documents) and a music campaign (period project, calendar, metrics).
"""
from __future__ import annotations

import copy
import json

import asyncpg
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, OperationalError

from packages.core.blueprints.app_view import remap_workspace_app_view
from packages.core.blueprints.exporter import ExportError
from packages.core.config import get_settings
from packages.core.database_runtime import DatabaseBusyError
from packages.core.models.workflow import WorkflowBinding, WorkflowDefinition, WorkflowProject
from packages.core.models.workspace import Workspace, WorkspaceOperationDraft, WorkspaceStaff
from packages.core.models.workspace_stat import WorkspaceStat
from packages.core.services import workspace_app_designer
from packages.core.services.workspace_app_designer import (
    AppDesignContext,
    app_designer_executor,
    content_calendar_pattern,
    workspace_app_design_context,
)
from packages.core.services.workspace_app_selfcheck import app_test_plan, candidate_read_probe, reads_only
from packages.core.services.workspace_part_designers import PartDesignResult

_ARTICLE_SCHEMA = {"type": "object", "properties": {
    "client": {"type": "string"}, "topic": {"type": "string"}, "deliverable_document_id": {"type": "string"},
    "project_root": {"type": "string"}}}
ARTICLE_FLOW = {"slug": "client_article", "name": "Client article", "steps": [
    {"id": "start", "type": "trigger", "next": ["create"], "config": {"run_inputs": [
        {"key": "client", "type": "string", "label": "Client", "required": True}]}},
    {"id": "create", "type": "workflow_project", "name": "Open the article", "next": ["approve"],
     "config": {"operation": "create", "project_type": "client_article", "project_key": "{{client}}", "schema_version": 1,
                "state_schema": _ARTICLE_SCHEMA, "folder": {"name": "Article"}, "current_stage": "drafting",
                "allowed_stages": ["drafting", "awaiting_approval", "delivered"]}},
    {"id": "approve", "type": "wait", "name": "Owner approval", "next": [],
     "config": {"wait_type": "approval", "message": "Approve the article?", "options": ["approve", "revise"],
                "approval_values": ["approve"], "response_variable": "decision"}},
]}
ARTICLE_PAGE = {
    "title": "Client desk",
    "entries": {"action:new-article": {"title": "New article"}},
    "targets": {"action:new-article": "workflow:client_article"},
    "hero": "action:new-article",
    "hidden": ["workflow:client_article"],
    "sections": [{"id": "decide", "block": "queue"}, {"id": "actions", "block": "actions"}],
    "data_sources": {
        "articles": {"type": "workflow_projects", "project_type": "client_article",
                     "producer_workflows": ["workflow_definition:client_article"],
                     "fields": [["client"], ["topic"]], "label_fields": [["client"]]},
        "waiting": {"type": "workflow_projects", "project_type": "client_article",
                    "producer_workflows": ["workflow_definition:client_article"], "stages": ["awaiting_approval"],
                    "fields": [["client"]]},
        "deliverable": {"type": "project_documents", "parent": "articles", "document_role": "article",
                        "document_path": ["deliverable_document_id"]},
        "files": {"type": "project_files", "parent": "articles"},
    },
    "components": [
        {"key": "desk", "type": "data_view", "context": {"kind": "workspace"},
         "blocks": [{"key": "waiting", "type": "progress", "source": "waiting", "title": "Waiting for you"}]},
        {"key": "article", "type": "data_view",
         "context": {"kind": "project", "source": "articles", "label": "Article", "display": "select"},
         "blocks": [{"key": "brief", "type": "summary", "source": "articles",
                     "fields": [{"field": ["topic"], "label": "Topic"}]},
                    {"key": "document", "type": "documents", "source": "deliverable"},
                    {"key": "folder", "type": "documents", "source": "files"}]},
    ],
}

_CAMPAIGN_SCHEMA = {"type": "object", "properties": {"title": {"type": "string"}, "plan_document_id": {"type": "string"}}}
_SLOT_SCHEMA = {"type": "object", "properties": {
    "title": {"type": "string"}, "campaign_id": {"type": "string"}, "post_on": {"type": "string"},
    "platform": {"type": "string"}}}
CAMPAIGN_FLOW = {"slug": "release_campaign", "name": "Release campaign", "steps": [
    {"id": "start", "type": "trigger", "next": ["week"], "config": {"run_inputs": [
        {"key": "track", "type": "string", "label": "Track", "required": True}]}},
    {"id": "week", "type": "workflow_project", "name": "Open the week", "next": ["slot"],
     "config": {"operation": "create", "project_type": "campaign_week", "project_key": "{{track}}", "schema_version": 1,
                "state_schema": _CAMPAIGN_SCHEMA, "current_stage": "active"}},
    {"id": "slot", "type": "workflow_project", "name": "Plan a post", "next": [],
     "config": {"operation": "create", "project_type": "post_slot", "project_key": "{{track}}", "schema_version": 1,
                "state_schema": _SLOT_SCHEMA, "state": {"campaign_id": "{{week.project_id}}"}, "current_stage": "idea",
                "allowed_stages": ["idea", "published"]}},
]}


def _campaign_page(context: AppDesignContext) -> dict:
    made = content_calendar_pattern(context, {
        "workflow": "release_campaign", "period_project_type": "campaign_week", "item_project_type": "post_slot",
        "parent_field": "campaign_id", "date_field": "post_on", "title": "Release weeks", "period_label": "Week",
        "item_fields": [{"field": "platform", "label": "Platform"}],
        "period_fields": [{"field": "title", "label": "Title"}],
        "plan_document_field": "plan_document_id", "plan_document_label": "Plan",
        "stages": {"idea": "Needs a script", "published": "Published"}, "done_stage": "published",
    })
    assert made["ok"], made
    return {"title": "Release weeks", "data_sources": made["fragment"]["data_sources"],
            "components": made["fragment"]["components"],
            "sections": [{"id": "numbers", "block": "metrics", "items": ["stat:streams"]},
                         {"id": "actions", "block": "actions"}]}


def _no_front_page_wait(page: dict) -> None:
    """A static break: the article waits on the owner, and only the per-article view shows it."""
    page["components"].pop(0)


def _undeclared_calendar_field(page: dict) -> None:
    """A static break: the calendar reads a date the item's schema does not declare."""
    slots = next(source for source in page["data_sources"].values() if source.get("project_type") == "post_slot")
    slots["fields"].append(["posted_at"])


def _block_outside_its_context(page: dict) -> None:
    """A read-only break: a view over one source shows a block over a source of another project type."""
    view = next(component for component in page["components"] if component["context"]["kind"] == "project")
    page["data_sources"]["elsewhere"] = {
        "type": "workflow_projects", "project_type": "post_slot",
        "producer_workflows": ["workflow_definition:release_campaign"], "fields": [["title"]]}
    view["blocks"].append({"key": "elsewhere", "type": "progress", "source": "elsewhere"})
    view["display"]["groups"][-1]["blocks"].append("elsewhere")


async def _workspace(client, *, name: str, flow: dict, username: str):
    """A real owner and Workspace running ``flow``, with their real rows."""
    import packages.core.database as db_module

    owner = (await client.post("/api/v1/auth/register", json={
        "username": username, "email": f"{username}@test.com", "password": "pass123", "entity_name": name,
    })).json()
    headers = {"Authorization": f"Bearer {owner['access_token']}"}
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    workspace = (await client.post("/api/v1/workspaces", headers=headers, json={"name": name})).json()
    async with db_module.async_session() as session:
        definition = WorkflowDefinition(entity_id=me["entity_id"], workspace_id=workspace["id"], name=flow["name"],
                                        steps=copy.deepcopy(flow["steps"]), status="active")
        session.add(definition)
        await session.flush()
        binding = WorkflowBinding(entity_id=me["entity_id"], workspace_id=workspace["id"], workflow_id=definition.id,
                                  name=flow["name"], status="active", enabled=True,
                                  config={"workspace_blueprint_workflow_slug": flow["slug"]})
        session.add(binding)
        await session.commit()
        binding_id = binding.id
    return headers, me, workspace["id"], binding_id


async def _article_studio(client, username: str = "selfcheck_articles"):
    import packages.core.database as db_module

    headers, me, workspace_id, binding_id = await _workspace(client, name="Client desk", flow=ARTICLE_FLOW, username=username)
    async with db_module.async_session() as session:
        session.add_all([
            WorkflowProject(entity_id=me["entity_id"], workspace_id=workspace_id, project_type="client_article",
                            schema_version=1, project_key="acme", current_stage="awaiting_approval", created_by=me["id"],
                            state={"client": "Acme", "topic": "Pricing"}),
        ])
        await session.commit()
    return headers, me, workspace_id, binding_id


async def _music_campaign(client, username: str = "selfcheck_music"):
    import packages.core.database as db_module

    headers, me, workspace_id, binding_id = await _workspace(client, name="Release", flow=CAMPAIGN_FLOW, username=username)
    async with db_module.async_session() as session:
        week = WorkflowProject(entity_id=me["entity_id"], workspace_id=workspace_id, project_type="campaign_week",
                               schema_version=1, project_key="w1", current_stage="active", created_by=me["id"],
                               state={"title": "Week one"})
        session.add(week)
        await session.flush()
        session.add_all([
            WorkflowProject(entity_id=me["entity_id"], workspace_id=workspace_id, project_type="post_slot",
                            schema_version=1, project_key="p1", current_stage="idea", created_by=me["id"],
                            state={"title": "Teaser", "campaign_id": week.id, "post_on": "2026-11-05", "platform": "TikTok"}),
            WorkspaceStat(entity_id=me["entity_id"], workspace_id=workspace_id, key="streams", name="Streams",
                          collector_type="manual", status="active"),
        ])
        await session.commit()
    return headers, me, workspace_id, binding_id


async def _context(workspace_id: str, me: dict) -> AppDesignContext:
    import packages.core.database as db_module

    async with db_module.async_session() as session:
        context = await workspace_app_design_context(session, await session.get(Workspace, workspace_id))
    context.read_probe = candidate_read_probe(workspace_id=workspace_id, entity_id=me["entity_id"], user_id=me["id"])
    return context


async def _submit(context: AppDesignContext, page: dict) -> tuple[dict, dict]:
    state: dict = {}
    reply = await app_designer_executor(context, state)("app_submit_candidate", {"app_view": page, "recipes": []})
    return json.loads(reply), state


def test_the_plan_lists_everything_a_page_offers_from_the_config_and_what_the_workspace_runs():
    context = AppDesignContext.from_draft_fields({"workflows": [ARTICLE_FLOW]})
    config, errors, _ = context.check(copy.deepcopy(ARTICLE_PAGE))
    assert errors == []
    rows = {row["id"]: row for row in app_test_plan(config, context)}
    assert {"page", "entry:action:new-article", "section:decide", "section:actions", "view:desk", "context:desk",
            "block:desk.waiting", "view:article", "context:article", "block:article.brief", "block:article.document",
            "block:article.folder", "source:articles", "source:waiting", "source:deliverable", "source:files"} == set(rows)
    # What each must satisfy comes from the Flow: nobody wrote these down for this page.
    assert any("client_article waiting on a person" in rule for rule in rows["entry:action:new-article"]["must"])
    assert any("client_article waiting on a person" in rule for rule in rows["block:desk.waiting"]["must"])
    assert any("each reads by its own client" in rule for rule in rows["context:article"]["must"])
    assert any("block:article.document" in rule for rule in rows["source:deliverable"]["must"])

    music = AppDesignContext.from_draft_fields({"workflows": [CAMPAIGN_FLOW], "stats": [{"key": "streams"}]})
    page = _campaign_page(music)
    config, errors, _ = music.check(page)
    assert errors == []
    rows = {row["id"]: row for row in app_test_plan(config, music)}
    blocks = [f"block:{component['key']}.{block['key']}" for component in page["components"] for block in component["blocks"]]
    assert blocks and set(blocks) <= set(rows)
    assert {"section:numbers", "entry:workflow:release_campaign"} <= set(rows)


@pytest.mark.parametrize("build, make_page, breaks", [
    pytest.param(_article_studio, lambda _context: copy.deepcopy(ARTICLE_PAGE), _no_front_page_wait, id="article-studio"),
    pytest.param(_music_campaign, _campaign_page, _undeclared_calendar_field, id="music-campaign"),
])
async def test_a_page_that_fails_its_static_checks_is_sent_back(client, build, make_page, breaks):
    _, me, workspace_id, _ = await build(client)
    context = await _context(workspace_id, me)
    page = make_page(context)
    reply, state = await _submit(context, copy.deepcopy(page))
    assert reply["ok"], reply
    assert state["self_check"]["status"] == "passed", state["self_check"]["read_failures"]

    breaks(page)
    reply, state = await _submit(context, page)
    assert reply["ok"] is False and all(isinstance(message, str) for message in reply["errors"]), reply
    assert "candidate" not in state


async def test_a_page_whose_entry_opens_nothing_warns_then_is_sent_back_once_blocking(client, monkeypatch):
    """The article studio's entry names a Flow its binding no longer runs: every static check holds."""
    import packages.core.database as db_module

    _, me, workspace_id, binding_id = await _article_studio(client)
    async with db_module.async_session() as session:
        (await session.get(WorkflowBinding, binding_id)).enabled = False
        await session.commit()
    context = await _context(workspace_id, me)

    reply, state = await _submit(context, copy.deepcopy(ARTICLE_PAGE))
    failure = "action:new-article is on the page but opens nothing for this person"
    assert reply["ok"] is True and failure in reply["warnings"][0], reply
    assert "reads only" in reply["self_check"].lower() and state["candidate"], reply

    monkeypatch.setattr(get_settings(), "WORKSPACE_APP_SELF_CHECK_BLOCKS", True)
    reply, state = await _submit(context, copy.deepcopy(ARTICLE_PAGE))
    assert reply["ok"] is False and failure in reply["errors"][0], reply
    assert "candidate" not in state


async def test_a_page_with_a_block_outside_its_context_warns_then_is_sent_back_once_blocking(client, monkeypatch):
    """The music campaign's view passes the data contract, and the read route refuses it."""
    _, me, workspace_id, _ = await _music_campaign(client)
    context = await _context(workspace_id, me)
    page = _campaign_page(context)
    _block_outside_its_context(page)

    reply, _ = await _submit(context, copy.deepcopy(page))
    assert reply["ok"] is True, reply
    failure = next(message for message in reply["warnings"] if "elsewhere could not be read (invalid_context)" in message)
    assert failure.endswith("or move it to a data view over that source"), failure

    monkeypatch.setattr(get_settings(), "WORKSPACE_APP_SELF_CHECK_BLOCKS", True)
    reply, _ = await _submit(context, page)
    assert reply["ok"] is False and any("(invalid_context)" in message for message in reply["errors"]), reply


async def test_a_view_with_nothing_to_read_is_not_a_failure(client):
    """A new Workspace has no projects yet: its page says so instead of claiming the reads passed."""
    _, me, workspace_id, _ = await _workspace(client, name="Fresh desk", flow=ARTICLE_FLOW, username="selfcheck_fresh")
    context = await _context(workspace_id, me)
    reply, state = await _submit(context, copy.deepcopy(ARTICLE_PAGE))
    assert reply["ok"] is True, reply
    report = state["self_check"]
    assert report["status"] == "passed" and report["counts"]["no_data"] >= 4
    assert {row["id"]: row["status"] for row in report["items"]}["block:article.document"] == "no_data"
    assert "had no data to read yet" in report["summary"]


async def _add_project(workspace_id: str, me: dict, project_type: str, key: str, stage: str, state: dict) -> None:
    import packages.core.database as db_module

    async with db_module.async_session() as session:
        if project_type == "post_slot":
            week = (await session.execute(text(
                "SELECT id FROM workflow_projects WHERE workspace_id = :id AND project_type = 'campaign_week'"),
                {"id": workspace_id})).scalar_one()
            state = {**state, "campaign_id": week}
        session.add(WorkflowProject(entity_id=me["entity_id"], workspace_id=workspace_id, project_type=project_type,
                                    schema_version=1, project_key=key, current_stage=stage, created_by=me["id"],
                                    state=state))
        await session.commit()


@pytest.mark.parametrize("build, make_page, project, healthy, unfit, view, shows", [
    pytest.param(_article_studio, lambda _context: copy.deepcopy(ARTICLE_PAGE), ("client_article", "awaiting_approval"),
                 {"client": "Northwind", "topic": "Follow-up on the pricing-page review, 2026-11-08 (q4_plan)"},
                 {"client": "Initech", "topic": "{{start.topic}}"},
                 "block:article.brief", "an unfilled template in its Topic field", id="article-studio"),
    pytest.param(_article_studio, lambda _context: copy.deepcopy(ARTICLE_PAGE), ("client_article", "awaiting_approval"),
                 {"client": "Globex-East", "topic": "Launch"}, {"client": "01M6X5QWZGAXVRCH08SV7JG1KF", "topic": "Launch"},
                 "context:article", "an id in its name", id="article-studio-label"),
    pytest.param(_music_campaign, _campaign_page, ("post_slot", "idea"),
                 {"title": "Behind the scenes", "post_on": "2026-11-06", "platform": "Instagram Reels"},
                 {"title": "Lyric clip", "post_on": "2026-11-07", "platform": "instagram_reels"},
                 None, "an internal key in its Platform field", id="music-campaign"),
])
async def test_a_page_that_would_show_people_internal_values_fails_on_the_view_that_shows_them(
        client, build, make_page, project, healthy, unfit, view, shows):
    """Records differ where a view does not: a later record's values are read too, and only what the page shows counts."""
    _, me, workspace_id, _ = await build(client)
    project_type, stage = project
    # Values that only look technical: a hyphenated name, a date, a key inside a sentence.
    await _add_project(workspace_id, me, project_type, "healthy", stage, healthy)
    context = await _context(workspace_id, me)
    page = make_page(context)
    _, state = await _submit(context, copy.deepcopy(page))
    assert state["self_check"]["status"] == "passed", state["self_check"]["read_failures"]

    await _add_project(workspace_id, me, project_type, "unfit", stage, unfit)
    _, state = await _submit(context, copy.deepcopy(page))
    report = state["self_check"]
    failed = [row for row in report["items"] if row["status"] == "failed" and row["kind"] in {"block", "context"}]
    assert report["status"] == "failed" and failed, report["summary"]
    assert all(shows in row["message"] and row["message"] in report["read_failures"] for row in failed), failed
    assert view is None or view in {row["id"] for row in failed}, failed
    # The message names the view, as the page shows it, and says what to change.
    assert all(row["message"].startswith(row["key"]) and row["message"].count(": ") >= 1 for row in failed), failed


@pytest.mark.parametrize("failure", [
    pytest.param(OperationalError("SELECT 1", {}, Exception("connection reset")), id="connection-reset"),
    pytest.param(DatabaseBusyError("database admission timeout"), id="admission-timeout"),
    pytest.param(DBAPIError("SELECT 1", {}, asyncpg.QueryCanceledError("statement timeout")), id="statement-timeout"),
    pytest.param(DBAPIError("SELECT 1", {}, asyncpg.LockNotAvailableError("lock timeout")), id="lock-timeout"),
])
async def test_a_fault_of_the_platform_is_never_sent_back_to_the_designer(client, monkeypatch, failure):
    _, me, workspace_id, _ = await _article_studio(client)
    context = await _context(workspace_id, me)

    async def down(*_args, **_kwargs):
        raise failure

    monkeypatch.setattr("packages.core.services.workspace_app_data.component_data", down)
    monkeypatch.setattr(get_settings(), "WORKSPACE_APP_SELF_CHECK_BLOCKS", True)
    reply, state = await _submit(context, copy.deepcopy(ARTICLE_PAGE))
    assert reply["ok"] is True, reply
    report = state["self_check"]
    assert report["read_failures"] == [] and report["counts"]["not_run"] > 0

    # A candidate on a Draft has nothing live to read: only what ran is claimed.
    draft = AppDesignContext.from_draft_fields({"workflows": [ARTICLE_FLOW]})
    _, state = await _submit(draft, copy.deepcopy(ARTICLE_PAGE))
    assert "reads have not run" in state["self_check"]["summary"]


async def test_reads_only_refuses_writes_and_leaves_the_session_writable(client):
    import packages.core.database as db_module

    _, me, workspace_id, _ = await _article_studio(client)
    async with db_module.async_session() as session:
        workspace = await session.get(Workspace, workspace_id)
        with pytest.raises(DBAPIError, match="read-only transaction"):
            async with reads_only(session):
                await session.execute(text("UPDATE workspaces SET name = 'changed' WHERE id = :id"), {"id": workspace_id})
        await session.rollback()
        async with reads_only(session):
            assert (await session.execute(text("SELECT 1"))).scalar_one() == 1
        # The caller's own transaction goes on: a draft is still written after its check.
        workspace.name = "Renamed"
        await session.commit()
    async with db_module.async_session() as session:
        assert (await session.get(Workspace, workspace_id)).name == "Renamed"


async def test_the_test_route_checks_a_draft_as_a_manager_and_changes_nothing(client, monkeypatch):
    import packages.core.database as db_module

    headers, me, workspace_id, _ = await _article_studio(client)
    base = f"/api/v1/workspaces/{workspace_id}"
    saved = (await client.get(f"{base}/app-view/test", headers=headers)).json()
    # No page of its own: the default page's reads are checked, and nobody designed it to hold L0.
    assert saved["status"] == "passed" and saved["errors"] == [], saved
    assert {"page", "entry:workflow:client_article"} <= {row["id"] for row in saved["items"]}

    async with db_module.async_session() as session:
        runtime_page = await remap_workspace_app_view(
            session, await session.get(Workspace, workspace_id), copy.deepcopy(ARTICLE_PAGE), to_runtime=True)
    draft = (await client.post(f"{base}/operation/drafts", headers=headers, json={
        "patches": [{"op": "app_view.update", "payload": {
            "app_view": workspace_app_designer.app_view_replacement(None, runtime_page)}}]})).json()
    assert draft.get("validation", {}).get("errors") == [], draft
    async with db_module.async_session() as session:
        before =(await session.get(Workspace, workspace_id)).updated_at
        draft_before = (await session.get(WorkspaceOperationDraft, draft["id"])).updated_at

    report = (await client.get(f"{base}/app-view/test", headers=headers, params={"draft_id": draft["id"]})).json()
    assert report["status"] == "passed", report["read_failures"] or report["errors"]
    statuses = {row["id"]: row["status"] for row in report["items"]}
    assert statuses["block:desk.waiting"] == statuses["entry:action:new-article"] == statuses["section:decide"] == "passed"
    assert "not that clicking works" in report["limits"]
    async with db_module.async_session() as session:
        assert (await session.get(Workspace, workspace_id)).updated_at == before
        assert (await session.get(WorkspaceOperationDraft, draft["id"])).updated_at == draft_before

    gone = await client.get(f"{base}/app-view/test", headers=headers, params={"draft_id": "0" * 26})
    assert gone.status_code == 404

    # A saved page's own fault stays that page's; a small edit of it is not held to account for it.
    async with db_module.async_session() as session:
        workspace = await session.get(Workspace, workspace_id)
        waiting_gone = copy.deepcopy(runtime_page)
        _no_front_page_wait(waiting_gone)
        # An uploaded cover cannot leave this deployment, which is no fault of the page.
        waiting_gone["cover_image_url"] = "/api/v1/documents/doc-1/thumbnail"
        workspace.operating_model = {**(workspace.operating_model or {}), "app_view": waiting_gone}
        await session.commit()
    saved = (await client.get(f"{base}/app-view/test", headers=headers)).json()
    assert saved["status"] == "failed" and len(saved["errors"]) == 1, saved
    assert "front page shows no client_article waiting" in saved["errors"][0], saved
    assert saved["pre_existing_issues"] == []
    edit = (await client.post(f"{base}/operation/drafts", headers=headers, json={
        "patches": [{"op": "app_view.update", "payload": {"app_view": {"title": "Renamed desk"}}}]})).json()
    edited = (await client.get(f"{base}/app-view/test", headers=headers, params={"draft_id": edit["id"]})).json()
    assert edited["status"] == "passed" and edited["pre_existing_issues"] == saved["errors"], edited

    # A Workflow that cannot be exported leaves the reads running, and the report says what did not.
    def unexportable(*_args, **_kwargs):
        raise ExportError("Workflow reads a Knowledge document not included in this Blueprint.")

    monkeypatch.setattr("packages.core.blueprints.exporter._export_workflows", unexportable)
    unexported = await client.get(f"{base}/app-view/test", headers=headers)
    assert unexported.status_code == 200 and "static checks could not run" in unexported.json()["summary"]
    assert unexported.json()["counts"]["passed"] > 0
    from packages.core.models.user import User
    from packages.core.services.auth_service import create_access_token, hash_password

    async with db_module.async_session() as session:
        viewer = User(entity_id=me["entity_id"], email="selfcheck_viewer@test.com", password_hash=hash_password("pass123"),
                      role="member", status="active")
        session.add(viewer)
        await session.flush()
        session.add(WorkspaceStaff(workspace_id=workspace_id, user_id=viewer.id, role="viewer", status="active"))
        await session.commit()
        viewer_headers = {"Authorization": f"Bearer {create_access_token(viewer.id, me['entity_id'], 'member')}"}
    assert (await client.get(f"{base}/app-view/test", headers=viewer_headers)).status_code == 403
    # A designer's delayed callback repeats manager admission before reading.
    viewer_probe = candidate_read_probe(workspace_id=workspace_id, entity_id=me["entity_id"], user_id=viewer.id)
    assert await viewer_probe(copy.deepcopy(ARTICLE_PAGE), []) is None


async def test_the_result_reaches_the_review_card_and_the_design_note(client, monkeypatch):
    """A designed page that warns: the card's notes and why carry it, until the page changes."""
    import packages.core.database as db_module
    from packages.core.ai.runtime.workspace_operation_actions import runtime_workspace_operation_action
    from packages.core.services.workspace_operation_service import (
        operation_draft_review_fingerprint,
        refresh_operation_draft_review,
    )

    _, me, workspace_id, _ = await _music_campaign(client)

    async def scripted_part_designer(_designer, context, **_kwargs):
        state: dict = {}
        page = _campaign_page(context)
        _block_outside_its_context(page)
        await app_designer_executor(context, state)("app_submit_candidate", {"app_view": page, "recipes": []})
        return PartDesignResult(candidate=state["candidate"], warnings=state["warnings"], notes="For the owner.",
                                attempts=1, state=state)

    monkeypatch.setattr(workspace_app_designer, "run_part_designer", scripted_part_designer)
    card = json.loads(await runtime_workspace_operation_action(
        entity_id=me["entity_id"], workspace_id=workspace_id, user_id=me["id"], conversation_id="conv-1",
        params={"action": "design_app", "brief": "Put this week's posts first"}))
    assert card["__hitl__"] is True, card
    operation = card["hitl"]["operation"]
    note = next(issue for issue in operation["validation"]["warnings"] if issue["code"] == "app_self_check")
    assert "elsewhere could not be read (invalid_context)" in note["message"]
    assert "Self-check:" in card["hitl"]["payload"]["why"] and "nothing was clicked" in card["hitl"]["payload"]["why"]
    assert card["design_notes"].startswith("For the owner.") and "Self-check:" in card["design_notes"]

    async with db_module.async_session() as session:
        refreshed = await refresh_operation_draft_review(session, card["draft_id"], me["entity_id"], workspace_id)
        # The report is a finding about the page, not part of what the person reviews.
        assert operation_draft_review_fingerprint(refreshed) == operation["review_fingerprint"]
        assert refreshed.validation["app_self_check"]["read_failures"]
        assert sum(issue.get("code") == "app_self_check" for issue in refreshed.validation["warnings"]) == 1
        await session.commit()

    patched = json.loads(await runtime_workspace_operation_action(
        entity_id=me["entity_id"], workspace_id=workspace_id, user_id=me["id"], conversation_id="conv-1",
        params={"action": "patch_draft", "draft_id": card["draft_id"], "patches": [
            {"op": "app_view.update", "payload": {"app_view": {"title": "The desk"}}}]}))
    summary = patched["hitl"]["operation"]["validation"]["app_self_check"]
    # Its page changed, so the check read it again.
    assert summary["page_digest"] != refreshed.validation["app_self_check"]["page_digest"]

    tested = json.loads(await runtime_workspace_operation_action(
        entity_id=me["entity_id"], workspace_id=workspace_id, user_id=me["id"], conversation_id="conv-1",
        params={"action": "test_app", "draft_id": card["draft_id"]}))
    assert tested["status"] == "failed" and tested["items"], tested
    outsider = json.loads(await runtime_workspace_operation_action(
        entity_id=me["entity_id"], workspace_id=workspace_id, user_id="someone-else", conversation_id="conv-1",
        params={"action": "test_app"}))
    assert outsider["error"] == "blocked_by_permission"


async def test_the_audit_reports_every_page_and_changes_nothing(client):
    import packages.core.database as db_module
    from scripts.audit_workspace_app_pages import audit_pages

    _, _, articles_id, binding_id = await _article_studio(client, username="selfcheck_audit_a")
    _, music_me, music_id, _ = await _music_campaign(client, username="selfcheck_audit_m")
    async with db_module.async_session() as session:
        workspace = await session.get(Workspace, articles_id)
        # A page that still names a target this Workspace has since lost.
        workspace.operating_model = {**(workspace.operating_model or {}), "app_view": {
            "entries": {"action:gone": {"title": "Gone"}}, "targets": {"action:gone": "workflow:removed-binding"}}}
        await session.commit()
    async with db_module.async_session() as session:
        before = (await session.get(Workspace, articles_id)).updated_at
    report = await audit_pages()
    rows = {row["workspace_id"]: row for row in report["results"]}
    assert rows[music_id]["page"] == "default" and rows[music_id]["status"] == "passed"
    assert rows[articles_id]["page"] == "designed" and rows[articles_id]["status"] == "failed"
    assert any("removed-binding" in message for message in rows[articles_id]["errors"]), rows[articles_id]
    assert report["failed_static"] >= 1 and "nothing was clicked" in report["limits"]
    assert [row["workspace_id"] for row in (await audit_pages(designed_only=True, entity_id=rows[articles_id]["entity_id"]))["results"]] == [articles_id]
    assert (await audit_pages(entity_id=music_me["entity_id"]))["pages"] == 1
    async with db_module.async_session() as session:
        assert (await session.get(Workspace, articles_id)).updated_at == before


def test_a_chart_part_named_by_a_stored_key_fails_and_says_how_to_name_it():
    from packages.core.services.workspace_app_selfcheck import _value_finding

    # A funnel by stage: "Won" has a label, "closed_won" shows as it is stored.
    items = [{"id": "f:lead", "title": "Lead", "labelled": True, "value": 3},
             {"id": "f:closed_won", "title": "closed_won", "labelled": False, "value": 1}]
    finding = _value_finding("pipeline.funnel", "chart", items)
    assert finding["status"] == "failed" and "an internal key in its category" in finding["message"]
    assert finding["message"].endswith("name its values with chart.labels, or its stages with the block's stage_labels")
    # A plain word, a date bucket or a labelled stage reads fine.
    assert _value_finding("pipeline.region", "chart", [{"title": "West", "labelled": False},
                                                      {"title": "2026-11", "labelled": True}]) is None
