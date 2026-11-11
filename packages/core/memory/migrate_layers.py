"""Move memory kept before the layer files into them, once per source.

Sources: memory candidates still waiting for review, the latest summary of
each Workspace main conversation, and the old ``_master`` MEMORY.md /
LEARNINGS.md. The memory skill consolidates each group: newer statements win,
questions and one-off requests are dropped. Entries are written through
remember, with its permission checks and audit rows, so a migration write can
be read back and rolled back like any other. Candidates end ``applied`` or
``archived``; summaries and files are recorded on a runtime evidence row, so a
second run skips them.
"""
from __future__ import annotations

import hashlib
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import or_, select

from packages.core.memory.layers import MemoryTurnScope
from packages.core.memory.policy import content_fingerprint
from packages.core.models.runtime_learning import AgentLearningCandidate, RuntimeEvidence

logger = logging.getLogger(__name__)
MIGRATION_SOURCE = "layer_migration"
EVIDENCE_TYPE = "memory_layer_migration"
_PERSONAL = ("profile", "preferences", "private")
_ITEM_MAX_CHARS = 8000
_BATCH_ITEMS = 30
_BATCH_CHARS = 40_000
# Demo Workspaces show a candidate waiting for review on purpose.
_DEMO_CREATORS = ("sandbox_demo",)


@dataclass
class _Group:
    scope: MemoryTurnScope
    layers: tuple[str, ...]
    items: list[dict] = field(default_factory=list)
    # item id -> candidate ids it stands for (exact repeats share one item)
    candidates: dict[str, list[str]] = field(default_factory=dict)


def _date(value) -> str:
    # To the minute: a correction often comes the same day as what it corrects.
    return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M") if value else ""


def _candidate_text(row: AgentLearningCandidate) -> str:
    return " ".join(str((row.payload or {}).get("content") or row.summary or "").split())


async def _done_sources(db, entity_id: str) -> set[str]:
    rows = await db.scalars(select(RuntimeEvidence.details).where(
        RuntimeEvidence.entity_id == entity_id, RuntimeEvidence.evidence_type == EVIDENCE_TYPE,
    ))
    return {key for details in rows for key in (details or {}).get("sources", [])}


def _legacy_file_items(entity_id: str, directory: str) -> list[dict]:
    from packages.core.constants.agents import MANOR_AGENT_FS_ID
    from packages.core.memory.layers import read_layer_text
    from packages.core.services.agent_files import agent_file_has_custom_content
    from packages.core.services.entity_fs import get_entity_root

    items = []
    for name in ("MEMORY.md", "LEARNINGS.md"):
        path = os.path.join(directory, name)
        text = read_layer_text(path)
        if not text or not agent_file_has_custom_content(MANOR_AGENT_FS_ID, name, text):
            continue
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
        rel = os.path.relpath(path, get_entity_root(entity_id))
        modified = datetime.fromtimestamp(os.path.getmtime(path), UTC)
        items.append({"id": f"file:{rel}:{digest}", "date": _date(modified), "text": text[:_ITEM_MAX_CHARS]})
    return items


async def _org_speaker(db, entity_id: str) -> str | None:
    """Organization memory is the owners' to edit; the migration writes as one."""
    from packages.core.models.user import User
    from packages.core.services.workspace_access import ENTITY_ADMIN_ROLES

    return await db.scalar(select(User.id).where(
        User.entity_id == entity_id, User.deleted_at.is_(None), User.status == "active",
        User.role.in_(ENTITY_ADMIN_ROLES),
    ).order_by(User.role != "owner", User.created_at).limit(1))


async def _entity_user_ids(db, entity_id: str) -> list[str]:
    from packages.core.models.user import User, UserMembership

    members = select(UserMembership.user_id).where(
        UserMembership.entity_id == entity_id, UserMembership.status == "active",
        UserMembership.deleted_at.is_(None),
    )
    return list((await db.scalars(select(User.id).where(
        User.deleted_at.is_(None), User.status == "active",
        or_(User.entity_id == entity_id, User.id.in_(members)),
    ))).all())


async def collect_groups(db, entity_id: str) -> list[_Group]:
    """Everything this entity still has to move, grouped by who may write it."""
    from packages.core.constants.agents import MANOR_AGENT_FS_ID
    from packages.core.models.task import Conversation
    from packages.core.models.workspace import Workspace
    from packages.core.services.agent_files import agent_user_dir
    from packages.core.services.entity_fs import get_agent_dir

    done = await _done_sources(db, entity_id)
    groups: dict[tuple, _Group] = {}

    def group(workspace_id, user_id) -> _Group:
        if (workspace_id, user_id) not in groups:
            layers = ((("workspace",) if workspace_id else ()) + (_PERSONAL if user_id else ())) or ("org",)
            scope = MemoryTurnScope(False, not workspace_id, user_id, workspace_id)
            groups[(workspace_id, user_id)] = _Group(scope, layers)
        return groups[(workspace_id, user_id)]

    live = select(Workspace.id).where(Workspace.entity_id == entity_id, Workspace.deleted_at.is_(None))
    candidates = (await db.scalars(select(AgentLearningCandidate).where(
        AgentLearningCandidate.entity_id == entity_id,
        AgentLearningCandidate.candidate_type == "memory",
        AgentLearningCandidate.status == "proposed",
        AgentLearningCandidate.created_by.not_in(_DEMO_CREATORS),
        or_(AgentLearningCandidate.workspace_id.in_(live),
            AgentLearningCandidate.workspace_id.is_(None) & AgentLearningCandidate.user_id.is_not(None)),
    ).order_by(AgentLearningCandidate.created_at.desc()))).all()
    for row in candidates:
        text = _candidate_text(row)
        target = group(row.workspace_id, row.user_id)
        if not text:
            target.candidates.setdefault(f"candidate:{row.id}", []).append(row.id)
            continue
        # Newest first: an exact repeat joins the newest copy of itself.
        same = next((i for i in target.items if i.get("fingerprint") == content_fingerprint(text)), None)
        if same is not None:
            target.candidates[same["id"]].append(row.id)
            continue
        key = f"candidate:{row.id}"
        target.items.append({"id": key, "date": _date(row.created_at), "text": text[:_ITEM_MAX_CHARS],
                             "fingerprint": content_fingerprint(text)})
        target.candidates[key] = [row.id]

    summaries = (await db.execute(select(
        Conversation.id, Conversation.workspace_id, Conversation.summary, Conversation.summary_version,
        Conversation.summary_covered_at, Conversation.updated_at,
    ).where(
        Conversation.entity_id == entity_id, Conversation.scope == "workspace_main",
        Conversation.workspace_id.in_(live), Conversation.summary.is_not(None),
    ))).all()
    for conv_id, workspace_id, summary, version, covered_at, updated_at in summaries:
        key = f"summary:{conv_id}:{version}"
        if summary.strip() and key not in done:
            group(workspace_id, None).items.append(
                {"id": key, "date": _date(covered_at or updated_at), "text": summary.strip()[:_ITEM_MAX_CHARS]})

    org = await _org_speaker(db, entity_id)
    if org:
        files = [i for i in _legacy_file_items(entity_id, get_agent_dir(entity_id, MANOR_AGENT_FS_ID))
                 if i["id"] not in done]
        if files:
            # Speaker set for the permission check; no personal layer is offered.
            target = group(None, None)
            target.scope = MemoryTurnScope(False, True, org, None)
            target.items.extend(files)
    for user_id in await _entity_user_ids(db, entity_id):
        files = [i for i in _legacy_file_items(entity_id, agent_user_dir(entity_id, MANOR_AGENT_FS_ID, user_id))
                 if i["id"] not in done]
        if files:
            group(None, user_id).items.extend(files)
    return [g for g in groups.values() if g.items or g.candidates]


def _batches(items: list[dict]):
    batch, size = [], 0
    for item in sorted(items, key=lambda i: i["date"]):
        if batch and (len(batch) >= _BATCH_ITEMS or size + len(item["text"]) > _BATCH_CHARS):
            yield batch
            batch, size = [], 0
        batch.append(item)
        size += len(item["text"])
    if batch:
        yield batch


async def _write_entry(db, entity_id, group, entry, existing, staged, sources):
    from packages.core.memory.remember import LAYER_TARGETS, SECTIONS, MemoryWriteRefused, remember_memory

    replaced = existing.get(entry["replaces"]) if entry["replaces"] else None
    attempts = [("update", replaced["content"])] if replaced and replaced["layer"] == entry["layer"] else []
    attempts.append(("add", ""))
    for action, line in attempts:
        try:
            return await remember_memory(
                db, group.scope, entity_id=entity_id, target=LAYER_TARGETS[entry["layer"]], action=action,
                content=entry["body"], entry=line, section=SECTIONS[entry["scope"]] if action == "add" else "",
                source=MIGRATION_SOURCE, created_by="migration", staged_files=staged,
                evidence={"migrated_from": sources},
            )
        except MemoryWriteRefused as refused:
            logger.info("[memory-migration] %s %s not written: %s", action, entry["layer"], refused)
    return None


async def _noncandidate_sources_current(db, entity_id: str, group: _Group, batch: list[dict]) -> bool:
    """Confirm the summary version and legacy file contents used by the model."""
    summaries = [item["id"].split(":", 2)[1:] for item in batch if item["id"].startswith("summary:")]
    if summaries:
        from packages.core.models.task import Conversation

        current = dict((await db.execute(select(Conversation.id, Conversation.summary_version).where(
            Conversation.id.in_([cid for cid, _ in summaries]), Conversation.entity_id == entity_id,
            Conversation.workspace_id == group.scope.workspace_id, Conversation.scope == "workspace_main",
            Conversation.summary.is_not(None),
        ).with_for_update(read=True))).all())
        if any(cid not in current or str(current[cid]) != version for cid, version in summaries):
            return False
    files = {item["id"] for item in batch if item["id"].startswith("file:")}
    if files:
        from packages.core.constants.agents import MANOR_AGENT_FS_ID
        from packages.core.services.agent_files import agent_user_dir
        from packages.core.services.entity_fs import get_agent_dir

        directory = (get_agent_dir(entity_id, MANOR_AGENT_FS_ID) if "org" in group.layers else
                     agent_user_dir(entity_id, MANOR_AGENT_FS_ID, group.scope.speaker_id))
        if not files.issubset(item["id"] for item in _legacy_file_items(entity_id, directory)):
            return False
    return True


async def _apply(db, entity_id: str, group: _Group, batch: list[dict], existing: dict, decision: dict) -> dict:
    from packages.core.memory.remember import stage_memory_writes
    from packages.core.models.workspace import Workspace

    stats = {"written": 0, "applied": 0, "archived": 0, "failed": 0}
    async with stage_memory_writes(db, entity_id) as staged:
        workspace_id = group.scope.workspace_id
        if workspace_id and not await db.scalar(select(Workspace.id).where(
            Workspace.id == workspace_id, Workspace.entity_id == entity_id, Workspace.deleted_at.is_(None),
        ).with_for_update()):
            return {**stats, "failed": 1}

        # A decision may combine several sources. If one disappeared or changed,
        # recompute the whole batch rather than carrying its text into a write.
        done = await _done_sources(db, entity_id)
        if not await _noncandidate_sources_current(db, entity_id, group, batch):
            return {**stats, "failed": 1}
        row_ids = [rid for item in batch for rid in group.candidates.get(item["id"], [])]
        rows = {row.id: row for row in (await db.scalars(select(AgentLearningCandidate).where(
            AgentLearningCandidate.id.in_(row_ids), AgentLearningCandidate.entity_id == entity_id,
            AgentLearningCandidate.status == "proposed", AgentLearningCandidate.workspace_id == workspace_id,
            AgentLearningCandidate.user_id == group.scope.speaker_id,
        ).with_for_update())).all()} if row_ids else {}
        live_candidates: dict[str, list[AgentLearningCandidate]] = {}
        for item in batch:
            item_id = item["id"]
            if item_id not in group.candidates:
                if item_id in done:
                    return {**stats, "failed": 1}
                continue
            matching = []
            for rid in group.candidates[item_id]:
                if (row := rows.get(rid)) is None:
                    continue
                text = _candidate_text(row)
                if (content_fingerprint(text) if text else None) != item.get("fingerprint"):
                    return {**stats, "failed": 1}
                matching.append(row)
            if not matching:
                return {**stats, "failed": 1}
            live_candidates[item_id] = matching
        live = {item["id"] for item in batch}
        kept: dict[str, list[str]] = {}
        for entry in decision["entries"]:
            sources = entry["sources"]
            saved = await _write_entry(db, entity_id, group, entry, existing, staged, sources)
            if saved is None:
                continue
            stats["written"] += saved["status"] == "saved"
            for source in sources:
                kept.setdefault(source, []).extend([saved["write_id"]] if saved.get("write_id") else [])
        now = datetime.now(UTC)
        for item_id, candidates in live_candidates.items():
            for row in candidates:
                row.status = "applied" if item_id in kept else "archived"
                row.applied_at = now if item_id in kept else None
                row.resolution = {**(row.resolution or {}), "migrated_by": MIGRATION_SOURCE,
                                  "reason": "kept" if item_id in kept else "dropped_or_superseded",
                                  "memory_write_ids": kept.get(item_id, [])}
                stats["applied" if item_id in kept else "archived"] += 1
        if live:
            db.add(RuntimeEvidence(
                entity_id=entity_id, workspace_id=group.scope.workspace_id, user_id=group.scope.speaker_id,
                evidence_type=EVIDENCE_TYPE, source=MIGRATION_SOURCE, status="succeeded",
                summary="Moved earlier memory into the memory layer files",
                details={"sources": sorted(live), "kept": sorted(kept)},
            ))
    return stats


async def migrate_entity_memory(entity_id: str, *, deadline: float | None = None) -> dict:
    """Move one entity's earlier memory; safe to run again. Returns counts."""
    from packages.core.database import create_worker_session
    from packages.core.memory.manager import run_memory_skill
    from packages.core.memory.remember import layer_entries

    stats = {"groups": 0, "written": 0, "applied": 0, "archived": 0, "failed": 0, "complete": True}
    async with create_worker_session()() as db:
        groups = await collect_groups(db, entity_id)
    for group in groups:
        stats["groups"] += 1
        # Candidates with no text are archived without asking the model.
        empty = [{"id": key, "date": "", "text": ""} for key in group.candidates
                 if key not in {i["id"] for i in group.items}]
        if empty:
            async with create_worker_session()() as db:
                result = await _apply(db, entity_id, group, empty, {}, {"entries": []})
            for key, value in result.items():
                stats[key] += value
            if result["failed"]:
                stats["complete"] = False
        for batch in _batches(group.items):
            if deadline is not None and time.monotonic() > deadline:
                stats["complete"] = False
                return stats
            scope = group.scope
            existing = layer_entries(entity_id, group.layers, user_id=scope.speaker_id if "profile" in group.layers else None,
                                     workspace_id=scope.workspace_id)
            payload = {"items": [{k: i[k] for k in ("id", "date", "text")} for i in batch],
                       "existing": [{"id": e["id"], "layer": e["layer"], "content": e["content"]} for e in existing],
                       "layers": list(group.layers)}
            try:
                decision = await run_memory_skill(
                    "consolidate", payload, entity_id=entity_id, workspace_id=scope.workspace_id,
                    user_id=scope.speaker_id,
                )
            except Exception:
                # Left as it was; the next sweep tries again.
                logger.warning("[memory-migration] consolidation failed for %s", entity_id, exc_info=True)
                stats["failed"] += 1
                stats["complete"] = False
                continue
            async with create_worker_session()() as db:
                result = await _apply(db, entity_id, group, batch, {e["id"]: e for e in existing}, decision)
            for key, value in result.items():
                stats[key] += value
            if result["failed"]:
                stats["complete"] = False
    if stats["complete"]:
        async with create_worker_session()() as db:
            db.add(RuntimeEvidence(
                entity_id=entity_id, evidence_type=EVIDENCE_TYPE, source=MIGRATION_SOURCE, status="succeeded",
                summary="Earlier memory is in the memory layer files",
                details={"sources": [], "complete": True},
            ))
            await db.commit()
    logger.info("[memory-migration] entity=%s %s", entity_id, stats)
    return stats


async def migrate_pending_entities(*, limit: int = 20, budget_seconds: float = 600) -> dict:
    """Migrate a few entities not yet marked complete; runs dry once all are."""
    from sqlalchemy import exists, func

    from packages.core.database import create_worker_session
    from packages.core.models.user import Entity
    from packages.core.services.entity_fs import is_fs_enabled

    if not is_fs_enabled():
        return {"skipped": "fs_disabled"}
    complete = select(RuntimeEvidence.id).where(
        RuntimeEvidence.entity_id == Entity.id, RuntimeEvidence.evidence_type == EVIDENCE_TYPE,
        RuntimeEvidence.details["complete"].as_boolean().is_(True),
    )
    async with create_worker_session()() as db:
        entity_ids = (await db.scalars(select(Entity.id).where(
            Entity.deleted_at.is_(None), ~exists(complete),
        ).order_by(func.random()).limit(limit))).all()
    deadline = time.monotonic() + budget_seconds
    results = {}
    for entity_id in entity_ids:
        if time.monotonic() > deadline:
            break
        try:
            results[entity_id] = await migrate_entity_memory(entity_id, deadline=deadline)
        except Exception:
            logger.exception("[memory-migration] entity %s failed", entity_id)
    return {"entities": len(results), "complete": sum(r["complete"] for r in results.values()),
            "written": sum(r["written"] for r in results.values())}
