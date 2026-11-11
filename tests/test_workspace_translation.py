"""A Workspace written in one language reads in each viewer's, without changing what runs."""
from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from auth_helpers import register_user_and_get_token
from packages.core.models.workflow import WorkflowBinding, WorkflowDefinition
from packages.core.models.workspace import Workspace
from packages.core.services import workspace_translation as wt


@pytest.fixture
def fake_translator(monkeypatch):
    calls: list[tuple[str, list[str]]] = []

    async def translate(texts, language, *, entity_id, workspace_id):
        calls.append((language, list(texts)))
        return {text: f"{language.upper()}:{text}" for text in texts}

    async def credit(entity_id, **kwargs):
        return None

    monkeypatch.setattr(wt, "_translate", translate)
    monkeypatch.setattr("packages.core.ai.runtime.runtime_assert_credit_available", credit)
    monkeypatch.setattr(wt, "schedule_workspace_translation", lambda workspace_id: None)
    return calls


async def _chinese_workspace(client: AsyncClient, db_session):
    token = (await register_user_and_get_token(client, json={
        "username": "wstranslate", "email": "wstranslate@test.com",
        "password": "WorkspaceTranslate123!", "entity_name": "Translate Corp",
    })).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    workspace_id = (await client.post("/api/v1/workspaces", headers=headers, json={"name": "短剧工作室"})).json()["id"]
    steps = [
        {"id": "trigger", "type": "trigger", "name": "开始", "next": ["write"], "config": {"run_inputs": [{
            "key": "topic", "type": "string", "label": "主题", "default": "校园", "required": True,
        }]}},
        {"id": "write", "type": "transform", "name": "写剧本", "config": {"set": {"draft": "done"}}, "next": ["end"]},
        {"id": "end", "type": "end", "name": "Done", "config": {}, "next": []},
    ]
    workflow = (await client.post("/api/v1/workflows", headers=headers, json={"name": "单集生成", "steps": steps})).json()
    binding = (await client.post("/api/v1/workflows/bindings", headers=headers, json={
        "workflow_id": workflow["id"], "workspace_id": workspace_id, "trigger_type": "manual", "name": "单集生成",
        "config": {"chat_entrypoint": {"enabled": True, "title": "单集生成", "description": "写一集短剧"}},
    })).json()
    workspace = await db_session.get(Workspace, workspace_id)
    workspace.operating_model = {**(workspace.operating_model or {}), "app_view": {
        "title": "写剧本", "labels": {f"workflow:{binding['id']}": "单集生成"},
    }}
    await db_session.commit()
    return headers, workspace_id, workflow["id"], binding["id"]


@pytest.mark.asyncio
async def test_an_edited_workspace_reads_in_each_viewers_language(client: AsyncClient, db_session, fake_translator):
    headers, workspace_id, workflow_id, binding_id = await _chinese_workspace(client, db_session)
    revision = (await db_session.get(WorkflowDefinition, workflow_id)).revision

    assert (await wt.translate_workspace_text(workspace_id))["status"] == "translated"
    # Each text goes to the other language only; values a run receives are never sent.
    sent = dict(fake_translator)
    assert set(sent["en"]) == {"写剧本", "单集生成", "写一集短剧", "开始", "主题"}
    assert sent["zh"] == ["Done"]

    english = (await client.get(f"/api/v1/workspaces/{workspace_id}/app-view",
                                headers={**headers, "X-Language": "en"})).json()
    assert english["title"] == "EN:写剧本"
    action = next(item for item in english["actions"] if item.get("binding_id") == binding_id)
    assert action["title"] == "EN:单集生成"
    assert action["inputs"][0]["label"] == "EN:主题"
    # A run receives what was authored.
    assert action["inputs"][0]["default"] == "校园"
    chinese = (await client.get(f"/api/v1/workspaces/{workspace_id}/app-view",
                                headers={**headers, "X-Language": "zh"})).json()
    assert chinese["title"] == "写剧本"

    db_session.expire_all()
    workflow = await db_session.get(WorkflowDefinition, workflow_id)
    # A translation is not an edit of the Flow.
    assert workflow.revision == revision
    assert workflow.steps[1]["i18n"]["en"]["name"] == "EN:写剧本"

    calls = len(fake_translator)
    assert (await wt.translate_workspace_text(workspace_id))["status"] == "current"
    assert len(fake_translator) == calls


@pytest.mark.asyncio
async def test_changed_text_is_translated_again_but_a_persons_translation_is_kept(
    client: AsyncClient, db_session, fake_translator,
):
    headers, workspace_id, workflow_id, binding_id = await _chinese_workspace(client, db_session)
    await wt.translate_workspace_text(workspace_id)

    db_session.expire_all()
    binding = await db_session.get(WorkflowBinding, binding_id)
    binding.config = {**binding.config, "i18n": {"en": {"name": "One episode", "chat_entrypoint": {"title": "One episode"}}}}
    workflow = await db_session.get(WorkflowDefinition, workflow_id)
    steps = [dict(step) for step in workflow.steps]
    steps[1]["name"] = "改剧本"
    workflow.steps = steps
    await db_session.commit()

    await wt.translate_workspace_text(workspace_id)
    db_session.expire_all()
    workflow = (await db_session.execute(select(WorkflowDefinition).where(WorkflowDefinition.id == workflow_id))).scalar_one()
    assert workflow.steps[1]["i18n"]["en"]["name"] == "EN:改剧本"
    binding = await db_session.get(WorkflowBinding, binding_id)
    assert binding.config["i18n"]["en"] == {"name": "One episode", "chat_entrypoint": {"title": "One episode"}}


def test_flow_descriptions_and_data_view_headings_are_translated_too():
    from types import SimpleNamespace

    from packages.core.services.viewer_language import localized
    from packages.core.services.workspace_workflow_router import normalize_chat_entrypoint

    workspace = SimpleNamespace(id="workspace", operating_model={"app_view": {"components": [{
        "key": "series", "type": "data_view", "title": "系列", "context": {"kind": "project", "source": "series", "label": "系列"},
        "blocks": [{"key": "progress", "type": "project_rollup", "source": "episodes", "title": "进度",
                    "stage_labels": {"made": "已完成"}, "fields": [{"label": "内容", "field": ["premise"]}]},
                   # A record's title names the field it reads from, below a text key.
                   {"key": "published", "type": "records", "source": "episodes",
                    "record_display": {"title": {"source": "series", "field": ["publication", "title"]}}}],
    }]}})
    workflow = SimpleNamespace(id="flow", name="flow", description="写一集短剧", variables={}, steps=[])
    binding = SimpleNamespace(id="binding", workspace_id="workspace", workflow_id="flow", name="单集生成",
                              config={"chat_entrypoint": {"enabled": True}})
    anchors = {anchor.kind: anchor for anchor in wt.workspace_anchors(workspace, [binding], [workflow])}

    page = anchors["app_view"]
    assert {text for _, text in page.wanted()["en"]} == {"系列", "进度", "已完成", "内容"}
    assert not [path for path, _ in page.leaves() if "record_display" in path]
    translated = page.with_translations("en", {path: f"EN:{text}" for path, text in page.wanted()["en"]})
    view = localized(translated, "en")["components"][0]
    assert (view["title"], view["blocks"][0]["stage_labels"]["made"]) == ("EN:系列", "EN:已完成")
    # Which data a view reads is never translated.
    assert (view["key"], view["blocks"][0]["source"], view["blocks"][0]["fields"][0]["field"]) == ("series", "episodes", ["premise"])

    entry = anchors["binding"]
    assert {text for _, text in entry.wanted()["en"]} == {"单集生成", "写一集短剧"}
    binding.config = entry.with_translations("en", {path: f"EN:{text}" for path, text in entry.wanted()["en"]})
    assert normalize_chat_entrypoint(binding, workflow, locale="en").description == "EN:写一集短剧"
    assert normalize_chat_entrypoint(binding, workflow, locale="zh").description == "写一集短剧"
