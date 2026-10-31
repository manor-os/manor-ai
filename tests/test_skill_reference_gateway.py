from __future__ import annotations

import hashlib
import json
import os
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from packages.core.services.skill_reference_materials import (
    SkillReferenceError,
    SkillReferenceMaterial,
    build_skill_reference_files,
)


def test_business_skill_invocations_use_the_shared_gateway():
    root = Path(__file__).resolve().parents[1]
    direct_patterns = (
        "from packages.core.services.skill_service import invoke_skill",
        "skill_service.invoke_skill(",
    )
    callers = list((root / "packages" / "core").rglob("*.py"))
    callers += list((root / "apps" / "api").rglob("*.py"))
    assert [
        str(path.relative_to(root)) for path in callers
        if path != root / "packages/core/ai/runtime/skills.py"
        and any(pattern in path.read_text(encoding="utf-8") for pattern in direct_patterns)
    ] == []


@pytest.mark.asyncio
async def test_web_fetch_text_reaches_skill_as_host_observed_partial_source(monkeypatch):
    from packages.core.ai.runtime import skills
    from packages.core.ai.runtime.artifacts import runtime_artifact_tracking_scope
    from packages.core.ai.tools import extended_tools
    from packages.core.services import skill_reference_materials, skill_service, web_fetch

    async def fetch(_url):
        return web_fetch.FetchResult(
            content=b"First page" + b"x" * 1100, content_type="text/plain",
            url="https://example.org/final", status_code=200,
        )

    async def resolve(**_kwargs):
        return []

    observed = {}

    async def invoke(_db, _skill, _entity, input_text, **_kwargs):
        observed["input"] = input_text
        return {"stop_reason": "sandbox_ready", "sandbox_id": "sandbox-one"}

    async def stage(_sandbox_id, files):
        observed["files"] = files

    monkeypatch.setattr(web_fetch, "fetch_url", fetch)
    monkeypatch.setattr(skill_reference_materials, "resolve_skill_reference_materials", resolve)
    monkeypatch.setattr(skill_reference_materials, "stage_skill_reference_files", stage)
    monkeypatch.setattr(skill_service, "invoke_skill", invoke)

    with runtime_artifact_tracking_scope():
        result = json.loads(await extended_tools._web_fetch_handler(
            url="https://example.org/original", max_length=1000,
        ))
        assert result["content"].startswith("First page")
        assert result["chars_returned"] == 1000
        await skills.runtime_invoke_skill(
            object(), "pptx", "entity-one", "Create a deck",
        )

    manifest = json.loads(observed["files"][-1][1])["materials"]
    assert observed["files"][0][1].startswith(b"First page")
    assert len(observed["files"][0][1]) == 1000
    assert manifest[0]["source"].startswith("web_fetch:https://example.org/final#offset=0")
    assert manifest[0]["complete_source"] is False
    assert '"complete": false' in observed["input"]


@pytest.mark.asyncio
async def test_url_in_skill_input_alone_is_not_a_web_source(monkeypatch):
    from packages.core.ai.runtime import skills
    from packages.core.ai.runtime.artifacts import runtime_artifact_tracking_scope
    from packages.core.services import skill_reference_materials, skill_service

    async def resolve(**_kwargs):
        return []

    async def invoke(_db, _skill, _entity, input_text, **_kwargs):
        assert "Verified reference materials" not in input_text
        return {"stop_reason": "completed", "content": "done"}

    monkeypatch.setattr(skill_reference_materials, "resolve_skill_reference_materials", resolve)
    monkeypatch.setattr(skill_service, "invoke_skill", invoke)
    with runtime_artifact_tracking_scope():
        await skills.runtime_invoke_skill(
            object(), "pptx", "entity-one", "Use https://example.org/claimed",
        )


@pytest.mark.asyncio
async def test_web_search_receipt_is_snippets_not_fetched_pages(monkeypatch):
    from packages.core.ai.runtime.artifacts import (
        runtime_artifact_tracking_scope, runtime_current_web_source_slices,
    )
    from packages.core.ai.tools import web_tools

    async def search(_query, _count):
        return json.dumps({"query": "market", "results": [{
            "title": "Example", "url": "https://example.org/story",
            "snippet": "A short result snippet",
        }]})

    monkeypatch.delenv("SEARCH_API_KEY", raising=False)
    monkeypatch.setattr(web_tools, "_duckduckgo_search", search)
    with runtime_artifact_tracking_scope():
        await web_tools._web_search("entity-one", query="market")
        sources = runtime_current_web_source_slices()
    assert len(sources) == 1
    assert sources[0].kind == "web_search_snippets"
    assert sources[0].complete_source is False
    assert "A short result snippet" in sources[0].content


def test_reference_bundle_has_original_bytes_and_provenance():
    original = b"first\x00second"
    files = build_skill_reference_files([
        SkillReferenceMaterial(
            path="/skill/inputs/source-01.pdf", data=original,
            source="document:doc-one", name="source.pdf", mime="application/pdf",
        ),
    ])
    manifest = json.loads(files[-1][1])
    assert files[0] == ("/skill/inputs/source-01.pdf", original)
    assert manifest["materials"][0]["sha256"] == hashlib.sha256(original).hexdigest()
    assert manifest["materials"][0]["source"] == "document:doc-one"
    with pytest.raises(SkillReferenceError, match="reference_invalid_path"):
        build_skill_reference_files([
            SkillReferenceMaterial("/skill/inputs/../escape", b"x", "document:doc-one"),
        ])


def test_web_source_checkpoint_survives_resume_without_promoting_a_url():
    from packages.core.ai.runtime.artifacts import (
        runtime_artifact_tracking_scope,
        runtime_current_web_source_slices,
        runtime_record_web_source_slice,
        runtime_web_source_checkpoint_state,
    )
    from packages.core.ai.runtime.control import RuntimeAgentCheckpoint

    content = "Fetched evidence for a slide"
    with runtime_artifact_tracking_scope():
        runtime_record_web_source_slice(
            url="https://example.org/report", content=content, offset=0,
            source_sha256=hashlib.sha256(content.encode()).hexdigest(),
            complete_source=True,
        )
        checkpoint = RuntimeAgentCheckpoint(
            messages=[], usage={}, rounds=1, tool_calls_made=["web_fetch"],
            pending_tool_call={"id": "call-one", "name": "generate_file"},
            web_source_slices=runtime_web_source_checkpoint_state(),
        ).to_dict()

    restored = RuntimeAgentCheckpoint.from_dict(checkpoint)
    with runtime_artifact_tracking_scope(web_source_slices=restored.web_source_slices):
        sources = runtime_current_web_source_slices()
    assert len(sources) == 1
    assert sources[0].content == content
    assert sources[0].complete_source is True

    checkpoint["web_source_slices"][0]["content"] = "Changed after checkpoint"
    with pytest.raises(ValueError, match="invalid web source checkpoint"):
        with runtime_artifact_tracking_scope(web_source_slices=checkpoint["web_source_slices"]):
            pass


@pytest.mark.asyncio
@pytest.mark.parametrize("skill", ["pptx", "pdf"])
async def test_ordinary_generate_file_office_route_stages_attachment_and_search(monkeypatch, skill):
    from packages.core.ai.runtime.artifacts import (
        runtime_artifact_tracking_scope, runtime_record_web_search_result,
    )
    from packages.core.ai.tools.generate_file import office_common
    from packages.core.services import skill_reference_materials, skill_service
    from packages.core import database

    @asynccontextmanager
    async def session():
        yield object()

    async def list_skills(_db, _entity):
        return []

    observed = {}

    async def resolve(**kwargs):
        observed.setdefault("resolve_kwargs", kwargs)
        return [SkillReferenceMaterial(
            "/skill/inputs/source-01.pdf", b"%PDF-original-reference",
            "chat_upload:uploads/brief.pdf", "brief.pdf", "application/pdf",
        )]

    async def invoke(_db, skill, _entity, input_text, **kwargs):
        observed["skill"] = skill
        observed["input"] = input_text
        observed["kwargs"] = kwargs
        return {"stop_reason": "sandbox_ready", "sandbox_id": "sandbox-one"}

    async def stage(_sandbox_id, files):
        observed["files"] = files

    monkeypatch.setattr(database, "async_session", session)
    monkeypatch.setattr(skill_service, "list_skills", list_skills)
    monkeypatch.setattr(skill_service, "invoke_skill", invoke)
    monkeypatch.setattr(skill_reference_materials, "resolve_skill_reference_materials", resolve)
    monkeypatch.setattr(skill_reference_materials, "stage_skill_reference_files", stage)
    context = SimpleNamespace(
        runtime_artifact_urls=frozenset({"/api/v1/fs/entity-one/media/run-chart.png"}),
        dependency_artifact_urls=frozenset({"/api/v1/fs/entity-one/media/upstream.wav"}),
        active_user_message="Use my brief and today's market evidence",
        runtime_envelope=SimpleNamespace(
            metadata={}, discovered_tool_grants=SimpleNamespace(grant=lambda _names: None),
        ),
    )
    with runtime_artifact_tracking_scope():
        runtime_record_web_search_result("market today", json.dumps({"results": [{
            "title": "Market report", "url": "https://example.org/market",
            "snippet": "The market moved today",
        }]}))
        result = await office_common._invoke_builtin_skill(
            skill=skill, prompt="Create a market report",
            entity_id="entity-one", user_id="user-one", conversation_id="chat-one",
            runtime_context=context,
        )
    manifest = json.loads(observed["files"][-1][1])["materials"]
    assert observed["skill"] == skill
    assert observed["files"][0][1] == b"%PDF-original-reference"
    assert b"The market moved today" in observed["files"][1][1]
    assert manifest[0]["source"] == "chat_upload:uploads/brief.pdf"
    assert manifest[1]["source"].startswith("web_search_snippets:market today")
    assert manifest[1]["complete_source"] is False
    assert "sandbox" in json.loads(result)["loaded_tools"]
    # The run's own and dependency artifacts are host-collected context:
    # optional references, never required ones.
    assert sorted(observed["resolve_kwargs"]["context_artifact_urls"]) == [
        "/api/v1/fs/entity-one/media/run-chart.png", "/api/v1/fs/entity-one/media/upstream.wav",
    ]
    assert not observed["resolve_kwargs"]["artifact_urls"]


@pytest.mark.asyncio
async def test_reference_stage_requires_sandbox_write_ack(monkeypatch):
    from packages.core.ai.tools import sandbox_tools
    from packages.core.services.skill_reference_materials import stage_skill_reference_files

    class Client:
        async def write_file_base64(self, _sandbox_id, path, _content):
            return SimpleNamespace(path=path, written=False)

        async def close(self):
            pass

    async def get_client(_sandbox_id):
        return Client()

    monkeypatch.setattr(sandbox_tools, "_get_client_for_sandbox", get_client)
    with pytest.raises(SkillReferenceError, match="reference_stage_failed"):
        await stage_skill_reference_files("sandbox-one", [("/skill/inputs/source.txt", b"text")])


@pytest.mark.asyncio
async def test_reference_resolver_rechecks_file_acl_before_open(monkeypatch):
    from packages.core.ai.runtime import file_actions
    from packages.core.services.skill_reference_materials import resolve_skill_reference_materials

    async def deny(**_kwargs):
        return '{"error":"file_permission_denied"}'

    def must_not_open(*_args, **_kwargs):
        raise AssertionError("denied bytes must never be opened")

    monkeypatch.setattr(file_actions, "runtime_guard_file_read_access", deny)
    monkeypatch.setattr(file_actions, "runtime_open_entity_file_snapshot", must_not_open)
    envelope = SimpleNamespace(metadata={
        "runtime_attachment_context": {"refs": [{"kind": "chat_upload", "path": "uploads/chat/a.txt"}]},
    })
    with pytest.raises(SkillReferenceError, match="reference_forbidden"):
        await resolve_skill_reference_materials(
            db=object(), entity_id="entity-one", user_id="user-one",
            conversation_id="chat-one", workspace_id=None,
            runtime_envelope=envelope,
        )


@pytest.mark.asyncio
async def test_reference_resolver_skips_host_internal_run_artifacts(monkeypatch, tmp_path):
    """A video job's staged first frame is cited in its result but is not a
    user file; it must not turn every later Skill call into reference_forbidden."""
    from packages.core.ai.runtime import file_actions
    from packages.core.services.skill_reference_materials import resolve_skill_reference_materials

    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"mp4")
    checked = []

    async def guard(**kwargs):
        checked.extend(kwargs["paths"])
        return None

    @contextmanager
    def snapshot(_entity_id, _path):
        descriptor = os.open(clip, os.O_RDONLY)
        try:
            yield SimpleNamespace(file_descriptor=descriptor, stat=os.fstat(descriptor))
        finally:
            os.close(descriptor)

    monkeypatch.setattr(file_actions, "runtime_guard_file_read_access", guard)
    monkeypatch.setattr(file_actions, "runtime_open_entity_file_snapshot", snapshot)
    materials = await resolve_skill_reference_materials(
        db=object(), entity_id="entity-one", user_id="user-one",
        conversation_id="chat-one", workspace_id=None, runtime_envelope=None,
        artifact_urls=[
            "/api/v1/fs/entity-one/uploads/media-references/job-1/00-first-frame-a3cbebc675.png",
            "/api/v1/fs/entity-one/Videos/clip.mp4",
        ],
    )

    assert checked == ["Videos/clip.mp4"]
    # References are metadata only; bytes are read at projection/staging.
    assert [(material.file_path, material.size, material.sha256) for material in materials] == [
        ("Videos/clip.mp4", 3, hashlib.sha256(b"mp4").hexdigest()),
    ]


@pytest.mark.asyncio
async def test_reference_resolver_carries_current_attachment_original_bytes(monkeypatch, tmp_path):
    from packages.core.ai.runtime import file_actions
    from packages.core.services.skill_reference_materials import resolve_skill_reference_materials

    original = b"source\x00bytes"
    source_path = tmp_path / "report.pdf"
    source_path.write_bytes(original)

    async def allow(**_kwargs):
        return None

    @contextmanager
    def snapshot(_entity_id, _path):
        descriptor = os.open(source_path, os.O_RDONLY)
        try:
            yield SimpleNamespace(file_descriptor=descriptor, stat=os.fstat(descriptor))
        finally:
            os.close(descriptor)

    monkeypatch.setattr(file_actions, "runtime_guard_file_read_access", allow)
    monkeypatch.setattr(file_actions, "runtime_open_entity_file_snapshot", snapshot)
    envelope = SimpleNamespace(metadata={
        "runtime_attachment_context": {"refs": [{
            "kind": "chat_upload", "path": "uploads/chat/report.pdf",
            "name": "report.pdf", "mime": "application/pdf",
        }]},
    })
    materials = await resolve_skill_reference_materials(
        db=object(), entity_id="entity-one", user_id="user-one",
        conversation_id="chat-one", workspace_id=None,
        runtime_envelope=envelope,
    )
    # Resolution carries metadata only; the original bytes are read at staging.
    assert materials[0].data is None
    assert materials[0].size == len(original)
    assert materials[0].sha256 == hashlib.sha256(original).hexdigest()
    assert materials[0].name == "report.pdf"
    assert materials[0].source == "chat_upload:uploads/chat/report.pdf"
    assert build_skill_reference_files(materials)[0] == ("/skill/inputs/source-01.pdf", original)


@pytest.mark.asyncio
async def test_reference_resolver_prioritizes_current_over_bounded_history(monkeypatch, tmp_path):
    from packages.core.ai.runtime import file_actions
    from packages.core.services import skill_reference_materials

    monkeypatch.setattr(skill_reference_materials, "MAX_SKILL_REFERENCE_BYTES", 30)
    monkeypatch.setattr(skill_reference_materials, "MAX_SKILL_HISTORY_REFERENCE_BYTES", 6)
    for name in ("current.txt", "recent.txt", "old.txt"):
        (tmp_path / name).write_bytes(b"source")

    async def allow(**_kwargs):
        return None

    @contextmanager
    def snapshot(_entity_id, path):
        descriptor = os.open(tmp_path / path, os.O_RDONLY)
        try:
            yield SimpleNamespace(file_descriptor=descriptor, stat=os.fstat(descriptor))
        finally:
            os.close(descriptor)

    monkeypatch.setattr(file_actions, "runtime_guard_file_read_access", allow)
    monkeypatch.setattr(file_actions, "runtime_open_entity_file_snapshot", snapshot)
    envelope = SimpleNamespace(metadata={
        "runtime_attachment_context": {"refs": [{"path": "current.txt"}]},
        "runtime_history_attachment_refs": [
            {"path": "recent.txt"}, {"path": "old.txt"}, {"path": "missing.txt"},
        ],
    })
    materials = await skill_reference_materials.resolve_skill_reference_materials(
        db=object(), entity_id="entity-one", user_id="user-one",
        conversation_id="chat-one", workspace_id=None,
        runtime_envelope=envelope,
    )
    # Every authorized reference is listed; the byte caps bound what is staged.
    assert [material.name for material in materials] == ["current.txt", "recent.txt", "old.txt"]
    assert [material.required for material in materials] == [True, False, False]
    files = skill_reference_materials.build_skill_reference_files(materials)
    assert [path for path, _ in files] == [
        "/skill/inputs/source-01.txt", "/skill/inputs/source-02.txt", "/skill/inputs/manifest.json",
    ]
    manifest = json.loads(files[-1][1])["materials"]
    assert [entry["staged"] for entry in manifest] == [True, True, False]
    assert manifest[2]["not_staged_reason"] == "staging_byte_limit"


@pytest.mark.asyncio
async def test_runtime_skill_gateway_stages_authorized_originals_before_handoff(monkeypatch):
    from packages.core.ai.runtime import skills
    from packages.core.services import skill_reference_materials, skill_service

    material = SkillReferenceMaterial(
        "/skill/inputs/source-01.txt", b"verified source text",
        "document:doc-one", "source.txt", "text/plain",
    )
    observed = {}

    async def resolve(**_kwargs):
        return [material]

    async def invoke(_db, _skill, _entity, input_text, **_kwargs):
        observed["input"] = input_text
        return {"stop_reason": "sandbox_ready", "sandbox_id": "sandbox-one", "content": "Run"}

    async def stage(sandbox_id, files):
        observed["sandbox_id"] = sandbox_id
        observed["files"] = files

    monkeypatch.setattr(skill_reference_materials, "resolve_skill_reference_materials", resolve)
    monkeypatch.setattr(skill_reference_materials, "stage_skill_reference_files", stage)
    monkeypatch.setattr(skill_service, "invoke_skill", invoke)

    result = await skills.runtime_invoke_skill(
        object(), "pptx", "entity-one", "Create slides",
        user_id="user-one", runtime_envelope=SimpleNamespace(metadata={}),
    )
    assert result["stop_reason"] == "sandbox_ready"
    assert observed["sandbox_id"] == "sandbox-one"
    assert observed["files"][0][1] == b"verified source text"
    assert "document:doc-one" in observed["input"]
    assert "/skill/inputs/manifest.json" in result["content"]


@pytest.mark.asyncio
async def test_runtime_skill_gateway_blocks_unavailable_reference_before_skill(monkeypatch):
    from packages.core.ai.runtime import skills
    from packages.core.services import skill_reference_materials, skill_service

    async def resolve(**_kwargs):
        raise SkillReferenceError("reference_forbidden")

    async def invoke(*_args, **_kwargs):
        raise AssertionError("an unavailable reference cannot reach the Skill")

    monkeypatch.setattr(skill_reference_materials, "resolve_skill_reference_materials", resolve)
    monkeypatch.setattr(skill_service, "invoke_skill", invoke)
    result = await skills.runtime_invoke_skill(object(), "pptx", "entity-one", "Create slides")
    assert result["code"] == "reference_forbidden"


@pytest.mark.asyncio
async def test_direct_skill_api_rejects_sandbox_without_an_agent_host(monkeypatch):
    from fastapi import HTTPException
    from apps.api.routers import skills as skills_router

    async def require_skill(*_args, **_kwargs):
        return SimpleNamespace(config={"type": "sandbox"}, tools=[])

    async def invoke(*_args, **_kwargs):
        raise AssertionError("direct API must not allocate an undriven sandbox")

    monkeypatch.setattr(skills_router, "_require_skill", require_skill)
    monkeypatch.setattr(skills_router, "runtime_invoke_skill", invoke)
    with pytest.raises(HTTPException) as exc:
        await skills_router.invoke_skill_endpoint(
            "skill-one", skills_router.InvokeRequest(input="Create a deck"),
            SimpleNamespace(id="user-one", entity_id="entity-one"), object(),
        )
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_direct_prompt_skill_api_passes_typed_document_references(monkeypatch):
    from apps.api.routers import skills as skills_router

    observed = {}

    async def require_skill(*_args, **_kwargs):
        return SimpleNamespace(config={"type": "prompt"}, tools=[])

    async def invoke(_db, _skill_id, _entity_id, _input_text, **kwargs):
        observed.update(kwargs)
        return {"skill": "prompt-one", "content": "done", "stop_reason": "completed"}

    monkeypatch.setattr(skills_router, "_require_skill", require_skill)
    monkeypatch.setattr(skills_router, "runtime_invoke_skill", invoke)
    result = await skills_router.invoke_skill_endpoint(
        "prompt-one",
        skills_router.InvokeRequest(input="Summarize", reference_document_ids=["doc-one"]),
        SimpleNamespace(id="user-one", entity_id="entity-one"), object(),
    )
    assert result["stop_reason"] == "completed"
    assert observed["reference_document_ids"] == ["doc-one"]
