from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from packages.core.services import finance_ledger


@pytest.fixture
def finance_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    directory = SimpleNamespace(storage_path="finance-ledger", display_path="finance-ledger")

    async def resolve_location(**kwargs):
        root = tmp_path / kwargs["storage"].directory
        root.mkdir(parents=True, exist_ok=True)
        return directory, str(root), str(tmp_path)

    async def sync_projection(**_kwargs):
        return SimpleNamespace(synced=True, document_id="finance-document-1")

    monkeypatch.setattr(finance_ledger, "ensure_ledger_location", resolve_location)
    monkeypatch.setattr(finance_ledger, "runtime_sync_entity_file_to_knowledge", sync_projection)
    return tmp_path


@pytest.mark.asyncio
async def test_finance_entry_is_validated_idempotent_and_readable(finance_runtime: Path) -> None:
    evidence = {
        "workspace_id": "workspace-1",
        "role": "receipt",
        "document_id": "doc-receipt-1",
        "version_number": 1,
        "sha256": "a" * 64,
    }
    recorded = await finance_ledger.record_finance_entry(
        entity_id="entity-1",
        workspace_id="workspace-1",
        entry_type="expense",
        amount_minor=1299,
        currency="usd",
        direction="outflow",
        account_ref="software",
        idempotency_key="receipt-1",
        evidence_refs=[evidence],
        payload={"vendor": "Example"},
    )
    assert recorded["ok"] is True
    assert recorded["document_id"] == "finance-document-1"

    repeated = await finance_ledger.record_finance_entry(
        entity_id="entity-1",
        workspace_id="workspace-1",
        entry_type="expense",
        amount_minor=1299,
        currency="USD",
        direction="outflow",
        account_ref="software",
        idempotency_key="receipt-1",
        evidence_refs=[evidence],
        payload={"vendor": "Example"},
    )
    assert repeated["idempotent"] is True
    assert repeated["entry_id"] == recorded["entry_id"]

    ledger = await finance_ledger.read_finance_ledger(
        entity_id="entity-1",
        workspace_id="workspace-1",
    )
    assert ledger["entry_count"] == 1
    assert ledger["entries"][0]["currency"] == "USD"
    assert ledger["entries"][0]["evidence_refs"][0]["document_id"] == "doc-receipt-1"


@pytest.mark.asyncio
async def test_finance_evidence_must_be_in_same_workspace(finance_runtime: Path) -> None:
    with pytest.raises(finance_ledger.FinanceLedgerError, match="another Workspace"):
        await finance_ledger.record_finance_entry(
            entity_id="entity-1",
            workspace_id="workspace-1",
            entry_type="bill",
            amount_minor=500,
            currency="USD",
            direction="outflow",
            account_ref="vendors",
            idempotency_key="bill-1",
            evidence_refs=[
                {
                    "workspace_id": "workspace-2",
                    "role": "bill",
                    "document_id": "doc-bill-1",
                    "version_id": "version-1",
                    "sha256": "b" * 64,
                }
            ],
        )


@pytest.mark.asyncio
async def test_finance_retry_reprojects_after_knowledge_sync_failure(
    finance_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    async def sync_projection(**_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return SimpleNamespace(synced=False)
        return SimpleNamespace(synced=True, document_id="finance-document-retry")

    monkeypatch.setattr(finance_ledger, "runtime_sync_entity_file_to_knowledge", sync_projection)
    kwargs = {
        "entity_id": "entity-1",
        "workspace_id": "workspace-1",
        "entry_type": "expense",
        "amount_minor": 500,
        "currency": "USD",
        "direction": "outflow",
        "account_ref": "hosting",
        "idempotency_key": "sync-retry-1",
    }
    with pytest.raises(finance_ledger.FinanceLedgerError, match="projection failed"):
        await finance_ledger.record_finance_entry(**kwargs)

    retried = await finance_ledger.record_finance_entry(**kwargs)
    assert retried["idempotent"] is True
    assert retried["document_id"] == "finance-document-retry"
    assert calls == 2


@pytest.mark.asyncio
async def test_finance_path_collision_rejects_conflicting_payload(
    finance_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded = await finance_ledger.record_finance_entry(
        entity_id="entity-1",
        workspace_id="workspace-1",
        entry_type="income",
        amount_minor=500,
        currency="USD",
        direction="inflow",
        account_ref="sales",
        idempotency_key="older-than-window",
    )
    assert recorded["ok"] is True

    async def outside_recent_window(**_kwargs):
        return {"entries": []}

    monkeypatch.setattr(finance_ledger, "read_finance_ledger", outside_recent_window)
    with pytest.raises(finance_ledger.FinanceLedgerError) as conflict:
        await finance_ledger.record_finance_entry(
            entity_id="entity-1",
            workspace_id="workspace-1",
            entry_type="income",
            amount_minor=900,
            currency="USD",
            direction="inflow",
            account_ref="sales",
            idempotency_key="older-than-window",
        )
    assert conflict.value.code == "idempotency_conflict"


def test_journal_entry_requires_balanced_lines() -> None:
    with pytest.raises(ValueError, match="must balance"):
        # The async service is not needed to exercise the contract boundary.
        finance_ledger.FinanceLedgerEntry(
            entry_id="entry-1",
            workspace_id="workspace-1",
            entity_id="entity-1",
            entry_type="journal_entry",
            occurred_at="2026-08-17T00:00:00+00:00",
            recorded_at="2026-08-17T00:00:00+00:00",
            amount_minor=100,
            currency="USD",
            direction="outflow",
            account_ref="general",
            idempotency_key="journal-1",
            journal_lines=[
                {"account_ref": "cash", "direction": "debit", "amount_minor": 100},
                {"account_ref": "expense", "direction": "credit", "amount_minor": 90},
            ],
        )
