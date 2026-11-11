"""Write what a person told the AI into a memory layer file.

Writes take effect at once, with no review card: memory is not a rule and
cannot loosen approvals or governance. Each write leaves an audit row that
``undo`` can roll back. Who may write which layer is decided here, from the
same turn scope that decides what a turn may read.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
from contextlib import asynccontextmanager, nullcontext, suppress
from datetime import UTC, datetime

from packages.core.memory.layers import (
    SOFT_LIMITS, MemoryTurnScope, org_memory_path, personal_memory_path, read_layer_text, workspace_memory_path,
)
from packages.core.memory.policy import normalized_content

logger = logging.getLogger(__name__)

TARGETS = {
    "org": "org", "workspace": "workspace",
    "my_profile": "profile", "my_preferences": "preferences", "my_private": "private",
}
LAYER_TARGETS = {layer: target for target, layer in TARGETS.items()}
# Markdown section for each kind of memory the memory skill returns.
SECTIONS = {"fact": "Facts", "preference": "Preferences", "decision": "Decisions",
            "guidance": "Guidance", "learning": "Lessons"}
ACTIONS = ("add", "update", "remove", "undo")
_ENTRY_MAX_CHARS = 600
_FILE_TITLES = {
    "org": "# Organization memory", "workspace": "# Workspace memory", "profile": "# Profile",
    "preferences": "# Preferences", "private": "# Private notes",
}
_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
# Beyond the shared redaction patterns: provider key prefixes and one-time codes.
_SECRET_SHAPES = (
    re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|xox[abposr]-[A-Za-z0-9-]{10,}|AIza[0-9A-Za-z_-]{30,})\b"),
    re.compile(r"(?:验证码|校验码|动态码|密码|口令)\s*(?:是|为|:|：|=)?\s*[A-Za-z0-9!@#$%^&*._-]{4,}"),
    re.compile(r"(?i)\b(?:verification code|one[- ]time code|otp|passcode|password|pin)\b"
               r"(?:\s*[:：=]\s*|\s+is\s+)[A-Za-z0-9!@#$%^&*._-]{4,}"),
)


class MemoryWriteRefused(Exception):
    """The write is not allowed or not possible; the message says why."""


def contains_secret(text: str) -> bool:
    from packages.core.services.sensitive_data import redact_sensitive_text

    return redact_sensitive_text(text) != text or any(p.search(text) for p in _SECRET_SHAPES)


def _bullet(content: str) -> str:
    return "- " + " ".join(str(content or "").split())


def _editable_text(layer: str, text: str) -> str:
    """A Workspace MEMORY.md without what setup seeded into it."""
    from packages.core.memory.canonical import workspace_memory_text

    return workspace_memory_text(text) if layer == "workspace" else text


def _line_text(line: str) -> str:
    return normalized_content(re.sub(r"^\s*(?:[-*+]|\d+[.)])\s+", "", line))


def apply_memory_edit(
    text: str, *, layer: str, action: str, content: str = "", entry: str = "", section: str = "",
) -> str:
    """Return the file text after one add / update / remove, by Markdown section."""
    lines = (text or "").rstrip("\n").split("\n") if (text or "").strip() else [_FILE_TITLES[layer]]
    if action == "add":
        bullet = _bullet(content)
        if any(_line_text(line) == _line_text(bullet) for line in lines):
            return "\n".join(lines) + "\n"
        heading = normalized_content(section.lstrip("#")).lower()
        if not heading:
            return "\n".join(lines + [bullet]) + "\n"
        # Sections are level 2 or deeper: "# Preferences" is the file title, not a section.
        start = next((i for i, line in enumerate(lines)
                      if (m := _HEADING.match(line)) and len(m.group(1)) > 1
                      and normalized_content(m.group(2)).lower() == heading), None)
        if start is None:
            return "\n".join(lines + ["", f"## {section.strip().lstrip('#').strip()}", bullet]) + "\n"
        level = len(_HEADING.match(lines[start]).group(1))
        end = next((i for i in range(start + 1, len(lines))
                    if (m := _HEADING.match(lines[i])) and len(m.group(1)) <= level), len(lines))
        while end > start + 1 and not lines[end - 1].strip():
            end -= 1
        return "\n".join(lines[:end] + [bullet] + lines[end:]) + "\n"
    needle = normalized_content(entry).lower()
    if not needle:
        raise MemoryWriteRefused("Pass params.entry: a distinctive part of the line to change.")
    matches = [i for i, line in enumerate(lines) if not _HEADING.match(line) and needle in normalized_content(line).lower()]
    if len(matches) != 1:
        found = "no line" if not matches else f"{len(matches)} lines"
        raise MemoryWriteRefused(f"entry matches {found}; quote more of the exact line.")
    i = matches[0]
    if action == "remove":
        return "\n".join(lines[:i] + lines[i + 1:]) + "\n"
    indent = re.match(r"^\s*", lines[i]).group(0)
    return "\n".join(lines[:i] + [indent + _bullet(content)] + lines[i + 1:]) + "\n"


async def authorize_memory_write(db, scope: MemoryTurnScope, *, entity_id: str, layer: str) -> str:
    """Return the file path this turn may write for ``layer``, or raise."""
    if scope.external:
        raise MemoryWriteRefused("Memory cannot be written from this conversation.")
    if layer == "org":
        from sqlalchemy import select
        from packages.core.models.user import User
        from packages.core.services.workspace_access import is_entity_admin_role

        role = await db.scalar(select(User.role).where(User.id == scope.speaker_id)) if scope.speaker_id else None
        if scope.collaborator or not is_entity_admin_role(role):
            raise MemoryWriteRefused(
                "Only an organization owner or admin can change organization memory. "
                "Save it to this Workspace or to the person's own preferences instead."
            )
        return org_memory_path(entity_id)
    if layer == "workspace":
        if not scope.workspace_id:
            raise MemoryWriteRefused("Workspace memory is written from inside that Workspace.")
        if scope.speaker_id:
            from packages.core.memory.layers import user_reads_workspace

            if not await user_reads_workspace(
                db, entity_id=entity_id, workspace_id=scope.workspace_id, user_id=scope.speaker_id,
            ):
                raise MemoryWriteRefused("This person cannot work in this Workspace.")
        return workspace_memory_path(entity_id, scope.workspace_id)
    if not scope.speaker_id:
        raise MemoryWriteRefused("Personal memory needs the person who said it; this run has none.")
    return personal_memory_path(entity_id, scope.speaker_id, layer)


def layer_entries(entity_id: str, layers, *, user_id: str | None = None,
                  workspace_id: str | None = None) -> list[dict]:
    """The bullet entries now in these layer files, for duplicate checks."""
    paths = {}
    for layer in layers:
        if layer == "org":
            paths[layer] = org_memory_path(entity_id)
        elif layer == "workspace" and workspace_id:
            paths[layer] = workspace_memory_path(entity_id, workspace_id)
        elif layer not in ("org", "workspace") and user_id:
            paths[layer] = personal_memory_path(entity_id, user_id, layer)
    entries = []
    for layer, path in paths.items():
        text = _editable_text(layer, read_layer_text(path))
        lines = [line.strip()[2:] for line in text.splitlines() if line.strip().startswith(("- ", "* "))]
        entries.extend({"id": f"{layer}:{i}", "content": line, "layer": layer} for i, line in enumerate(lines))
    return entries


async def _write(entity_id: str, path: str, text: str) -> None:
    from packages.core.services.entity_fs import get_entity_root, write_entity_file_atomic

    rel = os.path.relpath(path, get_entity_root(entity_id))
    write_entity_file_atomic(entity_id, rel, text.encode("utf-8"), allow_empty=True)


@asynccontextmanager
async def stage_memory_writes(db, entity_id: str):
    """Collect file edits until the audit and source receipts are ready to commit."""
    from packages.core.services.entity_fs import (
        entity_filesystem_mutation_lock, finish_entity_filesystem_mutation, get_entity_root,
    )
    from packages.core.services.runtime_file_journal import recover_runtime_file_commits

    async with entity_filesystem_mutation_lock(get_entity_root(entity_id)):
        await recover_runtime_file_commits(entity_id)
        files: dict[str, str] = {}
        yield files
        if files:
            await finish_entity_filesystem_mutation(_commit_memory_files(db, entity_id, files))
        else:
            await db.commit()


async def _commit_memory_files(db, entity_id: str, files: dict[str, str]) -> None:
    from packages.core.services.entity_fs import (
        EntityFilesystemStaleWriteError, stage_entity_file_rollback_snapshot, write_entity_file_atomic,
    )
    from packages.core.services.runtime_file_journal import RuntimeFileJournal

    await db.flush()
    journal = RuntimeFileJournal(entity_id, memory_audit=True)
    snapshots = {}
    installed = {}

    async def restore_files() -> None:
        cleanup_pending = False
        for path, snapshot in reversed(snapshots.items()):
            try:
                if path not in installed and path in journal.entries:
                    # The writer did not establish what was installed. Leave
                    # the exact snapshot and journal for recovery.
                    cleanup_pending = True
                elif (version := installed.get(path)) is None:
                    snapshot.discard_unwritten()
                else:
                    snapshot.rollback(version)
            except Exception:
                cleanup_pending = True
                logger.exception("Could not restore a staged memory file")
        if not cleanup_pending:
            await journal.settled(committed=False)

    try:
        for path, text in files.items():
            data = text.encode("utf-8")
            snapshot = snapshots[path] = stage_entity_file_rollback_snapshot(entity_id, path)
            await journal.prepare(snapshot, hashlib.sha256(data).hexdigest())

            def check_source(_rel_path, exists, _full_path, expected=snapshot.existed):
                if exists != expected:
                    raise EntityFilesystemStaleWriteError("Memory file changed before commit")

            def receive_installation(version, key=path):
                installed[key] = version

            write_entity_file_atomic(
                entity_id, path, data, allow_empty=True,
                expected_source_version=snapshot.source_version,
                write_precondition=check_source,
                installed_receipt=receive_installation,
            )
            await journal.installed(path, installed[path])
        await journal.mark_committed(db)
    except Exception:
        await restore_files()
        raise

    try:
        await db.commit()
    except Exception:
        with suppress(Exception):
            await db.rollback()
        # If the commit acknowledgement was lost, consult the marker before
        # touching the files. An unknown outcome leaves recovery evidence.
        if not await journal.committed():
            await restore_files()
            raise

    try:
        for snapshot in snapshots.values():
            snapshot.commit()
        await journal.settled(committed=True)
    except Exception:
        logger.exception("Committed memory file backups will be cleaned up from the journal")


async def remember_memory(
    db, scope: MemoryTurnScope, *, entity_id: str, target: str, action: str,
    content: str = "", entry: str = "", section: str = "",
    conversation_id: str | None = None, run_id: str | None = None, agent_id: str | None = None,
    source: str = "remember", created_by: str = "agent", staged_files: dict[str, str] | None = None,
    evidence: dict | None = None,
) -> dict:
    """Record one memory write, or stage it inside ``stage_memory_writes``."""
    from packages.core.models.runtime_learning import AgentLearningCandidate
    from packages.core.services.entity_fs import entity_filesystem_mutation_lock, get_entity_root

    if action == "undo":
        return await undo_memory_write(db, scope, entity_id=entity_id, conversation_id=conversation_id)
    layer = TARGETS.get(target)
    if layer is None:
        raise MemoryWriteRefused(f"Unknown target. Use one of: {', '.join(TARGETS)}.")
    if action not in ACTIONS:
        raise MemoryWriteRefused(f"Unknown action. Use one of: {', '.join(ACTIONS)}.")
    content = " ".join(str(content or "").split())
    if action != "remove" and not content:
        raise MemoryWriteRefused("Pass params.content: the fact, preference or decision in one line.")
    if len(content) > _ENTRY_MAX_CHARS:
        raise MemoryWriteRefused(f"Keep one entry under {_ENTRY_MAX_CHARS} characters; split longer notes.")
    if contains_secret(content):
        raise MemoryWriteRefused("Passwords, keys, tokens and codes are never stored in memory.")
    path = await authorize_memory_write(db, scope, entity_id=entity_id, layer=layer)
    root = get_entity_root(entity_id)
    rel_path = os.path.relpath(path, root)
    lock = entity_filesystem_mutation_lock(root) if staged_files is None else nullcontext()
    async with lock:
        staged = staged_files.get(rel_path) if staged_files is not None else None
        before = staged.strip() if staged is not None else read_layer_text(path)
        base = _editable_text(layer, before)
        after = apply_memory_edit(base, layer=layer, action=action,
                                  content=content, entry=entry, section=section)
        if after.strip() == base.strip():
            return {"status": "unchanged", "target": target, "note": "Already in memory."}
        if staged_files is None:
            await _write(entity_id, path, after)
        else:
            staged_files[rel_path] = after
    row = AgentLearningCandidate(
        entity_id=entity_id, workspace_id=scope.workspace_id if layer == "workspace" else None,
        agent_id=agent_id, user_id=scope.speaker_id, candidate_type="memory",
        scope={"org": "entity", "workspace": "workspace"}.get(layer, "user"),
        title=f"{action} {target}"[:255], summary=content or entry, status="applied", created_by=created_by,
        confidence=1.0, applied_at=datetime.now(UTC),
        payload={"target": target, "layer": layer, "file": rel_path,
                 "section": section or None, "action": action, "entry": entry or None,
                 "before": before, "after": after, "conversation_id": conversation_id,
                 "run_id": run_id, "source": source, **(evidence or {})},
    )
    db.add(row)
    if staged_files is None:
        await db.commit()
    else:
        await db.flush()
    result = {"status": "saved", "target": target, "action": action, "write_id": row.id}
    if len(after) > SOFT_LIMITS[layer]:
        result["note"] = (f"This file is over {SOFT_LIMITS[layer]} characters and will be shortened in prompts. "
                          "Offer to tidy it: merge or remove old entries.")
    return result


async def undo_memory_write(db, scope: MemoryTurnScope, *, entity_id: str, conversation_id: str | None) -> dict:
    """Roll back this person's latest write in this conversation."""
    from sqlalchemy import select
    from packages.core.models.runtime_learning import AgentLearningCandidate
    from packages.core.services.entity_fs import entity_filesystem_mutation_lock, get_entity_root

    if not conversation_id:
        raise MemoryWriteRefused("Nothing to undo in this conversation.")
    query = (
        select(AgentLearningCandidate)
        .where(AgentLearningCandidate.entity_id == entity_id,
               AgentLearningCandidate.candidate_type == "memory",
               AgentLearningCandidate.status == "applied",
               AgentLearningCandidate.created_by == "agent",
               AgentLearningCandidate.payload["conversation_id"].astext == conversation_id,
               (AgentLearningCandidate.user_id == scope.speaker_id) if scope.speaker_id
               else AgentLearningCandidate.user_id.is_(None))
        .order_by(AgentLearningCandidate.created_at.desc()).limit(1).with_for_update()
    )
    row = await db.scalar(query)
    if row is None:
        raise MemoryWriteRefused("Nothing to undo in this conversation.")
    payload = dict(row.payload or {})
    path = os.path.join(get_entity_root(entity_id), payload["file"])
    async with entity_filesystem_mutation_lock(get_entity_root(entity_id)):
        current = read_layer_text(path)
        if current != str(payload.get("after") or "").strip():
            raise MemoryWriteRefused("That memory file changed after the write; edit the entry instead.")
        await _write(entity_id, path, payload.get("before") or "")
    row.status = "rolled_back"
    row.resolution = {**(row.resolution or {}), "rolled_back_at": datetime.now(UTC).isoformat()}
    await db.commit()
    return {"status": "undone", "target": payload.get("target"), "action": payload.get("action"),
            "entry": row.summary}
