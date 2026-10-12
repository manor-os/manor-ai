"""GitHub Skill imports are complete, pinned, and never execute downloaded code."""
from contextlib import asynccontextmanager
import asyncio
import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from packages.core.services import github_skill_installer as installer


COMMIT = "a" * 40
ORIGINAL = (
    "---\nname: example\nlicense: MIT\ntools: [web_fetch]\n"
    "input_schema:\n  type: object\n---\n"
    "Read references/rules.md and run scripts/nested/main.py."
)


def _github(monkeypatch, files, *, entry_update=None, fail_path=None):
    """Mock GitHub directory listings and pinned raw downloads, no real network."""
    requests = []
    raw_files = {path: value.encode() if isinstance(value, str) else value for path, value in files.items()}

    def handler(request):
        requests.append(request)
        path = request.url.path
        if path == "/repos/owner/repo/commits/main":
            return httpx.Response(200, json={"sha": COMMIT})
        if request.url.host == "api.github.com":
            assert request.url.params["ref"] == COMMIT
            directory = path.removeprefix("/repos/owner/repo/contents/").rstrip("/")
            prefix = directory + "/" if directory else ""
            entries = {}
            for full_path, raw in raw_files.items():
                if not full_path.startswith(prefix):
                    continue
                remainder = full_path[len(prefix):]
                name = remainder.split("/")[0]
                child_path = prefix + name
                if "/" in remainder:
                    entries[name] = {"type": "dir", "path": child_path}
                else:
                    entry = {
                        "type": "file", "path": child_path, "size": len(raw),
                        "sha": hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest(),
                    }
                    if entry_update:
                        entry_update(entry)
                    entries[name] = entry
            return httpx.Response(200, json=list(entries.values()))
        assert request.url.host == "raw.githubusercontent.com"
        assert path.startswith(f"/owner/repo/{COMMIT}/")
        source_path = path.removeprefix(f"/owner/repo/{COMMIT}/")
        if source_path == fail_path:
            return httpx.Response(503, text="unavailable")
        return httpx.Response(200, content=raw_files[source_path])

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        installer.httpx, "AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs),
    )
    return requests


@pytest.mark.parametrize("url,path", [
    ("owner/repo/pack", "pack"),
    ("https://github.com/owner/repo/tree/main/pack", "pack"),
    ("https://github.com/owner/repo/blob/main/SKILL.md", ""),
    ("https://raw.githubusercontent.com/owner/repo/main/pack/SKILL.md", "pack"),
    ("https://github.com/owner/repo", ""),
])
def test_parse_supported_sources(url, path):
    assert installer._parse_github_url(url) == ("owner", "repo", path, "main")


@pytest.mark.parametrize("url", [
    "https://github.com.evil.invalid/owner/repo",
    "https://user:secret@github.com/owner/repo",
    "https://github.com/owner/repo/tree/main/../escape",
    "https://github.com/owner/repo/tree/main/%2e%2e/escape",
    "owner/repo/pack\\escape",
    "https://github.com/../repo",
])
def test_reject_unsafe_source_urls(url):
    with pytest.raises(ValueError):
        installer._parse_github_url(url)


@pytest.mark.asyncio
async def test_fetch_complete_nested_bundle_at_one_commit(monkeypatch):
    files = {
        "pack/SKILL.md": ORIGINAL,
        "pack/scripts/nested/main.py": "raise RuntimeError('must never execute')",
        "pack/references/rules.md": "Rules.",
        "pack/assets/layout.svg": "<svg/>",
        "pack/requirements.txt": "example-package==1.0",
        "pack/LICENSE": "MIT license text",
    }
    requests = _github(monkeypatch, files)
    bundle = await installer.fetch_github_skill_bundle("owner/repo/pack")
    assert bundle.commit_sha == COMMIT
    assert bundle.source_url == f"https://github.com/owner/repo/tree/{COMMIT}/pack"
    assert bundle.files == {key.removeprefix("pack/"): value for key, value in files.items()}
    assert bundle.metadata["license"] == "MIT"
    assert len(bundle.content_sha256) == 64
    spec = bundle.to_spec()
    assert spec["scripts"] == {"scripts/nested/main.py": files["pack/scripts/nested/main.py"]}
    assert spec["references"]["assets/layout.svg"] == "<svg/>"
    assert spec["requirements"] == "example-package==1.0"
    assert spec["input_schema"] == {"type": "object"}
    assert all("/main/" not in str(request.url) for request in requests)


@pytest.mark.asyncio
async def test_failed_supported_file_download_does_not_return_partial_bundle(monkeypatch):
    _github(monkeypatch, {"SKILL.md": ORIGINAL, "scripts/run.py": "pass"}, fail_path="scripts/run.py")
    with pytest.raises(httpx.HTTPStatusError):
        await installer.fetch_github_skill_bundle("owner/repo")


@pytest.mark.asyncio
@pytest.mark.parametrize("body,error", [
    ("---\nname: [broken\n---\nInstructions", "frontmatter"),
    ("---\n- not-an-object\n---\nInstructions", "frontmatter"),
    ("---\ninput_schema: [wrong]\n---\nInstructions", "input_schema"),
    ("---\ninput_schema:\n  type: nonexistent\n---\nInstructions", "input_schema"),
    ("---\nname: Empty\n---\n", "instructions"),
])
async def test_invalid_skill_definition_fails_before_persistence(monkeypatch, body, error):
    _github(monkeypatch, {"SKILL.md": body})
    with pytest.raises(ValueError, match=error):
        await installer.fetch_github_skill_bundle("owner/repo")


@pytest.mark.asyncio
@pytest.mark.parametrize("entry_update,error", [
    (lambda entry: entry.update(sha="0" * 40), "integrity"),
    (lambda entry: entry.update(target="outside"), "symlinks"),
    (lambda entry: entry.update(path="../escape.md"), "unsafe"),
    (lambda entry: entry.update(size=installer._MAX_FILE_BYTES + 1), "size"),
])
async def test_reject_unsafe_or_corrupt_manifest(monkeypatch, entry_update, error):
    _github(monkeypatch, {"SKILL.md": ORIGINAL}, entry_update=entry_update)
    with pytest.raises(ValueError, match=error):
        await installer.fetch_github_skill_bundle("owner/repo")


@pytest.mark.asyncio
@pytest.mark.parametrize("file,content,error", [
    ("assets/logo.png", b"binary", "Unsupported"),
    ("config.json", "{}", "reserved"),
    ("credentials.json", "{}", "reserved"),
    ("scripts/a.py", b"\xff", "UTF-8"),
    ("scripts/a.py", b"\0", "binary"),
])
async def test_unsupported_files_are_not_silently_skipped(monkeypatch, file, content, error):
    _github(monkeypatch, {"SKILL.md": ORIGINAL, file: content})
    with pytest.raises(ValueError, match=error):
        await installer.fetch_github_skill_bundle("owner/repo")


@pytest.mark.asyncio
@pytest.mark.parametrize("limit,value,error", [
    ("_MAX_FILES", 1, "file limit"),
    ("_MAX_DIRECTORIES", 1, "directory limit"),
    ("_MAX_DEPTH", 0, "directory limit"),
    ("_MAX_BUNDLE_BYTES", 1, "total size"),
])
async def test_bundle_bounds_fail_instead_of_truncating(monkeypatch, limit, value, error):
    _github(monkeypatch, {"SKILL.md": ORIGINAL, "scripts/a.py": "pass"})
    monkeypatch.setattr(installer, limit, value)
    with pytest.raises(ValueError, match=error):
        await installer.fetch_github_skill_bundle("owner/repo")


def _storage(monkeypatch, *, fail_save=False, corrupt_read=False):
    from packages.core.services import skill_file_storage, skill_service

    records = {}
    events = []
    async def create_skill(db, **kwargs):
        assert events == ["savepoint"]
        return SimpleNamespace(id="new-skill-id", config=kwargs["config"], **{
            key: value for key, value in kwargs.items() if key != "config"
        })

    @asynccontextmanager
    async def savepoint():
        events.append("savepoint")
        try:
            yield
        except BaseException:
            events.append("rollback")
            raise
        else:
            events.append("release")

    def save(entity_id, skill_id, **kwargs):
        records["prompt"] = kwargs["prompt"]
        records["extra"] = kwargs["extra_files"]
        records["config"] = kwargs["config_snapshot"]
        return "" if fail_save else skill_id

    cleanup = Mock()
    monkeypatch.setattr(skill_service, "create_skill", create_skill)
    monkeypatch.setattr(skill_file_storage, "save_skill_files", save)
    monkeypatch.setattr(skill_file_storage, "load_skill_prompt", lambda *a, **k: records["prompt"])
    monkeypatch.setattr(skill_file_storage, "load_skill_extra_files", lambda *a, **k: {} if corrupt_read else records["extra"])
    monkeypatch.setattr(skill_file_storage, "delete_skill_files", cleanup)
    db = SimpleNamespace(begin_nested=savepoint, flush=AsyncMock())
    return db, records, events, cleanup


@pytest.mark.asyncio
async def test_install_adapts_runtime_and_preserves_exact_files_and_provenance(monkeypatch):
    _github(monkeypatch, {
        "SKILL.md": ORIGINAL, "scripts/nested/main.py": "pass",
        "requirements.txt": "example==1", "LICENSE": "MIT text",
    })
    db, storage, events, cleanup = _storage(monkeypatch)
    skill = await installer.install_from_github("owner/repo", "entity", db)
    assert skill.config["type"] == "sandbox"
    assert "sandbox" in skill.tools
    assert skill.config["source_commit"] == COMMIT
    assert skill.config["source_license"] == {"declared": "MIT", "files": ["LICENSE"]}
    assert skill.config["file_sha256"]["SKILL.md"] == hashlib.sha256(ORIGINAL.encode()).hexdigest()
    assert storage["extra"]["scripts/nested/main.py"] == "pass"
    assert storage["extra"]["requirements.txt"] == "example==1"
    assert storage["prompt"] == ORIGINAL
    assert events == ["savepoint", "release"]
    db.flush.assert_awaited_once()
    cleanup.assert_not_called()


@pytest.mark.asyncio
async def test_prompt_only_does_not_add_sandbox_tools(monkeypatch):
    _github(monkeypatch, {"SKILL.md": "Write a concise summary.", "LICENSE": "MIT text"})
    db, _, _, _ = _storage(monkeypatch)
    skill = await installer.install_from_github("owner/repo", "entity", db)
    assert skill.config["type"] == "prompt"
    assert skill.tools == []


@pytest.mark.asyncio
async def test_requirements_without_execution_resources_do_not_claim_executable(monkeypatch):
    _github(monkeypatch, {"SKILL.md": "Use the dependency.", "requirements.txt": "example==1"})
    db, _, events, cleanup = _storage(monkeypatch)
    with pytest.raises(ValueError, match="packaged script or resource"):
        await installer.install_from_github("owner/repo", "entity", db)
    assert events == []
    cleanup.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["save", "readback", "flush"])
async def test_install_failure_rolls_back_and_cleans_only_new_prefix(monkeypatch, failure):
    _github(monkeypatch, {"SKILL.md": ORIGINAL, "scripts/run.py": "pass"})
    db, _, events, cleanup = _storage(
        monkeypatch, fail_save=failure == "save", corrupt_read=failure == "readback",
    )
    if failure == "flush":
        db.flush.side_effect = RuntimeError("database unavailable")
    with pytest.raises((ValueError, RuntimeError)):
        await installer.install_from_github("owner/repo", "entity", db)
    assert events == ["savepoint", "rollback"]
    cleanup.assert_called_once_with("entity", "new-skill-id", skill_dir="new-skill-id")


@pytest.mark.asyncio
async def test_cancelled_flush_cleans_new_bundle_and_rolls_back(monkeypatch):
    _github(monkeypatch, {"SKILL.md": ORIGINAL, "scripts/run.py": "pass"})
    db, _, events, cleanup = _storage(monkeypatch)
    db.flush.side_effect = asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await installer.install_from_github("owner/repo", "entity", db)
    assert events == ["savepoint", "rollback"]
    cleanup.assert_called_once_with("entity", "new-skill-id", skill_dir="new-skill-id")
