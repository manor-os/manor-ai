"""Text PostgreSQL cannot store is repaired instead of failing the whole write."""
from __future__ import annotations

import pytest
from sqlalchemy import Column, Integer, MetaData, Table, Text, insert, select, text
from sqlalchemy.dialects.postgresql import JSONB

from packages.core import database
from tests.test_database_runtime import runtime_settings

_rows = Table(
    "storable_text_probe", MetaData(),
    Column("id", Integer, primary_key=True),
    Column("body", Text),
    Column("payload", JSONB),
    prefixes=["TEMPORARY"],
)

# A browser ``slice()`` that cut 😀 in half, as ``json.loads`` hands it over.
HALF_EMOJI = "ab\ud83d"
VALID = "中文 😀 ok"


@pytest.mark.asyncio
async def test_unpaired_surrogate_and_nul_are_repaired_in_text_and_jsonb(_test_database_guard):
    from conftest import TEST_DATABASE_URL

    engine = database.create_database_engine(
        runtime_settings(TEST_DATABASE_URL), kind=database.DatabaseEngineKind.APPLICATION,
    )
    try:
        async with engine.begin() as conn:
            await conn.run_sync(_rows.metadata.create_all)
            await conn.execute(insert(_rows), [
                {
                    "id": 1,
                    "body": HALF_EMOJI + "\x00",
                    "payload": {"result": {"blocks": [HALF_EMOJI, VALID]}, "k\x00": "\udc00x"},
                },
                {"id": 2, "body": VALID, "payload": {"text": VALID, "raw": "\\ud83d is text"}},
            ])
            # Raw SQL text parameters take the same path.
            await conn.execute(
                text("INSERT INTO storable_text_probe (id, body) VALUES (3, :body)"),
                {"body": "\ud83d" + "\ude00" + "\ud83d"},
            )
            rows = (await conn.execute(select(_rows).order_by(_rows.c.id))).all()
    finally:
        await engine.dispose()

    assert rows[0].body == "ab�"
    assert rows[0].payload == {"result": {"blocks": ["ab�", VALID]}, "k": "�x"}
    assert rows[1].body == VALID
    assert rows[1].payload == {"text": VALID, "raw": "\\ud83d is text"}
    # Adjacent halves are one character; only the trailing lone half is replaced.
    assert rows[2].body == "😀�"
