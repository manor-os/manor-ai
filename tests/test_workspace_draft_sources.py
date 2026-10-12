"""Draft sources: attached documents and pasted links the architect can read."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from packages.core.models.base import generate_ulid


def test_extract_links_dedupes_and_strips_trailing_punctuation():
    from packages.core.services.workspace_draft_sources import extract_links

    text = "看 https://example.com/a. 再看 https://example.com/b，和 https://example.com/a"
    assert extract_links(text) == ["https://example.com/a", "https://example.com/b"]


def test_strip_source_text_removes_text_only():
    from packages.core.services.workspace_draft_sources import strip_source_text

    fields = {"name": "x", "sources": [{"id": "S1", "name": "a.md", "text": "secret", "chars": 6}]}
    out = strip_source_text(fields)
    assert out["sources"] == [{"id": "S1", "name": "a.md", "chars": 6}]
    assert fields["sources"][0]["text"] == "secret"  # input not mutated


def test_sources_context_block_lists_preview_and_new_flag():
    from packages.core.services.workspace_draft_sources import sources_context_block

    fields = {"sources": [
        {"id": "S1", "kind": "document", "name": "book.docx", "status": "ready", "chars": 5, "text": "hello", "turn_index": 0},
        {"id": "S2", "kind": "link", "name": "https://x.y", "status": "failed", "error": "http_404", "chars": 0, "text": "", "turn_index": 1},
    ]}
    block = sources_context_block(fields, current_turn_index=1)
    assert block.startswith("<sources>")
    assert "id=S1 | kind=document | name=book.docx | status=ready | chars=5 | new_this_turn=no" in block
    assert "id=S2 | kind=link | name=https://x.y | status=failed (http_404) | chars=0 | new_this_turn=yes" in block
    assert "preview: hello" in block
    assert "ws_read_source" in block
    assert sources_context_block({}, current_turn_index=0) == ""


def test_source_excerpt_concatenates_in_order_and_caps():
    from packages.core.services.workspace_draft_sources import source_excerpt

    fields = {"sources": [
        {"id": "S1", "name": "a", "status": "ready", "text": "A" * 10},
        {"id": "S2", "name": "b", "status": "ready", "text": "B" * 10},
        {"id": "S3", "name": "c", "status": "failed", "text": ""},
    ]}
    out = source_excerpt(fields, ["S2", "S1", "S3"], limit=15)
    assert out.startswith("## b\n" + "B" * 10)
    assert out.endswith("## a\n" + "A" * 5)


@pytest.mark.asyncio
async def test_fetch_link_source_converts_html(monkeypatch):
    import packages.core.services.web_fetch as web_fetch
    from packages.core.services.workspace_draft_sources import fetch_link_source

    async def fake_fetch(url, *, timeout=None, **_):
        return SimpleNamespace(
            content=b"<html><body><h1>Title</h1><p>Body text</p></body></html>",
            content_type="text/html; charset=utf-8",
            url=url,
            status_code=200,
        )

    monkeypatch.setattr(web_fetch, "fetch_url", fake_fetch)
    name, text, error = await fetch_link_source("https://example.com/page")
    assert error is None
    assert name == "https://example.com/page"
    assert "Body text" in text


@pytest.mark.asyncio
async def test_fetch_link_source_reports_http_errors(monkeypatch):
    import packages.core.services.web_fetch as web_fetch
    from packages.core.services.workspace_draft_sources import fetch_link_source

    async def fake_fetch(url, *, timeout=None, **_):
        return SimpleNamespace(content=b"", content_type="text/html", url=url, status_code=404)

    monkeypatch.setattr(web_fetch, "fetch_url", fake_fetch)
    _, text, error = await fetch_link_source("https://example.com/missing")
    assert text == ""
    assert error == "http_404"


@pytest.mark.asyncio
async def test_register_draft_sources_documents_links_dedupe_and_budget(db_session, monkeypatch):
    import packages.core.services.document_access as document_access
    import packages.core.services.web_fetch as web_fetch
    from packages.core.models.workspace_draft import WorkspaceDraft
    from packages.core.services import workspace_draft_sources as mod

    entity_id = generate_ulid()
    user_id = generate_ulid()
    doc_id = generate_ulid()
    fake_doc = SimpleNamespace(
        id=doc_id, name="chapter7.docx", fs_path=None, mime_type=None, file_type="docx",
        is_trashed=False, metadata_={"content_text": "第7章 拆开工作"},
    )

    async def fake_visible(db, document_id, entity, *, user_id=None, workspace_id=None):
        return fake_doc if document_id == doc_id else None

    async def fake_fetch(url, *, timeout=None, **_):
        return SimpleNamespace(content=b"<p>page</p>", content_type="text/html", url=url, status_code=200)

    monkeypatch.setattr(document_access, "get_visible_document", fake_visible)
    monkeypatch.setattr(web_fetch, "fetch_url", fake_fetch)
    monkeypatch.setattr(mod, "MAX_DRAFT_SOURCE_CHARS", 20)

    draft = WorkspaceDraft(entity_id=entity_id, user_id=user_id, fields={}, messages=[], missing=[], ready=False, status="active")
    db_session.add(draft)
    await db_session.flush()

    added = await mod.register_draft_sources(
        db_session, draft=draft, entity_id=entity_id, user_id=user_id,
        message="请看 https://example.com/one 和这个文件", document_ids=[doc_id, "01MISSING000000000000000000"],
    )
    kinds = [(s["kind"], s["status"], s.get("error")) for s in added]
    assert kinds == [("document", "ready", None), ("document", "failed", "not_found"), ("link", "ready", None)]
    assert added[0]["text"] == "第7章 拆开工作"
    assert added[0]["turn_index"] == 0
    assert draft.fields["sources"] == added

    # second turn: same document and url are not re-registered; budget trips on new content
    draft.messages = [{"role": "user", "content": "x"}, {"role": "assistant", "content": "y"}]
    added_again = await mod.register_draft_sources(
        db_session, draft=draft, entity_id=entity_id, user_id=user_id,
        message="再看 https://example.com/one 和 https://example.com/two", document_ids=[doc_id],
    )
    assert [s["url"] for s in added_again] == ["https://example.com/two"]
    assert added_again[0]["status"] == "failed"
    assert added_again[0]["error"] == "draft_source_budget_exceeded"
    assert added_again[0]["turn_index"] == 1
    assert len(draft.fields["sources"]) == 4


@pytest.mark.asyncio
async def test_create_draft_shell_registers_sources_and_process_message_appends_note(db_session, monkeypatch):
    import packages.core.services.document_access as document_access
    from packages.core.services import workspace_draft_service as svc

    entity_id = generate_ulid()
    user_id = generate_ulid()
    doc_id = generate_ulid()
    fake_doc = SimpleNamespace(
        id=doc_id, name="brief.md", fs_path=None, mime_type=None, file_type="md",
        is_trashed=False, metadata_={"content_text": "the brief"},
    )

    async def fake_visible(db, document_id, entity, *, user_id=None, workspace_id=None):
        return fake_doc if document_id == doc_id else None

    monkeypatch.setattr(document_access, "get_visible_document", fake_visible)

    draft = await svc.create_draft_shell(
        db_session, entity_id=entity_id, user_id=user_id,
        initial_brief="build it", document_ids=[doc_id],
    )
    assert [s["document_id"] for s in draft.fields["sources"]] == [doc_id]
    assert draft.fields["sources"][0]["turn_index"] == 0

    async def fake_turn(db, *, draft, entity_id, user_id, user_message, **_):
        return "ok"

    monkeypatch.setattr(svc, "_architect_turn", fake_turn)

    async def fake_lint(db, draft):
        return None

    monkeypatch.setattr(svc, "_refresh_missing_from_lint", fake_lint)

    reply, updated = await svc.process_draft_message(
        db_session, draft_id=draft.id, entity_id=entity_id, user_id=user_id,
        user_message="next", document_ids=[doc_id],
    )
    assert reply == "ok"
    assert len(updated.fields["sources"]) == 1  # deduped
    user_messages = [m["content"] for m in updated.messages if m["role"] == "user"]
    assert user_messages == ["next"]  # no note when nothing new was registered

    reply, updated = await svc.process_draft_message(
        db_session, draft_id=draft.id, entity_id=entity_id, user_id=user_id,
        user_message="and this https://example.com/x", document_ids=None,
    )
    assert reply == "ok"
    user_messages = [m["content"] for m in updated.messages if m["role"] == "user"]
    assert user_messages[-1].startswith("and this https://example.com/x\n[附件/Attached: https://example.com/x]")


@pytest.mark.asyncio
async def test_hydrate_response_strips_source_text():
    from apps.api.routers import workspace_drafts as router

    draft = SimpleNamespace(
        id="D", entity_id="E", user_id="U", status="active",
        fields={"sources": [{"id": "S1", "name": "a", "text": "hidden", "chars": 6, "status": "ready"}]},
        messages=[], missing=[], ready=False, suggested_blueprint_id=None,
        applied_blueprint_id=None, finalized_workspace_id=None, finalized_at=None,
        created_at=None, updated_at=None,
    )
    response = await router._hydrate_response(None, draft)
    assert response.fields["sources"] == [{"id": "S1", "name": "a", "chars": 6, "status": "ready"}]


def test_draft_message_request_accepts_document_ids():
    from apps.api.routers.workspace_drafts import DraftMessageRequest, StartDraftRequest

    assert DraftMessageRequest(message="hi", document_ids=["01ARZ3NDEKTSV4RRFFQ69G5FAV"]).document_ids == ["01ARZ3NDEKTSV4RRFFQ69G5FAV"]
    assert StartDraftRequest(initial_brief="x", document_ids=None).document_ids is None
    with pytest.raises(Exception):
        DraftMessageRequest(message="hi", document_ids=["not-a-ulid"])


@pytest.mark.asyncio
async def test_ws_read_source_pages_refreshes_and_rejects_unknown(db_session, monkeypatch):
    from packages.core.models.workspace_draft import WorkspaceDraft
    from packages.core.ai.tools.workspace_arch_tools import _read_source
    from packages.core.services import workspace_draft_sources as mod

    entity_id = generate_ulid()
    user_id = generate_ulid()
    draft = WorkspaceDraft(
        entity_id=entity_id, user_id=user_id, messages=[], missing=[], ready=False, status="active",
        fields={"sources": [
            {"id": "S1", "kind": "document", "name": "a", "status": "ready", "chars": 2500, "text": "x" * 2500, "turn_index": 0},
            {"id": "S2", "kind": "link", "name": "https://e.x", "url": "https://e.x", "status": "failed", "error": "http_500", "chars": 0, "text": "", "turn_index": 0},
        ]},
    )
    db_session.add(draft)
    await db_session.flush()

    first = json.loads(await _read_source(db_session, entity_id=entity_id, user_id=user_id, draft_id=draft.id, source_id="S1", max_chars=1000))
    assert first["ok"] is True
    assert first["total_chars"] == 2500 and first["offset"] == 0 and first["next_offset"] == 1000
    assert len(first["content"]) == 1000

    last = json.loads(await _read_source(db_session, entity_id=entity_id, user_id=user_id, draft_id=draft.id, source_id="S1", offset=2000, max_chars=1000))
    assert last["next_offset"] is None and len(last["content"]) == 500

    failed = json.loads(await _read_source(db_session, entity_id=entity_id, user_id=user_id, draft_id=draft.id, source_id="S2"))
    assert failed["ok"] is False and failed["status"] == "failed"

    async def fake_fetch(url):
        return (url, "fresh page", None)

    monkeypatch.setattr(mod, "fetch_link_source", fake_fetch)
    refreshed = json.loads(await _read_source(db_session, entity_id=entity_id, user_id=user_id, draft_id=draft.id, source_id="S2", refresh=True))
    assert refreshed["ok"] is True and refreshed["content"] == "fresh page"

    unknown = json.loads(await _read_source(db_session, entity_id=entity_id, user_id=user_id, draft_id=draft.id, source_id="NOPE"))
    assert unknown["ok"] is False and unknown["known"] == ["S1", "S2"]


def test_ws_read_source_is_registered_everywhere():
    from packages.core.ai.runtime.approval_classifier import _READ_ONLY_TOOLS
    from packages.core.ai.runtime.capabilities import CORE_CAPABILITIES
    from packages.core.ai.tools.workspace_arch_tools import ALL_TOOL_SCHEMAS, HANDLERS

    names = {schema["function"]["name"] for schema in ALL_TOOL_SCHEMAS}
    assert names == set(HANDLERS)
    assert "ws_read_source" in names
    assert "ws_read_source" in _READ_ONLY_TOOLS
    assert "ws_read_source" in CORE_CAPABILITIES["workspace.architect"].tool_names


def test_ws_flag_missing_integration_is_not_exposed():
    from packages.core.ai.runtime.capabilities import CORE_CAPABILITIES
    from packages.core.ai.tools.workspace_arch_tools import ALL_TOOL_SCHEMAS, HANDLERS

    name = "ws_flag_missing_integration"
    assert name not in {schema["function"]["name"] for schema in ALL_TOOL_SCHEMAS}
    assert name not in HANDLERS
    assert name not in CORE_CAPABILITIES["workspace.architect"].tool_names


def test_architect_prompt_requires_reading_sources_first():
    from packages.core.services.workspace_architect import ARCHITECT_SYSTEM_PROMPT

    assert "STEP A0." in ARCHITECT_SYSTEM_PROMPT
    assert "ws_read_source" in ARCHITECT_SYSTEM_PROMPT
    assert ARCHITECT_SYSTEM_PROMPT.index("STEP A0.") < ARCHITECT_SYSTEM_PROMPT.index("STEP A. ``ws_commit_basics``")


def test_architect_framing_includes_sources_block():
    from packages.core.services.workspace_architect import _draft_sources_block

    draft = SimpleNamespace(
        fields={"sources": [{"id": "S1", "kind": "document", "name": "n", "status": "ready", "chars": 1, "text": "t", "turn_index": 0}]},
        messages=[],
    )
    block = _draft_sources_block(draft)
    assert block.startswith("<sources>") and "new_this_turn=yes" in block
    assert _draft_sources_block(SimpleNamespace(fields={}, messages=[])) == ""


@pytest.mark.asyncio
async def test_ws_attach_knowledge_validates_source_ids(db_session):
    from packages.core.models.workspace_draft import WorkspaceDraft
    from packages.core.ai.tools.workspace_arch_tools import _attach_knowledge

    entity_id = generate_ulid()
    user_id = generate_ulid()
    draft = WorkspaceDraft(
        entity_id=entity_id, user_id=user_id, messages=[], missing=[], ready=False, status="active",
        fields={"sources": [
            {"id": "S1", "kind": "document", "name": "a", "status": "ready", "chars": 1, "text": "t", "turn_index": 0},
            {"id": "S2", "kind": "link", "name": "u", "status": "failed", "error": "http_404", "chars": 0, "text": "", "turn_index": 0},
        ]},
    )
    db_session.add(draft)
    await db_session.flush()

    bad = json.loads(await _attach_knowledge(db_session, entity_id=entity_id, user_id=user_id, draft_id=draft.id, name="KB", purpose="hold the method", mode="create_new", source_ids=["S1", "S2", "S9"]))
    assert bad["ok"] is False and bad["unknown"] == ["S2", "S9"] and bad["readable"] == ["S1"]

    good = json.loads(await _attach_knowledge(db_session, entity_id=entity_id, user_id=user_id, draft_id=draft.id, name="KB", purpose="hold the method", mode="create_new", source_ids=["S1", "S1"]))
    assert good["ok"] is True
    assert draft.fields["knowledge_attachments"][0]["source_ids"] == ["S1"]


@pytest.mark.asyncio
async def test_lint_flags_unused_ready_sources_as_p2(db_session):
    from packages.core.models.workspace_draft import WorkspaceDraft
    from packages.core.ai.tools.workspace_arch_tools import _lint_draft

    entity_id = generate_ulid()
    user_id = generate_ulid()
    draft = WorkspaceDraft(
        entity_id=entity_id, user_id=user_id, messages=[], missing=[], ready=False, status="active",
        fields={
            "name": "W", "kind": "project", "operating_context": "c", "primary_work": "p",
            "services": [{"service_key": "s1", "name": "S1", "description": "d", "autonomy_level": "assisted", "owner_role": "r"}],
            "agent_mappings": [{"service_key": "s1", "strategy": "create_custom", "create_agent_draft": {
                "agent_name": "A", "system_prompt": "x" * 100, "tool_bindings": ["rag"],
                "missing_skill_specs": [{"name": "m", "system_prompt": "y" * 80, "source_ids": ["S2"]}],
            }}],
            "knowledge_attachments": [{"name": "KB", "purpose": "p", "mode": "create_new", "source_ids": ["S1"]}],
            "sources": [
                {"id": "S1", "kind": "document", "name": "used-by-kb", "status": "ready", "chars": 1, "text": "t", "turn_index": 0},
                {"id": "S2", "kind": "document", "name": "used-by-skill", "status": "ready", "chars": 1, "text": "t", "turn_index": 0},
                {"id": "S3", "kind": "link", "name": "unused", "status": "ready", "chars": 1, "text": "t", "turn_index": 0},
                {"id": "S4", "kind": "link", "name": "failed", "status": "failed", "error": "http_404", "chars": 0, "text": "", "turn_index": 0},
            ],
        },
    )
    db_session.add(draft)
    await db_session.flush()

    result = json.loads(await _lint_draft(db_session, entity_id=entity_id, user_id=user_id, draft_id=draft.id))
    unused = [i for i in result["issues"] if i["where"].startswith("sources.")]
    assert [i["where"] for i in unused] == ["sources.S3"]
    assert unused[0]["severity"] == "P2"


@pytest.mark.asyncio
async def test_request_custom_agent_resolves_skill_source_refs(db_session, monkeypatch):
    from packages.core.models.workspace_draft import WorkspaceDraft
    from packages.core.ai.tools import workspace_arch_tools as arch

    entity_id = generate_ulid()
    user_id = generate_ulid()
    draft = WorkspaceDraft(
        entity_id=entity_id, user_id=user_id, messages=[], missing=[], ready=False, status="active",
        fields={
            "services": [{"service_key": "task_execution", "name": "Exec", "description": "d", "autonomy_level": "assisted", "owner_role": "coach"}],
            "agent_mappings": [],
            "sources": [
                {"id": "S1", "kind": "document", "name": "chapter7.docx", "document_id": "01DOC000000000000000000000", "status": "ready", "chars": 1, "text": "t", "turn_index": 0},
            ],
        },
    )
    db_session.add(draft)
    await db_session.flush()

    common = dict(
        entity_id=entity_id, user_id=user_id, draft_id=draft.id,
        service_key="task_execution", agent_name="Execution Coach",
        system_prompt="You are Execution Coach. " + "x" * 100,
        tool_bindings=["read_file", "invoke_skill"],
    )
    bad = json.loads(await arch._request_custom_agent(
        db_session, **common,
        missing_skill_specs=[{"name": "Work breakdown method", "system_prompt": "y" * 80, "tools": ["read_file"], "source_ids": ["S9"]}],
    ))
    assert bad["ok"] is False and bad["unknown"] == ["S9"]

    good = json.loads(await arch._request_custom_agent(
        db_session, **common,
        missing_skill_specs=[{"name": "Work breakdown method", "system_prompt": "y" * 80, "tools": ["read_file"], "source_ids": ["S1", "S1"]}],
    ))
    assert good["ok"] is True, good
    spec = draft.fields["agent_mappings"][0]["create_agent_draft"]["missing_skill_specs"][0]
    assert spec["source_ids"] == ["S1"]
    assert spec["source_refs"] == [{"source_id": "S1", "kind": "document", "name": "chapter7.docx", "document_id": "01DOC000000000000000000000"}]


def test_architect_prompt_encodes_user_methods_as_role_skills():
    from packages.core.services.workspace_architect import ARCHITECT_SYSTEM_PROMPT

    assert "missing_skill_specs" in ARCHITECT_SYSTEM_PROMPT
    assert "source_ids" in ARCHITECT_SYSTEM_PROMPT
    assert "Split by role" in ARCHITECT_SYSTEM_PROMPT
    assert "Never put the whole text into one skill" in ARCHITECT_SYSTEM_PROMPT


def test_creator_method_dead_reference_is_gone_and_registry_is_consistent():
    from packages.core.ai.runtime.approval_classifier import _FIXED_TOOL_ACTIONS, _READ_ONLY_TOOLS
    from packages.core.ai.runtime.capabilities import CORE_CAPABILITIES
    from packages.core.ai.tools.workspace_arch_tools import ALL_TOOL_SCHEMAS, HANDLERS

    schema_names = {schema["function"]["name"] for schema in ALL_TOOL_SCHEMAS}
    assert "ws_commit_creator_method" not in schema_names
    assert "ws_commit_creator_method" not in CORE_CAPABILITIES["workspace.architect"].tool_names
    assert "ws_commit_creator_method" not in _FIXED_TOOL_ACTIONS
    # every advertised architect capability tool is a real schema with a handler
    assert set(CORE_CAPABILITIES["workspace.architect"].tool_names) <= schema_names
    assert schema_names == set(HANDLERS)
    # and every architect tool has a declared execution effect
    classified = set(_READ_ONLY_TOOLS) | set(_FIXED_TOOL_ACTIONS)
    unclassified = sorted(name for name in schema_names if name not in classified)
    assert unclassified == [], f"architect tools without an execution-effect classification: {unclassified}"


@pytest.mark.asyncio
async def test_request_custom_agent_rejects_unknown_bindings_at_call_time(db_session):
    from packages.core.models.workspace_draft import WorkspaceDraft
    from packages.core.ai.tools.workspace_arch_tools import _request_custom_agent

    entity_id = generate_ulid()
    user_id = generate_ulid()
    draft = WorkspaceDraft(
        entity_id=entity_id, user_id=user_id, messages=[], missing=[], ready=False, status="active",
        fields={"services": [{"service_key": "delivery_review", "name": "Review", "description": "d", "autonomy_level": "supervised", "owner_role": "reviewer"}], "agent_mappings": []},
    )
    db_session.add(draft)
    await db_session.flush()

    result = json.loads(await _request_custom_agent(
        db_session, entity_id=entity_id, user_id=user_id, draft_id=draft.id,
        service_key="delivery_review", agent_name="Reviewer",
        system_prompt="You are Reviewer. " + "x" * 100,
        tool_bindings=["rag", "coding_based_vide"], skill_bindings=["no_such_skill"], mcp_bindings=["no_such_server"],
    ))
    assert result["ok"] is False
    assert result["error"] == "unknown bindings"
    assert result["tools"] == ["coding_based_vide"]
    assert result["skills"] == ["no_such_skill"]
    assert result["mcp"] == ["no_such_server"]
    assert draft.fields["agent_mappings"] == []
