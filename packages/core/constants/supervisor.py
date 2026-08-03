"""What the plan supervisor may decide, and what a decision must carry.

The supervisor reviews a finished plan and decides the parent task's fate.
Its verdict used to be a bare string matched with ``verdict in (...)`` — and
a bare word is also all it was: production task 01KWRR5VGHYHQD3A116TZ8ET0W
ended "failed" over seven all-successful logs, and the one word the model
returned was the entire record of why.

Two rules, both closed here:

* the verdict set is an enum. Code compares members, never spells strings,
  and a new verdict is a new member — not a new literal scattered through
  the executor.
* a verdict travels as a ``SupervisorDecision``: the verdict, the evidence
  for it, and which mechanism produced it. Evidence is required of the
  model and stated by the deterministic gates; "failed, no reason" is not a
  decision this type can express.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum

from packages.core.constants.task import TaskLogType


class SupervisorVerdict(str, Enum):
    """Every fate a supervised plan can assign its task."""

    #: The task's deliverable was produced and delivered.
    COMPLETED = "completed"

    #: One specific step's output is missing/empty/transiently broken, and
    #: re-running just that step could produce it. Carries the step key.
    RETRY_STEP = "retry_step"

    #: The deliverable was not produced; a different plan could produce it.
    #: At finalize time the replan budget is already spent, so this lands as
    #: ``failed`` — with that fact recorded as the note.
    NEEDS_REPLAN = "needs_replan"

    #: The task cannot finish without user input, access, or approval.
    NEEDS_HUMAN = "needs_human"

    #: The task cannot be completed or is permanently invalid.
    FAILED = "failed"

    #: Pass-throughs of a cancelled/blocked plan. Deterministic only — the
    #: model is never offered these.
    CANCELLED = "cancelled"
    BLOCKED = "blocked"

    @classmethod
    def values(cls) -> list[str]:
        return [member.value for member in cls]


#: The verdicts the model may choose. CANCELLED/BLOCKED are code
#: pass-throughs of the plan's own state, not judgements.
MODEL_CHOOSABLE_VERDICTS = frozenset(
    {
        SupervisorVerdict.COMPLETED,
        SupervisorVerdict.RETRY_STEP,
        SupervisorVerdict.NEEDS_REPLAN,
        SupervisorVerdict.NEEDS_HUMAN,
        SupervisorVerdict.FAILED,
    }
)


class SupervisorDecisionSource(str, Enum):
    """Which mechanism produced a decision — an auditable fact, since the
    three behave differently and only one of them is a model."""

    #: A deterministic code check (artifact evidence, structured blocker,
    #: total failure, cancelled/blocked pass-through).
    GATE = "gate"

    #: The supervisor model's judgement.
    MODEL = "model"

    #: The supervisor was unavailable or unparseable; the plan's own status
    #: was used.
    FALLBACK = "fallback"


#: The task-log type every supervisor decision is written under — the one
#: the frontend has always had an icon for. Write sites and query sites both
#: use this name, so the reader can never drift from the writer. The value
#: comes from the task-log enum, so there is one spelling in the codebase.
SUPERVISOR_VERDICT_LOG_TYPE = TaskLogType.AI_SUPERVISOR_VERDICT.value

#: Step-params flag marking that the supervisor already spent its one
#: re-run on this step. One per step per plan: a supervisor that keeps
#: retrying the same step against the same result is a loop, not a review.
SUPERVISOR_STEP_RETRY_FLAG = "_supervisor_retry_used"

#: A pathological-input guard, not a working limit — the supervisor's
#: explanation is the record a person (and its own next review) reads
#: later, so it is never asked to be brief.
MAX_EVIDENCE_CHARS = 2000


@dataclass(frozen=True)
class SupervisorDecision:
    """A verdict that can say why it was reached.

    ``evidence`` cites what actually happened — a step result, a gate's
    finding, or (for FALLBACK) the fact that no review took place.
    ``step_key`` is set only for RETRY_STEP, and only after validation
    against the plan's real steps; the model's word alone never names a
    step (see the StepResult-envelope rule: never require IDs from the
    model — offer, then validate).
    """

    verdict: SupervisorVerdict
    evidence: str
    source: SupervisorDecisionSource
    step_key: str | None = None

    def downgraded(self, verdict: SupervisorVerdict, extra_evidence: str) -> "SupervisorDecision":
        """The same decision, demoted with the reason appended."""
        evidence = f"{self.evidence} — {extra_evidence}" if self.evidence else extra_evidence
        return replace(self, verdict=verdict, evidence=evidence[:MAX_EVIDENCE_CHARS], step_key=None)
