"""Earlier memory moves into the layer files once, through remember."""
import os

import pytest

from packages.core.config import get_settings
from packages.core.memory.layers import org_memory_path, personal_memory_path, workspace_memory_path

E, OWNER, MEMBER, WS = "ent_migrate", "user_mig_owner", "user_mig_member", "ws_migrate"


@pytest.fixture
def migrate_fs(tmp_path, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "MANOR_FS_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "MANOR_FS_ENABLED", True)
    monkeypatch.setattr(settings, "DEPLOYMENT_MODE", "self_hosted")
    from packages.core.services.entity_fs import provision_entity_filesystem

    provision_entity_filesystem(E, "Migrate Inc")
    return tmp_path


def _read(path):
    return open(path, encoding="utf-8").read() if os.path.exists(path) else ""


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def _consolidate(payload):
    """Stand-in for the memory skill: keeps statements, drops questions, newer threshold wins."""
    entries, threshold = [], [i for i in payload["items"] if "threshold" in i["text"]]
    if threshold:
        entries.append({"scope": "decision", "layer": payload["layers"][0], "body": threshold[-1]["text"],
                        "sources": [i["id"] for i in threshold], "replaces": None})
    for item in payload["items"]:
        if item in threshold or item["text"].endswith("?"):
            continue
        layers = payload["layers"]
        layer = "workspace" if "workspace" in layers else "preferences" if "preferences" in layers else layers[0]
        entries.append({"scope": "guidance", "layer": layer, "body": item["text"].splitlines()[-1],
                        "sources": [item["id"]], "replaces": None})
    return {"entries": entries}


@pytest.mark.parametrize("change", [
    {"sources": ["candidate:unknown"]}, {"sources": []}, {"layer": "private"},
    {"replaces": "workspace:9"}, {"body": "x" * 601},
])
def test_consolidation_only_uses_supplied_items_and_layers(change):
    from packages.core.memory.manager import _validate

    payload = {"items": [{"id": "candidate:a", "date": "", "text": "t"}],
               "existing": [{"id": "workspace:0", "layer": "workspace", "content": "e"}], "layers": ["workspace"]}
    entry = {"scope": "fact", "layer": "workspace", "body": "Fact", "sources": ["candidate:a"], "replaces": "workspace:0"}
    assert _validate("consolidate", {"entries": [entry]}, payload)["entries"][0]["replaces"] == "workspace:0"
    with pytest.raises(ValueError):
        _validate("consolidate", {"entries": [{**entry, **change}]}, payload)


async def test_earlier_memory_moves_into_layer_files_once(client, migrate_fs, monkeypatch):
    import packages.core.database as database
    import packages.core.memory.manager as manager
    from datetime import UTC, datetime, timedelta
    from sqlalchemy import select
    from packages.core.constants.agents import MANOR_AGENT_FS_ID
    from packages.core.memory import migrate_layers
    from packages.core.models.runtime_learning import AgentLearningCandidate
    from packages.core.models.task import Conversation
    from packages.core.models.user import Entity, User
    from packages.core.models.workspace import Workspace, WorkspaceStaff
    from packages.core.services.agent_files import agent_user_dir
    from packages.core.services.entity_fs import get_agent_dir

    calls = []

    async def fake_skill(operation, payload, **scope):
        assert operation == "consolidate"
        calls.append(payload)
        return manager._validate("consolidate", _consolidate(payload), payload)

    monkeypatch.setattr(manager, "run_memory_skill", fake_skill)
    t0 = datetime(2026, 11, 1, tzinfo=UTC)

    def candidate(cid, text, *, workspace_id=WS, user_id=MEMBER, hours=0, created_by="runtime"):
        return AgentLearningCandidate(
            id=cid, entity_id=E, workspace_id=workspace_id, user_id=user_id, candidate_type="memory",
            scope="workspace" if workspace_id else "user", title="Remember user guidance", summary="",
            payload={"content": text}, status="proposed", created_by=created_by,
            created_at=t0 + timedelta(hours=hours),
        )

    async with database.async_session() as db:
        db.add_all([
            Entity(id=E, name="Migrate Inc"),
            User(id=OWNER, entity_id=E, email="mig-owner@test.invalid", password_hash="test", role="owner"),
            User(id=MEMBER, entity_id=E, email="mig-member@test.invalid", password_hash="test", role="member"),
            Workspace(id=WS, entity_id=E, name="Migrate Ops", status="active"),
            WorkspaceStaff(workspace_id=WS, user_id=MEMBER, role="editor", status="active"),
            candidate("cand_mig_old", "VC has a 1000 cup threshold", hours=1),
            candidate("cand_mig_new", "Cancelled: VC has no cup threshold", hours=2),
            candidate("cand_mig_question", "Which rules did we agree on?", hours=3),
            candidate("cand_mig_dup_a", "No exclamation marks in campaign copy", hours=4),
            candidate("cand_mig_dup_b", "No  exclamation marks in campaign copy", hours=5),
            candidate("cand_mig_reply", "Reply in Chinese", workspace_id=None, user_id=OWNER),
            candidate("cand_mig_demo", "Demo rule", created_by="sandbox_demo"),
            Conversation(entity_id=E, workspace_id=WS, user_id=OWNER, scope="workspace_main",
                         summary="Agreed: quote the cost before generating shots"),
        ])
        await db.commit()
    master = get_agent_dir(E, MANOR_AGENT_FS_ID)
    _write(os.path.join(master, "MEMORY.md"), "# Memory\n\nThe brand name is spoken as one word")
    _write(os.path.join(agent_user_dir(E, MANOR_AGENT_FS_ID, OWNER), "LEARNINGS.md"), "# Learnings\n\nSend drafts first")

    stats = await migrate_layers.migrate_entity_memory(E)
    assert stats["complete"] and stats["failed"] == 0

    # Only what each group's speaker may write is offered; a speakerless summary gets no personal layer.
    assert sorted(tuple(p["layers"]) for p in calls) == [
        ("org",), ("profile", "preferences", "private"), ("workspace",),
        ("workspace", "profile", "preferences", "private"),
    ]
    sent = [i["text"] for p in calls for i in p["items"]]
    # Exact repeats are sent once.
    assert sent.count("No exclamation marks in campaign copy") == 1 and len(sent) == 8
    assert "Demo rule" not in sent

    workspace_text = _read(workspace_memory_path(E, WS))
    assert "no cup threshold" in workspace_text and "1000 cup" not in workspace_text
    assert "exclamation marks" in workspace_text and "quote the cost" in workspace_text
    assert "Which rules" not in workspace_text
    assert "spoken as one word" in _read(org_memory_path(E))
    owner_prefs = _read(personal_memory_path(E, OWNER, "preferences"))
    assert "Reply in Chinese" in owner_prefs and "Send drafts first" in owner_prefs

    async with database.async_session() as db:
        rows = {r.id: r for r in (await db.scalars(select(AgentLearningCandidate).where(
            AgentLearningCandidate.entity_id == E))).all()}
    status = {cid: row.status for cid, row in rows.items() if cid.startswith("cand_")}
    assert status == {
        "cand_mig_old": "applied", "cand_mig_new": "applied", "cand_mig_question": "archived",
        "cand_mig_dup_a": "applied", "cand_mig_dup_b": "applied", "cand_mig_reply": "applied",
        "cand_mig_demo": "proposed",
    }
    writes = [r for r in rows.values() if r.created_by == "migration"]
    assert writes and all(r.status == "applied" and r.payload["source"] == "layer_migration" for r in writes)
    assert {r.user_id for r in writes} == {OWNER, MEMBER, None}

    # A second run finds nothing to move and asks the model nothing.
    again = await migrate_layers.migrate_entity_memory(E)
    assert again["groups"] == 0 and len(calls) == 4

    swept = []

    async def record(entity_id, **kwargs):
        swept.append(entity_id)
        return {"complete": True, "written": 0}

    monkeypatch.setattr(migrate_layers, "migrate_entity_memory", record)
    await migrate_layers.migrate_pending_entities(limit=1000)
    assert E not in swept


async def test_rejected_source_invalidates_merged_memory_decision(client, migrate_fs, monkeypatch):
    from datetime import UTC, datetime, timedelta

    from packages.core import database
    from packages.core.memory import manager, migrate_layers
    from packages.core.models.runtime_learning import AgentLearningCandidate
    from packages.core.models.user import Entity, User
    from packages.core.models.workspace import Workspace

    now = datetime.now(UTC)
    async with database.async_session() as db:
        db.add_all([
            Entity(id=E, name="Migrate Inc"),
            User(id=OWNER, entity_id=E, email="mig-owner@test.invalid", password_hash="test", role="owner"),
            Workspace(id=WS, entity_id=E, name="Migrate Ops", status="active"),
        ])
        for cid, text, created_at in (
            ("cand_mig_keep", "Use the 1000 cup threshold", now - timedelta(hours=1)),
            ("cand_mig_rejected", "Use the private threshold", now),
        ):
            db.add(AgentLearningCandidate(
                id=cid, entity_id=E, workspace_id=WS, user_id=OWNER, candidate_type="memory",
                scope="workspace", title="Threshold guidance", summary="", payload={"content": text},
                status="proposed", created_by="runtime", created_at=created_at,
            ))
        await db.commit()

    async def consolidate_then_reject(operation, payload, **scope):
        if len(payload["items"]) == 2:
            async with database.async_session() as db:
                rejected = await db.get(AgentLearningCandidate, "cand_mig_rejected")
                rejected.status = "rejected"
                await db.commit()
        return manager._validate(operation, _consolidate(payload), payload)

    monkeypatch.setattr(manager, "run_memory_skill", consolidate_then_reject)
    stale = await migrate_layers.migrate_entity_memory(E)
    assert not stale["complete"] and stale["written"] == stale["archived"] == 0
    assert "threshold" not in _read(workspace_memory_path(E, WS))

    retried = await migrate_layers.migrate_entity_memory(E)
    assert retried["complete"] and retried["applied"] == 1
    memory = _read(workspace_memory_path(E, WS))
    assert "1000 cup threshold" in memory and "private threshold" not in memory


async def test_migration_retries_a_replaced_summary_or_legacy_file(client, migrate_fs, monkeypatch):
    from packages.core import database
    from packages.core.constants.agents import MANOR_AGENT_FS_ID
    from packages.core.memory import manager, migrate_layers
    from packages.core.models.task import Conversation
    from packages.core.models.user import Entity, User
    from packages.core.models.workspace import Workspace
    from packages.core.services.entity_fs import get_agent_dir

    summary_id = "conv_migration_summary"
    async with database.async_session() as db:
        db.add_all([
            Entity(id=E, name="Migrate Inc"),
            User(id=OWNER, entity_id=E, email="mig-owner@test.invalid", password_hash="test", role="owner"),
            Workspace(id=WS, entity_id=E, name="Migrate Ops", status="active"),
            Conversation(id=summary_id, entity_id=E, workspace_id=WS, user_id=OWNER,
                         scope="workspace_main", summary="Use the old workspace threshold"),
        ])
        await db.commit()
    legacy = os.path.join(get_agent_dir(E, MANOR_AGENT_FS_ID), "MEMORY.md")
    _write(legacy, "# Memory\n\nUse the old company threshold")

    async def consolidate_then_replace(operation, payload, **scope):
        if "old workspace threshold" in payload["items"][0]["text"]:
            async with database.async_session() as db:
                conversation = await db.get(Conversation, summary_id)
                conversation.summary = "Use the new workspace threshold"
                conversation.summary_version += 1
                await db.commit()
        if "old company threshold" in payload["items"][0]["text"]:
            _write(legacy, "# Memory\n\nUse the new company threshold")
        return manager._validate(operation, _consolidate(payload), payload)

    monkeypatch.setattr(manager, "run_memory_skill", consolidate_then_replace)
    stale = await migrate_layers.migrate_entity_memory(E)
    assert not stale["complete"] and stale["failed"] == 2 and stale["written"] == 0
    retried = await migrate_layers.migrate_entity_memory(E)
    assert retried["complete"] and retried["written"] == 2
    workspace = _read(workspace_memory_path(E, WS))
    org = _read(org_memory_path(E))
    assert "new workspace threshold" in workspace and "old workspace threshold" not in workspace
    assert "new company threshold" in org and "old company threshold" not in org
