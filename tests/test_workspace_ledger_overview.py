import pytest

from packages.core.services import workspace_ledger_overview as module


def test_overview_breakdowns_are_bounded() -> None:
    rows = [{"stage": f"stage-{index}"} for index in range(40)]

    counts = module._counts(rows, "stage")

    assert len(counts) == 20
    assert counts[0] == {"key": "stage-0", "count": 1}


@pytest.mark.asyncio
async def test_finance_overview_totals_only_realized_effective_entries(
    monkeypatch,
) -> None:
    async def fake_read(**kwargs):
        assert kwargs["create_location"] is False
        return {
            "entries": [
                {
                    "entry_id": "income-posted",
                    "status": "posted",
                    "direction": "inflow",
                    "currency": "USD",
                    "amount_minor": 10_000,
                },
                {
                    "entry_id": "income-planned",
                    "status": "planned",
                    "direction": "inflow",
                    "currency": "USD",
                    "amount_minor": 50_000,
                },
                {
                    "entry_id": "income-other-posted",
                    "status": "posted",
                    "direction": "inflow",
                    "currency": "USD",
                    "amount_minor": 3_000,
                },
                {
                    "entry_id": "income-other-planned-correction",
                    "status": "planned",
                    "direction": "inflow",
                    "currency": "USD",
                    "amount_minor": 3_500,
                    "supersedes_entry_id": "income-other-posted",
                },
                {
                    "entry_id": "expense-void",
                    "status": "void",
                    "direction": "outflow",
                    "currency": "USD",
                    "amount_minor": 4_000,
                },
                {
                    "entry_id": "expense-original",
                    "status": "posted",
                    "direction": "outflow",
                    "currency": "USD",
                    "amount_minor": 1_000,
                },
                {
                    "entry_id": "expense-corrected",
                    "status": "reconciled",
                    "direction": "outflow",
                    "currency": "USD",
                    "amount_minor": 1_200,
                    "supersedes_entry_id": "expense-original",
                },
                {
                    "entry_id": "refund-posted",
                    "status": "paid",
                    "direction": "outflow",
                    "currency": "USD",
                    "amount_minor": 1_500,
                    "reverses_entry_id": "income-posted",
                },
            ],
        }

    monkeypatch.setattr(module, "read_finance_ledger", fake_read)
    result = await module.workspace_ledger_overview(
        entity_id="entity-1",
        workspace_id="workspace-1",
        settings={"ledger_contracts": ["manor.finance_ledger/v1"]},
        use_cache=False,
    )

    assert result["ledgers"][0]["totals"] == [{
        "currency": "USD",
        "inflow_minor": 13_000,
        "outflow_minor": 2_700,
    }]


@pytest.mark.asyncio
async def test_overview_exposes_recruiting_current_projection(monkeypatch) -> None:
    async def fake_read(**kwargs):
        assert kwargs["create_location"] is False
        return {
            "rows": [
                {"record_key": "candidate-1", "subject_type": "candidate", "stage": "screening", "status": "active"},
                {"record_key": "candidate-2", "subject_type": "candidate", "stage": "offer", "status": "active"},
                {"record_key": "employee-1", "subject_type": "employee", "stage": "active", "status": "completed"},
            ],
            "entries": [
                {"event_id": "event-1", "recorded_at": "2026-08-01T00:00:00Z"},
                {"event_id": "event-2", "recorded_at": "2026-08-02T00:00:00Z"},
                {"event_id": "event-3", "recorded_at": "2026-08-03T00:00:00Z"},
                {"event_id": "event-4", "recorded_at": "2026-08-04T00:00:00Z"},
            ],
        }

    monkeypatch.setattr(module, "read_recruiting_ledger", fake_read)
    result = await module.workspace_ledger_overview(
        entity_id="entity-1",
        workspace_id="workspace-1",
        settings={"ledger_contracts": ["manor.recruiting_ledger/v1"]},
        use_cache=False,
    )

    assert result["ledger_count"] == 1
    assert result["record_count"] == 3
    assert result["event_count"] == 4
    ledger = result["ledgers"][0]
    assert ledger["projection_kind"] == "current"
    assert ledger["stage_counts"] == [
        {"key": "active", "count": 1},
        {"key": "offer", "count": 1},
        {"key": "screening", "count": 1},
    ]
    assert ledger["updated_at"] == "2026-08-04T00:00:00Z"


@pytest.mark.asyncio
async def test_overview_surfaces_filesystem_outage(monkeypatch) -> None:
    async def unavailable(**_kwargs):
        raise module.RecruitingLedgerError(
            "filesystem_unavailable",
            "Workspace filesystem is not enabled",
        )

    monkeypatch.setattr(module, "read_recruiting_ledger", unavailable)

    with pytest.raises(
        module.WorkspaceLedgerOverviewUnavailable,
        match="filesystem is not enabled",
    ):
        await module.workspace_ledger_overview(
            entity_id="entity-1",
            workspace_id="workspace-1",
            settings={"ledger_contracts": ["manor.recruiting_ledger/v1"]},
            use_cache=False,
        )


@pytest.mark.asyncio
async def test_overview_reads_only_allowed_contracts(monkeypatch) -> None:
    async def fake_recruiting(**_kwargs):
        return {"rows": [], "entries": []}

    async def finance_must_not_run(**_kwargs):
        raise AssertionError("finance is outside the Agent Ledger binding")

    monkeypatch.setattr(module, "read_recruiting_ledger", fake_recruiting)
    monkeypatch.setattr(module, "read_finance_ledger", finance_must_not_run)

    result = await module.workspace_ledger_overview(
        entity_id="entity-1",
        workspace_id="workspace-1",
        settings={
            "ledger_contracts": [
                "manor.recruiting_ledger/v1",
                "manor.finance_ledger/v1",
            ]
        },
        allowed_contract_ids={"manor.recruiting_ledger/v1"},
        use_cache=False,
    )

    assert [ledger["contract_id"] for ledger in result["ledgers"]] == [
        "manor.recruiting_ledger/v1"
    ]


@pytest.mark.asyncio
async def test_overview_reuses_versioned_hot_projection(monkeypatch) -> None:
    reads = 0

    async def fake_read(**_kwargs):
        nonlocal reads
        reads += 1
        return {"rows": [], "entries": []}

    class FakeCache:
        value = None

        async def get(self, _key):
            return self.value

        async def set(self, _key, value, *, ttl):
            assert ttl > 0
            self.value = value
            return True

    async def fake_version(*_args):
        return 7

    fake_cache = FakeCache()
    monkeypatch.setattr(module, "read_recruiting_ledger", fake_read)
    monkeypatch.setattr(module, "cache", fake_cache)
    monkeypatch.setattr(module, "get_tool_cache_version", fake_version)

    kwargs = {
        "entity_id": "entity-1",
        "workspace_id": "workspace-1",
        "settings": {"ledger_contracts": ["manor.recruiting_ledger/v1"]},
    }
    first = await module.workspace_ledger_overview(**kwargs)
    second = await module.workspace_ledger_overview(**kwargs)

    assert first == second
    assert reads == 1
