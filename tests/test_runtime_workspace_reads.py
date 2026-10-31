"""Shared workspace-file read: ACL first, size cap before reading, distinct outcomes."""
from __future__ import annotations

import pytest

from packages.core.ai.runtime import workspace_reads
from packages.core.config import get_settings

ENTITY = "01TESTWORKSPACEREADS0000000"


@pytest.fixture
def entity_fs(tmp_path, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "MANOR_FS_ENABLED", True)
    monkeypatch.setattr(settings, "MANOR_FS_ROOT", str(tmp_path))
    root = tmp_path / ENTITY
    (root / "docs").mkdir(parents=True)
    (root / "docs" / "a.bin").write_bytes(b"0123456789")
    guarded = []

    async def allow(**kwargs):
        guarded.append(kwargs)
        return None

    monkeypatch.setattr(workspace_reads, "runtime_guard_file_read_access", allow)
    return guarded


async def _read(path, **kw):
    return await workspace_reads.runtime_read_workspace_file(entity_id=ENTITY, workspace_path=path, tool_name="t", **kw)


@pytest.mark.asyncio
async def test_ok_and_prefix_forms(entity_fs):
    for path in ("docs/a.bin", "/workspace/docs/a.bin", "workspace/docs/a.bin"):
        read = await _read(path)
        assert read.status == "ok" and read.content == b"0123456789"
    assert entity_fs[-1]["paths"] == ["docs/a.bin"] and entity_fs[-1]["tool_name"] == "t"


@pytest.mark.asyncio
async def test_not_found_is_distinct_from_error(entity_fs):
    assert (await _read("docs/missing.bin")).status == "not_found"
    assert (await _read("nodir/missing.bin")).status == "not_found"
    assert (await _read("../escape")).status == "not_found"
    assert (await _read("")).status == "not_found"
    # A directory exists but is not a readable file.
    assert (await _read("docs")).status == "error"


@pytest.mark.asyncio
async def test_oversize_file_is_refused_without_reading(entity_fs, monkeypatch):
    import builtins

    real_open = builtins.open

    def no_read(path, *args, **kwargs):
        if str(path).startswith(("/dev/fd", "/proc/self/fd")):
            raise AssertionError("oversize files must not be read")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", no_read)
    read = await _read("docs/a.bin", max_bytes=5)
    assert read.status == "too_large" and read.size == 10 and read.content is None
    monkeypatch.setattr(builtins, "open", real_open)
    assert (await _read("docs/a.bin", max_bytes=10)).content == b"0123456789"


@pytest.mark.asyncio
async def test_denied_returns_gate_payload_without_opening(entity_fs, monkeypatch):
    async def deny(**_kwargs):
        return '{"error":"denied"}'

    def must_not_open(*_args, **_kwargs):
        raise AssertionError("denied files must not be opened")

    monkeypatch.setattr(workspace_reads, "runtime_guard_file_read_access", deny)
    monkeypatch.setattr(workspace_reads, "runtime_open_entity_file_snapshot", must_not_open)
    read = await _read("docs/a.bin")
    assert read.status == "denied" and read.blocked == '{"error":"denied"}' and read.content is None


@pytest.mark.asyncio
async def test_storage_disabled_is_an_error(monkeypatch):
    monkeypatch.setattr(get_settings(), "MANOR_FS_ENABLED", False)
    assert (await _read("docs/a.bin")).status == "error"


@pytest.mark.asyncio
@pytest.mark.parametrize("cause,status", [
    (FileNotFoundError(2, "No such file"), "not_found"),
    (NotADirectoryError(20, "Not a directory"), "not_found"),
    (PermissionError(13, "Permission denied"), "error"),
])
async def test_failure_is_classified_by_exception_type(entity_fs, monkeypatch, cause, status):
    from packages.core.services.entity_fs import EntityFilesystemError

    def snapshot(*_args, **_kwargs):
        # Same shape as the real snapshot open: the OSError is wrapped.
        raise EntityFilesystemError("Entity file snapshot target changed or is not a safe file") from cause

    def no_probe(*_args, **_kwargs):
        raise AssertionError("classification must not probe the filesystem")

    root = workspace_reads.runtime_entity_file_root(ENTITY)
    monkeypatch.setattr(workspace_reads, "runtime_entity_file_root", lambda _entity_id: root)
    monkeypatch.setattr(workspace_reads, "runtime_open_entity_file_snapshot", snapshot)
    monkeypatch.setattr(workspace_reads.os.path, "lexists", no_probe)
    monkeypatch.setattr(workspace_reads.os.path, "exists", no_probe)
    # docs/a.bin exists on disk, so only the exception decides the outcome.
    assert (await _read("docs/a.bin")).status == status


@pytest.mark.asyncio
async def test_growth_past_cap_reports_real_size(entity_fs, monkeypatch):
    from contextlib import contextmanager
    from types import SimpleNamespace

    real_snapshot = workspace_reads.runtime_open_entity_file_snapshot

    @contextmanager
    def stale_size(entity_id, rel_path):
        with real_snapshot(entity_id, rel_path) as snap:
            # The size check saw 3 bytes; the file is really 10.
            yield SimpleNamespace(stat=SimpleNamespace(st_size=3), descriptor_path=snap.descriptor_path)

    monkeypatch.setattr(workspace_reads, "runtime_open_entity_file_snapshot", stale_size)
    read = await _read("docs/a.bin", max_bytes=5)
    assert read.status == "too_large" and read.size == 10 and read.content is None
