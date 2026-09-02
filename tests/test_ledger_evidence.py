from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest

from packages.core.services import ledger_evidence


class _Result:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class _Session:
    def __init__(self, values):
        self.values = iter(values)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return None

    async def execute(self, _query):
        return _Result(next(self.values))


@pytest.mark.asyncio
async def test_verified_knowledge_evidence_matches_immutable_version(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = b"vendor receipt\namount=1299\n"
    snapshot = tmp_path / ".versions" / "documents" / "document-1" / "v000001_receipt.pdf"
    snapshot.parent.mkdir(parents=True)
    snapshot.write_bytes(content)
    workspace = SimpleNamespace(id="workspace-1", entity_id="entity-1", artifact_folder_id=None)
    document = SimpleNamespace(
        id="document-1",
        entity_id="entity-1",
        is_trashed=False,
        metadata_={"origin": {"workspace_id": "workspace-1"}},
        folder_id=None,
        fs_path="receipt.pdf",
    )
    version = SimpleNamespace(
        id="version-1",
        document_id="document-1",
        version_number=1,
        fs_path=".versions/documents/document-1/v000001_receipt.pdf",
    )
    monkeypatch.setattr(
        "packages.core.database.async_session",
        lambda: _Session([workspace, document, version]),
    )
    monkeypatch.setattr(
        "packages.core.ai.runtime.file_actions.runtime_entity_file_root",
        lambda _entity_id: str(tmp_path),
    )
    reference = {
        "workspace_id": "workspace-1",
        "role": "receipt",
        "document_id": "document-1",
        "version_id": "version-1",
        "sha256": hashlib.sha256(content).hexdigest(),
        "verification_status": "verified",
        "verified_at": "2026-08-17T00:00:00Z",
    }
    verified = await ledger_evidence.verify_evidence_refs(
        [reference],
        entity_id="entity-1",
        workspace_id="workspace-1",
    )
    assert verified[0]["verification_status"] == "verified"
    assert verified[0]["source"] == "knowledge_hash"

    with pytest.raises(ledger_evidence.EvidenceReferenceError, match="hash"):
        await ledger_evidence.verify_evidence_refs(
            [{**reference, "sha256": "f" * 64}],
            entity_id="entity-1",
            workspace_id="workspace-1",
        )
