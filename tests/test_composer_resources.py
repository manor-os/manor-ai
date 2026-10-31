from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from packages.core.ai.runtime.skill_forcing import runtime_resolve_manual_skill_refs
from packages.core.models.skill import Skill
from packages.core.services import builtin_plugin_service, composer_resources, skill_service

pytestmark = pytest.mark.oss_smoke


def skill(id, *, workspace_id=None, is_public=False, entity_id="entity", visibility=None, **kwargs):
    return Skill(
        id=id,
        entity_id=entity_id,
        workspace_id=workspace_id,
        name="Stickman",
        slug="stickman",
        system_prompt="secret prompt",
        status="active",
        is_public=is_public,
        visibility=visibility or ("workspace" if workspace_id else "entity"),
        config={},
        **kwargs,
    )


@pytest.fixture
def catalog(monkeypatch):
    from packages.core.services import resource_access

    rows = [
        skill("builtin", entity_id=None),
        skill("global"),
        skill("local", workspace_id="studio"),
        skill("other", workspace_id="other-studio"),
        skill("shared", workspace_id="other-studio", visibility="entity"),
        skill("forbidden"),
        skill("inactive"),
    ]
    rows[-1].status = "inactive"
    monkeypatch.setattr(skill_service, "list_skills", AsyncMock(return_value=rows + [rows[0]]))
    monkeypatch.setattr(builtin_plugin_service, "load_user_builtin_plugin_preferences", AsyncMock(return_value={}))

    async def access(_db, *, descriptors, **_kwargs):
        return {descriptor.resource_id for descriptor in descriptors if descriptor.resource_id != "forbidden"}

    monkeypatch.setattr(resource_access, "readable_resource_ids", AsyncMock(side_effect=access))
    monkeypatch.setattr(resource_access, "user_can_access_resource", AsyncMock(side_effect=AssertionError(
        "Composer must not repeat role/grant queries for each Skill"
    )))
    monkeypatch.setattr(composer_resources, "resource_workspace_names", AsyncMock(return_value={"studio": "Studio"}))
    return SimpleNamespace(id="user", entity_id="entity", role="member"), rows


@pytest.mark.asyncio
async def test_global_catalog_omits_unrouted_workspace_copies_and_deduplicates_ids(catalog):
    user, _ = catalog
    rows = await composer_resources.list_composer_skills(object(), user)
    assert [row["id"] for row in rows] == ["builtin", "global", "shared"]
    assert all("system_prompt" not in row and "scripts" not in row for row in rows)


@pytest.mark.asyncio
async def test_workspace_catalog_preserves_local_instance_and_actor_access(catalog):
    user, _ = catalog
    rows = await composer_resources.list_composer_skills(object(), user, workspace_id="studio")
    assert [row["id"] for row in rows] == ["builtin", "global", "local", "shared"]
    assert rows[2]["workspace_name"] == "Studio"


@pytest.mark.asyncio
async def test_workspace_manor_ai_composer_uses_owner_chat_catalog(catalog, monkeypatch):
    from packages.core.constants.agents import MANOR_AGENT_ID

    user, _ = catalog
    bound = AsyncMock(side_effect=AssertionError(
        "Workspace Manor AI must not use the custom-Agent binding catalog"
    ))
    monkeypatch.setattr(skill_service, "list_skills_for_agent", bound)

    rows = await composer_resources.list_composer_skills(
        object(),
        user,
        workspace_id="studio",
        agent_id=MANOR_AGENT_ID,
    )

    assert [row["id"] for row in rows] == ["builtin", "global", "local", "shared"]
    bound.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("workspace_id", [None, "studio"])
@pytest.mark.parametrize("visibility", ["entity", "public", "workspace", "private"])
@pytest.mark.parametrize("is_public", [False, True])
async def test_cross_workspace_catalog_uses_visibility_not_agent_invocation_flag(
    catalog,
    monkeypatch,
    workspace_id,
    visibility,
    is_public,
):
    user, _ = catalog
    rows = [
        skill("reusable", workspace_id="other-studio", visibility=visibility, is_public=is_public),
        skill("forbidden", workspace_id="other-studio", visibility=visibility, is_public=is_public),
    ]
    monkeypatch.setattr(skill_service, "list_skills", AsyncMock(return_value=rows))
    result = await composer_resources.list_composer_skills(object(), user, workspace_id=workspace_id)
    assert [row["id"] for row in result] == (["reusable"] if visibility in {"entity", "public"} else [])


@pytest.mark.asyncio
async def test_agent_catalog_uses_runtime_bindings_including_cross_workspace_reuse(catalog, monkeypatch):
    user, skills = catalog
    bound = AsyncMock(return_value=[skills[0], skills[3]])
    monkeypatch.setattr(skill_service, "list_skills_for_agent", bound)
    db = object()
    rows = await composer_resources.list_composer_skills(db, user, workspace_id="studio", agent_id="agent")
    assert [row["id"] for row in rows] == ["builtin", "other"]
    bound.assert_awaited_once_with(db, "entity", "agent", workspace_id="studio")


@pytest.mark.asyncio
async def test_composer_preserves_disabled_builtin_plugin_gate(catalog, monkeypatch):
    user, _ = catalog
    monkeypatch.setattr(
        builtin_plugin_service, "builtin_plugin_key_for_skill", lambda row: "plugin" if row.id == "builtin" else None
    )
    monkeypatch.setattr(builtin_plugin_service, "builtin_plugin_is_enabled", lambda *_args: False)
    rows = await composer_resources.list_composer_skills(object(), user)
    assert "builtin" not in [row["id"] for row in rows]


@pytest.mark.asyncio
@pytest.mark.parametrize("agent_id", [None, "agent"])
async def test_composer_shows_builtin_once_without_hiding_custom_same_slug(catalog, monkeypatch, agent_id):
    user, _ = catalog
    builtin = skill("builtin", entity_id=None)
    builtin.config = {"source": "builtin"}
    legacy = skill("legacy-copy")
    legacy.config = {"source": "marketplace", "source_skill_id": builtin.id}
    custom = skill("custom-same-slug")
    rows = [legacy, custom, builtin]
    monkeypatch.setattr(skill_service, "list_skills", AsyncMock(return_value=rows))
    monkeypatch.setattr(skill_service, "list_skills_for_agent", AsyncMock(return_value=rows))

    result = await composer_resources.list_composer_skills(object(), user, agent_id=agent_id)

    assert [row["id"] for row in result] == [custom.id, builtin.id]
    assert legacy.config["source_skill_id"] == builtin.id


@pytest.mark.asyncio
async def test_manual_resolution_uses_same_workspace_and_exact_id():
    lookup = AsyncMock(
        return_value=[skill("installed-1", workspace_id="studio"), skill("installed-2", workspace_id="other")]
    )
    db = object()
    refs, missing = await runtime_resolve_manual_skill_refs(
        db,
        entity_id="entity",
        agent_id="agent",
        workspace_id="studio",
        manual_skill_refs='[{"kind":"id","value":"installed-1"}]',
        list_skills_for_agent_fn=lookup,
    )
    lookup.assert_awaited_once_with(db, "entity", "agent", workspace_id="studio")
    assert missing == []
    assert [ref["id"] for ref in refs] == ["installed-1"]


@pytest.mark.asyncio
async def test_workspace_manor_ai_manual_skill_uses_owner_chat_catalog():
    from packages.core.ai.runtime.surfaces import ChatSurface
    from packages.core.constants.agents import MANOR_AGENT_ID

    owner_catalog = AsyncMock(return_value=[skill("builtin", entity_id=None)])
    bound_catalog = AsyncMock(side_effect=AssertionError(
        "Workspace Manor AI must not use the custom-Agent binding catalog"
    ))

    refs, missing = await runtime_resolve_manual_skill_refs(
        object(),
        entity_id="entity",
        agent_id=MANOR_AGENT_ID,
        workspace_id="studio",
        user_id="user",
        surface=ChatSurface.WORKSPACE_CHAT,
        manual_skill_refs='[{"kind":"id","value":"builtin"}]',
        list_skills_fn=owner_catalog,
        list_skills_for_agent_fn=bound_catalog,
    )

    assert missing == []
    assert [ref["id"] for ref in refs] == ["builtin"]
    owner_catalog.assert_awaited_once()
    bound_catalog.assert_not_awaited()


@pytest.mark.asyncio
async def test_origin_does_not_disclose_an_unreadable_home_workspace(monkeypatch):
    workspaces = [SimpleNamespace(id="visible", name="Studio"), SimpleNamespace(id="hidden", name="Secret")]
    db = SimpleNamespace(
        execute=AsyncMock(return_value=SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: workspaces)))
    )

    readable = AsyncMock(return_value={"visible"})
    monkeypatch.setattr(composer_resources, "user_readable_workspace_ids", readable)
    user = SimpleNamespace(id="user", entity_id="entity", role="member")
    names = await composer_resources.resource_workspace_names(
        db, [skill("a", workspace_id="visible"), skill("b", workspace_id="hidden")], user
    )
    assert names == {"visible": "Studio"}
    readable.assert_awaited_once_with(
        db,
        workspace_ids={"visible", "hidden"},
        entity_id=user.entity_id,
        user_id=user.id,
        role=user.role,
    )


@pytest.mark.asyncio
async def test_workspace_names_without_workspace_resources_skip_database(monkeypatch):
    db = SimpleNamespace(execute=AsyncMock())
    readable = AsyncMock()
    monkeypatch.setattr(composer_resources, "user_readable_workspace_ids", readable)

    assert await composer_resources.resource_workspace_names(db, [skill("shared")], object()) == {}
    db.execute.assert_not_awaited()
    readable.assert_not_awaited()


@pytest.mark.asyncio
async def test_workspace_names_without_live_workspaces_skip_authorization(monkeypatch):
    db = SimpleNamespace(
        execute=AsyncMock(return_value=SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [])))
    )
    readable = AsyncMock()
    monkeypatch.setattr(composer_resources, "user_readable_workspace_ids", readable)
    user = SimpleNamespace(id="user", entity_id="entity", role="member")

    assert await composer_resources.resource_workspace_names(db, [skill("a", workspace_id="missing")], user) == {}
    readable.assert_not_awaited()


@pytest.mark.asyncio
async def test_catalog_api_uses_authoritative_task_host(monkeypatch):
    from apps.api.routers import chat
    from packages.core.models.workspace import Agent
    from packages.core.services import resource_access

    user = SimpleNamespace(id="user", entity_id="entity", role="member")
    resolve_scope = AsyncMock(return_value=("studio", "task", "task-1"))
    resolve_host = AsyncMock(return_value="host")
    monkeypatch.setattr(chat, "_resolve_chat_workspace_scope", resolve_scope)
    monkeypatch.setattr(chat, "_resolve_task_session_request_agent", resolve_host)
    monkeypatch.setattr(resource_access, "user_can_access_resource", AsyncMock(return_value=True))
    project = AsyncMock(return_value=[])
    monkeypatch.setattr(composer_resources, "list_composer_skills", project)
    agent = Agent(id="host", entity_id="entity")
    db = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: agent)))
    await chat.list_chat_composer_skills(conversation_id="conv", agent_id="client-agent", user=user, db=db)
    resolve_host.assert_awaited_once_with(
        db,
        user,
        requested_agent_id="client-agent",
        conversation_id="conv",
        workspace_id="studio",
        thread_ref_kind="task",
        thread_ref_id="task-1",
    )
    project.assert_awaited_once_with(db, user, workspace_id="studio", agent_id="host")


@pytest.mark.asyncio
async def test_catalog_api_stops_on_inaccessible_conversation(monkeypatch):
    from fastapi import HTTPException
    from apps.api.routers import chat

    monkeypatch.setattr(chat, "_resolve_chat_workspace_scope", AsyncMock(side_effect=HTTPException(404)))
    project = AsyncMock()
    monkeypatch.setattr(composer_resources, "list_composer_skills", project)
    with pytest.raises(HTTPException) as error:
        await chat.list_chat_composer_skills(conversation_id="other-users-conv", user=object(), db=object())
    assert error.value.status_code == 404
    project.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "entity_id,subscribed,allowed",
    [
        ("entity", False, False),
        ("entity", True, True),
        ("foreign", True, False),
    ],
)
async def test_catalog_agent_access_requires_visibility_or_current_workspace_binding(
    monkeypatch, entity_id, subscribed, allowed
):
    from fastapi import HTTPException
    from apps.api.routers import chat
    from packages.core.models.workspace import Agent
    from packages.core.services import resource_access

    user = SimpleNamespace(id="user", entity_id="entity", role="member")
    monkeypatch.setattr(chat, "_resolve_chat_workspace_scope", AsyncMock(return_value=("studio", None, None)))
    monkeypatch.setattr(chat, "_resolve_task_session_request_agent", AsyncMock(return_value="agent"))
    monkeypatch.setattr(resource_access, "user_can_access_resource", AsyncMock(return_value=False))
    project = AsyncMock(return_value=[])
    monkeypatch.setattr(composer_resources, "list_composer_skills", project)
    agent = Agent(id="agent", entity_id=entity_id)
    db = SimpleNamespace(
        execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: agent)),
        scalar=AsyncMock(return_value="subscription" if subscribed else None),
    )
    if allowed:
        await chat.list_chat_composer_skills(workspace_id="studio", agent_id="agent", user=user, db=db)
        project.assert_awaited_once_with(db, user, workspace_id="studio", agent_id="agent")
    else:
        with pytest.raises(HTTPException) as error:
            await chat.list_chat_composer_skills(workspace_id="studio", agent_id="agent", user=user, db=db)
        assert error.value.status_code == 404
        project.assert_not_awaited()


@pytest.mark.asyncio
async def test_master_host_keeps_runtime_binding_scope_without_loading_an_agent_row(monkeypatch):
    from apps.api.routers import chat

    user = SimpleNamespace(id="user", entity_id="entity", role="member")
    monkeypatch.setattr(chat, "_resolve_chat_workspace_scope", AsyncMock(return_value=("studio", "task", "task")))
    monkeypatch.setattr(chat, "_resolve_task_session_request_agent", AsyncMock(return_value="manor-master"))
    project = AsyncMock(return_value=[])
    monkeypatch.setattr(composer_resources, "list_composer_skills", project)
    db = object()
    await chat.list_chat_composer_skills(workspace_id="studio", user=user, db=db)
    project.assert_awaited_once_with(db, user, workspace_id="studio", agent_id="manor-master")
