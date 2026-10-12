"""Validate declarative task completion requirements from durable runtime evidence."""

from __future__ import annotations

import asyncio
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.ai.runtime.events import RuntimeToolResultStatus
from packages.core.models.execution import ExecutionStep


def task_completion_requirements(task: Any | None) -> dict[str, Any]:
    """Return the completion requirements persisted on a Task-like object."""

    if task is None:
        return {}
    details = getattr(task, "details", None)
    configured = details.get("completion_requirements") if isinstance(details, dict) else None
    requirements = dict(configured) if isinstance(configured, dict) else {}
    required_skills = [
        str(value).strip() for value in (getattr(task, "required_skills", None) or []) if str(value or "").strip()
    ]
    if required_skills and not requirements.get("required_skill_invocations"):
        requirements["required_skill_invocations"] = required_skills
    return requirements


def successful_runtime_tool_event(event: Any) -> bool:
    """Return whether a durable runtime event proves a completed tool call."""

    if getattr(event, "event_type", None) != "tool_end":
        return False
    data = getattr(event, "event_data", None)
    if not isinstance(data, dict) or data.get("status") is None:
        return True
    status = RuntimeToolResultStatus.parse(data.get("status"))
    return bool(status and status.is_successful_completion)


async def task_runtime_events(db: AsyncSession, task: Any) -> list[Any]:
    """Load runtime evidence from the current execution of one Task."""

    from packages.core.models.runtime_learning import RuntimeEventLog

    stmt = select(RuntimeEventLog).where(RuntimeEventLog.task_id == task.id)
    if getattr(task, "entity_id", None):
        stmt = stmt.where(RuntimeEventLog.entity_id == task.entity_id)
    if getattr(task, "workspace_id", None):
        stmt = stmt.where(RuntimeEventLog.workspace_id == task.workspace_id)
    started_at = getattr(task, "started_at", None)
    if started_at is not None:
        stmt = stmt.where(RuntimeEventLog.created_at >= started_at)
    stmt = stmt.order_by(RuntimeEventLog.created_at, RuntimeEventLog.sequence)
    return list((await db.execute(stmt)).scalars().all())


def _mapping_list(value: Any) -> list[dict[str, Any]]:
    return [dict(item) for item in value or [] if isinstance(item, dict)]


def _latest_tool_event(
    events: list[Any],
    tool_name: str,
    *,
    kind: str | None = None,
) -> Any | None:
    matching = [
        event
        for event in events
        if str(getattr(event, "tool_name", "") or "").strip() == tool_name
        and successful_runtime_tool_event(event)
        and isinstance(getattr(event, "event_data", None), dict)
        and (not kind or str((event.event_data or {}).get("kind") or "").strip() == kind)
    ]
    return matching[-1] if matching else None


def _evidence_label(rule: dict[str, Any], *, fallback: str) -> str:
    return str(rule.get("label") or fallback).strip()


async def completion_contract_issue(
    db: AsyncSession,
    task: Any | None,
) -> str | None:
    """Return the first unmet completion requirement, if one exists."""

    requirements = task_completion_requirements(task)
    if not requirements or task is None:
        return None
    events = await task_runtime_events(db, task)

    skill_invocations = {
        str((event.event_data or {}).get("skill_ref") or "").strip()
        for event in events
        if getattr(event, "event_type", None) == "tool_end"
        and getattr(event, "tool_name", None) == "invoke_skill"
        and successful_runtime_tool_event(event)
        and isinstance(getattr(event, "event_data", None), dict)
    }
    required_skills = {
        str(value).strip()
        for value in (requirements.get("required_skill_invocations") or [])
        if str(value or "").strip()
    }
    missing_skills = sorted(required_skills - skill_invocations)
    if missing_skills:
        return "Required Skill invocation evidence is missing: " + ", ".join(missing_skills)

    successful_tools = {
        str(getattr(event, "tool_name", "") or "").strip() for event in events if successful_runtime_tool_event(event)
    }
    required_tools = {
        str(value).strip() for value in (requirements.get("required_tool_evidence") or []) if str(value or "").strip()
    }
    missing_tools = sorted(required_tools - successful_tools)
    if missing_tools:
        return "Required tool evidence is missing: " + ", ".join(missing_tools)

    for rule in _mapping_list(requirements.get("artifact_evidence")):
        tool_name = str(rule.get("tool") or "").strip()
        if not tool_name:
            continue
        event = _latest_tool_event(
            events,
            tool_name,
            kind=str(rule.get("kind") or "").strip() or None,
        )
        data = event.event_data if event is not None else {}
        reference_fields = [
            str(value).strip()
            for value in (rule.get("reference_fields") or ["fs_path", "result_url", "video_url", "file_url"])
            if str(value or "").strip()
        ]
        if not event or not any(data.get(field) for field in reference_fields):
            label = _evidence_label(rule, fallback=f"Artifact from {tool_name}")
            return f"{label} evidence is missing."
        missing_fields = [
            str(field)
            for field in (rule.get("required_fields") or [])
            if str(field or "").strip() and data.get(str(field)) is None
        ]
        if missing_fields:
            label = _evidence_label(rule, fallback=f"Artifact from {tool_name}")
            return f"{label} is missing fields: {', '.join(missing_fields)}."

    for rule in _mapping_list(requirements.get("numeric_evidence")):
        tool_name = str(rule.get("tool") or "").strip()
        field = str(rule.get("field") or "").strip()
        if not tool_name or not field:
            continue
        event = _latest_tool_event(
            events,
            tool_name,
            kind=str(rule.get("kind") or "").strip() or None,
        )
        label = _evidence_label(rule, fallback=f"{tool_name}.{field}")
        try:
            value = float((event.event_data or {}).get(field))
        except (AttributeError, TypeError, ValueError):
            value = None
        if value is None:
            return f"{label} evidence is missing."
        minimum = rule.get("min")
        maximum = rule.get("max")
        try:
            if minimum is not None and value < float(minimum):
                return f"{label} is {value:g}; minimum required value is {float(minimum):g}."
            if maximum is not None and value > float(maximum):
                return f"{label} is {value:g}; maximum allowed value is {float(maximum):g}."
        except (TypeError, ValueError):
            return f"{label} has an invalid numeric completion rule."
    return None


async def file_deliverable_contract_issue(
    db: AsyncSession, task: Any | None, payload: Any,
) -> str | None:
    """Validate opt-in Document contracts against Knowledge and actual tool receipts."""
    from pydantic import ValidationError
    from packages.core.models.document import Document
    from packages.core.services.entity_fs import EntityFilesystemError, open_entity_file_snapshot
    from packages.core.services.workspace_artifacts import resolve_workspace_folder_binding
    from packages.core.strategist.proposal import Deliverable

    expected = getattr(task, "expected_output", None)
    rules = [
        rule for rule in _mapping_list(expected.get("deliverables"))
        if rule.get("document_id_path") is not None
        or rule.get("workspace_folder") is not None or rule.get("require_readback")
    ] if isinstance(expected, dict) else []
    if not rules:
        return None
    if not getattr(task, "workspace_id", None):
        return "Document deliverables require a Workspace."
    events = await task_runtime_events(db, task)
    for raw_rule in rules:
        try:
            rule = Deliverable.model_validate(raw_rule)
        except ValidationError:
            return "Invalid file deliverable contract."
        document_id = payload
        try:
            for token in rule.document_id_path[1:].split("/"):
                key = token.replace("~1", "/").replace("~0", "~")
                if isinstance(document_id, list):
                    if re.fullmatch(r"0|[1-9][0-9]*", key) is None:
                        raise ValueError("Invalid JSON Pointer array index")
                    document_id = document_id[int(key)]
                else:
                    document_id = document_id[key]
        except (KeyError, IndexError, TypeError, ValueError):
            document_id = None
        if not isinstance(document_id, str) or not document_id.strip():
            return f"{rule.name}: missing canonical Document ID at {rule.document_id_path}."
        document = (await db.execute(select(Document).where(
            Document.id == document_id, Document.entity_id == task.entity_id,
            Document.is_trashed.is_(False),
        ))).scalar_one_or_none()
        if document is None or not document.fs_path:
            return f"{rule.name}: canonical Document is unavailable."
        binding = await resolve_workspace_folder_binding(
            db, entity_id=task.entity_id, folder_id=document.folder_id,
        )
        if binding is None or binding.workspace_id != task.workspace_id:
            return f"{rule.name}: Document is not filed in this Workspace."
        if rule.workspace_folder is not None and "/".join(binding.relative_parts) != rule.workspace_folder:
            return f"{rule.name}: Document must be filed in {rule.workspace_folder}."

        digest = None
        if rule.require_readback:
            writes, reads = [], []
            for index, event in enumerate(events):
                data = event.event_data if isinstance(event.event_data, dict) else {}
                if not successful_runtime_tool_event(event) or not data.get("source_sha256"):
                    continue
                same_document = data.get("document_id") == document.id
                same_path = (data.get("resolved_path") or data.get("fs_path") or data.get("path")) == document.fs_path
                if event.tool_name in {"generate_file", "patch_file"} and same_document:
                    writes.append((index, data))
                elif event.tool_name == "read_file" and (same_document or (not data.get("document_id") and same_path)):
                    reads.append((index, data))
            if not writes or not reads or reads[-1][0] <= writes[-1][0]:
                return f"{rule.name}: missing write followed by read_file for this Document in the current Task."
            digest = writes[-1][1]["source_sha256"]
            if digest != reads[-1][1]["source_sha256"]:
                return f"{rule.name}: write and readback refer to different file revisions."

        def verify_file():
            with open_entity_file_snapshot(task.entity_id, document.fs_path, expected_content_sha256=digest):
                pass

        try:
            await asyncio.to_thread(verify_file)
        except (EntityFilesystemError, OSError, ValueError):
            return f"{rule.name}: canonical file is missing, unsafe, or changed after readback."
    return None


def _artifact_reference(
    *,
    tool_name: str,
    rule: dict[str, Any],
    data: dict[str, Any],
) -> tuple[dict[str, Any], str] | None:
    reference_fields = [
        str(value).strip()
        for value in (rule.get("reference_fields") or ["fs_path", "result_url", "video_url", "file_url"])
        if str(value or "").strip()
    ]
    source_field = next(
        (field for field in reference_fields if data.get(field)),
        None,
    )
    if source_field is None:
        return None
    identity = str(data[source_field])
    kind = str(rule.get("kind") or data.get("kind") or "artifact").strip()
    ref: dict[str, Any] = {
        "type": kind,
        "source": f"runtime_event:{tool_name}",
    }
    if source_field == "fs_path" or source_field.endswith("_path"):
        ref["fs_path"] = data[source_field]
    elif "url" in source_field:
        ref["url"] = data[source_field]
    else:
        ref[source_field] = data[source_field]
    for field in rule.get("copy_fields") or []:
        field_name = str(field or "").strip()
        if field_name and data.get(field_name) is not None:
            ref[field_name] = data[field_name]
    return ref, identity


async def hydrate_completion_artifacts(
    db: AsyncSession,
    task: Any | None,
    steps: list[ExecutionStep],
) -> None:
    """Promote declared runtime artifact evidence onto the producing step."""
    from packages.core.contracts.artifact_delivery import readonly_artifact_delivery

    requirements = task_completion_requirements(task)
    artifact_rules = _mapping_list(requirements.get("artifact_evidence"))
    if task is None or not steps or not artifact_rules:
        return
    events = await task_runtime_events(db, task)
    references: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    for rule in artifact_rules:
        tool_name = str(rule.get("tool") or "").strip()
        if not tool_name:
            continue
        event = _latest_tool_event(
            events,
            tool_name,
            kind=str(rule.get("kind") or "").strip() or None,
        )
        if event is None:
            continue
        data = dict(event.event_data or {})
        artifact = _artifact_reference(
            tool_name=tool_name,
            rule=rule,
            data=data,
        )
        if artifact is not None:
            ref, identity = artifact
            references.append((identity, ref, data))
    if not references:
        return

    owner_service_key = getattr(task, "owner_service_key", None)
    # A read-only delivery is an exact host-verified submission, not the
    # producer of arbitrary Task-level runtime events. Mutating it here would
    # invalidate its receipt or falsely attribute another Step's write.
    producing_steps = [
        step for step in reversed(steps)
        if step.step_status == "done" and not readonly_artifact_delivery(getattr(step, "params", None))
    ]
    target = next(
        (step for step in producing_steps if step.service_key == owner_service_key),
        next(iter(producing_steps), None),
    )
    if target is None:
        return

    result = dict(target.result or {})
    existing_files = result.get("files")
    files = list(existing_files) if isinstance(existing_files, list) else []
    evidence = list(target.evidence_refs or [])
    runtime_evidence: list[dict[str, Any]] = []
    for identity, ref, data in references:
        if not any(isinstance(item, dict) and (item.get("fs_path") or item.get("url")) == identity for item in files):
            files.append(ref)
        if not any(
            isinstance(item, dict)
            and item.get("kind") == "artifact"
            and (item.get("fs_path") or item.get("url")) == identity
            for item in evidence
        ):
            evidence.append({"kind": "artifact", **ref})
        runtime_evidence.append(
            {
                "tool": str(ref.get("source") or "").removeprefix("runtime_event:"),
                "fields": {key: data[key] for key in ref if key not in {"type", "source"} and key in data},
            }
        )

    result["files"] = files
    result["artifact_materialized"] = True
    result["runtime_completion_evidence"] = runtime_evidence
    target.result = result
    target.evidence_refs = evidence
    await db.flush()


__all__ = [
    "completion_contract_issue",
    "hydrate_completion_artifacts",
    "successful_runtime_tool_event",
    "task_completion_requirements",
    "task_runtime_events",
]
