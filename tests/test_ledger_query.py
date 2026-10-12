from __future__ import annotations

import json

import pytest

from packages.core.services.ledger_query import (
    MAX_QUERY_GROUPS,
    LedgerQueryError,
    query_ledger_rows,
)


ROWS = [
    {
        "entry_id": "entry-1",
        "recorded_at": "2026-08-01T10:00:00+00:00",
        "occurred_at": "2026-08-01T09:00:00+00:00",
        "status": "posted",
        "entry_type": "expense",
        "currency": "USD",
        "direction": "outflow",
        "account_ref": "software",
        "amount_minor": 1200,
        "tax_minor": 100,
        "fee_minor": 20,
    },
    {
        "entry_id": "entry-2",
        "recorded_at": "2026-08-02T10:00:00+00:00",
        "occurred_at": "2026-08-02T09:00:00+00:00",
        "status": "paid",
        "entry_type": "expense",
        "currency": "USD",
        "direction": "outflow",
        "account_ref": "software",
        "amount_minor": 800,
        "tax_minor": 0,
        "fee_minor": 5,
    },
    {
        "entry_id": "entry-3",
        "recorded_at": "2026-08-03T10:00:00+00:00",
        "occurred_at": "2026-08-03T09:00:00+00:00",
        "status": "posted",
        "entry_type": "income",
        "currency": "USD",
        "direction": "inflow",
        "account_ref": "sales",
        "amount_minor": 5000,
        "tax_minor": 300,
        "fee_minor": 10,
    },
]


def test_query_filters_groups_and_aggregates() -> None:
    result = query_ledger_rows(
        ROWS,
        contract_id="manor.finance_ledger/v1",
        filters={"status": "POSTED", "currency": "usd"},
        group_by=["account_ref"],
        metrics=["count", "sum_amount_minor", "sum_tax_minor", "sum_outflow_minor", "sum_inflow_minor"],
    )

    assert result["matched_count"] == 2
    assert result["aggregates"] == {
        "count": 2,
        "sum_amount_minor": 6200,
        "sum_tax_minor": 400,
        "sum_inflow_minor": 5000,
        "sum_outflow_minor": 1200,
    }
    assert result["groups"] == [
        {
            "key": {"account_ref": "sales"},
            "aggregates": {
                "count": 1,
                "sum_amount_minor": 5000,
                "sum_inflow_minor": 5000,
                "sum_outflow_minor": 0,
                "sum_tax_minor": 300,
            },
        },
        {
            "key": {"account_ref": "software"},
            "aggregates": {
                "count": 1,
                "sum_amount_minor": 1200,
                "sum_inflow_minor": 0,
                "sum_outflow_minor": 1200,
                "sum_tax_minor": 100,
            },
        },
    ]
    assert result["group_count"] == 2
    assert result["groups_truncated"] is False


def test_query_supports_generic_filter_operators_on_nested_and_collection_fields() -> None:
    rows = [
        {
            "identity_key": "lead:C-346",
            "identity_aliases": ["email:ali@example.com", "skool:planet-ai"],
            "payload": {
                "intent_level": "interested",
                "follow_up_count": 1,
                "next_action": "schedule_meeting",
            },
        },
        {
            "identity_key": "lead:C-347",
            "identity_aliases": ["email:other@example.com"],
            "payload": {
                "intent_level": "cold",
                "follow_up_count": 2,
            },
        },
    ]

    result = query_ledger_rows(
        rows,
        contract_id="manor.relationship_ledger/v1",
        filters={
            "identity_aliases": {"$contains": "EMAIL:ALI@EXAMPLE.COM"},
            "payload.intent_level": {"$in": ["interested", "ready"]},
            "payload.follow_up_count": {"$lte": 1},
            "payload.next_action": {"$exists": True},
        },
    )

    assert result["matched_count"] == 1
    assert result["rows"][0]["identity_key"] == "lead:C-346"


@pytest.mark.parametrize(
    "filters",
    [
        {"contact_points": {"$contains": {"consent_status": "DO_NOT_CONTACT"}}},
        {"contact_points.consent_status": "do_not_contact"},
        {"contact_points.consent_status": {"$eq": "do_not_contact"}},
    ],
)
def test_query_filters_nested_contact_statuses_inside_arrays(filters) -> None:
    rows = [
        {
            "identity_key": "lead:blocked",
            "contact_points": [
                {
                    "kind": "email",
                    "value": "blocked@example.com",
                    "consent_status": "do_not_contact",
                    "metadata": {"source": "reply"},
                },
                {
                    "kind": "linkedin",
                    "value": "blocked",
                    "consent_status": "unknown",
                },
            ],
        },
        {
            "identity_key": "lead:available",
            "contact_points": [
                {
                    "kind": "email",
                    "value": "available@example.com",
                    "consent_status": "opted_in",
                }
            ],
        },
    ]

    result = query_ledger_rows(
        rows,
        contract_id="manor.relationship_ledger/v1",
        filters=filters,
    )

    assert result["matched_count"] == 1
    assert result["rows"][0]["identity_key"] == "lead:blocked"


def test_query_collection_contains_recursively_matches_partial_objects() -> None:
    result = query_ledger_rows(
        [
            {
                "identity_key": "lead:blocked",
                "contact_points": [
                    {
                        "kind": "email",
                        "consent_status": "do_not_contact",
                        "metadata": {"source": "human_reply", "message_id": "m-1"},
                    }
                ],
            }
        ],
        contract_id="manor.relationship_ledger/v1",
        filters={
            "contact_points": {
                "$contains": {
                    "kind": "EMAIL",
                    "metadata": {"source": "HUMAN_REPLY"},
                },
            },
        },
    )

    assert result["matched_count"] == 1


@pytest.mark.parametrize(
    ("filters", "expected_count"),
    [
        ({"identity_aliases": {"$contains_any": ["missing", "email:ali@example.com"]}}, 1),
        ({"identity_aliases": {"$contains_all": ["email:ali@example.com", "skool:planet-ai"]}}, 1),
        ({"payload.summary": {"$contains": "CREATOR PARTNER"}}, 1),
        ({"payload.follow_up_count": {"$gt": 0, "$lt": 2}}, 1),
        ({"payload.follow_up_count": {"$gte": 1, "$lte": 1}}, 1),
        ({"payload.intent_level": {"$ne": "cold"}}, 1),
        ({"payload.intent_level": {"$not_in": ["cold", "paused"]}}, 1),
        ({"payload.intent_level": {"$in": ["cold", "paused"]}}, 0),
    ],
)
def test_query_generic_filter_operator_semantics(filters, expected_count) -> None:
    result = query_ledger_rows(
        [
            {
                "identity_key": "lead:C-346",
                "identity_aliases": ["email:ali@example.com", "skool:planet-ai"],
                "payload": {
                    "summary": "Interested creator partner",
                    "intent_level": "interested",
                    "follow_up_count": 1,
                },
            }
        ],
        contract_id="manor.relationship_ledger/v1",
        filters=filters,
    )

    assert result["matched_count"] == expected_count


@pytest.mark.parametrize(
    ("operator", "operand", "expected_count"),
    [
        ("$eq", ["email", "skool"], 1),
        ("$ne", ["email", "skool"], 0),
        ("$eq", ["EMAIL", "SKOOL"], 1),
        ("$ne", ["EMAIL", "SKOOL"], 0),
        ("$eq", ["email", "linkedin"], 0),
        ("$ne", ["email", "linkedin"], 1),
        ("$eq", "email", 1),
        ("$ne", "email", 0),
    ],
)
def test_query_array_equality_and_inequality_are_complements(
    operator: str,
    operand: object,
    expected_count: int,
) -> None:
    result = query_ledger_rows(
        [{"identity_key": "lead:C-346", "channels": ["email", "skool"]}],
        contract_id="manor.relationship_ledger/v1",
        filters={"channels": {operator: operand}},
    )

    assert result["matched_count"] == expected_count


@pytest.mark.parametrize(
    ("operator", "expected_count"),
    [
        ("$in", 1),
        ("$not_in", 0),
    ],
)
def test_query_structured_membership_uses_recursive_equality(
    operator: str,
    expected_count: int,
) -> None:
    result = query_ledger_rows(
        [{"contact_points": [{"kind": "email", "status": "active"}]}],
        contract_id="manor.relationship_ledger/v1",
        filters={
            "contact_points": {
                operator: [{"kind": "EMAIL", "status": "ACTIVE"}],
            },
        },
    )

    assert result["matched_count"] == expected_count


def test_query_plain_structured_filter_uses_recursive_equality() -> None:
    result = query_ledger_rows(
        [{"payload": {"intent_level": "interested", "next_action": "schedule_meeting"}}],
        contract_id="manor.relationship_ledger/v1",
        filters={
            "payload": {
                "intent_level": "INTERESTED",
                "next_action": "SCHEDULE_MEETING",
            }
        },
    )

    assert result["matched_count"] == 1


def test_query_supports_dotted_payload_date_ranges() -> None:
    rows = [
        {
            "identity_key": "due",
            "recorded_at": "2026-08-20T10:00:00Z",
            "payload": {"next_action_at": "2026-08-26T14:30:00Z"},
        },
        {
            "identity_key": "later",
            "recorded_at": "2026-08-20T10:00:00Z",
            "payload": {"next_action_at": "2026-08-27T14:30:00Z"},
        },
    ]

    result = query_ledger_rows(
        rows,
        contract_id="manor.relationship_ledger/v1",
        date_field="payload.next_action_at",
        date_to="2026-08-26",
        timezone_name="America/Los_Angeles",
    )

    assert result["matched_count"] == 1
    assert result["rows"][0]["identity_key"] == "due"


@pytest.mark.parametrize(
    "filters",
    [
        {"status": {"$unknown": "active"}},
        {"status": {"$in": "active"}},
        {"status": {"$exists": "yes"}},
        {"status": {"$eq": "active", "value": "active"}},
    ],
)
def test_query_rejects_invalid_generic_filter_operators(filters) -> None:
    with pytest.raises(LedgerQueryError, match="filter"):
        query_ledger_rows(
            [{"identity_key": "lead:1", "status": "active"}],
            contract_id="manor.relationship_ledger/v1",
            filters=filters,
        )


def test_query_validates_filters_before_scanning_rows() -> None:
    for rows, filters in (
        ([], {"payload.x": {"$unknown": 1}}),
        (
            [{"status": "active"}],
            {"status": "missing", "payload.x": {"$in": "not-an-array"}},
        ),
    ):
        with pytest.raises(LedgerQueryError, match="filter"):
            query_ledger_rows(
                rows,
                contract_id="manor.relationship_ledger/v1",
                filters=filters,
            )


def test_query_rejects_lexical_fallback_for_range_filters() -> None:
    with pytest.raises(LedgerQueryError, match="range filters"):
        query_ledger_rows(
            [{"payload": {"next_action_at": "not-a-date"}}],
            contract_id="manor.relationship_ledger/v1",
            filters={"payload.next_action_at": {"$lte": "2026-08-26T23:59:59Z"}},
        )


def test_query_rejects_invalid_range_operand_before_scanning_rows() -> None:
    with pytest.raises(LedgerQueryError, match="number or ISO-8601 date"):
        query_ledger_rows(
            [],
            contract_id="manor.relationship_ledger/v1",
            filters={"payload.next_action_at": {"$lte": "not-a-date"}},
        )


@pytest.mark.parametrize(
    ("granularity", "expected"),
    [
        (
            "day",
            [
                {"key": {"occurred_at": "2026-07-31"}, "aggregates": {"count": 1}},
                {"key": {"occurred_at": "2026-08-01"}, "aggregates": {"count": 2}},
            ],
        ),
        (
            "week",
            [{"key": {"occurred_at": "2026-07-27"}, "aggregates": {"count": 3}}],
        ),
        (
            "month",
            [
                {"key": {"occurred_at": "2026-07-01"}, "aggregates": {"count": 1}},
                {"key": {"occurred_at": "2026-08-01"}, "aggregates": {"count": 2}},
            ],
        ),
    ],
)
def test_query_date_groups_use_user_timezone_buckets(granularity, expected) -> None:
    rows = [
        {"entry_id": "before-midnight", "occurred_at": "2026-08-01T06:30:00Z"},
        {"entry_id": "after-midnight", "occurred_at": "2026-08-01T07:30:00Z"},
        {"entry_id": "late-same-day", "occurred_at": "2026-08-02T06:30:00Z"},
    ]

    result = query_ledger_rows(
        rows,
        contract_id="manor.finance_ledger/v1",
        group_by=["occurred_at"],
        metrics=["count"],
        date_granularity=granularity,
        timezone_name="America/Los_Angeles",
    )

    assert result["date_granularity"] == granularity
    assert result["groups"] == expected


def test_query_rejects_metrics_outside_the_ledger_contract() -> None:
    with pytest.raises(LedgerQueryError, match="not allowed for manor.recruiting_ledger/v1"):
        query_ledger_rows(
            [{"record_key": "candidate:1"}],
            contract_id="manor.recruiting_ledger/v1",
            metrics=["sum_amount_minor"],
        )

    with pytest.raises(LedgerQueryError, match="requires grouping by"):
        query_ledger_rows(
            [{"record_key": "candidate:1"}],
            contract_id="manor.recruiting_ledger/v1",
            group_by=["stage"],
            metrics=["count"],
            date_granularity="week",
        )


def test_query_group_output_is_bounded_independently_of_row_limit() -> None:
    rows = [
        {
            "identity_key": f"customer:{index}",
            "recorded_at": "2026-08-23T10:00:00Z",
        }
        for index in range(1000)
    ]

    result = query_ledger_rows(
        rows,
        contract_id="manor.relationship_ledger/v1",
        group_by=["identity_key"],
        metrics=["count"],
        limit=1,
    )

    assert len(result["rows"]) == 1
    assert len(result["groups"]) == MAX_QUERY_GROUPS
    assert result["group_count"] == 1000
    assert result["groups_truncated"] is True


def test_query_visualization_reports_truncated_groups_separately_from_rows() -> None:
    from packages.core.ai.tools.ledger_query_tools import _query_ledger_visualization

    rows = [
        {
            "identity_key": f"customer:{index}",
            "recorded_at": "2026-08-23T10:00:00Z",
        }
        for index in range(MAX_QUERY_GROUPS + 1)
    ]
    query = query_ledger_rows(
        rows,
        contract_id="manor.relationship_ledger/v1",
        group_by=["identity_key"],
        metrics=["count"],
        limit=500,
    )

    visualization = _query_ledger_visualization(
        query,
        contract_id="manor.relationship_ledger/v1",
        view="current",
        group_by=["identity_key"],
        metrics=["count"],
        filters=None,
    )

    assert query["next_cursor"] is None
    assert query["groups_truncated"] is True
    assert visualization["has_more"] is False
    assert visualization["group_count"] == MAX_QUERY_GROUPS + 1
    assert visualization["groups_truncated"] is True


def test_query_group_limit_keeps_the_largest_business_groups() -> None:
    rows = [
        {
            "identity_key": f"customer:{index:02d}",
            "recorded_at": "2026-08-23T10:00:00Z",
        }
        for index in range(MAX_QUERY_GROUPS)
    ]
    rows.extend(
        {
            "identity_key": "customer:dominant",
            "recorded_at": "2026-08-23T10:00:00Z",
        }
        for _index in range(100)
    )

    result = query_ledger_rows(
        rows,
        contract_id="manor.relationship_ledger/v1",
        group_by=["identity_key"],
        metrics=["count"],
        limit=500,
    )

    dominant = next(group for group in result["groups"] if group["key"]["identity_key"] == "customer:dominant")
    assert dominant["aggregates"]["count"] == 100
    assert result["next_cursor"] is None
    assert result["group_count"] == MAX_QUERY_GROUPS + 1
    assert result["groups_truncated"] is True


def test_query_timeline_group_limit_keeps_the_latest_periods() -> None:
    rows = [
        {
            "recorded_at": f"2026-08-{index + 1:02d}T10:00:00Z",
        }
        for index in range(MAX_QUERY_GROUPS + 1)
    ]

    result = query_ledger_rows(
        rows,
        contract_id="manor.relationship_ledger/v1",
        group_by=["recorded_at"],
        metrics=["count"],
        limit=500,
    )

    timestamps = [group["key"]["recorded_at"] for group in result["groups"]]
    assert "2026-08-01T10:00:00Z" not in timestamps
    assert timestamps[-1] == "2026-08-25T10:00:00Z"


@pytest.mark.asyncio
async def test_finance_query_rejects_mixed_currency_monetary_aggregate(
    monkeypatch,
) -> None:
    from packages.core.services import ledger_query_service as module

    async def fake_read(**_kwargs):
        return {
            "entries": [
                {
                    "entry_id": "usd-expense",
                    "status": "posted",
                    "occurred_at": "2026-08-01T09:00:00+00:00",
                    "currency": "USD",
                    "direction": "outflow",
                    "amount_minor": 10_000,
                },
                {
                    "entry_id": "eur-expense",
                    "status": "posted",
                    "occurred_at": "2026-08-02T09:00:00+00:00",
                    "currency": "EUR",
                    "direction": "outflow",
                    "amount_minor": 10_000,
                },
            ],
        }

    monkeypatch.setattr(module, "read_finance_ledger", fake_read)

    with pytest.raises(LedgerQueryError) as exc_info:
        await module.query_finance_ledger(
            entity_id="entity-1",
            workspace_id="workspace-1",
            metrics=["sum_outflow_minor"],
        )

    assert exc_info.value.code == "mixed_currency_aggregate"
    usd_result = await module.query_finance_ledger(
        entity_id="entity-1",
        workspace_id="workspace-1",
        filters={"currency": "USD"},
        metrics=["sum_outflow_minor"],
    )
    assert usd_result["aggregates"]["sum_outflow_minor"] == 10_000


@pytest.mark.asyncio
async def test_finance_query_uses_effective_projection_for_default_monetary_totals(
    monkeypatch,
) -> None:
    from packages.core.services import ledger_query_service as module

    async def fake_read(**_kwargs):
        return {
            "entries": [
                {
                    "entry_id": "income-posted",
                    "status": "posted",
                    "currency": "USD",
                    "direction": "inflow",
                    "amount_minor": 100,
                },
                {
                    "entry_id": "income-planned",
                    "status": "planned",
                    "currency": "USD",
                    "direction": "inflow",
                    "amount_minor": 900,
                },
                {
                    "entry_id": "expense-void",
                    "status": "void",
                    "currency": "USD",
                    "direction": "outflow",
                    "amount_minor": 400,
                },
                {
                    "entry_id": "expense-original",
                    "status": "posted",
                    "currency": "USD",
                    "direction": "outflow",
                    "amount_minor": 50,
                },
                {
                    "entry_id": "expense-corrected",
                    "status": "reconciled",
                    "currency": "USD",
                    "direction": "outflow",
                    "amount_minor": 60,
                    "supersedes_entry_id": "expense-original",
                },
            ],
        }

    monkeypatch.setattr(module, "read_finance_ledger", fake_read)
    effective = await module.query_finance_ledger(
        entity_id="entity-1",
        workspace_id="workspace-1",
        metrics=["sum_inflow_minor", "sum_outflow_minor"],
    )
    planned = await module.query_finance_ledger(
        entity_id="entity-1",
        workspace_id="workspace-1",
        filters={"status": "planned"},
        metrics=["sum_inflow_minor"],
    )

    assert effective["aggregates"] == {
        "sum_inflow_minor": 100,
        "sum_outflow_minor": 60,
    }
    assert effective["projection_kind"] == "current"
    assert planned["aggregates"]["sum_inflow_minor"] == 900
    assert planned["projection_kind"] == "events"


def test_query_visualization_uses_grouped_bar_layout_and_bounded_data() -> None:
    from packages.core.ai.tools.ledger_query_tools import _query_ledger_visualization

    query = query_ledger_rows(
        ROWS,
        contract_id="manor.finance_ledger/v1",
        group_by=["account_ref"],
        metrics=["count", "sum_amount_minor"],
    )
    query["rows"][0]["payload"] = {"unsafe": "not persisted in visualization"}
    visualization = _query_ledger_visualization(
        query,
        contract_id="manor.finance_ledger/v1",
        view="events",
        group_by=["account_ref"],
        metrics=["count", "sum_amount_minor"],
        filters={"currency": "usd"},
    )

    assert visualization["layout"] == "bar"
    assert visualization["matched_count"] == 3
    assert visualization["currency"] == "USD"
    assert visualization["groups"][0]["key"] == {"account_ref": "sales"}
    assert "payload" not in visualization["columns"]
    assert all("payload" not in row for row in visualization["rows"])
    assert len(visualization["columns"]) <= 10
    assert len(visualization["rows"]) <= 20
    assert len(visualization["query_fingerprint"]) == 64
    assert visualization["coalesce_previous_visualization"] is False


def test_query_visualization_drops_unapproved_group_fields() -> None:
    from packages.core.ai.tools.ledger_query_tools import _query_ledger_visualization

    secret = "CONFIDENTIAL acquisition target"
    query = query_ledger_rows(
        [
            {
                "identity_key": "customer:1",
                "recorded_at": "2026-08-23T10:00:00Z",
                "payload": {"private_notes": secret},
            }
        ],
        contract_id="manor.relationship_ledger/v1",
        group_by=["payload.private_notes"],
        metrics=["count"],
    )

    visualization = _query_ledger_visualization(
        query,
        contract_id="manor.relationship_ledger/v1",
        view="current",
        group_by=["payload.private_notes"],
        metrics=["count"],
        filters=None,
    )

    assert visualization["group_by"] == []
    assert visualization["groups"] == []
    assert visualization["group_count"] == 0
    assert visualization["groups_truncated"] is False
    assert secret not in json.dumps(visualization)


def test_group_by_allows_safe_contract_business_dimensions() -> None:
    from packages.core.ai.tools.ledger_query_tools import _validated_group_by

    assert _validated_group_by(
        "manor.recruiting_ledger/v1",
        ["location", "subject_type", "channel"],
    ) == ("location", "subject_type", "channel")
    assert _validated_group_by(
        "manor.relationship_ledger/v1",
        ["subject_type", "channel"],
    ) == ("subject_type", "channel")


def test_query_visualization_fingerprint_distinguishes_overlapping_query_scopes() -> None:
    from packages.core.ai.tools.ledger_query_tools import _query_ledger_visualization

    query = query_ledger_rows(
        ROWS,
        contract_id="manor.finance_ledger/v1",
    )
    common = {
        "contract_id": "manor.finance_ledger/v1",
        "view": "events",
        "group_by": [],
        "metrics": [],
    }
    all_rows = _query_ledger_visualization(
        query,
        filters=None,
        **common,
    )
    inflow_rows = _query_ledger_visualization(
        query,
        filters={"direction": "inflow"},
        **common,
    )

    assert all_rows["query_fingerprint"] != inflow_rows["query_fingerprint"]


def test_query_visualization_fingerprint_includes_ledger_revision() -> None:
    from packages.core.ai.tools.ledger_query_tools import _query_ledger_visualization

    query = query_ledger_rows(
        ROWS,
        contract_id="manor.finance_ledger/v1",
    )
    common = {
        "contract_id": "manor.finance_ledger/v1",
        "view": "events",
        "group_by": [],
        "metrics": [],
        "filters": None,
    }

    before = _query_ledger_visualization(
        {**query, "ledger_revision": "revision-before"},
        **common,
    )
    after = _query_ledger_visualization(
        {**query, "ledger_revision": "revision-after"},
        **common,
    )

    assert before["query_fingerprint"] != after["query_fingerprint"]


def test_query_visualization_selects_metrics_timeline_table_and_empty_layouts() -> None:
    from packages.core.ai.tools.ledger_query_tools import _query_ledger_visualization

    base = query_ledger_rows(
        ROWS,
        contract_id="manor.finance_ledger/v1",
        metrics=["count", "sum_inflow_minor", "sum_outflow_minor"],
    )
    metrics = _query_ledger_visualization(
        base,
        contract_id="manor.finance_ledger/v1",
        view="events",
        group_by=[],
        metrics=["count", "sum_inflow_minor", "sum_outflow_minor"],
        filters=None,
    )
    timeline_query = query_ledger_rows(
        ROWS,
        contract_id="manor.finance_ledger/v1",
        group_by=["occurred_at"],
        metrics=["count"],
    )
    timeline = _query_ledger_visualization(
        timeline_query,
        contract_id="manor.finance_ledger/v1",
        view="events",
        group_by=["occurred_at"],
        metrics=["count"],
        filters=None,
    )
    table_query = query_ledger_rows(
        ROWS,
        contract_id="manor.finance_ledger/v1",
    )
    table = _query_ledger_visualization(
        table_query,
        contract_id="manor.finance_ledger/v1",
        view="events",
        group_by=[],
        metrics=[],
        filters=None,
    )
    empty_query = query_ledger_rows(
        ROWS,
        contract_id="manor.finance_ledger/v1",
        filters={"status": "missing"},
    )
    empty = _query_ledger_visualization(
        empty_query,
        contract_id="manor.finance_ledger/v1",
        view="events",
        group_by=[],
        metrics=[],
        filters=None,
    )

    assert metrics["layout"] == "metrics"
    assert timeline["layout"] == "timeline"
    assert table["layout"] == "table"
    assert table["columns"] == [
        "entry_id",
        "occurred_at",
        "entry_type",
        "status",
        "direction",
        "currency",
        "amount_minor",
        "account_ref",
    ]
    assert empty["layout"] == "empty"


def test_query_visualization_accepts_bounded_composable_presentations() -> None:
    from packages.core.ai.tools.ledger_query_tools import (
        _query_ledger_visualization,
        _validated_query_presentation,
    )

    presentation = _validated_query_presentation(
        "manor.finance_ledger/v1",
        {
            "title": "Cash development",
            "subtitle": "Observed and projected",
            "sections": [
                {"type": "metrics", "metrics": ["sum_inflow_minor", "sum_outflow_minor"]},
                {"type": "chart", "chart": "line", "metric": "sum_amount_minor"},
                {"type": "projection", "metric": "sum_amount_minor", "forecast_periods": 3},
            ],
        },
        group_by=("occurred_at",),
        date_granularity="month",
    )
    assert presentation == {
        "version": 1,
        "title": "Cash development",
        "subtitle": "Observed and projected",
        "sections": [
            {"type": "metrics", "metrics": ["sum_inflow_minor", "sum_outflow_minor"]},
            {"type": "chart", "metric": "sum_amount_minor", "chart": "line"},
            {"type": "projection", "metric": "sum_amount_minor", "forecast_periods": 3},
        ],
    }
    query = query_ledger_rows(
        ROWS,
        contract_id="manor.finance_ledger/v1",
        group_by=["occurred_at"],
        metrics=["sum_amount_minor", "sum_inflow_minor", "sum_outflow_minor"],
        date_granularity="month",
    )
    visualization = _query_ledger_visualization(
        query,
        contract_id="manor.finance_ledger/v1",
        view="events",
        group_by=["occurred_at"],
        metrics=["sum_amount_minor", "sum_inflow_minor", "sum_outflow_minor"],
        date_granularity="month",
        filters={"currency": "USD"},
        presentation=presentation,
    )
    default_visualization = _query_ledger_visualization(
        query,
        contract_id="manor.finance_ledger/v1",
        view="events",
        group_by=["occurred_at"],
        metrics=["sum_amount_minor", "sum_inflow_minor", "sum_outflow_minor"],
        date_granularity="month",
        filters={"currency": "USD"},
    )

    assert visualization["presentation"] == presentation
    assert visualization["date_granularity"] == "month"
    assert "presentation" not in default_visualization
    assert visualization["query_fingerprint"] != default_visualization["query_fingerprint"]


def test_query_presentation_rejects_unsafe_or_incompatible_recipes() -> None:
    from packages.core.ai.tools.ledger_query_tools import (
        _validated_metrics,
        _validated_query_presentation,
    )

    assert _validated_metrics(
        "manor.finance_ledger/v1",
        ["count", "sum_amount_minor", "count"],
    ) == ("count", "sum_amount_minor")
    with pytest.raises(LedgerQueryError, match="not allowed for manor.recruiting_ledger/v1"):
        _validated_metrics(
            "manor.recruiting_ledger/v1",
            ["sum_amount_minor"],
        )

    with pytest.raises(LedgerQueryError, match="chart presentation requires group_by"):
        _validated_query_presentation(
            "manor.relationship_ledger/v1",
            {"sections": [{"type": "chart", "chart": "pie"}]},
            group_by=(),
        )
    with pytest.raises(LedgerQueryError, match="date-based group_by"):
        _validated_query_presentation(
            "manor.relationship_ledger/v1",
            {"sections": [{"type": "projection"}]},
            group_by=("stage",),
        )
    with pytest.raises(LedgerQueryError, match="requires date_granularity"):
        _validated_query_presentation(
            "manor.recruiting_ledger/v1",
            {"sections": [{"type": "projection", "metric": "count"}]},
            group_by=("occurred_at",),
        )
    with pytest.raises(LedgerQueryError, match="exactly one date-based group_by field"):
        _validated_query_presentation(
            "manor.recruiting_ledger/v1",
            {"sections": [{"type": "projection", "metric": "count"}]},
            group_by=("occurred_at", "stage"),
            date_granularity="month",
        )
    with pytest.raises(LedgerQueryError, match="not allowed for manor.recruiting_ledger/v1"):
        _validated_query_presentation(
            "manor.recruiting_ledger/v1",
            {"sections": [{"type": "metrics", "metric": "sum_amount_minor"}]},
            group_by=(),
        )
    with pytest.raises(LedgerQueryError, match="not allowed"):
        _validated_query_presentation(
            "manor.recruiting_ledger/v1",
            {"sections": [{"type": "profile", "columns": ["private_notes"]}]},
            group_by=(),
        )


def test_query_date_range_and_cursor_are_snapshot_stable() -> None:
    first = query_ledger_rows(
        ROWS,
        contract_id="manor.finance_ledger/v1",
        date_field="occurred_at",
        date_from="2026-08-02",
        date_to="2026-08-03T23:59:59Z",
        limit=1,
    )
    assert first["matched_count"] == 2
    assert [row["entry_id"] for row in first["rows"]] == ["entry-3"]
    assert first["next_cursor"]

    second = query_ledger_rows(
        ROWS,
        contract_id="manor.finance_ledger/v1",
        date_field="occurred_at",
        date_from="2026-08-02",
        date_to="2026-08-03T23:59:59Z",
        limit=1,
        cursor=first["next_cursor"],
    )
    assert [row["entry_id"] for row in second["rows"]] == ["entry-2"]
    assert second["next_cursor"] is None

    with pytest.raises(LedgerQueryError, match="Ledger changed"):
        query_ledger_rows(
            [*ROWS, {**ROWS[0], "entry_id": "entry-4"}],
            contract_id="manor.finance_ledger/v1",
            limit=1,
            cursor=first["next_cursor"],
        )


def test_query_cursor_is_bound_to_filters() -> None:
    first = query_ledger_rows(
        ROWS,
        contract_id="manor.finance_ledger/v1",
        limit=1,
    )

    with pytest.raises(LedgerQueryError, match="Query changed"):
        query_ledger_rows(
            ROWS,
            contract_id="manor.finance_ledger/v1",
            filters={"status": "posted"},
            limit=1,
            cursor=first["next_cursor"],
        )


def test_query_cursor_is_bound_to_date_granularity() -> None:
    first = query_ledger_rows(
        ROWS,
        contract_id="manor.finance_ledger/v1",
        group_by=["occurred_at"],
        metrics=["count"],
        date_granularity="day",
        limit=1,
    )

    with pytest.raises(LedgerQueryError, match="Query changed"):
        query_ledger_rows(
            ROWS,
            contract_id="manor.finance_ledger/v1",
            group_by=["occurred_at"],
            metrics=["count"],
            date_granularity="month",
            limit=1,
            cursor=first["next_cursor"],
        )


def test_query_cursor_and_visualization_fingerprint_are_bound_to_timezone() -> None:
    from packages.core.ai.tools.ledger_query_tools import _query_ledger_visualization

    rows = [
        {"entry_id": "before-midnight", "occurred_at": "2026-08-01T06:30:00Z"},
        {"entry_id": "after-midnight", "occurred_at": "2026-08-01T07:30:00Z"},
    ]
    common = {
        "contract_id": "manor.recruiting_ledger/v1",
        "group_by": ["occurred_at"],
        "metrics": ["count"],
        "date_granularity": "day",
        "limit": 1,
    }
    los_angeles = query_ledger_rows(
        rows,
        timezone_name="America/Los_Angeles",
        **common,
    )
    utc = query_ledger_rows(rows, timezone_name="UTC", **common)

    with pytest.raises(LedgerQueryError, match="Query changed"):
        query_ledger_rows(
            rows,
            timezone_name="UTC",
            cursor=los_angeles["next_cursor"],
            **common,
        )

    def visualization(result: dict) -> dict:
        return _query_ledger_visualization(
            result,
            contract_id="manor.recruiting_ledger/v1",
            view="current",
            group_by=["occurred_at"],
            metrics=["count"],
            date_granularity="day",
            filters=None,
        )

    assert visualization(los_angeles)["query_fingerprint"] != visualization(utc)["query_fingerprint"]


def test_query_cursor_is_bound_to_the_selected_projection() -> None:
    current_rows = ROWS[:2]
    first = query_ledger_rows(
        current_rows,
        revision_rows=ROWS,
        contract_id="manor.finance_ledger/v1",
        limit=1,
    )

    with pytest.raises(LedgerQueryError, match="Query changed"):
        query_ledger_rows(
            ROWS,
            revision_rows=ROWS,
            contract_id="manor.finance_ledger/v1",
            limit=1,
            cursor=first["next_cursor"],
        )


def test_query_date_only_upper_bound_includes_the_whole_calendar_day() -> None:
    rows = [
        {
            "entry_id": "same-day",
            "occurred_at": "2026-08-31T23:59:59.999999Z",
        },
        {
            "entry_id": "next-day",
            "occurred_at": "2026-09-01T00:00:00Z",
        },
    ]

    result = query_ledger_rows(
        rows,
        contract_id="manor.finance_ledger/v1",
        date_field="occurred_at",
        date_to="2026-08-31",
    )

    assert [row["entry_id"] for row in result["rows"]] == ["same-day"]


def test_query_compact_date_only_upper_bound_includes_the_whole_calendar_day() -> None:
    rows = [
        {
            "entry_id": "same-day",
            "occurred_at": "2026-08-31T12:00:00Z",
        },
        {
            "entry_id": "next-day",
            "occurred_at": "2026-09-01T00:00:00Z",
        },
    ]

    result = query_ledger_rows(
        rows,
        contract_id="manor.finance_ledger/v1",
        date_field="occurred_at",
        date_to="20260831",
    )

    assert [row["entry_id"] for row in result["rows"]] == ["same-day"]


def test_query_date_only_range_uses_the_user_timezone() -> None:
    rows = [
        {
            "entry_id": "previous-local-day",
            "occurred_at": "2026-08-31T06:59:59Z",
        },
        {
            "entry_id": "same-local-day",
            "occurred_at": "2026-09-01T06:30:00Z",
        },
        {
            "entry_id": "next-local-day",
            "occurred_at": "2026-09-01T07:00:00Z",
        },
    ]

    result = query_ledger_rows(
        rows,
        contract_id="manor.finance_ledger/v1",
        date_field="occurred_at",
        date_from="2026-08-31",
        date_to="2026-08-31",
        timezone_name="America/Los_Angeles",
    )

    assert [row["entry_id"] for row in result["rows"]] == ["same-local-day"]


@pytest.mark.parametrize("date_to", ["9999-12-31", "99991231"])
def test_query_maximum_date_only_upper_bound_does_not_overflow(date_to) -> None:
    result = query_ledger_rows(
        [],
        contract_id="manor.finance_ledger/v1",
        date_to=date_to,
        timezone_name="America/Los_Angeles",
    )

    assert result["matched_count"] == 0


@pytest.mark.parametrize("date_from", ["9999-12-31", "99991231"])
def test_query_maximum_date_only_lower_bound_does_not_overflow(date_from) -> None:
    result = query_ledger_rows(
        [
            {
                "entry_id": "previous-local-day",
                "occurred_at": "9999-12-31T07:59:59Z",
            },
            {
                "entry_id": "same-local-day",
                "occurred_at": "9999-12-31T08:00:00Z",
            },
        ],
        contract_id="manor.finance_ledger/v1",
        date_field="occurred_at",
        date_from=date_from,
        timezone_name="America/Los_Angeles",
    )

    assert [row["entry_id"] for row in result["rows"]] == ["same-local-day"]


def test_query_rejects_invalid_cursor_and_limit() -> None:
    with pytest.raises(LedgerQueryError, match="cursor is invalid"):
        query_ledger_rows(ROWS, contract_id="manor.finance_ledger/v1", cursor="not-a-cursor")
    with pytest.raises(LedgerQueryError, match="between 1 and 500"):
        query_ledger_rows(ROWS, contract_id="manor.finance_ledger/v1", limit=0)


def test_query_tool_is_registered_as_read_only() -> None:
    from packages.core.ai.runtime.approval_classifier import classify_runtime_tool
    from packages.core.ai.runtime.tool_effect_classification import RuntimeToolEffect
    from packages.core.ai.tools.ledger_query_tools import get_legacy_tools, get_tools

    schema, _handler = get_tools()[0]
    assert schema["function"]["name"] == "query_ledger"
    assert schema["function"]["parameters"]["properties"]["group_by"]["maxItems"] == 3
    assert schema["function"]["parameters"]["properties"]["date_granularity"]["enum"] == [
        "day",
        "month",
        "week",
    ]
    assert "payload.private_notes" not in (schema["function"]["parameters"]["properties"]["group_by"]["items"]["enum"])
    assert schema["function"]["parameters"]["properties"]["limit"]["maximum"] == 20
    assert schema["function"]["parameters"]["properties"]["presentation"]["properties"]["sections"]["maxItems"] == 4
    assert schema["function"]["parameters"]["properties"]["coalesce_previous_visualization"]["type"] == "boolean"
    assert classify_runtime_tool("query_ledger", {}).effect is RuntimeToolEffect.READ_ONLY
    visualization_schema, _visualization_handler = get_legacy_tools()[0]
    assert visualization_schema["function"]["name"] == "visualize_workspace_ledgers"
    assert classify_runtime_tool("visualize_workspace_ledgers", {}).effect is RuntimeToolEffect.READ_ONLY


@pytest.mark.asyncio
async def test_content_query_reads_projection_or_events_without_creating_storage(monkeypatch) -> None:
    from packages.core.services import ledger_query_service as module

    calls: dict = {}

    async def fake_read(**kwargs):
        calls.update(kwargs)
        return {
            "rows": [{"entry_id": "reservation-1", "status": "video_ready", "recorded_at": "2026-08-01T00:00:00Z"}],
            "recent_entries": [{"event_id": "event-1", "status": "video_ready", "recorded_at": "2026-08-01T00:00:00Z"}],
        }

    monkeypatch.setattr(module, "read_content_ledger", fake_read)
    result = await module.query_content_ledger(
        entity_id="entity-1",
        workspace_id="workspace-1",
        view="events",
        metrics=["count"],
    )

    assert calls["create_location"] is False
    assert calls["recent_limit"] == 0
    assert result["matched_count"] == 1
    assert result["rows"][0]["event"] == "video_ready"


@pytest.mark.asyncio
async def test_relationship_query_reads_current_projection_or_events(monkeypatch) -> None:
    from packages.core.services import ledger_query_service as module

    async def fake_read(**kwargs):
        assert kwargs["create_location"] is False
        return {
            "rows": [{"identity_key": "customer:1", "status": "active", "recorded_at": "2026-08-01T00:00:00Z"}],
            "entries": [
                {
                    "event_id": "event-1",
                    "event": "call_completed",
                    "status": "active",
                    "recorded_at": "2026-08-01T00:00:00Z",
                }
            ],
        }

    monkeypatch.setattr(module, "read_relationship_ledger", fake_read)
    current = await module.query_relationship_ledger(
        entity_id="entity-1",
        workspace_id="workspace-1",
        filters={"status": "active"},
    )
    events = await module.query_relationship_ledger(
        entity_id="entity-1",
        workspace_id="workspace-1",
        view="events",
    )
    assert current["matched_count"] == 1
    assert current["rows"][0]["identity_key"] == "customer:1"
    assert events["matched_count"] == 1
    assert events["rows"][0]["event"] == "call_completed"


@pytest.mark.asyncio
async def test_recruiting_query_reads_current_projection_or_events(monkeypatch) -> None:
    from packages.core.services import ledger_query_service as module

    async def fake_read(**kwargs):
        assert kwargs["create_location"] is False
        return {
            "rows": [{"record_key": "candidate:1", "stage": "offer", "recorded_at": "2026-08-01T00:00:00Z"}],
            "entries": [
                {"event_id": "event-1", "event": "offer_sent", "stage": "offer", "recorded_at": "2026-08-01T00:00:00Z"}
            ],
        }

    monkeypatch.setattr(module, "read_recruiting_ledger", fake_read)
    current = await module.query_recruiting_ledger(
        entity_id="entity-1",
        workspace_id="workspace-1",
        filters={"stage": "offer"},
    )
    events = await module.query_recruiting_ledger(
        entity_id="entity-1",
        workspace_id="workspace-1",
        view="events",
    )
    assert current["rows"][0]["record_key"] == "candidate:1"
    assert events["rows"][0]["event"] == "offer_sent"


@pytest.mark.asyncio
async def test_recruiting_query_projects_safe_channel_alias_for_grouping(
    monkeypatch,
) -> None:
    from packages.core.services import ledger_query_service as module

    async def fake_read(**_kwargs):
        rows = [
            {
                "record_key": "candidate:1",
                "recorded_at": "2026-08-02T00:00:00Z",
                "payload": {"channel": "referral", "private_notes": "secret"},
            },
            {
                "record_key": "candidate:2",
                "recorded_at": "2026-08-01T00:00:00Z",
                "payload": {"channel": "inbound"},
            },
        ]
        return {"rows": rows, "entries": rows}

    monkeypatch.setattr(module, "read_recruiting_ledger", fake_read)
    result = await module.query_recruiting_ledger(
        entity_id="entity-1",
        workspace_id="workspace-1",
        group_by=["channel"],
        metrics=["count"],
    )

    assert result["groups"] == [
        {"key": {"channel": "inbound"}, "aggregates": {"count": 1}},
        {"key": {"channel": "referral"}, "aggregates": {"count": 1}},
    ]
    assert result["rows"][0]["channel"] == "referral"


def test_public_customer_surface_never_exposes_ledger_query() -> None:
    from packages.core.ai.runtime.surfaces import ChatSurface
    from packages.core.ai.runtime.tool_surface import runtime_public_agent_tool_surface

    _schemas, allowed = runtime_public_agent_tool_surface(
        surface=ChatSurface.PUBLIC_CUSTOMER_CHAT,
        bound_tool_names={"read_content_ledger"},
        allowed_tool_names={"read_content_ledger", "query_ledger"},
    )

    assert "read_content_ledger" in allowed
    assert "query_ledger" not in allowed


@pytest.mark.asyncio
async def test_query_tool_rejects_unbound_and_external_calls_before_storage_access() -> None:
    from types import SimpleNamespace

    from packages.core.ai.runtime.profiles import RuntimeProfile
    from packages.core.ai.runtime.surfaces import ChatSurface
    from packages.core.ai.tools.ledger_query_tools import _query_ledger

    unbound = await _query_ledger(
        "entity-1",
        workspace_id="workspace-1",
        contract_id="manor.content_ledger/v1",
        _allowed_tool_names_from_context=["query_ledger"],
    )
    assert json.loads(unbound)["error"]["code"] == "ledger_contract_not_bound"

    external = await _query_ledger(
        "entity-1",
        workspace_id="workspace-1",
        contract_id="manor.content_ledger/v1",
        _runtime_envelope_from_context=SimpleNamespace(
            surface=ChatSurface.PUBLIC_CUSTOMER_CHAT,
            profile=RuntimeProfile.EXTERNAL_CUSTOMER_SAFE,
        ),
    )
    assert json.loads(external)["error"]["code"] == "ledger_query_not_available_on_external_surface"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "group_by",
    [
        ["payload.private_notes"],
        ["status", "stage", "relationship_type", "event"],
    ],
)
async def test_query_tool_rejects_unapproved_group_fields_before_storage_access(
    monkeypatch,
    group_by,
) -> None:
    from packages.core import database
    from packages.core.ai.tools.ledger_query_tools import _query_ledger

    def storage_must_not_run():
        raise AssertionError("storage should not run for an invalid group_by")

    monkeypatch.setattr(database, "async_session", storage_must_not_run)
    result = json.loads(
        await _query_ledger(
            "entity-1",
            workspace_id="workspace-1",
            contract_id="manor.relationship_ledger/v1",
            group_by=group_by,
            _allowed_tool_names_from_context=[
                "query_ledger",
                "read_relationship_ledger",
            ],
        )
    )

    assert result["error"]["code"] == "invalid_query"
    assert "group_by" in result["error"]["message"]


@pytest.mark.asyncio
async def test_query_tool_returns_a_host_rendered_visualization_spec(monkeypatch) -> None:
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    from packages.core import database
    from packages.core.ai.tools import ledger_query_tools as module

    class Result:
        def __init__(self, value):
            self.value = value

        def scalar_one_or_none(self):
            return self.value

    class Session:
        def __init__(self):
            self.call_count = 0

        async def execute(self, _statement):
            self.call_count += 1
            if self.call_count == 1:
                return Result(SimpleNamespace(settings={"ledger_contracts": ["manor.recruiting_ledger/v1"]}))
            return Result("America/Los_Angeles")

    @asynccontextmanager
    async def fake_session():
        yield Session()

    async def fake_query(**kwargs):
        assert kwargs["group_by"] == ["stage"]
        assert kwargs["limit"] == 20
        assert kwargs["timezone_name"] == "America/Los_Angeles"
        return {
            "contract_id": "manor.recruiting_ledger/v1",
            "schema_version": 1,
            "ledger_revision": "revision-1",
            "matched_count": 5,
            "rows": [
                {
                    "record_key": "candidate:1",
                    "stage": "screening",
                    "occurred_at": "2026-08-22T16:30:00Z",
                    "payload": {
                        "channel": "email",
                        "private_notes": "x" * 1_000_000,
                    },
                }
            ],
            "aggregates": {"count": 5},
            "groups": [
                {"key": {"stage": "screening"}, "aggregates": {"count": 3}},
                {"key": {"stage": "offer"}, "aggregates": {"count": 2}},
            ],
            "group_count": 2,
            "groups_truncated": False,
            "next_cursor": None,
            "as_of": "2026-08-23T17:00:00Z",
        }

    monkeypatch.setattr(database, "async_session", fake_session)
    monkeypatch.setattr(
        module,
        "workspace_queryable_ledger_configs",
        lambda _settings: {
            "manor.recruiting_ledger/v1": {
                "contract_id": "manor.recruiting_ledger/v1",
                "directory": "recruiting-ledger",
                "legacy_directories": (),
                "legacy_identity_fields": (),
            },
        },
    )
    monkeypatch.setattr(module, "query_recruiting_ledger", fake_query)

    result = json.loads(
        await module._query_ledger(
            "entity-1",
            workspace_id="workspace-1",
            contract_id="recruiting_ledger",
            group_by=["stage"],
            metrics=["count"],
            coalesce_previous_visualization=True,
            _user_id_from_context="user-1",
            _allowed_tool_names_from_context=["read_recruiting_ledger", "query_ledger"],
        )
    )

    assert result["ok"] is True
    assert result["visualization"]["kind"] == "ledger_query_result"
    assert result["visualization"]["data"]["layout"] == "bar"
    assert result["visualization"]["data"]["groups"][1] == {
        "key": {"stage": "offer"},
        "aggregates": {"count": 2},
    }
    assert result["visualization"]["data"]["coalesce_previous_visualization"] is True
    assert len(result["visualization"]["data"]["query_fingerprint"]) == 64
    assert "occurred_at" in result["visualization"]["data"]["columns"]
    assert "channel" in result["visualization"]["data"]["columns"]
    assert result["visualization"]["data"]["rows"][0]["channel"] == "email"
    assert result["returned_row_count"] == 1
    assert "rows" not in result
    assert "groups" not in result
    assert "private_notes" not in json.dumps(result)
    assert len(json.dumps(result).encode("utf-8")) < 100_000


@pytest.mark.asyncio
async def test_visualization_tool_returns_trusted_aggregate_envelope(monkeypatch) -> None:
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    from packages.core import database
    from packages.core.ai.tools import ledger_query_tools as module

    class Result:
        @staticmethod
        def scalar_one_or_none():
            return SimpleNamespace(settings={"ledger_contracts": ["manor.recruiting_ledger/v1"]})

    class Session:
        async def execute(self, _statement):
            return Result()

    @asynccontextmanager
    async def fake_session():
        yield Session()

    async def fake_overview(**kwargs):
        assert kwargs == {
            "entity_id": "entity-1",
            "workspace_id": "workspace-1",
            "settings": {"ledger_contracts": ["manor.recruiting_ledger/v1"]},
            "allowed_contract_ids": {"manor.recruiting_ledger/v1"},
        }
        return {
            "workspace_id": "workspace-1",
            "ledger_count": 1,
            "record_count": 2,
            "event_count": 3,
            "ledgers": [],
        }

    monkeypatch.setattr(database, "async_session", fake_session)
    monkeypatch.setattr(module, "workspace_ledger_overview", fake_overview)

    result = json.loads(
        await module._visualize_workspace_ledgers(
            "entity-1",
            workspace_id="workspace-1",
            _allowed_tool_names_from_context=[
                "read_recruiting_ledger",
                "visualize_workspace_ledgers",
            ],
        )
    )

    assert result["ok"] is True
    assert result["visualization"] == {
        "kind": "workspace_ledger_overview",
        "data": {
            "workspace_id": "workspace-1",
            "ledger_count": 1,
            "record_count": 2,
            "event_count": 3,
            "ledgers": [],
        },
    }


@pytest.mark.asyncio
async def test_visualization_tool_returns_empty_configuration_entry(monkeypatch) -> None:
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    from packages.core import database
    from packages.core.ai.tools import ledger_query_tools as module

    class Result:
        @staticmethod
        def scalar_one_or_none():
            return SimpleNamespace(settings={"ledger_contracts": []})

    class Session:
        async def execute(self, _statement):
            return Result()

    @asynccontextmanager
    async def fake_session():
        yield Session()

    async def fake_overview(**kwargs):
        assert kwargs["allowed_contract_ids"] == set()
        return {
            "workspace_id": "workspace-1",
            "ledger_count": 0,
            "record_count": 0,
            "event_count": 0,
            "ledgers": [],
        }

    monkeypatch.setattr(database, "async_session", fake_session)
    monkeypatch.setattr(module, "workspace_ledger_overview", fake_overview)

    result = json.loads(
        await module._visualize_workspace_ledgers(
            "entity-1",
            workspace_id="workspace-1",
            _allowed_tool_names_from_context=["visualize_workspace_ledgers"],
        )
    )

    assert result["ok"] is True
    assert result["visualization"]["data"]["ledger_count"] == 0


@pytest.mark.asyncio
async def test_visualization_tool_rejects_external_surface_before_storage(monkeypatch) -> None:
    from types import SimpleNamespace

    from packages.core import database
    from packages.core.ai.runtime.profiles import RuntimeProfile
    from packages.core.ai.runtime.surfaces import ChatSurface
    from packages.core.ai.tools import ledger_query_tools as module

    def storage_must_not_run():
        raise AssertionError("storage should not run for external chat")

    monkeypatch.setattr(database, "async_session", storage_must_not_run)
    result = json.loads(
        await module._visualize_workspace_ledgers(
            "entity-1",
            workspace_id="workspace-1",
            _runtime_envelope_from_context=SimpleNamespace(
                surface=ChatSurface.PUBLIC_CUSTOMER_CHAT,
                profile=RuntimeProfile.EXTERNAL_CUSTOMER_SAFE,
            ),
        )
    )

    assert result["error"]["code"] == ("ledger_visualization_not_available_on_external_surface")


def test_workspace_ledger_binding_auto_exposes_query_tool() -> None:
    from packages.core.ai.runtime.profiles import WORKSPACE_AGENT_TOOL_PROFILE
    from packages.core.ai.runtime.tool_visibility import resolve_runtime_tool_surface

    ledger_surface = resolve_runtime_tool_surface(
        (
            "read_content_ledger",
            "record_content_ledger",
            "query_ledger",
            "visualize_workspace_ledgers",
        ),
        bound_tool_names={"read_content_ledger"},
        is_master=False,
        mcp_allowed_names=set(),
        tool_profile=WORKSPACE_AGENT_TOOL_PROFILE,
    )
    assert "query_ledger" in ledger_surface.visible_tool_names
    assert "query_ledger" in ledger_surface.eager_tool_names
    assert "visualize_workspace_ledgers" in ledger_surface.visible_tool_names
    assert "visualize_workspace_ledgers" in ledger_surface.eager_tool_names

    generic_ledger_surface = resolve_runtime_tool_surface(
        ("read_relationship_ledger", "query_ledger", "visualize_workspace_ledgers"),
        bound_tool_names={"read_relationship_ledger"},
        is_master=False,
        mcp_allowed_names=set(),
        tool_profile=WORKSPACE_AGENT_TOOL_PROFILE,
    )
    assert "query_ledger" in generic_ledger_surface.visible_tool_names
    assert "visualize_workspace_ledgers" in generic_ledger_surface.visible_tool_names

    no_ledger_surface = resolve_runtime_tool_surface(
        ("query_ledger", "visualize_workspace_ledgers"),
        bound_tool_names={"record_youtube_workspace_metrics"},
        is_master=False,
        mcp_allowed_names=set(),
        tool_profile=WORKSPACE_AGENT_TOOL_PROFILE,
    )
    assert "query_ledger" not in no_ledger_surface.visible_tool_names
    assert "visualize_workspace_ledgers" not in no_ledger_surface.visible_tool_names


def test_workspace_settings_mark_ledger_query_as_derived_capability() -> None:
    from packages.core.services.ledger_query_service import workspace_queryable_ledger_configs
    from packages.core.services.workspace_runtime import _settings_declare_ledger

    assert _settings_declare_ledger(
        {
            "content_ledger": {"contract_id": "manor.content_ledger/v1"},
        }
    )
    assert _settings_declare_ledger(
        {
            "ledger_contracts": ["manor.finance_ledger/v1"],
        }
    )
    assert not _settings_declare_ledger({"timezone": "UTC", "metrics": {"version": 1}})
    assert not _settings_declare_ledger({"notes": "finance_ledger"})
    assert not _settings_declare_ledger(
        {
            "unrelated": {"label": "recruiting_ledger"},
        }
    )
    assert _settings_declare_ledger({"ledger_contracts": ["manor.relationship_ledger/v1"]})
    assert _settings_declare_ledger({"ledger_contracts": ["manor.recruiting_ledger/v1"]})
    assert workspace_queryable_ledger_configs(
        {
            "content_ledger": {
                "contract_id": "manor.content_ledger/v1",
                "directory": "content-ledger",
                "legacy_directories": ["topic-ledger"],
                "legacy_identity_fields": ["selected_topic"],
            },
        }
    ) == {
        "manor.content_ledger/v1": {
            "contract_id": "manor.content_ledger/v1",
            "directory": "content-ledger",
            "legacy_directories": ("topic-ledger",),
            "legacy_identity_fields": ("selected_topic",),
        },
    }
    assert (
        workspace_queryable_ledger_configs(
            {
                "ledger_contracts": [
                    {
                        "contract_id": "manor.relationship_ledger/v1",
                        "directory": "relationship-ledger",
                    }
                ],
            }
        )["manor.relationship_ledger/v1"]["directory"]
        == "relationship-ledger"
    )
    assert (
        workspace_queryable_ledger_configs(
            {
                "content_ledger": {
                    "contract_id": "manor.content_ledger/v1",
                    "directory": "topic-ledger",
                },
                "ledger_contracts": [],
            }
        )
        == {}
    )
    assert not _settings_declare_ledger(
        {
            "content_ledger": {
                "contract_id": "manor.content_ledger/v1",
                "directory": "topic-ledger",
            },
            "ledger_contracts": [],
        }
    )
