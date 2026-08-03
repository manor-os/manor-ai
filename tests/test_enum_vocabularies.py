"""The converted status vocabularies stay converted.

Production task 01KWRR5VGHYHQD3A116TZ8ET0W finished "failed" over seven
successful steps because a supervisor verdict was a bare string: nothing
connected the word the model produced to the words the executor tested for.
That is one instance of a pattern, not a one-off — a closed vocabulary
spelled as a literal at every site that reads or writes it, with no type
holding the spellings together.

Each vocabulary below now has an enum. These tests are what keeps the
literals from coming back: they scan the backend for comparisons and
assignments that spell a vocabulary word next to a receiver that owns it,
and require zero. A new state is then a new enum member — not a literal
scattered across the executor, the routers and the read models.

The scan is deliberately receiver-anchored. ``"completed"`` on its own is
an ordinary English word that appears in prompts, docstrings and unrelated
tables; ``task.status == "completed"`` is the defect. Anything that isn't
anchored to a receiver in the list is not this test's business.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from packages.core.constants.approvals import (
    ApprovalOriginKind,
    ApprovalStatus,
    HitlType,
)
from packages.core.constants.blueprints import BlueprintStatus
from packages.core.constants.pending_actions import PendingActionKind
from packages.core.constants.execution import (
    ExecutionPlanStatus,
    ExecutionStepStatus,
    WorkerStatus,
    WorkLeaseStatus,
)
from packages.core.constants.supervisor import SupervisorVerdict
from packages.core.models.media_job import MediaJobStatus
from packages.core.constants.task import TaskLogType, TaskStatus

REPO_ROOT = Path(__file__).resolve().parents[1]
SCANNED_ROOTS = ("packages/core", "apps/api")

#: The enum definitions themselves must spell their values — that is what a
#: definition is. Migrations are frozen historical SQL and are never edited
#: to follow a refactor.
EXEMPT_PATHS = (
    "packages/core/constants/",
    "alembic/",
    "migrations/",
)

#: (label, receiver attribute names, the enum whose values are the closed
#: vocabulary, and optionally the files that own it). A receiver name is what
#: makes a hit a real one: ``status`` alone belongs to fifty tables,
#: ``step_status`` belongs to one. Where even the receiver is ambiguous —
#: ``job.status`` is a media job here and a dashboard-generation job there,
#: and both vocabularies contain the word "failed" — the entry names the
#: files that own the vocabulary, and the scan looks only at those.
VOCABULARIES: tuple[tuple[str, tuple[str, ...], type, tuple[str, ...]], ...] = (
    ("execution step status", ("step_status",), ExecutionStepStatus, ()),
    (
        "execution plan status",
        ("ExecutionPlan.status", "plan.status", "plan_row.status"),
        ExecutionPlanStatus,
        (),
    ),
    ("work lease status", ("WorkLease.status", "lease.status"), WorkLeaseStatus, ()),
    ("worker status", ("Worker.status", "worker.status", "w.status"), WorkerStatus, ()),
    ("task status", ("Task.status", "task.status", "task_row.status"), TaskStatus, ()),
    ("task log type", ("log_type", "TaskLog.log_type"), TaskLogType, ()),
    ("supervisor verdict", ("verdict", "decision.verdict"), SupervisorVerdict, ()),
    (
        "approval status",
        ("HitlRequest.status", "req.status", "request.status"),
        ApprovalStatus,
        (),
    ),
    (
        "media job status",
        ("MediaJob.status", "job.status"),
        MediaJobStatus,
        (
            "packages/core/tasks/media_tasks.py",
            "packages/core/ai/tools/media_tools.py",
            "packages/core/services/document_file_repair.py",
        ),
    ),
    (
        "blueprint status",
        ("WorkspaceBlueprint.status", "blueprint.status", "bp.status"),
        BlueprintStatus,
        (),
    ),
    # ── The HITL trio ────────────────────────────────────────────────────
    # These three are the vocabularies of the unified request layer. They are
    # the ones the enum sweep was originally motivated by and the ones it had
    # not yet reached: a card kind, the kind of human involvement, and where
    # the block is happening. All three are read on one plane and written on
    # another, which is exactly the shape that drifts.
    (
        "hitl type",
        (
            "hitl_type",
            "_hitl_type",
            "card_hitl_type",
            "HitlRequest.hitl_type",
            "req.hitl_type",
            "request.hitl_type",
        ),
        HitlType,
        (),
    ),
    (
        # A card whose kind the resolve endpoint does not route is a card with
        # no working buttons — and, for a lease-origin pause, a request stuck
        # PENDING behind a step that never leaves waiting_human.
        "pending action kind",
        (
            "kind",
            "pending_kind",
            'pending_action["kind"].as_string()',
            'pending_action.get("kind")',
            'pa.get("kind")',
            'action.get("kind")',
        ),
        PendingActionKind,
        (),
    ),
    (
        # ``origin_kind`` picks the dedup key and scopes the runtime guard's
        # whole read model. A misspelling here does not raise; it mints a
        # duplicate card, or hides a request from the plane waiting on it.
        "approval origin kind",
        (
            "origin_kind",
            "_RUNTIME_ORIGIN_KIND",
            "HitlRequest.origin_kind",
            "req.origin_kind",
            "origin.kind",
        ),
        ApprovalOriginKind,
        (),
    ),
)


def _python_sources() -> list[Path]:
    files: list[Path] = []
    for root in SCANNED_ROOTS:
        for path in (REPO_ROOT / root).rglob("*.py"):
            rel = path.relative_to(REPO_ROOT).as_posix()
            if any(part in rel for part in EXEMPT_PATHS):
                continue
            files.append(path)
    assert files, "the scan found no sources — the roots moved"
    return files


def _comparison_pattern(receivers: tuple[str, ...], values: list[str]) -> re.Pattern[str]:
    """``<receiver> ==/!=/in/.in_( "<vocabulary word>"`` and assignments.

    Both directions matter. A comparison against a literal is a branch that
    a typo silently makes unreachable; an assignment of a literal is a state
    written without going through the vocabulary at all.
    """
    receiver_alt = "|".join(re.escape(r) for r in receivers)
    value_alt = "|".join(re.escape(v) for v in values)
    # ``(?<![\w.])`` keeps a short receiver from matching the tail of a longer
    # one: without it, ``w.status`` matches inside ``row.status`` and the scan
    # reports every unrelated table that happens to have a status column.
    return re.compile(
        rf'(?<![\w.])(?:{receiver_alt})\s*(?:==|!=|=|\bin\b|\bnot in\b|\.in_\()\s*'
        rf'[\(\[\{{]?\s*"(?:{value_alt})"'
    )


def _hits(pattern: re.Pattern[str], only: tuple[str, ...] = ()) -> list[str]:
    if only:
        paths = [REPO_ROOT / rel for rel in only]
        for path in paths:
            assert path.exists(), f"{path} moved — update the vocabulary's file list"
    else:
        paths = _python_sources()
    found: list[str] = []
    for path in paths:
        rel = path.relative_to(REPO_ROOT).as_posix()
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if pattern.search(line):
                found.append(f"{rel}:{lineno}: {stripped}")
    return found


@pytest.mark.parametrize(
    ("label", "receivers", "enum_cls", "only"),
    VOCABULARIES,
    ids=[v[0].replace(" ", "-") for v in VOCABULARIES],
)
def test_vocabulary_has_no_literal_call_sites(label, receivers, enum_cls, only) -> None:
    hits = _hits(_comparison_pattern(receivers, enum_cls.values()), only)
    assert not hits, (
        f"{label} is spelled as a string literal at {len(hits)} site(s). "
        f"Use {enum_cls.__name__} members (comparisons) or .value "
        f"(assignments) instead:\n" + "\n".join(hits)
    )


def test_task_logs_are_written_with_the_log_type_enum() -> None:
    """``add_task_log(db, id, "step_failed", ...)`` — the log type is the
    third positional argument, so a literal there is invisible to a scan for
    ``log_type =``. The frontend keys its icons off these values; a type it
    has never heard of renders as a blank row.
    """
    offenders: list[str] = []
    for path in _python_sources():
        rel = path.relative_to(REPO_ROOT).as_posix()
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError:  # pragma: no cover - syntax is checked elsewhere
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
            if name != "add_task_log" or len(node.args) < 3:
                continue
            arg = node.args[2]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                offenders.append(f"{rel}:{arg.lineno}: add_task_log(..., {arg.value!r}, ...)")
    assert not offenders, (
        "task logs written with a literal type; pass a TaskLogType member:\n"
        + "\n".join(offenders)
    )


def test_supervisor_verdict_log_type_is_the_enum_value() -> None:
    """The writer and the reader of the verdict log share one spelling."""
    from packages.core.constants.supervisor import SUPERVISOR_VERDICT_LOG_TYPE

    assert SUPERVISOR_VERDICT_LOG_TYPE == TaskLogType.AI_SUPERVISOR_VERDICT.value


def test_plan_terminal_log_types_exist_for_every_terminal_plan_status() -> None:
    """``plan_terminal_log_type`` replaced an f-string that could build a
    type nothing knew about. Every terminal plan status must map to a real
    log type, or the call raises in production instead of at import.
    """
    from packages.core.constants.task import plan_terminal_log_type

    for status in (
        ExecutionPlanStatus.COMPLETED,
        ExecutionPlanStatus.FAILED,
        ExecutionPlanStatus.CANCELLED,
        ExecutionPlanStatus.REPLANNED,
    ):
        assert plan_terminal_log_type(status.value) in TaskLogType.values()


def test_task_status_enum_matches_the_status_catalog() -> None:
    """The enum and the label/colour catalog the UI reads describe the same
    set of states — neither may grow a state the other has never heard of.
    """
    from packages.core.constants.task import TASK_STATUSES

    assert set(TaskStatus.values()) == set(TASK_STATUSES.keys())


def test_open_and_terminal_step_statuses_partition_the_vocabulary() -> None:
    """Every step status is either open or terminal, with no overlap.

    This is not tidiness. Readers branch ``if terminal: … elif open: …``, so
    a status in neither set falls through both — which is exactly what
    happened to ``paused``: the executor's terminal check listed it, the
    enum's first draft did not, and a paused step would have been read as a
    finished plan.
    """
    from packages.core.constants.execution import (
        STEP_OPEN_STATUSES,
        STEP_TERMINAL_STATUSES,
    )

    assert STEP_OPEN_STATUSES | STEP_TERMINAL_STATUSES == set(ExecutionStepStatus)
    assert not (STEP_OPEN_STATUSES & STEP_TERMINAL_STATUSES)


def test_step_status_enum_covers_what_the_timeline_offers_to_retry() -> None:
    """The execution timeline offers a retry per step status. A status it
    knows and the backend doesn't is a button wired to nothing.
    """
    source = (
        REPO_ROOT / "apps/web/src/components/task/TaskExecutionTimeline.tsx"
    ).read_text()
    block = re.search(
        r"const RETRYABLE_STEP_STATUSES = new Set\(\[(.*?)\]\);", source, re.S
    )
    assert block, "RETRYABLE_STEP_STATUSES moved — update this check, don't delete it"
    claimed = set(re.findall(r'"([a-z_]+)"', block.group(1)))
    unknown = sorted(claimed - set(ExecutionStepStatus.values()))
    assert not unknown, f"timeline offers retry for unknown step statuses: {unknown}"


def test_worker_reported_step_status_aliases_resolve_to_members() -> None:
    """Remote workers report a finished step in their own words.

    Before this, each reader carried the variants inline (``in {"done",
    "complete"}``), so a reader written without that folklore treated a
    finished step as still running. The aliases are now one table.
    """
    from packages.core.constants.execution import coerce_step_status

    assert coerce_step_status("complete") is ExecutionStepStatus.DONE
    assert coerce_step_status("completed") is ExecutionStepStatus.DONE
    assert coerce_step_status("succeeded") is ExecutionStepStatus.DONE
    assert coerce_step_status("canceled") is ExecutionStepStatus.CANCELLED
    assert coerce_step_status("CANCELLED") is ExecutionStepStatus.CANCELLED
    assert coerce_step_status("error") is ExecutionStepStatus.FAILED
    for member in ExecutionStepStatus:
        assert coerce_step_status(member.value) is member


def test_unrecognised_step_status_is_not_silently_a_failure() -> None:
    """``None`` means "no information", which is not the same as FAILED.

    Mapping an unknown word to FAILED would fail real steps over a
    spelling — the exact shape of the bug this sweep exists to remove.
    """
    from packages.core.constants.execution import coerce_step_status

    for junk in ("", None, "  ", "finished-ish", "kinda_done"):
        assert coerce_step_status(junk) is None


def test_provider_media_status_words_map_to_one_answer() -> None:
    """The five poll loops disagreed about what a provider had said.

    Seedance and the generic video poller accepted only {"succeeded",
    "success", "completed"}; the OpenRouter poller also accepted "complete"
    and "done"; the failure sets split over "failure" and the one-L
    "canceled". A provider answering "done" to the Seedance loop was
    therefore neither done nor failed — it polled until timeout and
    reported a finished video as still pending.
    """
    from packages.core.constants.media import coerce_provider_media_status

    for word in ("completed", "complete", "succeeded", "success", "done", "DONE"):
        assert coerce_provider_media_status(word) is MediaJobStatus.COMPLETED, word
    for word in ("failed", "failure", "error", "cancelled", "canceled", "expired"):
        assert coerce_provider_media_status(word) is MediaJobStatus.FAILED, word
    # Still-running and unknown words both mean "keep polling" — never an
    # invented failure.
    for word in ("", None, "processing", "queued", "in_progress", "weird"):
        assert coerce_provider_media_status(word) is None, word


def test_every_task_status_lands_in_a_kanban_column() -> None:
    """A status no column claims is a task nobody can see.

    Tasks.tsx groups the board by ``COLUMN_META[col].statuses.includes(
    task.status)``. That is a whitelist: a task whose status appears in none
    of the columns is silently absent from the Kanban — not misplaced, not
    greyed out, gone. So the columns must cover the whole enum, and adding a
    backend status means placing it in a column.
    """
    source = (REPO_ROOT / "apps/web/src/pages/Tasks.tsx").read_text()
    block = re.search(
        r"const COLUMN_META: Record<string, ColumnMeta> = \{(.*?)\n\};",
        source,
        re.S,
    )
    assert block, "COLUMN_META moved — this check needs updating, not deleting"
    covered = set(re.findall(r'statuses: \[([^\]]*)\]', block.group(1)))
    claimed = {
        value.strip().strip('"')
        for group in covered
        for value in group.split(",")
        if value.strip()
    }
    missing = sorted(set(TaskStatus.values()) - claimed)
    assert not missing, (
        "these task statuses appear in no Kanban column, so tasks holding "
        f"them are invisible on the board: {missing}"
    )
    unknown = sorted(claimed - set(TaskStatus.values()))
    assert not unknown, f"Kanban columns claim statuses the backend never writes: {unknown}"


def test_frontend_log_icons_are_real_log_types() -> None:
    """The activity feed keys its icons off the log type. A key that is not
    a real type is an icon that never renders — the log falls through to the
    generic comment icon, and nobody notices.
    """
    source = (REPO_ROOT / "apps/web/src/components/task/TaskLogItem.tsx").read_text()
    block = re.search(
        r"export const LOG_ICONS: Record<string, \{[^}]*\}> = \{(.*?)\n\};",
        source,
        re.S,
    )
    assert block, "LOG_ICONS moved — this check needs updating, not deleting"
    keys = set(re.findall(r"^\s{2}([a-z_]+):\s*\{", block.group(1), re.M))
    assert keys, "no icon keys parsed — the map's shape changed"
    unknown = sorted(keys - set(TaskLogType.values()))
    assert not unknown, f"icons keyed on log types the backend never writes: {unknown}"


def test_frontend_pending_action_kinds_are_real_kinds() -> None:
    """The chat surface picks the card component off ``pending_action.kind``.

    A kind the frontend knows and the backend never writes is a component that
    never mounts; a kind the backend writes and the frontend has never heard of
    is the generic fallback card — which is how a typed error card became an
    approval prompt in the first place. The mirror file must therefore hold a
    subset of the enum, spelled identically.
    """
    source = (REPO_ROOT / "apps/web/src/lib/pendingActionKinds.ts").read_text()
    block = re.search(r"export const PendingActionKind = \{(.*?)\n\} as const;", source, re.S)
    assert block, "PendingActionKind moved — this check needs updating, not deleting"
    claimed = set(re.findall(r':\s*"([a-z_]+)"', block.group(1)))
    assert claimed, "no kinds parsed — the mirror's shape changed"
    unknown = sorted(claimed - set(PendingActionKind.values()))
    assert not unknown, f"frontend claims card kinds the backend never writes: {unknown}"
    # The other direction, which nothing used to catch. A kind the backend
    # WRITES and the mirror has never heard of is the case that actually hurt:
    # the render switch falls through to the generic approval card, which is
    # how a typed error card became "this step needs your approval" with an
    # Approve button under it. Equality, not containment.
    missing = sorted(set(PendingActionKind.values()) - claimed)
    assert not missing, (
        "the backend writes these card kinds and the frontend mirror does not "
        f"list them, so they render as the generic approval card: {missing}"
    )


def test_lease_closeable_kinds_are_members_of_the_kind_enum() -> None:
    """The mint side and the close side share one frozenset of plain strings.

    Plain ``str`` and not enum members on purpose: what is tested against this
    set is a raw word read back out of a JSONB blob. Both ends must still be
    spelling kinds that exist.
    """
    from packages.core.ai.pending_action import LEASE_HITL_CLOSEABLE_KINDS

    assert LEASE_HITL_CLOSEABLE_KINDS <= set(PendingActionKind.values())
    assert all(type(kind) is str for kind in LEASE_HITL_CLOSEABLE_KINDS)


def test_pending_action_kind_is_str_based_so_stored_cards_still_resolve() -> None:
    """Rows written before this refactor hold bare strings in JSONB.

    The resolve endpoint now compares them against enum members. That only
    keeps working because the enum mixes in ``str``: equality, set membership
    and ``json.dumps`` all have to behave exactly as the literal did, or every
    card in flight at deploy time stops routing.
    """
    import json

    stored = {"kind": "governance_approval", "step_id": "01STEP"}
    assert stored["kind"] == PendingActionKind.GOVERNANCE_APPROVAL
    assert stored["kind"] in {PendingActionKind.GOVERNANCE_APPROVAL.value}
    assert PendingActionKind.GOVERNANCE_APPROVAL.value in {stored["kind"]}
    assert json.loads(json.dumps({"kind": PendingActionKind.GOVERNANCE_APPROVAL})) == {
        "kind": "governance_approval"
    }


def test_hitl_vocabularies_stringify_to_their_wire_value() -> None:
    """``str(member)`` must be the word, not ``"PendingActionKind.HUMAN_INPUT"``.

    A plain ``str, Enum`` mixin stringifies to its qualified name. The
    dispatcher reads the pause kind as
    ``str(pending_action.get("kind") or KIND_HUMAN_INPUT)`` and then tests it
    for membership in ``LEASE_HITL_CLOSEABLE_KINDS`` — so the moment the
    fallback branch produced a qualified name, no request was minted for any
    card posted without a structured payload, and the step waited on a request
    that did not exist. It is the same failure for every f-string that builds a
    ``matched_rule`` or a log line out of one of these members.

    Covers EVERY enum in this family, not just the two that were introduced
    when the bug was found: the trap is the mixin, so leaving one uncovered
    just moves the landmine. ``matched_rule`` in particular is assembled by
    f-string from a pause kind, and a qualified name written into that column
    is invisible until something tries to match on it.
    """
    for member in (*PendingActionKind, *ApprovalOriginKind, *HitlType, *ApprovalStatus):
        assert str(member) == member.value, member
        assert f"{member}" == member.value, member
        assert "%s" % member == member.value, member


def test_approval_status_groupings_are_derived_from_the_enum() -> None:
    from packages.core.constants.approvals import (
        APPROVAL_LIVE_STATUSES,
        APPROVAL_OPEN_STATUSES,
        APPROVAL_TERMINAL_STATUSES,
    )

    assert set(APPROVAL_OPEN_STATUSES) | set(APPROVAL_TERMINAL_STATUSES) == set(
        ApprovalStatus.values()
    )
    assert ApprovalStatus.PENDING in APPROVAL_LIVE_STATUSES
    assert ApprovalStatus.CONSUMED not in APPROVAL_LIVE_STATUSES
