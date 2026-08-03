"""Shared vocabularies for the proposal-item governance layer (M7/M8).

``REASON_CODES`` is the human-decision rejection vocabulary (M9.3): the
chat card / API require one of these when an item is rejected, so the
learning loop gets a machine-readable signal instead of free prose.

``ACTION_KEY_BY_KIND`` is the M8 approval-catalog mapping: proposal
kind (+ operation for change kinds) → the governance ``action_key`` the
HitlRequest is minted against. ``None`` means the kind never mints
a HitlRequest (human_request items are created automatically; the
addressee can decline instead).

The change-kind vocabularies at the bottom (target kinds, operations,
patch whitelists, risk mapping) are the single source of truth shared by
the Strategist output schema, the M7 validator and the M10 executor.
"""
from __future__ import annotations

REASON_CODES: frozenset[str] = frozenset({
    "WRONG_DIRECTION",
    "DUPLICATE",
    "TOO_EXPENSIVE",
    "BAD_TIMING",
    "NEEDS_CHANGES",
    "POLICY_BLOCKED",
    "STALE_REVISION",
    "INSUFFICIENT_DATA",
    "SUPERSEDED",
    "OTHER",
})

# The subset a human may pick in the UI / API. POLICY_BLOCKED,
# STALE_REVISION, INSUFFICIENT_DATA and SUPERSEDED are system-only
# outcomes and are never offered on the reject card; an unknown
# user-supplied code falls back to OTHER instead of erroring.
USER_REASON_CODES: tuple[str, ...] = (
    "WRONG_DIRECTION",
    "DUPLICATE",
    "TOO_EXPENSIVE",
    "BAD_TIMING",
    "NEEDS_CHANGES",
    "OTHER",
)

# Rejection codes that must NOT feed the learning loop's
# rejection-reason distribution. SUPERSEDED means "a fresher review
# replaced this cohort" — it says nothing about whether the proposal was
# any good, so counting it as negative feedback would teach the
# Strategist to avoid perfectly sound proposals.
LEARNING_EXCLUDED_REASON_CODES: frozenset[str] = frozenset({"SUPERSEDED"})

ITEM_KINDS: tuple[str, ...] = (
    "task",
    "human_request",
    "automation_change",
    "workflow_change",
    "goal_change",
    "experiment",
)

# proposal item statuses (lifecycle vocabulary; v1 uses the first three)
ITEM_STATUSES: tuple[str, ...] = (
    "proposed", "approved", "rejected", "expired",
    "executing", "succeeded", "failed", "cancelled",
)

# kind (or "kind.operation" for change kinds) → governance action_key.
# Per the M8 catalog table. Note: pause AND resume share the
# `...automation_change.pause` key (one standing grant covers both
# directions of the same low-stakes toggle, per the design doc).
ACTION_KEY_BY_KIND: dict[str, str | None] = {
    "task": "workspace.proposal.task",
    # task whose deliverables include external publish (v1: not computed)
    "task.external": "workspace.proposal.task.external",
    "human_request": None,
    "automation_change.create": "workspace.proposal.automation_change.create",
    "automation_change.update": "workspace.proposal.automation_change.update",
    "automation_change.pause": "workspace.proposal.automation_change.pause",
    "automation_change.resume": "workspace.proposal.automation_change.pause",
    "automation_change.delete": "workspace.proposal.automation_change.delete",
    "workflow_change.create": "workspace.proposal.workflow_change.create",
    "workflow_change.update": "workspace.proposal.workflow_change.update",
    "workflow_change.pause": "workspace.proposal.workflow_change.pause",
    "workflow_change.resume": "workspace.proposal.workflow_change.pause",
    "workflow_change.delete": "workspace.proposal.workflow_change.delete",
    "goal_change.create": "workspace.proposal.goal_change.create",
    "goal_change.update_target": "workspace.proposal.goal_change.update_target",
    "goal_change.update_deadline": "workspace.proposal.goal_change.update_deadline",
    "goal_change.pause": "workspace.proposal.goal_change.pause",
    "goal_change.archive": "workspace.proposal.goal_change.archive",
    "experiment": "workspace.proposal.experiment",
}

TASK_ACTION_KEY = ACTION_KEY_BY_KIND["task"]

# Every distinct governance action_key a Strategist proposal can mint, in
# catalog order (tasks → automation → workflow → goal → experiment). The
# "always approve" blanket grant writes all of these; Settings → Approval
# automation renders one matrix row per entry.
STRATEGIST_ACTION_KEYS: tuple[str, ...] = tuple(
    dict.fromkeys(key for key in ACTION_KEY_BY_KIND.values() if key)
)

# action_key → sentence-case label shown in the settings matrix and in the
# "auto-approved by your standing approval for X" chat line. Keyed by the
# action_key (not the kind spec) so pause/resume share one label.
STRATEGIST_ACTION_LABELS: dict[str, str] = {
    "workspace.proposal.task": "Start proposed tasks",
    "workspace.proposal.task.external": "Tasks that publish externally",
    "workspace.proposal.automation_change.create": "Create automations",
    "workspace.proposal.automation_change.update": "Update automations",
    "workspace.proposal.automation_change.pause": "Pause or resume automations",
    "workspace.proposal.automation_change.delete": "Delete automations",
    "workspace.proposal.workflow_change.create": "Create workflows",
    "workspace.proposal.workflow_change.update": "Update workflows",
    "workspace.proposal.workflow_change.pause": "Pause or resume workflows",
    "workspace.proposal.workflow_change.delete": "Delete workflows",
    "workspace.proposal.goal_change.create": "Create goals",
    "workspace.proposal.goal_change.update_target": "Change goal targets",
    "workspace.proposal.goal_change.update_deadline": "Change goal deadlines",
    "workspace.proposal.goal_change.pause": "Pause goals",
    "workspace.proposal.goal_change.archive": "Archive goals",
    "workspace.proposal.experiment": "Start experiments",
}


def strategist_action_label(action_key: str) -> str:
    """Human label for one Strategist action_key (falls back to the key)."""
    return STRATEGIST_ACTION_LABELS.get(action_key, action_key)


def strategist_approval_catalog() -> list[dict[str, str]]:
    """One row per distinct Strategist ``workspace.proposal.*`` action_key.

    Derived from ``ACTION_KEY_BY_KIND`` so the settings matrix can never
    drift from the approval catalog. Shape:
    ``{action_key, kind, operation, label, risk_level}`` — ``operation`` is
    "" for the kinds that have none (task / experiment).
    """
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    for spec, action_key in ACTION_KEY_BY_KIND.items():
        if not action_key or action_key in seen:
            continue
        seen.add(action_key)
        kind, _, operation = spec.partition(".")
        if kind in CHANGE_KINDS:
            risk = change_risk_level(operation)
        elif spec == "task":
            risk = "low"
        elif spec == "task.external":
            risk = "high"
        else:
            risk = "medium"
        rows.append({
            "action_key": action_key,
            "kind": kind,
            "operation": operation,
            "label": strategist_action_label(action_key),
            "risk_level": risk,
        })
    return rows

# ── change kinds (M7 payloads / M10 execution router) ─────────────────

CHANGE_KINDS: tuple[str, ...] = (
    "automation_change",
    "workflow_change",
    "goal_change",
)

# kind → the target_kind vocabulary it may address.
TARGET_KINDS_BY_CHANGE_KIND: dict[str, tuple[str, ...]] = {
    "automation_change": ("scheduled_job", "workflow_binding"),
    "workflow_change": ("workflow_definition", "workflow_binding"),
    "goal_change": ("goal",),
}

# kind → the operation vocabulary it may request.
OPERATIONS_BY_CHANGE_KIND: dict[str, tuple[str, ...]] = {
    "automation_change": ("create", "update", "pause", "resume", "delete"),
    "workflow_change": ("create", "update", "pause", "resume", "delete"),
    "goal_change": (
        "create", "update_target", "update_deadline", "pause", "archive",
    ),
}

# Every non-create operation mutates an existing row, so it MUST carry the
# revision it was decided against (M11 CAS). ``create`` must NOT carry one.
REVISION_REQUIRED_OPERATIONS: frozenset[str] = frozenset({
    "update", "pause", "resume", "delete", "archive",
    "update_target", "update_deadline",
})

# Operations whose patch must be non-empty (a "change nothing" update is a
# bug, not a proposal). pause/resume/delete/archive are self-describing.
PATCH_REQUIRED_OPERATIONS: frozenset[str] = frozenset({
    "create", "update", "update_target", "update_deadline",
})

# target_kind → field-level patch whitelist (M7: "patch 为字段级…白名单校验").
# Anything outside these keys is rejected at schema-validation time: the
# Strategist may retune an automation, never rewrite arbitrary columns.
CHANGE_PATCH_WHITELIST: dict[str, frozenset[str]] = {
    "scheduled_job": frozenset({
        "enabled", "cron_expr", "every_seconds", "schedule_kind", "timezone",
        "name", "execution_target", "payload_message",
    }),
    "workflow_binding": frozenset({
        "enabled", "status", "trigger_type", "trigger_ref", "variables", "name",
    }),
    "workflow_definition": frozenset({
        "name", "steps", "variables", "description",
    }),
    "goal": frozenset({
        "target_value", "deadline", "baseline_value", "priority",
        "measurement_cadence", "status",
    }),
}

# Extra keys accepted ONLY on ``create``: the identity fields a brand-new row
# cannot exist without. They are deliberately outside the update whitelist —
# re-pointing a live binding at another workflow, or renaming a goal's metric
# mid-flight, would silently invalidate every measurement/run before it.
CHANGE_CREATE_EXTRA_FIELDS: dict[str, frozenset[str]] = {
    "scheduled_job": frozenset({"execution_type", "agent_id", "job_id"}),
    "workflow_binding": frozenset({"workflow_id", "trigger_config"}),
    "workflow_definition": frozenset({"trigger_type", "steps"}),
    "goal": frozenset({"title", "metric_key", "description", "measurement_source"}),
}


def change_patch_whitelist(target_kind: str, operation: str) -> frozenset[str]:
    """Allowed patch keys for one (target_kind, operation) pair."""
    allowed = CHANGE_PATCH_WHITELIST.get(target_kind, frozenset())
    if operation == "create":
        allowed = allowed | CHANGE_CREATE_EXTRA_FIELDS.get(target_kind, frozenset())
    return allowed


def change_risk_level(operation: str) -> str:
    """M8 catalog risk by operation: create/delete/archive are high (they
    add or remove an execution surface), the reversible tweaks are medium."""
    return "high" if operation in ("create", "delete", "archive") else "medium"


def change_action_key(kind: str, operation: str) -> str:
    action_key = ACTION_KEY_BY_KIND.get(f"{kind}.{operation}")
    if not action_key:
        raise ValueError(f"no approval action_key for {kind}.{operation}")
    return action_key

EXPERIMENT_ACTION_KEY = ACTION_KEY_BY_KIND["experiment"]

# M8 catalog: guardrails.max_cost at or below this keeps an experiment item
# at medium risk; anything above is high risk (standing grants for high-risk
# keys carry the extra-confirmation constraints).
EXPERIMENT_MEDIUM_RISK_MAX_COST = 20.0

HUMAN_REQUEST_ACTION_KEY = "workspace.proposal.human_request"
"""Label stored on kind="human_request" items (the action_key column is
NOT NULL). Purely descriptive: per the M8 catalog these items never mint
a HitlRequest — ``ACTION_KEY_BY_KIND["human_request"]`` stays None
and governs the (non-)minting decision."""
