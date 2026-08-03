"""Migration idempotency + index-existence checks for the metrics schema
prerequisite (outcome/rounds columns, platform-wide indexes)."""
from __future__ import annotations

import pytest
from sqlalchemy import text


@pytest.mark.asyncio
async def test_metrics_columns_and_indexes_exist(db_session):
    rows = (await db_session.execute(text(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = 'tool_call_logs' AND column_name = 'outcome'"
    ))).fetchall()
    assert len(rows) == 1

    rows = (await db_session.execute(text(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = 'token_usage_logs' AND column_name = 'rounds'"
    ))).fetchall()
    assert len(rows) == 1

    for index_name in (
        "ix_token_usage_source_created",
        "ix_tool_call_source_created",
        "ix_tool_call_outcome",
    ):
        rows = (await db_session.execute(
            text("SELECT 1 FROM pg_indexes WHERE indexname = :name"),
            {"name": index_name},
        )).fetchall()
        assert len(rows) == 1, f"missing index {index_name}"


@pytest.mark.asyncio
async def test_platform_wide_source_query_uses_the_new_index(db_session):
    """A platform-wide (no entity_id filter) query grouped by source is
    exactly the shape the metrics dashboard will run — confirm the new
    index is usable for it, instead of forcing a full table scan.

    The test DB's ``tool_call_logs`` table is empty, so Postgres's
    cost-based planner will always prefer a sequential scan over an
    index scan here regardless of which indexes exist — a seq scan of
    an empty/near-empty table is genuinely cheaper, and that holds true
    even after seeding tens/hundreds of thousands of synthetic rows
    (verified empirically while writing this test: at 5k, 20k, and
    200k rows with a selective ``created_at`` filter, the planner still
    chose a (parallel) seq scan every time, because the query only
    needs two narrow columns and the table is small in page terms).
    Reproducing genuine production-scale cardinality here would require
    millions of rows, which is impractical for a fast unit test.

    So instead of asserting what the cost-based planner *chooses*
    (data-volume-dependent, and not what this migration controls),
    assert what it *can* choose: ``SET LOCAL enable_seqscan = off``
    forces an index-based plan when a usable index exists for the
    query shape. Either ``ix_tool_call_source_created`` (source,
    created_at) or ``ix_tool_call_outcome`` (outcome, created_at) can
    serve the ``created_at`` range filter here — which exact one the
    planner picks between two structurally similar indexes is an
    implementation detail, so this checks that *an* index scan over
    ``tool_call_logs`` is used and no seq scan remains, rather than
    pinning to one specific index name.
    """
    await db_session.execute(text("SET LOCAL enable_seqscan = off"))
    plan_rows = (await db_session.execute(text(
        "EXPLAIN SELECT source, count(*) FROM tool_call_logs "
        "WHERE created_at >= now() - interval '7 days' GROUP BY source"
    ))).fetchall()
    plan_text = "\n".join(r[0] for r in plan_rows)
    assert "Seq Scan" not in plan_text
    assert "Index Scan" in plan_text or "Bitmap Index Scan" in plan_text
