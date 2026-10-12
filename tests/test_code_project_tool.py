from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from packages.core.ai.tools import code_project_tool as tool
from packages.core.services import code_projects as cp

PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(256))


@pytest.fixture
def fs(tmp_path, monkeypatch, db_session):
    """Entity filesystem on tmp_path with Knowledge projection stubbed out."""
    from packages.core.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "MANOR_FS_ENABLED", True)
    monkeypatch.setattr(settings, "MANOR_FS_ROOT", str(tmp_path))
    mutations: list[tuple[str, list[str]]] = []

    async def record_mutation(**kwargs):
        mutations.append((str(getattr(kwargs["action"], "value", kwargs["action"])), kwargs["paths"]))
        return None

    monkeypatch.setattr(
        "packages.core.services.ai_file_permissions.guard_ai_file_mutation",
        record_mutation,
    )
    monkeypatch.setattr(
        "packages.core.services.ai_file_permissions.guard_ai_file_read_access",
        AsyncMock(return_value=None),
    )
    counter = {"n": 0}

    async def fake_sync(**_kwargs):
        counter["n"] += 1
        return SimpleNamespace(synced=True, document_id=f"doc_{counter['n']}", reason=None)

    monkeypatch.setattr("packages.core.services.knowledge_sync.sync_file_to_knowledge", fake_sync)
    monkeypatch.setattr(
        "packages.core.services.knowledge_sync.trash_path",
        AsyncMock(return_value=True),
    )
    entity_id = f"ent{uuid4().hex[:20]}"
    (tmp_path / entity_id).mkdir()
    return SimpleNamespace(root=tmp_path / entity_id, entity_id=entity_id, mutations=mutations)


async def call(fs, **kwargs):
    kwargs.setdefault("user_id", "user-1")
    return json.loads(await tool._code_project_handler(entity_id=fs.entity_id, **kwargs))


async def create_site(fs, name="Acme Site", files=None):
    created = await call(fs, action="create", name=name, profile="static_site")
    created["write"] = await call(
        fs,
        action="write",
        project_id=created["project_id"],
        files=files or [
            {"path": "index.html", "content": "<!doctype html><a href='about.html'>About</a>"},
            {"path": "about.html", "content": "<!doctype html><a href='index.html'>Home</a>"},
        ],
    )
    return created


@pytest.mark.asyncio
async def test_create_then_keep_adding_pages_in_a_later_call(fs):
    created = await create_site(fs)
    assert created["created"] is True
    assert created["root_path"] == "Code/acme-site"
    assert created["write"]["validation"]["valid"] is True
    project_id = created["project_id"]

    later = await call(
        fs,
        action="write",
        project_id=project_id,
        files=[
            {"path": "blog/post-1.html", "content": "<!doctype html><a href='../index.html'>Home</a>"},
            {"path": "index.html", "content": "<!doctype html><a href='blog/post-1.html'>Blog</a>"},
        ],
    )
    assert "error" not in later, later
    assert {item["path"]: item["created"] for item in later["written"]} == {
        "blog/post-1.html": True,
        "index.html": False,
    }
    removed = await call(fs, action="write", project_id=project_id, delete=["about.html"])
    assert removed["deleted"] == ["about.html"]
    assert (fs.root / "Code/acme-site/blog/post-1.html").exists()
    assert not (fs.root / "Code/acme-site/about.html").exists()
    assert removed["validation"]["valid"] is True

    # Exactly one approval check per tool call, even when a batch mixes new
    # files and overwrites, so one approval token always covers the call.
    assert [action for action, _ in fs.mutations] == ["write", "write", "delete"]
    assert fs.mutations[1][1] == ["Code/acme-site/blog/post-1.html", "Code/acme-site/index.html"]

    listing = await call(fs, action="list")
    assert [item["project_id"] for item in listing["projects"]] == [project_id]

    detail = await call(fs, action="get", project_id=project_id)
    assert [item["path"] for item in detail["files"]] == ["blog/post-1.html", "index.html"]


@pytest.mark.asyncio
async def test_write_reports_broken_links_without_blocking(fs):
    created = await create_site(fs, files=[
        {"path": "index.html", "content": "<!doctype html><a href='missing.html'>x</a>"},
    ])
    validation = created["write"]["validation"]
    assert validation["valid"] is False
    assert any("missing.html" in error for error in validation["errors"])
    assert (fs.root / "Code/acme-site/index.html").exists()


@pytest.mark.asyncio
async def test_import_binary_asset_from_knowledge_path_and_fs_link(fs):
    (fs.root / "Knowledge/brand").mkdir(parents=True)
    (fs.root / "Knowledge/brand/logo.png").write_bytes(PNG)
    created = await create_site(fs)
    result = await call(
        fs,
        action="write",
        project_id=created["project_id"],
        files=[
            {"path": "img/logo.png", "source": "Knowledge/brand/logo.png"},
            {"path": "img/logo2.png", "source": f"/api/v1/fs/{fs.entity_id}/Knowledge/brand/logo.png"},
        ],
    )
    assert "error" not in result, result
    digest = hashlib.sha256(PNG).hexdigest()
    assert [item["sha256"] for item in result["written"]] == [digest, digest]
    assert result["written"][0]["copied_from"] == "Knowledge/brand/logo.png"
    assert (fs.root / "Code/acme-site/img/logo2.png").read_bytes() == PNG


@pytest.mark.asyncio
async def test_import_respects_knowledge_read_access(fs, monkeypatch):
    (fs.root / "Knowledge").mkdir()
    (fs.root / "Knowledge/private.png").write_bytes(PNG)
    created = await create_site(fs)
    monkeypatch.setattr(
        "packages.core.services.ai_file_permissions.guard_ai_file_read_access",
        AsyncMock(return_value=json.dumps({"error": "access_denied"})),
    )
    result = await call(
        fs,
        action="write",
        project_id=created["project_id"],
        files=[{"path": "img/p.png", "source": "Knowledge/private.png"}],
    )
    assert result["error"] == "access_denied"
    assert not (fs.root / "Code/acme-site/img/p.png").exists()


@pytest.mark.asyncio
async def test_import_from_another_entity_link_is_refused(fs):
    created = await create_site(fs)
    result = await call(
        fs,
        action="write",
        project_id=created["project_id"],
        files=[{"path": "img/x.png", "source": "/api/v1/fs/other-entity/Knowledge/x.png"}],
    )
    assert "another entity" in result["error"]


@pytest.mark.asyncio
async def test_import_from_public_url(fs, monkeypatch):
    fetched: list[str] = []

    async def fake_download(url, *, max_bytes):
        fetched.append(url)
        assert max_bytes == cp.PROFILES["static_site"].max_file_bytes
        return PNG

    monkeypatch.setattr(cp, "download_public_https", fake_download)
    created = await create_site(fs)
    result = await call(
        fs,
        action="write",
        project_id=created["project_id"],
        files=[{"path": "img/hero.png", "source": "https://cdn.example.com/hero.png"}],
    )
    assert "error" not in result, result
    assert fetched == ["https://cdn.example.com/hero.png"]
    assert (fs.root / "Code/acme-site/img/hero.png").read_bytes() == PNG


@pytest.mark.asyncio
@pytest.mark.parametrize("url", [
    "https://127.0.0.1/logo.png",
    "https://localhost/logo.png",
    "https://169.254.169.254/latest/meta-data",
    "http://example.com/logo.png",
])
async def test_public_download_refuses_private_or_plain_http_targets(url):
    with pytest.raises(cp.CodeProjectError):
        await cp.download_public_https(url, max_bytes=1024)


@pytest.mark.asyncio
async def test_profile_rules_apply_at_write_time(fs):
    created = await create_site(fs)
    project_id = created["project_id"]
    wrong_type = await call(
        fs, action="write", project_id=project_id,
        files=[{"path": "data.csv", "content": "a,b"}],
    )
    assert "not allowed in a static_site project" in wrong_type["error"]

    binary_as_text = await call(
        fs, action="write", project_id=project_id,
        files=[{"path": "logo.png", "content": "not really"}],
    )
    assert "must be imported with source" in binary_as_text["error"]

    too_big = await call(
        fs, action="write", project_id=project_id,
        files=[{"path": "big.js", "content": "x" * (cp.PROFILES["static_site"].max_file_bytes + 1)}],
    )
    assert "per-file limit" in too_big["error"]


@pytest.mark.asyncio
async def test_expected_sha256_guards_concurrent_edits(fs):
    created = await create_site(fs)
    result = await call(
        fs, action="write", project_id=created["project_id"],
        files=[{"path": "index.html", "content": "<!doctype html>", "expected_sha256": "0" * 64}],
    )
    assert "expected_sha256" in result["error"]


@pytest.mark.asyncio
async def test_unsafe_paths_are_refused(fs):
    created = await create_site(fs)
    for path in ("../escape.html", ".env", "a/.hidden/x.html", "/abs.html"):
        result = await call(
            fs, action="write", project_id=created["project_id"],
            files=[{"path": path, "content": "x"}],
        )
        assert "error" in result, path


@pytest.mark.asyncio
async def test_workspace_projects_live_in_the_workspace_home_not_a_task_archive(fs, monkeypatch):
    async def task_scoped_base(**_kwargs):
        return "Workspaces/_by_id/F1/tasks/T1"

    monkeypatch.setattr(
        "packages.core.services.generated_media_naming.resolve_workspace_artifact_base_dir",
        task_scoped_base,
    )
    created = await call(
        fs, action="create", name="Landing", profile="static_site", workspace_id="ws-1",
    )
    assert created["root_path"] == "Workspaces/_by_id/F1/code/landing"
    assert created["workspace_id"] == "ws-1"

    again = await call(
        fs, action="create", name="Landing", profile="static_site", workspace_id="ws-1",
    )
    assert again["root_path"] != created["root_path"], "a second project never reuses a taken root"


@pytest.mark.asyncio
async def test_adopt_existing_folder_is_idempotent(fs):
    (fs.root / "Sites/old").mkdir(parents=True)
    (fs.root / "Sites/old/index.html").write_text("<!doctype html>", encoding="utf-8")
    first = await call(fs, action="create", name="Old", profile="static_site", adopt_path="Sites/old")
    second = await call(fs, action="create", name="Old", profile="static_site", adopt_path="Sites/old")
    assert first["created"] is True
    assert second["created"] is False
    assert first["project_id"] == second["project_id"]
    assert (fs.root / "Sites/old/index.html").exists()


# ── guards: everything advertised is executable ─────────────────────────────

def test_schema_actions_match_dispatch():
    schema_actions = tool.CODE_PROJECT_SCHEMA["function"]["parameters"]["properties"]["action"]["enum"]
    assert set(schema_actions) == set(tool._DISPATCH) == set(tool.ACTIONS)


def test_profile_validators_and_publish_targets_resolve():
    from packages.core.ai.tools import site_tools

    tools_by_name = {schema["function"]["name"] for schema, _ in site_tools.get_tools()}
    for profile in cp.PROFILES.values():
        for name in profile.validators:
            assert callable(cp.VALIDATORS.get(name)), f"{profile.id}: validator {name!r}"
        for target in profile.publish_targets:
            assert cp.PUBLISH_TARGET_TOOLS.get(target) in tools_by_name, f"{profile.id}: {target!r}"


def test_schema_profiles_match_registry():
    enum = tool.CODE_PROJECT_SCHEMA["function"]["parameters"]["properties"]["profile"]["enum"]
    assert set(enum) == set(cp.PROFILES)


# ── generate_file(kind='code') registers a project ──────────────────────────

async def generate_code(fs, monkeypatch, **overrides):
    from packages.core.ai.tools import generate_file_tool

    async def scope(**kwargs):
        return f"Workspaces/Demo/code/{kwargs['name']}"

    monkeypatch.setattr(generate_file_tool, "_scope_workspace_output_name", scope)
    args = {
        "entity_id": fs.entity_id,
        "user_id": "user-1",
        "conversation_id": "conv-1",
        "kind": "code",
        "name": "shop",
        "prompt": "a shop site",
        "params": {"files": [{"path": "index.html", "content": "<!doctype html><img src='img/logo.png'>"}]},
    }
    args.update(overrides)
    return json.loads(await generate_file_tool._generate_file_handler(**args))


@pytest.mark.asyncio
async def test_generate_code_registers_a_project_and_imports_assets(fs, monkeypatch):
    (fs.root / "Knowledge").mkdir()
    (fs.root / "Knowledge/logo.png").write_bytes(PNG)
    result = await generate_code(fs, monkeypatch, params={
        "files": [
            {"path": "index.html", "content": "<!doctype html><img src='img/logo.png'>"},
            {"path": "img/logo.png", "source": "Knowledge/logo.png"},
        ],
    })
    assert result["created"] is True
    assert result["project_profile"] == "static_site"
    assert [item["path"] for item in result["assets"]] == ["img/logo.png"]
    assert result["validation"] == {"valid": True, "errors": [], "error_count": 0}
    assert (fs.root / "Workspaces/Demo/code/shop/img/logo.png").read_bytes() == PNG

    # The documented params.assets form works too, and a repeat run reuses the project.
    again = await generate_code(fs, monkeypatch, params={
        "files": [{"path": "index.html", "content": "<!doctype html><img src='img/logo.png'>"}],
        "assets": [{"path": "img/logo.png", "source_path": "Knowledge/logo.png"}],
    })
    assert again["project_id"] == result["project_id"]
    assert again["validation"]["valid"] is True


@pytest.mark.asyncio
async def test_generate_code_reports_broken_references(fs, monkeypatch):
    result = await generate_code(fs, monkeypatch)
    assert result["created"] is True
    assert result["validation"]["valid"] is False
    assert any("img/logo.png" in error for error in result["validation"]["errors"])


@pytest.mark.asyncio
async def test_generate_code_with_framework_sources_uses_generic_profile(fs, monkeypatch):
    result = await generate_code(fs, monkeypatch, params={
        "files": [
            {"path": "index.html", "content": "<!doctype html>"},
            {"path": "src/App.tsx", "content": "export default () => null"},
        ],
    })
    assert result["project_profile"] == "generic"
    assert result["validation"]["valid"] is True


# ── Workspace context names the Workspace's projects ────────────────────────

@pytest.mark.asyncio
async def test_workspace_context_lists_code_projects(db_session):
    from packages.core.ai.runtime.context_blocks import _workspace_code_projects_block
    from packages.core.ai.runtime.requests import AIRuntimeRequest
    from packages.core.ai.runtime.surfaces import ChatSurface
    from packages.core.models.code_project import CodeProject

    entity_id = f"ent{uuid4().hex[:20]}"
    project = CodeProject(
        entity_id=entity_id, workspace_id="ws-ctx", name="Landing",
        root_path="Workspaces/_by_id/F/code/landing", profile="static_site",
    )
    db_session.add_all([
        project,
        CodeProject(entity_id=entity_id, workspace_id="ws-other", name="Elsewhere", root_path="Code/elsewhere"),
    ])
    await db_session.commit()

    request = AIRuntimeRequest(surface=ChatSurface.WORKSPACE_CHAT, entity_id=entity_id, workspace_id="ws-ctx")
    block = await _workspace_code_projects_block(request)
    assert block is not None
    assert f"project_id={project.id}" in block.content
    assert "Elsewhere" not in block.content

    for surface in (ChatSurface.PUBLIC_CUSTOMER_CHAT, ChatSurface.EXTERNAL_CHANNEL_CHAT):
        hidden = AIRuntimeRequest(surface=surface, entity_id=entity_id, workspace_id="ws-ctx")
        assert await _workspace_code_projects_block(hidden) is None

    empty = AIRuntimeRequest(surface=ChatSurface.WORKSPACE_CHAT, entity_id=entity_id, workspace_id="ws-none")
    assert await _workspace_code_projects_block(empty) is None


# ── review regressions ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_files_and_deletions_never_share_one_call(fs):
    created = await create_site(fs)
    result = await call(
        fs, action="write", project_id=created["project_id"],
        files=[{"path": "x.html", "content": "<!doctype html>"}], delete=["about.html"],
    )
    assert "separate" in result["error"]
    assert (fs.root / "Code/acme-site/about.html").exists()


@pytest.mark.asyncio
async def test_create_takes_no_files(fs):
    created = await call(fs, action="create", name="Empty", profile="static_site",
                         files=[{"path": "index.html", "content": "x"}])
    assert created["created"] is True
    assert "write" not in created
    assert not (fs.root / "Code/empty").exists()
    assert fs.mutations == []


@pytest.mark.asyncio
async def test_approval_envelope_is_returned_unchanged(fs, monkeypatch):
    envelope = {"__hitl__": True, "error": "approval_required", "approval_token": "tok"}

    async def ask(**_kwargs):
        return json.dumps(envelope)

    created = await call(fs, action="create", name="Ask", profile="static_site")
    monkeypatch.setattr("packages.core.services.ai_file_permissions.guard_ai_file_mutation", ask)
    result = await call(fs, action="write", project_id=created["project_id"],
                        files=[{"path": "index.html", "content": "<!doctype html>"}])
    assert result == envelope


@pytest.mark.asyncio
async def test_symlinked_source_is_read_guarded_by_its_real_path(fs, monkeypatch):
    import os

    (fs.root / "Private").mkdir()
    (fs.root / "Private/secret.png").write_bytes(PNG)
    (fs.root / "Public").mkdir()
    os.symlink(fs.root / "Private/secret.png", fs.root / "Public/looks-fine.png")
    checked: list[list[str]] = []

    async def read_guard(**kwargs):
        checked.append(kwargs["paths"])
        return None

    created = await create_site(fs)
    monkeypatch.setattr("packages.core.services.ai_file_permissions.guard_ai_file_read_access", read_guard)
    await call(fs, action="write", project_id=created["project_id"],
               files=[{"path": "img/a.png", "source": "Public/looks-fine.png"}])
    assert checked == [["Private/secret.png"]]


@pytest.mark.asyncio
async def test_adopt_refuses_symlinked_folders_and_other_workspaces_projects(fs, monkeypatch):
    import os

    (fs.root / "Real").mkdir()
    (fs.root / "Real/index.html").write_text("<!doctype html>", encoding="utf-8")
    os.symlink(fs.root / "Real", fs.root / "Alias")
    linked = await call(fs, action="create", name="A", profile="static_site", adopt_path="Alias")
    assert "symbolic link" in linked["error"]

    async def home(**_kwargs):
        return "Workspaces/_by_id/F1"

    monkeypatch.setattr(
        "packages.core.services.generated_media_naming.resolve_workspace_artifact_base_dir", home,
    )
    owned = await call(fs, action="create", name="Owned", profile="static_site", workspace_id="ws-a")
    (fs.root / owned["root_path"]).mkdir(parents=True)
    stolen = await call(fs, action="create", name="Owned", profile="static_site",
                        adopt_path=owned["root_path"], workspace_id="ws-b")
    assert "another Workspace" in stolen["error"]


@pytest.mark.asyncio
async def test_user_less_runs_only_list_their_workspace(db_session, fs):
    from packages.core.models.code_project import CodeProject

    db_session.add_all([
        CodeProject(entity_id=fs.entity_id, workspace_id="ws-1", name="Mine", root_path="W1/code/mine"),
        CodeProject(entity_id=fs.entity_id, workspace_id="ws-2", name="Theirs", root_path="W2/code/theirs"),
    ])
    await db_session.commit()
    listing = json.loads(await tool._code_project_handler(
        entity_id=fs.entity_id, action="list", scope="all", workspace_id="ws-1",
    ))
    assert [item["name"] for item in listing["projects"]] == ["Mine"]


@pytest.mark.asyncio
async def test_rejected_file_type_costs_no_download(fs, monkeypatch):
    async def must_not_download(*_args, **_kwargs):
        raise AssertionError("downloaded before the type check")

    monkeypatch.setattr(cp, "download_public_https", must_not_download)
    created = await create_site(fs)
    result = await call(fs, action="write", project_id=created["project_id"],
                        files=[{"path": "data.csv", "source": "https://cdn.example.com/d.csv"}])
    assert "not allowed" in result["error"]


@pytest.mark.asyncio
async def test_generate_code_assets_share_the_bundle_approval(fs, monkeypatch):
    (fs.root / "Knowledge").mkdir()
    (fs.root / "Knowledge/logo.png").write_bytes(PNG)
    result = await generate_code(fs, monkeypatch, params={
        "files": [{"path": "index.html", "content": "<!doctype html><img src='img/logo.png'>"}],
        "assets": [{"path": "img/logo.png", "source": "Knowledge/logo.png"}],
    })
    assert result["created"] is True
    assert len(fs.mutations) == 1
    assert fs.mutations[0][1] == [
        "Workspaces/Demo/code/shop/index.html",
        "Workspaces/Demo/code/shop/img/logo.png",
    ]


@pytest.mark.asyncio
async def test_workspace_receipt_authorizes_exactly_the_guarded_writes(fs, db_session):
    """The public call's receipt must match the handler's single inner guard."""
    from packages.core.ai.runtime.approval_classifier import (
        _classify_known_runtime_tool_action,
        resolve_runtime_workspace_file_scope,
    )
    from packages.core.ai.runtime.authorization_receipts import (
        RuntimeToolAuthorizationReceipt,
        WorkspaceFileAuthorizationResourceFactory,
        WorkspaceFileScopeBinding,
    )
    from packages.core.ai.runtime.tool_effect_classification import RuntimeAuthorizationAccess
    from packages.core.models.code_project import CodeProject
    from packages.core.services.ai_file_permissions import _precise_workspace_file_action_key

    project = CodeProject(entity_id=fs.entity_id, workspace_id="ws-1", name="S", root_path="W/code/s")
    db_session.add(project)
    await db_session.commit()
    (fs.root / "W/code/s").mkdir(parents=True)
    (fs.root / "W/code/s/index.html").write_text("<!doctype html>", encoding="utf-8")

    cases = [
        ({"files": [{"path": "about.html", "content": "x"}]}, "write"),
        ({"files": [{"path": "about.html", "content": "x"}, {"path": "index.html", "content": "y"}]}, "write"),
        ({"delete": ["index.html"]}, "delete"),
    ]
    for extra, inner_action in cases:
        args = {"action": "write", "project_id": project.id, **extra}
        scope = await resolve_runtime_workspace_file_scope(
            tool_name="code_project", arguments=args, entity_id=fs.entity_id,
            workspace_id="ws-1", task_id=None,
        )
        action = _classify_known_runtime_tool_action(
            "code_project", args, entity_id=fs.entity_id, workspace_file_scope=scope,
        )
        request = SimpleNamespace(
            tool_name="code_project", arguments=args, workspace_file_scope=scope,
            workspace_id="ws-1", task_id=None, entity_id=fs.entity_id,
        )
        receipt = RuntimeToolAuthorizationReceipt(
            tool_name="code_project", entity_id=fs.entity_id, user_id="user-1",
            workspace_id="ws-1", conversation_id=None, task_id=None,
            action_key=action.action_key, capability_id="file.write",
            access=RuntimeAuthorizationAccess.ACTION, arguments_hash="h", resource_kind="file",
            file_resources=WorkspaceFileAuthorizationResourceFactory.create(
                request=request, action_key=action.action_key, resource_id=action.resource_id,
            ),
            workspace_artifact_base_dir=scope.artifact_base_dir,
            workspace_scope_binding=WorkspaceFileScopeBinding.EXACT,
        )
        paths = [f"W/code/s/{item['path']}" for item in extra.get("files", [])] or [
            f"W/code/s/{path}" for path in extra["delete"]
        ]
        inner_key = _precise_workspace_file_action_key(
            entity_id=fs.entity_id, action=inner_action, paths=paths,
        )
        assert receipt.authorizes_workspace_file_mutation(
            capability_id="file.write", action_key=inner_key, paths=paths,
            entity_id=fs.entity_id, user_id="user-1", workspace_id="ws-1",
            conversation_id=None, task_id=None,
        ), (extra, action.action_key, inner_key)
        # ...and nothing outside the batch.
        assert not receipt.authorizes_workspace_file_mutation(
            capability_id="file.write", action_key=inner_key, paths=["W/code/s/other.html"],
            entity_id=fs.entity_id, user_id="user-1", workspace_id="ws-1",
            conversation_id=None, task_id=None,
        )


@pytest.mark.asyncio
async def test_workspace_context_reports_site_activity(db_session):
    from packages.core.ai.runtime.context_blocks import _workspace_code_projects_block
    from packages.core.ai.runtime.requests import AIRuntimeRequest
    from packages.core.ai.runtime.surfaces import ChatSurface
    from packages.core.models.code_project import CodeProject
    from packages.core.models.site import Site, SiteEvent

    entity_id = f"ent{uuid4().hex[:20]}"
    project = CodeProject(entity_id=entity_id, workspace_id="ws-act", name="Shop", root_path="W/code/shop",
                          profile="static_site")
    db_session.add(project)
    await db_session.flush()
    site = Site(entity_id=entity_id, name="Shop", slug=f"shop{uuid4().hex[:8]}", source_path="W/code/shop",
                revision=1, status="active", project_id=project.id)
    db_session.add(site)
    await db_session.flush()
    events = [
        ("page_view", "/", {"status": "200"}, "a"),
        ("page_view", "/pricing?utm_source=ad", {"status": "200"}, "b"),
        ("page_view", "/old", {"status": "404"}, "b"),
        ("form_submit", "/contact", {}, "b"),
    ]
    for event_type, path, properties, session in events:
        db_session.add(SiteEvent(site_id=site.id, entity_id=entity_id, event_type=event_type,
                                 action="lead" if event_type == "form_submit" else None,
                                 session_id=session, path=path, properties=properties))
    await db_session.commit()

    block = await _workspace_code_projects_block(
        AIRuntimeRequest(surface=ChatSurface.WORKSPACE_CHAT, entity_id=entity_id, workspace_id="ws-act"),
    )
    assert "7d: 2 page views; 2 visitors; 1 form submissions; 0 chat opens" in block.content
    assert "missing pages visitors hit /old (1)" in block.content


@pytest.mark.asyncio
async def test_workspace_context_lists_sites_connected_from_elsewhere(db_session):
    from packages.core.ai.runtime.context_blocks import _workspace_code_projects_block
    from packages.core.ai.runtime.requests import AIRuntimeRequest
    from packages.core.ai.runtime.surfaces import ChatSurface
    from packages.core.models.code_project import CodeProject
    from packages.core.models.site import Site

    entity_id = f"ent{uuid4().hex[:20]}"
    project = CodeProject(entity_id=entity_id, workspace_id=None, name="Solo", root_path="Code/solo",
                          profile="static_site")
    db_session.add(project)
    await db_session.flush()
    db_session.add(Site(entity_id=entity_id, name="Solo Site", slug=f"solo{uuid4().hex[:8]}",
                        source_path="Code/solo", revision=1, status="active",
                        project_id=project.id, workspace_id="ws-sales"))
    await db_session.commit()

    block = await _workspace_code_projects_block(
        AIRuntimeRequest(surface=ChatSurface.WORKSPACE_CHAT, entity_id=entity_id, workspace_id="ws-sales"),
    )
    assert "Websites connected to this Workspace" in block.content
    assert f"Solo Site" in block.content and f"project_id={project.id}" in block.content
    other = await _workspace_code_projects_block(
        AIRuntimeRequest(surface=ChatSurface.WORKSPACE_CHAT, entity_id=entity_id, workspace_id="ws-other"),
    )
    assert other is None
