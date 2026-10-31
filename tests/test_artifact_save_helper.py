# tests/test_artifact_save_helper.py
from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.asyncio


class _Tx:
    def __init__(self, calls, sync=None, project_error=None):
        self.calls = calls
        self.sync = sync
        self.project_error = project_error
        self.write_kwargs = None
        self.project_kwargs = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def write_bytes(self, rel_path, content, **kw):
        self.write_kwargs = kw
        self.calls.append(("write", rel_path, kw["expected_size"]))
        return f"/root/{rel_path}"

    async def project_file(self, **kw):
        self.project_kwargs = kw
        self.calls.append(("project", kw["source"], kw["tool_name"], kw["workspace_id"]))
        if self.project_error is not None:
            raise self.project_error
        if self.sync is not None:
            return self.sync
        return SimpleNamespace(document_id="doc-1", fs_path=None, name=None, file_size=None, mime_type=None)

    async def commit(self):
        self.calls.append(("commit",))


def _factory(tx):
    return SimpleNamespace(create=lambda entity_id: tx)


async def test_projects_and_normalizes_record():
    from packages.core.ai.runtime.artifact_save import project_bytes_into_knowledge

    calls = []
    factory = SimpleNamespace(create=lambda entity_id: _Tx(calls))
    rec = await project_bytes_into_knowledge(
        entity_id="E", entity_dir="/root", rel_path="ws/artifacts/report.csv", content_bytes=b"a,b\n",
        source="sandbox", created_by="U", tool_name="sandbox_save_result", workspace_id="W",
        transaction_factory=factory,
    )
    assert [c[0] for c in calls] == ["write", "project", "commit"]
    assert calls[1] == ("project", "sandbox", "sandbox_save_result", "W")
    assert (rec.document_id, rec.fs_path, rec.name, rec.file_size, rec.mime_type) == (
        "doc-1", "ws/artifacts/report.csv", "report.csv", 4, "text/csv")


async def test_pins_write_and_projection_flags():
    from packages.core.ai.runtime.artifact_save import project_bytes_into_knowledge

    content = b"a,b\n"
    sha = hashlib.sha256(content).hexdigest()
    tx = _Tx([])
    await project_bytes_into_knowledge(
        entity_id="E", entity_dir="/root", rel_path="ws/artifacts/report.csv", content_bytes=content,
        source="sandbox", created_by="U", tool_name="sandbox_save_result", workspace_id="W",
        transaction_factory=_factory(tx),
    )
    assert tx.write_kwargs["require_missing"] is True
    assert tx.write_kwargs["allow_empty"] is False
    assert tx.write_kwargs["expected_size"] == len(content)
    assert tx.write_kwargs["expected_content_sha256"] == sha
    assert tx.project_kwargs["force"] is True
    assert tx.project_kwargs["expected_content_sha256"] == sha
    assert tx.project_kwargs["abs_path"] == "/root/ws/artifacts/report.csv"
    assert tx.project_kwargs["entity_root"] == "/root"


async def test_uses_sync_supplied_metadata_when_present():
    from packages.core.ai.runtime.artifact_save import project_bytes_into_knowledge

    sync = SimpleNamespace(
        document_id="doc-9",
        fs_path="canonical/renamed.csv",
        name="renamed.csv",
        file_size="4",
        mime_type="application/x-custom",
    )
    rec = await project_bytes_into_knowledge(
        entity_id="E", entity_dir="/root", rel_path="ws/artifacts/report.csv", content_bytes=b"a,b\n",
        source="sandbox", created_by="U", tool_name="sandbox_save_result",
        transaction_factory=_factory(_Tx([], sync=sync)),
    )
    assert (rec.document_id, rec.fs_path, rec.name, rec.file_size, rec.mime_type) == (
        "doc-9", "canonical/renamed.csv", "renamed.csv", 4, "application/x-custom")
    assert isinstance(rec.file_size, int)


async def test_projection_failure_propagates_and_skips_commit():
    from packages.core.ai.runtime.artifact_save import project_bytes_into_knowledge

    class _Boom(Exception):
        pass

    err = _Boom("projection failed")
    calls = []
    with pytest.raises(_Boom) as info:
        await project_bytes_into_knowledge(
            entity_id="E", entity_dir="/root", rel_path="ws/artifacts/report.csv", content_bytes=b"a,b\n",
            source="sandbox", created_by="U", tool_name="sandbox_save_result",
            transaction_factory=_factory(_Tx(calls, project_error=err)),
        )
    assert info.value is err
    assert [c[0] for c in calls] == ["write", "project"]
