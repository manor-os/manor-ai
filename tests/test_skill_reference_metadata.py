"""Skill references are metadata until a Sandbox Skill stages their bytes.

Regression for 2026-10-11: a 51-85 MB WAV produced by normalize_audio_loudness
and referenced by a Prompt Skill failed every invoke_skill with a generic
``reference_too_large`` because all reference bytes were loaded (and capped)
before the Skill type was known.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from packages.core.services import skill_reference_materials
from packages.core.services.skill_reference_materials import (
    MAX_SKILL_REFERENCE_BYTES,
    MAX_SKILL_REFERENCE_COUNT,
    SkillReferenceError,
    SkillReferenceMaterial,
    build_skill_reference_files,
    skill_reference_manifest,
)

LARGE_SIZE = MAX_SKILL_REFERENCE_BYTES + 1024 * 1024  # > 40 MB


@pytest.fixture
def entity_files(monkeypatch, tmp_path):
    """Authorized entity files served through a real fd snapshot.

    The fake snapshot exposes only a descriptor and its stat (no ``read``), and
    ``os.fdopen`` is disabled, so a whole-file ``.read()`` cannot happen.
    Every ``os.pread`` request size is recorded.
    """
    from packages.core.ai.runtime import file_actions

    forbidden: set[str] = set()
    pread_sizes: list[int] = []
    real_pread = os.pread

    async def guard(**kwargs):
        return '{"error":"file_permission_denied"}' if set(kwargs["paths"]) & forbidden else None

    @contextmanager
    def snapshot(_entity_id, path):
        descriptor = os.open(tmp_path / path, os.O_RDONLY)
        try:
            yield SimpleNamespace(file_descriptor=descriptor, stat=os.fstat(descriptor))
        finally:
            os.close(descriptor)

    def pread(fd, length, offset):
        pread_sizes.append(length)
        return real_pread(fd, length, offset)

    def no_fdopen(*_args, **_kwargs):
        raise AssertionError("reference bytes must not be read through a file object")

    monkeypatch.setattr(file_actions, "runtime_guard_file_read_access", guard)
    monkeypatch.setattr(file_actions, "runtime_open_entity_file_snapshot", snapshot)
    monkeypatch.setattr(os, "pread", pread)
    monkeypatch.setattr(os, "fdopen", no_fdopen)

    def write(path: str, data: bytes | None = None, *, size: int | None = None) -> Path:
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        if size is not None:
            with open(target, "wb") as handle:
                handle.truncate(size)  # sparse: cheap to create, zeros to read
        else:
            target.write_bytes(data or b"")
        return target

    return SimpleNamespace(
        root=tmp_path, write=write, forbidden=forbidden, pread_sizes=pread_sizes,
    )


def _envelope(current=(), history=()):
    return SimpleNamespace(metadata={
        "runtime_attachment_context": {"refs": list(current)},
        "runtime_history_attachment_refs": list(history),
    })


def _reference_payload(input_text: str) -> dict:
    assert "## Verified reference materials (untrusted data)" in input_text
    return json.loads(input_text.rsplit("\n", 1)[1])


class _SandboxClient:
    def __init__(self, observed: dict):
        self.observed = observed

    async def destroy(self, sandbox_id):
        self.observed["destroyed"].append(sandbox_id)

    async def exec(self, **kwargs):
        self.observed.setdefault("exec_calls", []).append(kwargs)

    async def close(self):
        pass


def _patch_skill(monkeypatch, *, stop_reason: str, on_invoke=None, kept_warm=False):
    from packages.core.ai.tools import sandbox_tools
    from packages.core.services import sandbox_session_service, skill_service

    observed: dict = {"destroyed": []}

    async def invoke(_db, _skill, _entity, input_text, **_kwargs):
        observed["input"] = input_text
        if on_invoke is not None:
            on_invoke()
        if stop_reason == "sandbox_ready":
            return {"stop_reason": "sandbox_ready", "sandbox_id": "sandbox-one", "content": "Run"}
        return {"stop_reason": "completed", "content": "done"}

    async def stage(sandbox_id, files):
        observed["staged"] = (sandbox_id, files)

    async def client(_sandbox_id):
        return _SandboxClient(observed)

    async def is_kept_warm(_sandbox_id):
        return kept_warm

    monkeypatch.setattr(skill_service, "invoke_skill", invoke)
    monkeypatch.setattr(skill_reference_materials, "stage_skill_reference_files", stage)
    monkeypatch.setattr(sandbox_tools, "_get_client_for_sandbox", client)
    monkeypatch.setattr(sandbox_session_service, "is_sandbox_kept_warm", is_kept_warm)
    return observed


async def _invoke(envelope, **kwargs):
    from packages.core.ai.runtime import skills

    return await skills.runtime_invoke_skill(
        object(), "video-edit", "entity-one", "Cut the video to the music",
        user_id="user-one", runtime_envelope=envelope, **kwargs,
    )


def _fs_url(path: str) -> str:
    from packages.core.services.generated_file_refs import entity_fs_open_url

    return entity_fs_open_url("entity-one", path)


@pytest.mark.asyncio
async def test_prompt_skill_lists_large_binary_reference_without_reading_it(
    monkeypatch, entity_files,
):
    entity_files.write("media/track.wav", size=LARGE_SIZE)
    entity_files.write("notes/brief.txt", "Cut on the downbeat.".encode())

    def no_staging(_material):
        raise AssertionError("a Prompt Skill must never load reference bytes")

    monkeypatch.setattr(skill_reference_materials, "_read_staged_bytes", no_staging)
    observed = _patch_skill(monkeypatch, stop_reason="completed")

    result = await _invoke(_envelope(current=[
        {"kind": "runtime_artifact", "path": "media/track.wav", "name": "track.wav",
         "mime": "audio/wav", "document_id": None},
        {"kind": "chat_upload", "path": "notes/brief.txt", "name": "brief.txt"},
    ]))

    assert result == {"stop_reason": "completed", "content": "done"}
    payload = _reference_payload(observed["input"])
    wav, brief = payload["manifest"]
    assert wav["file_path"] == "media/track.wav"
    assert wav["mime"] == "audio/wav"
    assert wav["size"] == LARGE_SIZE
    assert wav["sha256"] == hashlib.sha256(bytes(LARGE_SIZE)).hexdigest()
    assert wav["required"] is True
    assert brief["mime"] == "text/plain"
    # Text references are still projected; the binary has no text entry.
    assert payload["text"] == [{
        "source": "chat_upload:notes/brief.txt", "text": "Cut on the downbeat.", "complete": True,
    }]
    assert "probe_media" in observed["input"]
    # The large file was streamed in bounded chunks, never read whole.
    assert entity_files.pread_sizes
    assert max(entity_files.pread_sizes) <= 1024 * 1024
    assert "staged" not in observed


@pytest.mark.asyncio
async def test_sandbox_skill_names_the_oversized_required_reference(monkeypatch, entity_files):
    entity_files.write("media/track.wav", size=LARGE_SIZE)
    observed = _patch_skill(monkeypatch, stop_reason="sandbox_ready")

    result = await _invoke(_envelope(current=[
        {"kind": "runtime_artifact", "path": "media/track.wav", "name": "track.wav"},
    ]))

    assert result["code"] == "reference_too_large"
    assert result["reference"]["name"] == "track.wav"
    assert result["reference"]["path"] == "media/track.wav"
    assert result["reference"]["size"] == LARGE_SIZE
    assert result["reference"]["limit"] == MAX_SKILL_REFERENCE_BYTES
    assert "'track.wav'" in result["error"]
    assert f"{LARGE_SIZE:,} bytes" in result["error"]
    assert f"{MAX_SKILL_REFERENCE_BYTES:,} bytes" in result["error"]
    assert "staged" not in observed
    assert observed["destroyed"] == ["sandbox-one"]
    assert max(entity_files.pread_sizes) <= 1024 * 1024


@pytest.mark.asyncio
async def test_sandbox_reference_failure_clears_warm_skill_inputs_without_destroying(monkeypatch, entity_files):
    entity_files.write("media/track.wav", size=LARGE_SIZE)
    observed = _patch_skill(monkeypatch, stop_reason="sandbox_ready", kept_warm=True)

    result = await _invoke(_envelope(current=[
        {"kind": "runtime_artifact", "path": "media/track.wav", "name": "track.wav"},
    ]))

    assert result["code"] == "reference_too_large"
    assert observed["destroyed"] == []
    assert observed["exec_calls"] == [{
        "sandbox_id": "sandbox-one",
        "command": "rm -rf /skill/inputs",
        "timeout": 30,
        "queue_timeout": 120,
    }]


@pytest.mark.asyncio
async def test_sandbox_skill_stages_byte_identical_reference(monkeypatch, entity_files):
    original = b"RIFF\x00\x01binary\xffaudio"
    entity_files.write("media/clip.wav", original)
    observed = _patch_skill(monkeypatch, stop_reason="sandbox_ready")

    result = await _invoke(_envelope(current=[
        {"kind": "runtime_artifact", "path": "media/clip.wav", "name": "clip.wav"},
    ]))

    assert result["stop_reason"] == "sandbox_ready"
    sandbox_id, files = observed["staged"]
    assert sandbox_id == "sandbox-one"
    assert files[0] == ("/skill/inputs/source-01.wav", original)
    manifest = json.loads(files[-1][1])["materials"]
    assert manifest[0]["sha256"] == hashlib.sha256(original).hexdigest()
    assert manifest[0]["staged"] is True
    assert observed["destroyed"] == []


@pytest.mark.asyncio
async def test_sandbox_revalidation_catches_a_file_changed_after_admission(
    monkeypatch, entity_files,
):
    source = entity_files.write("media/clip.wav", b"original bytes")
    observed = _patch_skill(
        monkeypatch, stop_reason="sandbox_ready",
        on_invoke=lambda: source.write_bytes(b"replaced bytes"),
    )

    result = await _invoke(_envelope(current=[
        {"kind": "runtime_artifact", "path": "media/clip.wav", "name": "clip.wav"},
    ]))

    assert result["code"] == "reference_changed"
    assert result["reference"]["name"] == "clip.wav"
    assert "'clip.wav'" in result["error"]
    assert "staged" not in observed
    assert observed["destroyed"] == ["sandbox-one"]


def test_staging_rejects_bytes_that_differ_from_the_authorized_hash(entity_files):
    entity_files.write("media/clip.wav", b"current bytes")
    material = SkillReferenceMaterial(
        "/skill/inputs/source-01.wav", None, "runtime_artifact:media/clip.wav",
        name="clip.wav", size=len(b"current bytes"),
        sha256=hashlib.sha256(b"earlier bytes").hexdigest(),
        entity_id="entity-one", file_path="media/clip.wav",
    )
    with pytest.raises(SkillReferenceError, match="reference_changed") as exc:
        build_skill_reference_files([material])
    assert exc.value.name == "clip.wav"


@pytest.mark.asyncio
@pytest.mark.parametrize("stop_reason", ["completed", "sandbox_ready"])
async def test_history_references_never_fail_a_call(monkeypatch, entity_files, stop_reason):
    entity_files.write("current.txt", b"current")
    entity_files.write("history/old-render.mp4", size=LARGE_SIZE)
    entity_files.write("history/secret.txt", b"secret")
    entity_files.forbidden.add("history/secret.txt")
    observed = _patch_skill(monkeypatch, stop_reason=stop_reason)

    result = await _invoke(_envelope(
        current=[{"kind": "chat_upload", "path": "current.txt"}],
        history=[
            {"kind": "chat_upload", "path": "history/old-render.mp4"},
            {"kind": "chat_upload", "path": "history/missing.wav"},
            {"kind": "chat_upload", "path": "history/secret.txt"},
            {"kind": "chat_upload", "path": "   "},
        ],
    ))

    assert "error" not in result
    manifest = _reference_payload(observed["input"])["manifest"]
    assert [entry["file_path"] for entry in manifest] == ["current.txt", "history/old-render.mp4"]
    history = manifest[1]
    assert history["required"] is False
    assert history["size"] == LARGE_SIZE
    # Too large to ever stage, so it is not even hashed.
    assert history["sha256"] is None
    # Not hashed means not read at all.
    assert sum(entity_files.pread_sizes) < 1024 * 1024
    if stop_reason == "sandbox_ready":
        _sandbox_id, files = observed["staged"]
        staged_manifest = json.loads(files[-1][1])["materials"]
        assert [entry["staged"] for entry in staged_manifest] == [True, False]
        assert [path for path, _ in files] == [
            "/skill/inputs/source-01.txt", "/skill/inputs/manifest.json",
        ]


def test_history_reference_changed_before_staging_is_skipped_not_fatal(entity_files):
    entity_files.write("history/note.txt", b"new")
    material = SkillReferenceMaterial(
        "/skill/inputs/source-01.txt", None, "chat_upload:history/note.txt",
        name="note.txt", required=False, size=3,
        sha256=hashlib.sha256(b"old").hexdigest(),
        entity_id="entity-one", file_path="history/note.txt",
    )
    files = build_skill_reference_files([material])
    manifest = json.loads(files[-1][1])["materials"]
    assert [path for path, _ in files] == ["/skill/inputs/manifest.json"]
    assert manifest[0]["staged"] is False
    assert manifest[0]["not_staged_reason"] == "reference_changed"


@pytest.mark.asyncio
async def test_required_reference_errors_name_the_file(monkeypatch, entity_files):
    entity_files.write("private/plan.pdf", b"%PDF")
    entity_files.forbidden.add("private/plan.pdf")
    _patch_skill(monkeypatch, stop_reason="completed")

    forbidden = await _invoke(_envelope(current=[
        {"kind": "chat_upload", "path": "private/plan.pdf", "name": "plan.pdf"},
    ]))
    missing = await _invoke(_envelope(current=[
        {"kind": "chat_upload", "path": "media/gone.wav", "name": "gone.wav"},
    ]))

    assert forbidden["code"] == "reference_forbidden"
    assert forbidden["reference"] == {
        "name": "plan.pdf", "path": "private/plan.pdf", "reason": "is not readable by this run",
    }
    assert "'plan.pdf'" in forbidden["error"]
    assert missing["code"] == "reference_missing"
    assert missing["reference"]["name"] == "gone.wav"
    assert "'gone.wav' (media/gone.wav)" in missing["error"]


# ---------------------------------------------------------------------------
# Host-collected run context is optional; explicit references are required.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sandbox_skill_after_a_large_run_artifact_succeeds_and_lists_it(
    monkeypatch, entity_files,
):
    entity_files.write("brief.txt", b"Make the cut")
    entity_files.write("media/track.wav", size=LARGE_SIZE)
    entity_files.write("media/cover.png", b"\x89PNG small")
    observed = _patch_skill(monkeypatch, stop_reason="sandbox_ready")

    result = await _invoke(
        _envelope(current=[{"kind": "chat_upload", "path": "brief.txt"}]),
        context_artifact_urls=[_fs_url("media/track.wav"), _fs_url("media/cover.png")],
    )

    assert "error" not in result
    assert result["stop_reason"] == "sandbox_ready"
    _sandbox_id, files = observed["staged"]
    manifest = {entry["file_path"]: entry for entry in json.loads(files[-1][1])["materials"]}
    track = manifest["media/track.wav"]
    assert track["required"] is False
    assert track["size"] == LARGE_SIZE
    assert track["staged"] is False
    assert track["not_staged_reason"] == "staging_byte_limit"
    # Optional and too large to ever stage: listed, never hashed or read.
    assert track["sha256"] is None
    assert sum(entity_files.pread_sizes) < 1024 * 1024
    assert manifest["media/cover.png"]["staged"] is True
    assert manifest["brief.txt"]["required"] is True
    assert [path for path, _ in files] == [
        "/skill/inputs/source-01.txt", "/skill/inputs/source-03.png", "/skill/inputs/manifest.json",
    ]
    assert observed["destroyed"] == []


@pytest.mark.asyncio
async def test_explicit_large_artifact_reference_still_fails_with_the_file_named(
    monkeypatch, entity_files,
):
    entity_files.write("media/track.wav", size=LARGE_SIZE)
    observed = _patch_skill(monkeypatch, stop_reason="sandbox_ready")

    result = await _invoke(_envelope(), reference_artifact_urls=[_fs_url("media/track.wav")])

    assert result["code"] == "reference_too_large"
    assert result["reference"]["name"] == "track.wav"
    assert result["reference"]["size"] == LARGE_SIZE
    assert result["reference"]["limit"] == MAX_SKILL_REFERENCE_BYTES
    assert "'track.wav'" in result["error"]
    assert "staged" not in observed
    assert observed["destroyed"] == ["sandbox-one"]


@pytest.mark.asyncio
async def test_unavailable_run_artifacts_never_fail_a_call(monkeypatch, entity_files):
    entity_files.write("media/secret.wav", b"secret")
    entity_files.forbidden.add("media/secret.wav")
    observed = _patch_skill(monkeypatch, stop_reason="completed")

    result = await _invoke(
        _envelope(),
        context_artifact_urls=[_fs_url("media/secret.wav"), _fs_url("media/deleted.wav")],
    )

    assert result == {"stop_reason": "completed", "content": "done"}
    assert "Verified reference materials" not in observed["input"]


@pytest.mark.asyncio
async def test_run_artifact_changed_after_admission_is_dropped_not_failed(
    monkeypatch, entity_files,
):
    entity_files.write("brief.txt", b"Make the cut")
    artifact = entity_files.write("media/cover.png", b"first render")
    observed = _patch_skill(
        monkeypatch, stop_reason="sandbox_ready",
        on_invoke=lambda: artifact.write_bytes(b"second render, longer"),
    )

    result = await _invoke(
        _envelope(current=[{"kind": "chat_upload", "path": "brief.txt"}]),
        context_artifact_urls=[_fs_url("media/cover.png")],
    )

    assert "error" not in result
    _sandbox_id, files = observed["staged"]
    manifest = json.loads(files[-1][1])["materials"]
    assert [entry["staged"] for entry in manifest] == [True, False]
    assert manifest[1]["not_staged_reason"] == "reference_changed"
    assert [path for path, _ in files] == [
        "/skill/inputs/source-01.txt", "/skill/inputs/manifest.json",
    ]


# ---------------------------------------------------------------------------
# The reference count is a budget like the byte size: required first, then
# run context, then history. Only required references can exceed it.
# Regression for 2026-10-13: a video project's continuation stage failed with
# ``reference_too_large`` (55 references against 50), all host-collected.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_optional_references_beyond_the_count_cap_never_fail_a_run(
    monkeypatch, entity_files,
):
    from packages.core.ai.runtime import artifacts

    current = [f"brief-{index}.txt" for index in range(2)]
    project = [f"project/frame-{index:02d}.png" for index in range(40)]
    history = [f"history/take-{index}.wav" for index in range(8)]  # newest first
    for path in (*current, *project, *history):
        entity_files.write(path, path.encode())
    web = [
        artifacts.RuntimeWebSourceSlice(
            url=f"https://example.org/{index}", content=f"page {index}", offset=0,
            source_sha256=hashlib.sha256(b"page").hexdigest(), complete_source=True,
        )
        for index in range(5)
    ]
    monkeypatch.setattr(artifacts, "runtime_current_web_source_slices", lambda: tuple(web))
    observed = _patch_skill(monkeypatch, stop_reason="sandbox_ready")

    result = await _invoke(
        _envelope(
            current=[{"kind": "chat_upload", "path": path} for path in current],
            history=[{"kind": "chat_upload", "path": path} for path in history],
        ),
        context_artifact_urls=[_fs_url(path) for path in project],
    )

    assert "error" not in result
    listed = _reference_payload(observed["input"])["manifest"]
    _sandbox_id, files = observed["staged"]
    staged = json.loads(files[-1][1])["materials"]
    # 55 references: every required one, all run context (artifacts and web
    # observations), and the newest history fill the 50; the oldest history
    # is left out of both the listing and the staging.
    expected = [*current, *project, *history[:3]]
    assert [entry.get("file_path") for entry in listed if entry.get("file_path")] == expected
    assert sum(entry["source"].startswith("web_fetch:") for entry in listed) == 5
    assert len(listed) == len(staged) == MAX_SKILL_REFERENCE_COUNT
    assert [entry["source"] for entry in staged] == [entry["source"] for entry in listed]
    assert all(entry["staged"] for entry in staged)
    assert len(files) == MAX_SKILL_REFERENCE_COUNT + 1
    assert observed["destroyed"] == []


def test_required_references_beyond_the_count_cap_fail_with_the_count():
    def material(index, *, required=True):
        return SkillReferenceMaterial(
            f"/skill/inputs/source-{index:02d}.txt", f"text {index}".encode(),
            f"chat_upload:{index}", name=f"{index}.txt", required=required,
        )

    within = [material(index) for index in range(MAX_SKILL_REFERENCE_COUNT)]
    optional = [material(index, required=False) for index in range(50, 60)]
    # A full required set plus optional overflow: the optional part is dropped.
    files = build_skill_reference_files([*optional[:5], *within, *optional[5:]])
    staged = json.loads(files[-1][1])["materials"]
    assert [entry["source"] for entry in staged] == [item.source for item in within]
    assert skill_reference_manifest([*within, *optional]) == skill_reference_manifest(within)

    over = [*within, material(MAX_SKILL_REFERENCE_COUNT)]
    for build in (build_skill_reference_files, skill_reference_manifest):
        with pytest.raises(SkillReferenceError, match="reference_too_large") as exc:
            build(over)
        assert f"{MAX_SKILL_REFERENCE_COUNT + 1} required references" in exc.value.message
        assert f"the {MAX_SKILL_REFERENCE_COUNT} references" in exc.value.message


def test_host_context_artifact_order_is_deterministic():
    from packages.core.services.skill_reference_materials import skill_context_artifact_urls

    own = {"b", "a", "shared"}
    dependency = frozenset({"z", "shared", "c"})
    assert skill_context_artifact_urls(own, dependency) == ("a", "b", "shared", "c", "z")
    assert skill_context_artifact_urls(None, None) == ()


def test_revalidation_fails_only_for_a_changed_required_reference():
    from dataclasses import replace

    from packages.core.services.skill_reference_materials import (
        revalidate_skill_reference_materials,
    )

    def material(path, required, data):
        return SkillReferenceMaterial(
            f"/skill/inputs/{path}", None, f"runtime_artifact:{path}", name=path,
            required=required, size=len(data), sha256=hashlib.sha256(data).hexdigest(),
            entity_id="entity-one", file_path=path,
        )

    required = material("a.txt", True, b"a")
    optional = material("b.txt", False, b"b")
    changed_optional = replace(optional, sha256=hashlib.sha256(b"B").hexdigest())
    reconciled = revalidate_skill_reference_materials(
        [required, optional], [required, changed_optional],
    )
    assert reconciled == [required, replace(optional, not_staged_reason="reference_changed")]
    # An optional reference that disappeared is dropped the same way.
    assert revalidate_skill_reference_materials([required, optional], [required])[1].not_staged_reason
    with pytest.raises(SkillReferenceError, match="reference_changed") as exc:
        revalidate_skill_reference_materials(
            [required, optional], [replace(required, size=2), optional],
        )
    assert exc.value.name == "a.txt"


def test_reference_error_detail_survives_the_invoke_skill_result_formatter():
    from packages.core.ai.runtime.skills import runtime_format_invoke_skill_result

    error = SkillReferenceError(
        "reference_too_large", name="track.wav", path="media/track.wav",
        size=LARGE_SIZE, limit=MAX_SKILL_REFERENCE_BYTES,
    )
    payload = json.loads(runtime_format_invoke_skill_result("video-edit", error.to_result()))
    assert payload["status"] == "failed"
    assert payload["code"] == "reference_too_large"
    assert payload["reference"]["name"] == "track.wav"
    assert payload["reference"]["limit"] == MAX_SKILL_REFERENCE_BYTES
    assert payload["error"].startswith("A referenced file is unavailable for this Skill run: 'track.wav'")


# ---------------------------------------------------------------------------
# Guard: reference bytes are read in exactly the functions that use them.
# ---------------------------------------------------------------------------

_ROOT = Path(__file__).resolve().parents[1]
_MODULE = _ROOT / "packages/core/services/skill_reference_materials.py"
_RAW_READS = {"read", "readall", "readinto", "read_bytes", "pread", "fdopen", "open"}
# Which functions may call which byte readers. Everything else (resolution
# above all) handles metadata only.
_READ_CALLERS = {
    "_iter_snapshot_chunks": {"_snapshot_metadata", "_read_file_prefix", "_read_staged_bytes"},
    "_read_staged_bytes": {"build_skill_reference_files"},
    "_read_file_prefix": {"skill_reference_text_projections"},
}


def _called_name(call: ast.Call) -> str | None:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def reference_byte_read_violations(source: str) -> list[str]:
    violations = []
    tree = ast.parse(source)
    for function in ast.walk(tree):
        if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(function):
            if not isinstance(node, ast.Call):
                continue
            name = _called_name(node)
            if name in _RAW_READS and function.name != "_iter_snapshot_chunks":
                violations.append(f"{function.name} calls {name}()")
            if name in _READ_CALLERS and function.name not in _READ_CALLERS[name]:
                violations.append(f"{function.name} calls {name}()")
            if (
                name == "_iter_snapshot_chunks"
                and not any(keyword.arg == "limit" for keyword in node.keywords)
            ):
                violations.append(f"{function.name} streams a snapshot without a limit")
    return violations


def test_reference_bytes_are_read_only_where_they_are_used():
    assert reference_byte_read_violations(_MODULE.read_text(encoding="utf-8")) == []


def test_byte_read_guard_catches_a_full_read_in_resolution():
    source = _MODULE.read_text(encoding="utf-8")
    anchor = "        source_id = (\n"
    assert anchor in source
    for mutation in (
        "        os.fdopen(0).read()\n",
        "        _read_staged_bytes(None)\n",
        "        _iter_snapshot_chunks(0, limit=1)\n",
    ):
        mutated = source.replace(anchor, mutation + anchor, 1)
        assert any(
            violation.startswith("resolve_skill_reference_materials")
            for violation in reference_byte_read_violations(mutated)
        ), mutation


def test_runtime_skill_gateway_does_not_consume_reference_bytes():
    source = (_ROOT / "packages/core/ai/runtime/skills.py").read_text(encoding="utf-8")
    gateway = next(
        node for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "runtime_invoke_skill"
    )
    assert [
        node.lineno for node in ast.walk(gateway)
        if isinstance(node, ast.Attribute) and node.attr == "data"
    ] == []


# Guard: host-collected run context reaches a Skill only as optional context.
_HOST_COLLECTED = ("runtime_artifact_urls", "dependency_artifact_urls", "runtime_current_artifact_urls")


def host_context_passed_as_required(source: str) -> list[int]:
    lines = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        for keyword in node.keywords:
            if keyword.arg in {"reference_artifact_urls", "artifact_urls", "document_ids",
                               "reference_document_ids"} and any(
                marker in ast.unparse(keyword.value) for marker in _HOST_COLLECTED
            ):
                lines.append(keyword.value.lineno)
    return lines


def _python_sources():
    for base in ("packages", "apps"):
        for path in (_ROOT / base).rglob("*.py"):
            if "node_modules" not in path.parts:
                yield path


def test_host_collected_run_artifacts_are_never_required_references():
    violations = {
        str(path.relative_to(_ROOT)): lines
        for path in _python_sources()
        if any(marker in (text := path.read_text(encoding="utf-8")) for marker in _HOST_COLLECTED)
        and (lines := host_context_passed_as_required(text))
    }
    assert violations == {}
    # Both auto-collection sites exist and use the optional channel.
    for relative in ("packages/core/ai/runtime/skills.py",
                     "packages/core/ai/tools/generate_file/office_common.py"):
        text = (_ROOT / relative).read_text(encoding="utf-8")
        assert "context_artifact_urls=skill_context_artifact_urls(" in text


def test_host_context_guard_catches_a_required_run_artifact():
    source = (_ROOT / "packages/core/ai/tools/generate_file/office_common.py").read_text(
        encoding="utf-8",
    )
    mutated = source.replace(
        "context_artifact_urls=skill_context_artifact_urls(",
        "reference_artifact_urls=skill_context_artifact_urls(", 1,
    )
    assert mutated != source
    assert host_context_passed_as_required(mutated)
