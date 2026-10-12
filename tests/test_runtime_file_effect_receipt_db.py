"""Journal owner persistence and exact JSONB receipt queries (isolated test DB)."""
import hashlib
import json

import pytest
from sqlalchemy import insert, text
from sqlalchemy.dialects import postgresql

from packages.core.ai.runtime.tool_effect_intents import runtime_tool_effect_checkpoint_scope
from packages.core.models.document import Document
from packages.core.models.base import generate_ulid
from packages.core.models.runtime_file_commit import RuntimeFileCommit
from packages.core.services.entity_fs import (
    open_entity_file_snapshot, stage_entity_file_rollback_snapshot, write_entity_file_atomic,
)
from packages.core.services.runtime_file_effect_recovery import (
    read_runtime_file_effect_receipt, runtime_file_effect_scope,
)
from packages.core.services.runtime_file_journal import RuntimeFileJournal
from test_runtime_file_effect_recovery import ARGUMENTS, SCOPE, authorization, effect_ref
from test_runtime_file_journal import storage as storage


async def prepare(storage, monkeypatch):
    scope = {**SCOPE, "entity_id": storage.name}
    receipt = authorization(entity_id=storage.name)
    checkpoint = {"pending_tool_call": {"id": "call-1", "name": "patch_file", "arguments": ARGUMENTS}}
    snapshot = stage_entity_file_rollback_snapshot(storage.name, "report.md")
    journal = RuntimeFileJournal(storage.name)
    with monkeypatch.context() as capture:
        capture.setattr(
            "packages.core.ai.runtime.authorization_receipts.runtime_current_tool_authorization_receipt",
            lambda: receipt,
        )
        with runtime_file_effect_scope(**scope), runtime_tool_effect_checkpoint_scope({"call-1": checkpoint}):
            await journal.prepare(snapshot, hashlib.sha256(b"new").hexdigest())
    write_entity_file_atomic(storage.name, "report.md", b"new")
    with open_entity_file_snapshot(storage.name, "report.md") as current:
        version = current.version
    await journal.installed("report.md", version)
    query = {key: value for key, value in scope.items() if key != "effect_generation"}
    return journal, snapshot, version, query


@pytest.mark.asyncio
async def test_committed_journal_owner_recovers_only_exact_call(db_session, storage, monkeypatch):
    journal, _, _, query = await prepare(storage, monkeypatch)
    document = Document(entity_id=storage.name, name="report.md", fs_path="report.md")
    db_session.add(document)
    await db_session.flush()
    await journal.mark_committed(db_session, {"report.md": document.id})
    await db_session.commit()
    result = await read_runtime_file_effect_receipt(db_session, **query, effect_ref=effect_ref())
    assert result.operation_status == "committed"
    assert result.observation["files"][0]["document_id"] == document.id
    assert result.journal_ids == (journal.entries["report.md"],)
    assert await read_runtime_file_effect_receipt(
        db_session, **query, effect_ref=effect_ref(effect_generation="4"),
    ) is None
    assert await read_runtime_file_effect_receipt(
        db_session, **{**query, "workspace_id": "another-workspace"}, effect_ref=effect_ref(),
    ) is None
    write_entity_file_atomic(storage.name, "report.md", b"later edit")
    recovered = await read_runtime_file_effect_receipt(db_session, **query, effect_ref=effect_ref())
    assert recovered.observation == result.observation
    assert (storage / "report.md").read_bytes() == b"later edit"


@pytest.mark.asyncio
async def test_only_recorded_rollback_proves_rolled_back(db_session, storage, monkeypatch):
    journal, snapshot, version, query = await prepare(storage, monkeypatch)
    assert await read_runtime_file_effect_receipt(db_session, **query, effect_ref=effect_ref()) is None
    snapshot.rollback(version)
    await journal.settled(committed=False)
    db_session.expire_all()
    result = await read_runtime_file_effect_receipt(db_session, **query, effect_ref=effect_ref())
    assert result.operation_status == "rolled_back"
    assert not (storage / "report.md").exists()


@pytest.mark.asyncio
async def test_exact_recovery_lookup_uses_bounded_effect_index(db_session, storage, monkeypatch):
    journal, snapshot, version, query = await prepare(storage, monkeypatch)
    snapshot.rollback(version)
    await journal.settled(committed=False)
    # Same-entity history prevents an entity-only scan from looking selective.
    rows = [{
        "id": generate_ulid(), "transaction_id": generate_ulid(), "entity_id": storage.name,
        "rel_path": f"history/{number}.md", "path_sha256": hashlib.sha256(str(number).encode()).hexdigest(),
        "state": "rolled_back", "snapshot": {}, "owner": {}, "cleanup_pending": False,
        "effect_id": hashlib.sha256(f"history-call-{number}".encode()).hexdigest(),
    } for number in range(1500)]
    await db_session.execute(insert(RuntimeFileCommit), rows)
    await db_session.flush()
    await db_session.execute(text("ANALYZE runtime_file_commits"))
    original_execute = db_session.execute
    captured = []

    async def capture(statement, *args, **kwargs):
        captured.append(statement)
        return await original_execute(statement, *args, **kwargs)

    with monkeypatch.context() as capture_context:
        capture_context.setattr(db_session, "execute", capture)
        assert await read_runtime_file_effect_receipt(db_session, **query, effect_ref=effect_ref()) is not None
    sql = captured[0].compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    plan = (await original_execute(text(f"EXPLAIN (ANALYZE, FORMAT JSON) {sql}"))).scalar_one()
    if isinstance(plan, str):
        plan = json.loads(plan)
    encoded = json.dumps(plan)
    assert '"Index Name": "ix_runtime_file_commit_effect"' in encoded
    assert '"Node Type": "Seq Scan"' not in encoded
    assert plan[0]["Plan"]["Actual Rows"] == 1
