"""Host integrity checks for the Task agent loop's ``complete_task``.

The main agent verifies its own work (design §5.5); the host only checks that
the submission is *coherent with what this run observably did*:

1. every acceptance criterion of the current plan has an answer (and all are
   met when the outcome is ``completed``);
2. every deliverable is real: a receipt handle in this run's ledger, or a
   Knowledge file that exists and is readable in the Task's scope;
3. every file deliverable was inspected after this run last wrote it;
4. nothing this Task started is still running (delegation, media jobs);
5. an unconfirmed effect is never reported as success (W1a outcome rule).

A failed check is returned to the loop as a preflight error, so the model
corrects itself in place without spending the continuation budget.
"""
from __future__ import annotations

import copy
import re
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import or_, select

from packages.core.ai.runtime.composite_tools import RuntimeCompositeToolCallFactory
from packages.core.ai.runtime.outcome import Disposition, OutcomeDecision, decide_outcome
from packages.core.ai.runtime.receipts import EffectState
from packages.core.services.file_refs import is_open_address
from packages.core.services.generated_file_refs import (
    ArtifactReferenceFactory,
    canonical_document_id,
    canonical_fs_path,
    dedupe_generated_file_refs,
)

# Read-only tools that inspect one file, and the arguments naming it. Every
# entry must stay classified READ_ONLY (guarded in tests).
INSPECTION_TOOL_ARGUMENTS: dict[str, tuple[str, ...]] = {
    "read_file": ("path", "document_id"),
    "probe_media": ("input_path",),
    "list_files": ("path",),
}
# Action tools whose specific operation looks at an existing file:
# tool -> (operation filter or None, argument, covers files under the path).
ACTION_INSPECTIONS: dict[str, tuple[str | None, str, bool]] = {
    # Review renders approval frames of the editable project directory.
    "video_edit": ("review", "project_path", True),
    # Frame samples are the model's visual read of a video.
    "render_frame_samples": (None, "input_path", False),
}

_RECEIPT_HANDLE = re.compile(r"r[1-9][0-9]*")
# The only verification error that just means the produced file is being
# written right now. artifact_changed can be permanent (bytes gone, not a
# regular file, no mount), so it is never a fallback.
_RECEIPT_FALLBACK_CODES = frozenset({"artifact_busy"})


def _changed_reason(reference: str) -> str:
    return (
        f"The file {reference} changed or could not be read just now; read it again and "
        "call complete_task again."
    )
# Ledger handles are host-only until W2: accepted silently, never advertised.
NON_FILE_ADVICE = (
    "If this is not a Workspace file (a sent message, an external link, a record), remove it "
    "from deliverables and describe it in summary or in the acceptance evidence."
)
_MAX_TRACKED_KEYS = 512
_MAX_OUTPUT_RECEIPTS = 50
_RECEIPT_OUTPUT_FIELDS = (
    "handle", "tool", "kind", "operation", "document_id", "fs_path", "url", "name",
    "external_id", "revision", "effect_state",
)
_RECEIPT_FIELD_MAX_CHARS = 500
_MAX_OUTPUT_EFFECT_GROUPS = 20
_MAX_EFFECT_TARGETS = 5
# Real, honest effects, but workspace-internal compute: shown after the rest.
LOW_SIGNAL_EFFECT_KINDS = frozenset({"cli", "sandbox", "sandbox_file"})


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def file_keys(*, document_id: Any = None, fs_path: Any = None) -> list[str]:
    keys = []
    doc = canonical_document_id(document_id)
    if doc:
        keys.append(f"doc:{doc}")
    path = canonical_fs_path(fs_path)
    if path:
        keys.append(f"path:{path}")
    return keys


def _reference_keys(value: Any) -> list[str]:
    """Identity keys of one model-written path, viewer URL or Document id."""

    text = _text(value)
    if not text:
        return []
    if text.startswith("/viewer/"):
        return file_keys(document_id=text[len("/viewer/"):].strip("/"))
    path = canonical_fs_path(text)
    keys = [f"path:{path}"] if path else []
    if "/" not in text and "." not in text and canonical_document_id(text):
        keys.append(f"doc:{text}")
    return keys


def _bounded(mapping: dict[str, int]) -> dict[str, int]:
    while len(mapping) > _MAX_TRACKED_KEYS:
        mapping.pop(next(iter(mapping)))
    return mapping


class InspectionTracker:
    """Which files this run wrote and read, in tool-call order.

    Writes come from the host receipt ledger (receipts are appended in call
    order and never reordered); reads from the explicit inspection tools
    above. The state is checkpointed with each round.
    """

    def __init__(self, state: Any = None) -> None:
        state = state if isinstance(state, dict) else {}
        self.seq = int(state.get("seq") or 0)
        self.receipts_seen = int(state.get("receipts_seen") or 0)
        self.written: dict[str, int] = {
            str(k): int(v) for k, v in (state.get("written") or {}).items() if isinstance(v, int)
        }
        self.inspected: dict[str, int] = {
            str(k): int(v) for k, v in (state.get("inspected") or {}).items() if isinstance(v, int)
        }
        self.inspected_dirs: dict[str, int] = {
            str(k): int(v) for k, v in (state.get("inspected_dirs") or {}).items() if isinstance(v, int)
        }

    def state(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "receipts_seen": self.receipts_seen,
            "written": dict(self.written),
            "inspected": dict(self.inspected),
            "inspected_dirs": dict(self.inspected_dirs),
        }

    def observe(self, name: str, result: Any, arguments: Any, evidence: Any) -> None:
        """Called after the ledger observed one finished tool call."""

        self.seq += 1
        receipts = list(getattr(evidence, "receipts", None) or [])
        for receipt in receipts[self.receipts_seen:]:
            if isinstance(receipt, dict):
                for key in file_keys(document_id=receipt.get("document_id"), fs_path=receipt.get("fs_path")):
                    self.written.pop(key, None)
                    self.written[key] = self.seq
        self.receipts_seen = len(receipts)
        _bounded(self.written)
        call = RuntimeCompositeToolCallFactory.create(name, arguments if isinstance(arguments, dict) else {})
        args = call.arguments if isinstance(call.arguments, dict) else {}
        keys: list[str] = []
        directory = ""
        if call.tool_name in INSPECTION_TOOL_ARGUMENTS:
            # The file the tool says it read, not what the model typed: a
            # display path or a document_id resolves to the stored file.
            keys.extend(_resolved_read_keys(result))
            if not keys:
                for argument in INSPECTION_TOOL_ARGUMENTS[call.tool_name]:
                    if argument == "document_id":
                        keys.extend(file_keys(document_id=args.get(argument)))
                    else:
                        keys.extend(_reference_keys(args.get(argument)))
        elif call.tool_name in ACTION_INSPECTIONS:
            operation, argument, covers_directory = ACTION_INSPECTIONS[call.tool_name]
            if operation is None or _text(args.get("operation")).lower() == operation:
                if covers_directory:
                    directory = canonical_fs_path(args.get(argument))
                else:
                    keys.extend(_reference_keys(args.get(argument)))
        if (not keys and not directory) or not _succeeded(result):
            return
        # Same sequence number as this call's own receipts: a call never
        # counts as inspecting a file it wrote itself (read > written).
        for key in keys:
            self.inspected.pop(key, None)
            self.inspected[key] = self.seq
        if directory:
            self.inspected_dirs.pop(directory, None)
            self.inspected_dirs[directory] = self.seq
        _bounded(self.inspected)
        _bounded(self.inspected_dirs)

    def inspected_after_write(self, keys: list[str]) -> bool:
        if not keys:
            return True
        written = max((self.written.get(key, 0) for key in keys), default=0)
        read = max((self.inspected.get(key, 0) for key in keys), default=0)
        for key in keys:
            if not key.startswith("path:"):
                continue
            path = key[len("path:"):]
            for directory, seq in self.inspected_dirs.items():
                if path == directory or path.startswith(directory.rstrip("/") + "/"):
                    read = max(read, seq)
        return read > written


def _resolved_read_keys(result: Any) -> list[str]:
    """Identity keys of the file a read reports it resolved, or []."""

    from packages.core.ai.runtime.streams import _runtime_tool_result_payload

    payload = _runtime_tool_result_payload(result)
    if not isinstance(payload, dict):
        return []
    return file_keys(
        document_id=payload.get("document_id") if isinstance(payload.get("document_id"), str) else None,
        fs_path=next(
            (payload[key] for key in ("resolved_path", "fs_path") if isinstance(payload.get(key), str)),
            None,
        ),
    )


def _succeeded(result: Any) -> bool:
    from packages.core.ai.runtime.streams import (
        _runtime_tool_control_status,
        _runtime_tool_result_payload,
        runtime_tool_call_error,
    )
    from packages.core.ai.runtime.tool_results import RuntimePreflightValidationError

    if isinstance(result, RuntimePreflightValidationError):
        return False
    if _runtime_tool_control_status(result) is not None or runtime_tool_call_error(result) is not None:
        return False
    payload = _runtime_tool_result_payload(result)
    return not (
        isinstance(payload, dict)
        and (payload.get("error") or payload.get("ok") is False or payload.get("success") is False)
    )


# ── 1. Acceptance coverage ─────────────────────────────────────────────


def normalize_criterion(value: Any) -> str:
    return re.sub(r"\s+", " ", _text(value)).strip(" .;:").casefold()


def acceptance_problems(submission: dict[str, Any], task_plan: Any) -> list[str]:
    criteria = (
        [c for c in task_plan.get("acceptance_criteria") or [] if _text(c)]
        if isinstance(task_plan, dict)
        else []
    )
    entries = [e for e in submission.get("acceptance") or [] if isinstance(e, dict)]
    by_criterion = {normalize_criterion(e.get("criterion")): e for e in entries}
    problems: list[str] = []
    if criteria:
        for criterion in criteria:
            if normalize_criterion(criterion) not in by_criterion:
                problems.append(
                    f"acceptance has no entry for the plan criterion '{criterion}'. Answer every "
                    "acceptance criterion of your current plan with {criterion, met, evidence} "
                    "(revise the plan with update_task_plan if a criterion no longer applies)."
                )
    elif not any(_text(e.get("criterion")) and _text(e.get("evidence")) for e in entries):
        problems.append(
            "acceptance is empty and this Task has no planned acceptance criteria. State at least "
            "one criterion you checked, whether it is met, and the evidence you observed."
        )
    for entry in entries:
        if entry.get("met") is True and not _text(entry.get("evidence")):
            problems.append(
                f"acceptance criterion '{entry.get('criterion')}' is met=true without evidence; "
                "say what you checked and what you observed."
            )
    if submission.get("outcome") == "completed":
        for entry in entries:
            if entry.get("met") is not True:
                problems.append(
                    f"acceptance criterion '{entry.get('criterion')}' is met=false, but "
                    "outcome=completed needs every criterion met. Finish the work, or report "
                    "outcome=blocked with blocked_reason."
                )
    return problems


# ── 2/3. Deliverables are real and inspected ───────────────────────────


def _is_file_receipt(receipt: dict[str, Any]) -> bool:
    return bool(receipt.get("document_id") or receipt.get("fs_path"))


def _receipt_matching(receipts: list[dict[str, Any]], reference: str) -> dict[str, Any] | None:
    if _RECEIPT_HANDLE.fullmatch(reference):
        # Handles are sequential and invisible to the model, so a guess can
        # land on any receipt. Only a file receipt can back one, and that
        # file still has to pass verification and the inspection rule.
        receipt = next((r for r in receipts if r.get("handle") == reference), None)
        return receipt if receipt is not None and _is_file_receipt(receipt) else None
    keys = set(_reference_keys(reference))
    for receipt in receipts:
        if keys & set(file_keys(document_id=receipt.get("document_id"), fs_path=receipt.get("fs_path"))):
            return receipt
        if _text(receipt.get("url")) and _text(receipt.get("url")) == reference:
            return receipt
    return None


def _file_ref_for_reference(reference: str) -> dict[str, Any]:
    if is_open_address(reference):
        return {"type": "file", "url": reference}
    if "/" not in reference and "." not in reference and canonical_document_id(reference):
        return {"type": "file", "document_id": reference}
    return {"type": "file", "fs_path": reference}


async def resolve_deliverables(
    db: Any,
    *,
    deliverables: list[dict[str, Any]],
    evidence: Any,
    entity_id: str,
    workspace_id: str | None,
    user_id: str | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Resolve each deliverable to a ledger receipt or a verified file.

    Returns ``(resolved, unresolved)``. A resolved entry carries the identity
    ``keys`` used by the inspection check and, for a file, its canonical
    ``file_ref`` (the ``actual_output.files`` shape). An unresolved entry
    carries the ``reason`` shown to the model.
    """

    from packages.core.services.artifact_delivery import (
        ArtifactDeliveryError,
        verify_artifact_delivery_refs,
    )

    receipts = [r for r in getattr(evidence, "receipts", None) or [] if isinstance(r, dict)]
    factory = ArtifactReferenceFactory(entity_id=entity_id)

    async def verify(ref: dict[str, Any]) -> dict[str, Any]:
        verified = await verify_artifact_delivery_refs(
            db, entity_id=entity_id, workspace_id=workspace_id, user_id=user_id, refs=[ref],
        )
        return verified.projection.refs[0]

    resolved: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    for index, item in enumerate(deliverables, start=1):
        reference = _text(item.get("reference"))
        label = f"deliverable {index} '{reference or item.get('title') or ''}'"

        def refuse(reason: str) -> None:
            unresolved.append({
                "index": index, "title": item.get("title"), "reference": reference,
                "reason": reason,
            })

        if not reference:
            refuse(
                f"{label} has no reference. Give the delivered file's Document id, or the "
                f"exact fs_path a tool result returned. {NON_FILE_ADVICE}"
            )
            continue
        receipt = _receipt_matching(receipts, reference)
        if receipt is not None:
            entry = {"index": index, "title": item.get("title"), "reference": reference,
                     "source": "receipt", "handle": receipt.get("handle")}
            identity = {k: receipt.get(k) for k in ("document_id", "fs_path") if receipt.get(k)}
            if identity:
                try:
                    file_ref = await verify({"type": "file", **identity})
                except ArtifactDeliveryError as exc:
                    if exc.code == "artifact_changed":
                        refuse(_changed_reason(reference))
                        continue
                    if exc.code not in _RECEIPT_FALLBACK_CODES:
                        # Gone, unreadable or not a Knowledge file: the receipt
                        # proves it was produced, not that it can be delivered.
                        refuse(
                            f"{label} is not an existing, readable file in this Workspace "
                            f"({exc}). Use its Document id, or the exact fs_path a tool result "
                            f"returned. {NON_FILE_ADVICE}"
                        )
                        continue
                    # Being written right now: the receipt is the proof.
                    file_ref = factory.create({
                        "type": "file",
                        **{k: receipt.get(k) for k in ("name", "document_id", "fs_path", "url") if receipt.get(k)},
                    })
                entry["file_ref"] = file_ref
                entry["keys"] = sorted(set(
                    file_keys(document_id=file_ref.get("document_id"), fs_path=file_ref.get("fs_path"))
                    + file_keys(document_id=receipt.get("document_id"), fs_path=receipt.get("fs_path"))
                ))
            resolved.append(entry)
            continue
        try:
            file_ref = await verify(_file_ref_for_reference(reference))
        except ArtifactDeliveryError as exc:
            refuse(
                f"{label} is not an existing, readable file in this Workspace ({exc}). Use its "
                f"Document id, or the exact fs_path a tool result returned. {NON_FILE_ADVICE}"
            )
            continue
        resolved.append({
            "index": index, "title": item.get("title"), "reference": reference, "source": "file",
            "file_ref": file_ref,
            "keys": file_keys(document_id=file_ref.get("document_id"), fs_path=file_ref.get("fs_path")),
        })
    return resolved, unresolved


def inspection_problems(resolved: list[dict[str, Any]], tracker: InspectionTracker) -> list[str]:
    problems = []
    for entry in resolved:
        keys = entry.get("keys") or []
        if not keys or tracker.inspected_after_write(keys):
            continue
        path = (entry.get("file_ref") or {}).get("fs_path") or entry.get("reference")
        problems.append(
            f"Inspect {path} before completing (e.g. read_file/probe_media): deliverable "
            f"{entry['index']} was not read or inspected in this run after it was last written."
        )
    return problems


FILE_ORIGIN_CREATED = "created"
FILE_ORIGIN_REFERENCED = "referenced"


def delivered_files(resolved: list[dict[str, Any]], *, entity_id: str) -> list[dict[str, Any]]:
    """Verified deliverable refs, each tagged with its origin in this run.

    ``created``: backed by a receipt from this run's ledger. ``referenced``: an
    existing readable file the run delivered without producing it; it is
    shown on the Task but never recorded as an artifact this Task created.
    """
    from packages.core.services.generated_file_refs import generated_file_ref_aliases

    refs = [copy.deepcopy(e["file_ref"]) for e in resolved if isinstance(e.get("file_ref"), dict)]
    if not refs:
        return []
    created: set[str] = set()
    for entry in resolved:
        if entry.get("source") == "receipt" and isinstance(entry.get("file_ref"), dict):
            created |= set(generated_file_ref_aliases(entry["file_ref"]))
    out = dedupe_generated_file_refs(refs, entity_id=entity_id)
    for ref in out:
        ref["origin"] = (
            FILE_ORIGIN_CREATED
            if set(generated_file_ref_aliases(ref)) & created
            else FILE_ORIGIN_REFERENCED
        )
    return out


# ── 4. Nothing still running ───────────────────────────────────────────


async def running_work_problems(
    db: Any,
    *,
    entity_id: str,
    task_id: str,
    conversation_id: str | None,
) -> list[str]:
    from packages.core.models.agent_delegation_session import AgentDelegationSession
    from packages.core.models.media_job import MediaJob, MediaJobStatus

    problems: list[str] = []
    if conversation_id:
        now = datetime.now(timezone.utc)
        services = list((await db.execute(
            select(AgentDelegationSession.service_key, AgentDelegationSession.agent_id).where(
                AgentDelegationSession.entity_id == entity_id,
                AgentDelegationSession.conversation_id == conversation_id,
                AgentDelegationSession.status == "running",
                or_(
                    AgentDelegationSession.active_until.is_(None),
                    AgentDelegationSession.active_until > now,
                ),
            ).limit(10)
        )).all())
        if services:
            names = ", ".join(str(row.service_key or row.agent_id) for row in services)
            problems.append(
                f"A delegated agent is still working on this Task ({names}). Wait for its "
                "result before completing."
            )
    jobs = list((await db.execute(
        select(MediaJob.id, MediaJob.kind).where(
            MediaJob.entity_id == entity_id,
            MediaJob.status.in_((MediaJobStatus.PENDING.value, MediaJobStatus.PROCESSING.value)),
            MediaJob.params["task_id"].astext == task_id,
        ).limit(20)
    )).all())
    if jobs:
        ids = ", ".join(str(job.id) for job in jobs)
        problems.append(
            f"Wait for job(s) {ids} (wait_media_jobs) before completing; they are still running "
            "for this Task."
        )
    return problems


# ── 5. Outcome rule ────────────────────────────────────────────────────


def outcome_decision(effect_state: Any, outcome: str) -> OutcomeDecision:
    try:
        state = EffectState(str(effect_state or EffectState.NONE.value))
    except ValueError:
        state = EffectState.UNCONFIRMED
    disposition = Disposition.SUCCESS if outcome == "completed" else Disposition.NON_SUCCESS
    return decide_outcome(state, disposition)


def unconfirmed_call_ids(evidence: Any) -> list[str]:
    return [
        str(call.get("call_id"))
        for call in getattr(evidence, "unconfirmed_calls", None) or []
        if isinstance(call, dict) and call.get("call_id")
    ]


def describe_unconfirmed(calls: list[dict[str, Any]], *, limit: int = 20) -> str:
    parts = []
    for call in calls[:limit]:
        tool = str(call.get("tool") or "unknown")
        operation = str(call.get("operation") or "")
        parts.append(f"{tool}/{operation}" if operation and operation != tool else tool)
    more = f" (+{len(calls) - limit} more)" if len(calls) > limit else ""
    return ", ".join(parts) + more if parts else "an earlier tool call (ledger floor)"


def output_effects(evidence_state: Any) -> tuple[list[dict[str, Any]], int]:
    """The run's confirmed non-file effects (the ledger's per-action groups).

    Returns ``(groups, omitted)``: at most 20 groups, workspace-internal
    compute (cli/sandbox) after everything else so it never crowds out a
    created Task or a sent message, each otherwise in first-seen order; and
    how many effect calls are not shown (dropped groups, or never kept by the
    ledger's own cap).
    """

    state = evidence_state if isinstance(evidence_state, dict) else {}
    effects = state.get("effects") if isinstance(state.get("effects"), list) else []
    omitted = state.get("effects_omitted") if isinstance(state.get("effects_omitted"), int) else 0
    groups = [
        effect for effect in effects
        if isinstance(effect, dict) and _text(effect.get("action_key"))
        and isinstance(effect.get("count"), int) and effect["count"] > 0
    ]
    groups.sort(key=lambda effect: _text(effect.get("resource_kind")) in LOW_SIGNAL_EFFECT_KINDS)
    out = []
    for effect in groups:
        if len(out) >= _MAX_OUTPUT_EFFECT_GROUPS:
            omitted += effect["count"]
            continue
        targets = [
            _text(target)[:_RECEIPT_FIELD_MAX_CHARS] for target in effect.get("targets") or []
            if _text(target)
        ][:_MAX_EFFECT_TARGETS]
        out.append({k: v for k, v in {
            "action_key": _text(effect["action_key"]),
            "title": effect_title(effect["action_key"]),
            "resource_kind": _text(effect.get("resource_kind")) or None,
            "count": effect["count"],
            "targets": targets,
        }.items() if v not in (None, [])})
    return out, max(0, omitted)


# Localized labels for the effects people most often need to see; any other
# action falls back to effect_title (English, from the classifier's key).
_EFFECT_LABEL_KEYS = {
    "workspace.task.create": "plan.log.effect.task_create",
    "external_message.send": "plan.log.effect.message_send",
    "email.send": "plan.log.effect.email_send",
    "workspace.site.publish": "plan.log.effect.site_publish",
    "workspace.rule.update": "plan.log.effect.rule_update",
    "workspace.operation.apply": "plan.log.effect.operation_apply",
    "google_calendar.create_event": "plan.log.effect.calendar_event_create",
    "ms_calendar.create_event": "plan.log.effect.calendar_event_create",
}


def effect_label(action_key: Any, *, locale: str) -> str:
    """A person-facing label for one effect group."""
    from packages.core.i18n import t

    key = _EFFECT_LABEL_KEYS.get(_text(action_key))
    if key:
        return t(key, locale=locale)
    title = effect_title(action_key)
    return f"{title[:1].upper()}{title[1:]}"


def effect_title(action_key: Any) -> str:
    """A short English label from an action key: workspace.task.create ->
    "create workspace task". Not stored per call; derived when shown."""

    parts = [part.replace("_", " ") for part in _text(action_key).split(".") if part]
    if len(parts) < 2:
        return " ".join(parts)
    return f"{parts[-1]} {' '.join(parts[:-1])}"


_MAX_LEDGER_COMMENT_FILES = 20


def people_facing_effects(effects: Any) -> list[dict[str, Any]]:
    """The effect groups a Task comment shows: workspace-internal compute
    (cli/sandbox) stays in actual_output.effects only."""

    return [
        effect for effect in effects or []
        if isinstance(effect, dict) and effect.get("action_key")
        and effect.get("resource_kind") not in LOW_SIGNAL_EFFECT_KINDS
    ]


def ledger_work_so_far(evidence_state: Any) -> dict[str, Any]:
    """What a run confirmably produced, for a stop that has no submission.

    ``ledger_files``: identities of confirmed file receipts (newest 20, one
    per file); ``effects``/``effects_omitted``: as ``output_effects``. Empty
    when the run produced nothing, so no empty comment is ever posted.
    """

    receipts = evidence_state.get("receipts") if isinstance(evidence_state, dict) else None
    files: dict[tuple[str, ...], dict[str, Any]] = {}
    for receipt in receipts if isinstance(receipts, list) else []:
        if (
            not isinstance(receipt, dict)
            or not _is_file_receipt(receipt)
            or receipt.get("effect_state") != EffectState.CONFIRMED.value
        ):
            continue
        identity = {k: receipt[k] for k in ("document_id", "fs_path", "name") if _text(receipt.get(k))}
        key = tuple(file_keys(document_id=identity.get("document_id"), fs_path=identity.get("fs_path")))
        files.pop(key, None)
        files[key] = identity
    effects, omitted = output_effects(evidence_state)
    if not files and not people_facing_effects(effects) and not omitted:
        # Nothing a person would see in the comment (cli/sandbox only).
        return {}
    return {
        "ledger_files": list(files.values())[-_MAX_LEDGER_COMMENT_FILES:],
        "effects": effects,
        "effects_omitted": omitted,
    }


def ledger_file_refs(identities: Any, *, entity_id: str) -> list[dict[str, Any]]:
    """Canonical file refs (with markdown links) for ``ledger_files``."""

    factory = ArtifactReferenceFactory(entity_id=entity_id)
    refs = [
        factory.create({"type": "file", **identity})
        for identity in identities if isinstance(identity, dict)
    ] if isinstance(identities, list) else []
    return dedupe_generated_file_refs(refs, entity_id=entity_id) if refs else []


def output_receipts(evidence_state: Any) -> list[dict[str, Any]]:
    """Ledger receipts for actual_output: newest 50, short fields only."""

    receipts = evidence_state.get("receipts") if isinstance(evidence_state, dict) else None
    out = []
    for receipt in (receipts if isinstance(receipts, list) else [])[-_MAX_OUTPUT_RECEIPTS:]:
        if not isinstance(receipt, dict):
            continue
        out.append({
            key: (receipt[key][:_RECEIPT_FIELD_MAX_CHARS] if isinstance(receipt[key], str) else receipt[key])
            for key in _RECEIPT_OUTPUT_FIELDS
            if receipt.get(key) not in (None, "")
            and isinstance(receipt.get(key), (str, int, float, bool))
        })
    return out
