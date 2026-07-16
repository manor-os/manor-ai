"""Multi-tenant safety tests for the api-side artifact downloader.

The downloader is called when a browser tool returns
``{"artifacts": [{token, filename, ...}]}``. It fetches each token
from the runner and writes to ``/mnt/manor/{entity_id}/<folder>/<file>``.

We verify:

  1. Token fetched, file written under the correct entity root, result
     mutated in place (token dropped, saved_to added).
  2. Two artifacts in one call → two files written.
  3. Filename collision → numeric suffix; never overwrites pre-existing
     knowledge files.
  4. Folder traversal payload (``../../etc``) collapsed to safe folder.
  5. Filename traversal payload sanitized to basename — file ends up
     inside entity_root, not above it.
  6. Tenant isolation — entity_id from one call NEVER writes into
     another entity's root, even when called with a stale-looking
     folder string.
  7. Runner returning 404 (expired token) → artifact entry gets `error`,
     no file written.
  8. Empty entity_id surfaces structured error per artifact, doesn't
     silently lose bytes.
  9. Cap enforcement — artifact bigger than the byte cap is truncated/
     unlinked and surfaces an error.
"""

from __future__ import annotations

from typing import Dict

import httpx
import pytest

from packages.core.ai.mcp import _knowledge_artifact as ka
from packages.core.config import get_settings


# ── Test rig: minimal mock runner over httpx.MockTransport ─────────────────


class _MockRunner:
    """Maps token → (status, bytes). Records consumed tokens to model
    the runner's one-time semantics."""

    def __init__(self) -> None:
        self.files: Dict[str, tuple[int, bytes, str]] = {}  # token → (status, body, mime)
        self.consumed: set[str] = set()

    def serve(self, request: httpx.Request) -> httpx.Response:
        # /artifacts/{token}
        path = request.url.path
        if not path.startswith("/artifacts/"):
            return httpx.Response(404)
        token = path.removeprefix("/artifacts/")
        if token in self.consumed:
            return httpx.Response(404)
        if token not in self.files:
            return httpx.Response(404)
        status, body, mime = self.files[token]
        if status == 200:
            self.consumed.add(token)
        return httpx.Response(status, content=body, headers={"content-type": mime or "application/octet-stream"})


@pytest.fixture
def mock_runner(monkeypatch):
    runner = _MockRunner()
    transport = httpx.MockTransport(runner.serve)

    # Patch the AsyncClient httpx uses inside _knowledge_artifact so
    # all outbound /artifacts requests hit our in-memory runner.
    real_client = httpx.AsyncClient

    class _PatchedClient(real_client):
        def __init__(self, *a, **kw):
            kw.setdefault("transport", transport)
            super().__init__(*a, **kw)

    monkeypatch.setattr(httpx, "AsyncClient", _PatchedClient)
    return runner


@pytest.fixture
def fake_fs(tmp_path):
    """Re-roots the entity filesystem under a tmp dir per test."""
    settings = get_settings()
    old_enabled = settings.MANOR_FS_ENABLED
    old_root = settings.MANOR_FS_ROOT
    old_mode = settings.DEPLOYMENT_MODE
    root = tmp_path / "manor-fs"
    root.mkdir()
    settings.MANOR_FS_ENABLED = True
    settings.MANOR_FS_ROOT = str(root)
    settings.DEPLOYMENT_MODE = "oss"
    try:
        yield root
    finally:
        settings.MANOR_FS_ENABLED = old_enabled
        settings.MANOR_FS_ROOT = old_root
        settings.DEPLOYMENT_MODE = old_mode


# ── Tests ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_artifact_fetched_and_saved_under_entity_root(mock_runner, fake_fs):
    mock_runner.files["tok1"] = (200, b"PDF body", "application/pdf")
    result = await ka.process_result_artifacts(
        {"artifacts": [{"token": "tok1", "filename": "report.pdf", "size": 8, "mime": "application/pdf"}]},
        entity_id="ent-A",
        provider="linkedin_browser",
    )
    art = result["artifacts"][0]
    assert "token" not in art
    assert art["saved_to"] == "Browser Downloads/linkedin_browser/report.pdf"
    written = fake_fs / "ent-A" / "Browser Downloads/linkedin_browser/report.pdf"
    assert written.is_file()
    assert written.read_bytes() == b"PDF body"


@pytest.mark.asyncio
async def test_two_artifacts_both_saved(mock_runner, fake_fs):
    mock_runner.files["a"] = (200, b"AAA", "image/png")
    mock_runner.files["b"] = (200, b"BBB", "image/png")
    await ka.process_result_artifacts(
        {
            "artifacts": [
                {"token": "a", "filename": "a.png", "size": 3, "mime": "image/png"},
                {"token": "b", "filename": "b.png", "size": 3, "mime": "image/png"},
            ]
        },
        entity_id="ent-X",
        provider="browser_exports",
    )
    a = (fake_fs / "ent-X/Browser Downloads/browser_exports/a.png").read_bytes()
    b = (fake_fs / "ent-X/Browser Downloads/browser_exports/b.png").read_bytes()
    assert a == b"AAA"
    assert b == b"BBB"


@pytest.mark.asyncio
async def test_filename_collision_gets_numeric_suffix(mock_runner, fake_fs):
    dest = fake_fs / "ent-A/Browser Downloads/browser_exports"
    dest.mkdir(parents=True)
    (dest / "post.pdf").write_bytes(b"OLD")  # pre-existing knowledge file
    mock_runner.files["t"] = (200, b"NEW", "application/pdf")
    result = await ka.process_result_artifacts(
        {"artifacts": [{"token": "t", "filename": "post.pdf", "size": 3, "mime": "application/pdf"}]},
        entity_id="ent-A",
        provider="browser_exports",
    )
    assert result["artifacts"][0]["saved_to"].endswith("post (2).pdf")
    assert (dest / "post.pdf").read_bytes() == b"OLD"  # NEVER overwrite
    assert (dest / "post (2).pdf").read_bytes() == b"NEW"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "evil_folder",
    [
        "../../escape",
        "/etc",
        "..",
        "Browser Downloads/../../escape",
        "Browser\x00Downloads",
    ],
)
async def test_folder_traversal_collapses_to_safe_path(mock_runner, fake_fs, evil_folder):
    mock_runner.files["t"] = (200, b"X", "text/plain")
    result = await ka.process_result_artifacts(
        {"artifacts": [{"token": "t", "filename": "x.txt", "size": 1, "mime": "text/plain"}]},
        entity_id="ent-A",
        provider="browser_exports",
        target_folder=evil_folder,
    )
    saved_to = result["artifacts"][0]["saved_to"]
    # The resolved file must stay under entity_root
    resolved = (fake_fs / "ent-A" / saved_to).resolve()
    entity_root = (fake_fs / "ent-A").resolve()
    assert str(resolved).startswith(str(entity_root))
    # And the file actually exists where we said
    assert resolved.is_file()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "evil_name",
    [
        "../../etc/passwd",
        "/etc/shadow",
        "foo/bar.txt",
        "..",
        "x\x00.txt",
    ],
)
async def test_filename_traversal_sanitized(mock_runner, fake_fs, evil_name):
    mock_runner.files["t"] = (200, b"X", "text/plain")
    result = await ka.process_result_artifacts(
        {"artifacts": [{"token": "t", "filename": evil_name, "size": 1, "mime": "text/plain"}]},
        entity_id="ent-A",
        provider="browser_exports",
    )
    saved = result["artifacts"][0].get("saved_to") or ""
    # The saved_to path is relative to entity_root; the LAST segment
    # must not contain traversal characters, and the resolved abs path
    # must be under entity_root.
    abs_path = (fake_fs / "ent-A" / saved).resolve()
    entity_root = (fake_fs / "ent-A").resolve()
    assert str(abs_path).startswith(str(entity_root))
    leaf = saved.rsplit("/", 1)[-1]
    assert ".." not in leaf
    assert "\x00" not in leaf
    # filename "/etc/shadow" → basename "shadow"; "foo/bar.txt" → "bar.txt"


@pytest.mark.asyncio
async def test_tenant_isolation_writes_under_correct_root(mock_runner, fake_fs):
    """Token doesn't carry tenant info — the api wrapper does. Two
    parallel calls with different entity_ids must land in different
    roots even if filenames collide."""
    mock_runner.files["a"] = (200, b"A-data", "text/plain")
    mock_runner.files["b"] = (200, b"B-data", "text/plain")
    import asyncio

    res_a, res_b = await asyncio.gather(
        ka.process_result_artifacts(
            {"artifacts": [{"token": "a", "filename": "shared.txt", "size": 6, "mime": "text/plain"}]},
            entity_id="ent-A",
            provider="browser_exports",
        ),
        ka.process_result_artifacts(
            {"artifacts": [{"token": "b", "filename": "shared.txt", "size": 6, "mime": "text/plain"}]},
            entity_id="ent-B",
            provider="browser_exports",
        ),
    )
    a_path = fake_fs / "ent-A/Browser Downloads/browser_exports/shared.txt"
    b_path = fake_fs / "ent-B/Browser Downloads/browser_exports/shared.txt"
    assert a_path.read_bytes() == b"A-data"
    assert b_path.read_bytes() == b"B-data"
    # Confirm no cross-bleed
    assert a_path.read_bytes() != b_path.read_bytes()


@pytest.mark.asyncio
async def test_runner_404_surfaces_error_not_file(mock_runner, fake_fs):
    # Token never registered
    result = await ka.process_result_artifacts(
        {"artifacts": [{"token": "ghost", "filename": "x.txt", "size": 1, "mime": "text/plain"}]},
        entity_id="ent-A",
        provider="browser_exports",
    )
    art = result["artifacts"][0]
    assert "saved_to" not in art
    assert "error" in art
    assert "expired" in art["error"] or "consumed" in art["error"]
    # No file should have been written
    candidates = list((fake_fs / "ent-A").rglob("*.txt"))
    assert candidates == []


@pytest.mark.asyncio
async def test_missing_entity_id_surfaces_error_per_artifact(mock_runner):
    mock_runner.files["t"] = (200, b"X", "text/plain")
    result = await ka.process_result_artifacts(
        {"artifacts": [{"token": "t", "filename": "x.txt", "size": 1, "mime": "text/plain"}]},
        entity_id="",  # missing!
        provider="browser_exports",
    )
    art = result["artifacts"][0]
    assert "token" not in art  # token dropped to prevent stale exposure
    assert "error" in art and "entity_id" in art["error"]


@pytest.mark.asyncio
async def test_byte_cap_enforced(monkeypatch, mock_runner, fake_fs):
    # Tighten cap to 16 bytes for the test
    monkeypatch.setattr(ka, "_MAX_ARTIFACT_BYTES", 16)
    mock_runner.files["t"] = (200, b"X" * 1024, "application/octet-stream")
    result = await ka.process_result_artifacts(
        {"artifacts": [{"token": "t", "filename": "big.bin", "size": 1024, "mime": "application/octet-stream"}]},
        entity_id="ent-A",
        provider="browser_exports",
    )
    art = result["artifacts"][0]
    assert "saved_to" not in art
    assert "error" in art and "cap" in art["error"]
    # Partial write was unlinked
    candidates = list((fake_fs / "ent-A").rglob("big.bin"))
    assert candidates == []


@pytest.mark.asyncio
async def test_cloud_filesystem_unavailable_surfaces_error_without_fetching(mock_runner, fake_fs):
    settings = get_settings()
    settings.DEPLOYMENT_MODE = "cloud"
    mock_runner.files["t"] = (200, b"X", "text/plain")

    result = await ka.process_result_artifacts(
        {"artifacts": [{"token": "t", "filename": "x.txt", "size": 1, "mime": "text/plain"}]},
        entity_id="ent-A",
        provider="browser_exports",
    )

    art = result["artifacts"][0]
    assert "token" not in art
    assert "saved_to" not in art
    assert "filesystem unavailable" in art["error"]
    assert mock_runner.consumed == set()
    assert not (fake_fs / "ent-A").exists()


@pytest.mark.asyncio
async def test_no_artifacts_field_passes_through_unchanged(mock_runner, fake_fs):
    """Tools that don't produce binaries see no behavior change."""
    original = {"count": 5, "results": [{"id": 1}]}
    result = await ka.process_result_artifacts(
        dict(original),
        entity_id="ent-A",
        provider="browser_exports",
    )
    assert result == original
