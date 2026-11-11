"""Re-run a Workflow step that failed only because the platform briefly could not serve it.

The Task agent loop survives a brief platform fault: an exception re-wakes it
from its durable checkpoint, a provider outage re-stages it with a delay, and
a lost worker is redelivered. A Workflow run used to fail at the first such
fault instead. This module gives every Workflow step the same bounded
resilience, through contracts the runner already owns:

* A step whose failure is *transient* (the credit/budget check or the
  database was unavailable, the model provider stayed down through
  llm_client's own retries) parks on the durable timed continuation timer
  waits use and is re-executed after a backoff, a bounded number of times
  within a bounded window. Past the budget the step fails with the error of
  its last attempt.
* Re-execution is automatic only when it cannot repeat an external effect:
  the node type has none, or the host receipt ledger proves the failed
  attempt had none. Anything else keeps the ordinary "check results, then
  retry from this step" failure.
* A step interrupted by a lost worker is re-run by claim-loss recovery when
  its effect-intent journal proves it never started an effectful tool call.
  The journal is written before a tool can cross effect I/O, so a process
  that dies mid-call always leaves the intent behind.

Policy is Workflow configuration (``config.transient_retry`` on a node), with
platform defaults from the environment; no scenario owns a code path here.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, Callable

logger = logging.getLogger(__name__)

TRANSIENT_RETRY_CONFIG_KEY = "transient_retry"
TRANSIENT_RETRY_RESULT_KEY = "transient_retry"
TRANSIENT_RETRY_WAIT_TYPE = "transient_retry"
TRANSIENT_RETRY_CODE = "workflow_step_transient_retry"
# Internal ``trigger_data`` key, underscore-prefixed like the runner's others.
STEP_EFFECT_JOURNAL_TRIGGER_FIELD = "_step_effect_journal"
# Nodes that run a Runtime agent loop: their tools report effect intents.
EFFECT_JOURNALED_STEP_TYPES = frozenset({"agent", "llm", "classifier"})

DEFAULT_MAX_RETRIES = 3
DEFAULT_WINDOW_SECONDS = 15 * 60
DEFAULT_BASE_DELAY_SECONDS = 60
DEFAULT_MAX_DELAY_SECONDS = 300


class TransientStepFailure(StrEnum):
    CREDIT_CHECK_UNAVAILABLE = "credit_check_unavailable"
    DATABASE_UNAVAILABLE = "database_unavailable"
    CONNECTION_UNAVAILABLE = "connection_unavailable"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    # An Agent step's tool hit a platform fault it never got past, so the
    # step's declared output was not produced.
    TOOL_UNAVAILABLE = "tool_unavailable"


_REASON_MESSAGES = {
    TransientStepFailure.CREDIT_CHECK_UNAVAILABLE: "the credit and budget check was temporarily unavailable",
    TransientStepFailure.DATABASE_UNAVAILABLE: "the database was temporarily unavailable",
    TransientStepFailure.CONNECTION_UNAVAILABLE: "a platform service refused the connection",
    TransientStepFailure.PROVIDER_UNAVAILABLE: "the model provider stopped responding",
    TransientStepFailure.TOOL_UNAVAILABLE: "a tool the step needed was temporarily unavailable",
}
# Runtime loop error codes that name an outage, not a request problem.
_PROVIDER_OUTAGE_ERROR_CODES = frozenset({"llm_provider_unavailable"})


@dataclass(frozen=True)
class StepTransientRetryPolicy:
    max_retries: int = DEFAULT_MAX_RETRIES
    window_seconds: int = DEFAULT_WINDOW_SECONDS
    base_delay_seconds: int = DEFAULT_BASE_DELAY_SECONDS
    max_delay_seconds: int = DEFAULT_MAX_DELAY_SECONDS

    @property
    def enabled(self) -> bool:
        return self.max_retries > 0 and self.window_seconds > 0

    def delay_seconds(self, retry: int) -> int:
        return min(self.max_delay_seconds, self.base_delay_seconds * (2 ** max(0, retry - 1)))


def _bounded_int(value: Any, *, default: int, low: int, high: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def _env_default(name: str, default: int, *, low: int, high: int) -> int:
    return _bounded_int(os.environ.get(name), default=default, low=low, high=high)


def resolve_step_transient_retry_policy(step: dict[str, Any]) -> StepTransientRetryPolicy:
    """Platform defaults (environment), overridden by ``config.transient_retry``.

    ``transient_retry: false`` (or ``max_retries: 0``) turns it off for a node.
    """
    defaults = {
        "max_retries": _env_default(
            "WORKFLOW_STEP_TRANSIENT_MAX_RETRIES", DEFAULT_MAX_RETRIES, low=0, high=10,
        ),
        "window_seconds": _env_default(
            "WORKFLOW_STEP_TRANSIENT_WINDOW_SECONDS", DEFAULT_WINDOW_SECONDS, low=0, high=86_400,
        ),
        "base_delay_seconds": _env_default(
            "WORKFLOW_STEP_TRANSIENT_BASE_DELAY_SECONDS", DEFAULT_BASE_DELAY_SECONDS, low=1, high=3_600,
        ),
        "max_delay_seconds": _env_default(
            "WORKFLOW_STEP_TRANSIENT_MAX_DELAY_SECONDS", DEFAULT_MAX_DELAY_SECONDS, low=1, high=3_600,
        ),
    }
    config = step.get("config") if isinstance(step.get("config"), dict) else {}
    raw = config.get(TRANSIENT_RETRY_CONFIG_KEY)
    if raw is False:
        return StepTransientRetryPolicy(max_retries=0)
    overrides = raw if isinstance(raw, dict) else {}
    if overrides.get("enabled") is False:
        return StepTransientRetryPolicy(max_retries=0)
    bounds = {
        "max_retries": (0, 10),
        "window_seconds": (0, 86_400),
        "base_delay_seconds": (1, 3_600),
        "max_delay_seconds": (1, 3_600),
    }
    values = {
        key: _bounded_int(overrides.get(key), default=default, low=bounds[key][0], high=bounds[key][1])
        if key in overrides else default
        for key, default in defaults.items()
    }
    return StepTransientRetryPolicy(**values)


def _exception_chain(exc: BaseException):
    # Explicit causes only: an unrelated error handled on the way does not
    # make this failure transient.
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = getattr(current, "orig", None) or current.__cause__


def transient_failure_from_exception(exc: BaseException) -> TransientStepFailure | None:
    """Classify an exception that escaped a step, or ``None`` when it is not transient."""
    from sqlalchemy.exc import InterfaceError, OperationalError

    from packages.core.ai.llm_client import CreditCheckUnavailableError
    from packages.core.database_runtime import DatabaseFailureKind

    chain = list(_exception_chain(exc))
    if any(isinstance(item, CreditCheckUnavailableError) for item in chain):
        return TransientStepFailure.CREDIT_CHECK_UNAVAILABLE
    if any(DatabaseFailureKind.from_exception(item) is not None for item in chain) or any(
        isinstance(item, (OperationalError, InterfaceError)) for item in chain
    ):
        return TransientStepFailure.DATABASE_UNAVAILABLE
    if any(isinstance(item, (ConnectionRefusedError, ConnectionResetError)) for item in chain):
        return TransientStepFailure.CONNECTION_UNAVAILABLE
    return None


def transient_failure_reason(result: Any) -> TransientStepFailure | None:
    """The transient cause recorded on a failed step result, if any."""
    if not isinstance(result, dict) or result.get("status") != "failed":
        return None
    recorded = result.get("transient_failure")
    if recorded in TransientStepFailure._value2member_map_:
        return TransientStepFailure(recorded)
    if result.get("provider_transient") is True or result.get("runtime_error") in _PROVIDER_OUTAGE_ERROR_CODES:
        return TransientStepFailure.PROVIDER_UNAVAILABLE
    # A Skill path reports the loop's exception as text only.
    if str(result.get("error") or "").startswith("credit_check_unavailable"):
        return TransientStepFailure.CREDIT_CHECK_UNAVAILABLE
    return None


def step_failure_replay_safe(step: dict[str, Any], result: dict[str, Any]) -> bool:
    """Whether re-running this failed attempt cannot repeat an external effect."""
    from packages.core.ai.runtime.outcome import attempt_forbids_replay
    from packages.core.services.workflow_claim_recovery import workflow_step_is_replay_safe

    if attempt_forbids_replay(result) or result.get("retry_blocked") is True:
        return False
    if workflow_step_is_replay_safe(step):
        return True
    # Only the host ledger (or the pre-I/O intent watcher) proves "none".
    return result.get("effect_state") == "none"


def is_transient_retry_record(result: Any) -> bool:
    return (
        isinstance(result, dict)
        and result.get("status") == "paused"
        and result.get("wait_type") == TRANSIENT_RETRY_WAIT_TYPE
        and isinstance(result.get(TRANSIENT_RETRY_RESULT_KEY), dict)
    )


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _error_summary(result: dict[str, Any]) -> str:
    error = result.get("error")
    text = error if isinstance(error, str) else str(error or "")
    return text.strip()[:500]


def transient_retry_status_message(state: dict[str, Any]) -> str:
    """``Retrying (n/N): reason`` for run status, step results and Chat."""
    reason = _REASON_MESSAGES.get(str(state.get("reason") or ""), "a temporary platform problem")
    return f"Retrying ({state.get('retry')}/{state.get('max_retries')}): {reason}."


def plan_step_transient_retry(
    step: dict[str, Any],
    result: dict[str, Any],
    *,
    previous_result: Any,
    now: datetime,
) -> dict[str, Any] | None:
    """The result to record instead of a transient failure, or ``None`` to keep it.

    Returns a paused ``transient_retry`` record while budget remains, or the
    original failure annotated with its exhausted retry history.
    """
    reason = transient_failure_reason(result)
    if reason is None:
        return None
    policy = resolve_step_transient_retry_policy(step)
    if not policy.enabled or not step_failure_replay_safe(step, result):
        return None
    prior = previous_result.get(TRANSIENT_RETRY_RESULT_KEY) if is_transient_retry_record(previous_result) else None
    prior = prior if isinstance(prior, dict) else {}
    retry = _bounded_int(prior.get("retry"), default=0, low=0, high=1_000) + 1
    first_failed_at = _parse_time(prior.get("first_failed_at")) or now
    delay = policy.delay_seconds(retry)
    resume_at = now + timedelta(seconds=delay)
    history = [*(prior.get("history") or [])][-(policy.max_retries + 1):]
    history.append({
        "reason": reason.value,
        "error": _error_summary(result),
        "code": result.get("code") or result.get("runtime_error"),
        "failed_at": now.isoformat(),
    })
    state = {
        "retry": retry,
        "max_retries": policy.max_retries,
        "reason": reason.value,
        "first_failed_at": first_failed_at.isoformat(),
        "window_seconds": policy.window_seconds,
        "history": history,
    }
    if retry > policy.max_retries or (resume_at - first_failed_at).total_seconds() > policy.window_seconds:
        # Fail with the attempt's own structured error, plus what was tried.
        return {
            **result,
            TRANSIENT_RETRY_RESULT_KEY: {**state, "retry": retry - 1, "exhausted": True},
        }
    state["next_retry_at"] = resume_at.isoformat()
    record = {
        "status": "paused",
        "wait_type": TRANSIENT_RETRY_WAIT_TYPE,
        "code": TRANSIENT_RETRY_CODE,
        "resume_strategy": "reexecute",
        "resume_at": resume_at.isoformat(),
        "auto_resume_scheduled": True,
        "output": transient_retry_status_message(state),
        TRANSIENT_RETRY_RESULT_KEY: state,
    }
    for key in ("effect_state", "step_id", "inputs", "duration_ms", "completed_at", "usage"):
        if key in result:
            record[key] = result[key]
    return record


# ── Effect-intent journal (worker loss) ─────────────────────────────────


def _latest_trace_entry(run: Any, step_id: str) -> dict[str, Any] | None:
    trace = run.execution_trace if isinstance(getattr(run, "execution_trace", None), list) else []
    return next(
        (entry for entry in reversed(trace) if isinstance(entry, dict) and str(entry.get("node_id")) == step_id),
        None,
    )


def arm_step_effect_journal(run: Any, step: dict[str, Any]) -> int | None:
    """Open the journal of the attempt whose ``running`` trace entry was just appended.

    Returns the trace sequence the journal is bound to, or ``None`` when the
    step keeps no journal (its interruption then stays unknown).
    """
    step_id = str(step.get("id") or "")
    config = step.get("config") if isinstance(step.get("config"), dict) else {}
    if step.get("type") not in EFFECT_JOURNALED_STEP_TYPES or config.get("batch") or not step_id:
        return None
    entry = _latest_trace_entry(run, step_id)
    if not entry or entry.get("status") != "running" or not isinstance(entry.get("sequence"), int):
        return None
    trigger_data = dict(run.trigger_data or {})
    journal = trigger_data.get(STEP_EFFECT_JOURNAL_TRIGGER_FIELD)
    journal = dict(journal) if isinstance(journal, dict) else {}
    journal[step_id] = {"trace_sequence": entry["sequence"], "effect_intent": False}
    trigger_data[STEP_EFFECT_JOURNAL_TRIGGER_FIELD] = journal
    run.trigger_data = trigger_data
    return entry["sequence"]


def armed_step_effect_journal_sequence(run: Any, step_id: str) -> int | None:
    trigger_data = run.trigger_data if isinstance(getattr(run, "trigger_data", None), dict) else {}
    journal = trigger_data.get(STEP_EFFECT_JOURNAL_TRIGGER_FIELD)
    entry = journal.get(step_id) if isinstance(journal, dict) else None
    sequence = entry.get("trace_sequence") if isinstance(entry, dict) else None
    return sequence if isinstance(sequence, int) else None


def interrupted_step_proved_effect_free(run: Any, step_id: str) -> bool:
    """True when the interrupted attempt's journal shows no effectful call started.

    The journal must belong to that exact attempt (its ``running`` trace
    entry); a missing, stale or unbound journal proves nothing.
    """
    trigger_data = run.trigger_data if isinstance(getattr(run, "trigger_data", None), dict) else {}
    journal = trigger_data.get(STEP_EFFECT_JOURNAL_TRIGGER_FIELD)
    entry = journal.get(step_id) if isinstance(journal, dict) else None
    latest = _latest_trace_entry(run, step_id)
    return (
        isinstance(entry, dict)
        and latest is not None
        and latest.get("status") == "running"
        and isinstance(entry.get("trace_sequence"), int)
        and entry["trace_sequence"] == latest.get("sequence")
        and entry.get("effect_intent") is False
    )


class StepEffectIntentWatcher:
    """Pre-I/O watcher for one step attempt.

    Counts effectful tool calls in memory (an in-run failure is effect-free
    only when it saw none) and, for an attempt with an armed journal, durably
    records the first one before its handler runs. A failed write keeps the
    handler from running, so the journal never under-reports.
    """

    def __init__(
        self,
        *,
        run_id: str,
        step_id: str,
        trace_sequence: int | None,
        session_factory: Callable[[], Any] | None,
    ) -> None:
        self.run_id = run_id
        self.step_id = step_id
        self.trace_sequence = trace_sequence
        self.session_factory = session_factory
        self.effect_intents = 0
        self._journaled = False

    async def __call__(self, tool_name: str, _classification: Any) -> None:
        if self.trace_sequence is not None and not self._journaled:
            await self._journal(tool_name)
            self._journaled = True
        self.effect_intents += 1

    async def _journal(self, tool_name: str) -> None:
        from sqlalchemy import Text, func, literal, update
        from sqlalchemy.dialects.postgresql import ARRAY, JSONB

        from packages.core.models.workflow import WorkflowRun

        entry = {
            "trace_sequence": self.trace_sequence,
            "effect_intent": True,
            "tool": str(tool_name or "")[:120],
        }
        async with self.session_factory() as session:
            await session.execute(
                update(WorkflowRun)
                .where(
                    WorkflowRun.id == self.run_id,
                    WorkflowRun.trigger_data[
                        (STEP_EFFECT_JOURNAL_TRIGGER_FIELD, self.step_id, "trace_sequence")
                    ].astext == str(self.trace_sequence),
                )
                .values(trigger_data=func.jsonb_set(
                    WorkflowRun.trigger_data,
                    literal([STEP_EFFECT_JOURNAL_TRIGGER_FIELD, self.step_id], ARRAY(Text)),
                    literal(entry, JSONB),
                    True,
                ))
                .execution_options(synchronize_session=False)
            )
            await session.commit()


__all__ = [
    "EFFECT_JOURNALED_STEP_TYPES",
    "STEP_EFFECT_JOURNAL_TRIGGER_FIELD",
    "StepEffectIntentWatcher",
    "StepTransientRetryPolicy",
    "TRANSIENT_RETRY_CODE",
    "TRANSIENT_RETRY_CONFIG_KEY",
    "TRANSIENT_RETRY_RESULT_KEY",
    "TRANSIENT_RETRY_WAIT_TYPE",
    "TransientStepFailure",
    "arm_step_effect_journal",
    "armed_step_effect_journal_sequence",
    "interrupted_step_proved_effect_free",
    "is_transient_retry_record",
    "plan_step_transient_retry",
    "resolve_step_transient_retry_policy",
    "step_failure_replay_safe",
    "transient_failure_from_exception",
    "transient_failure_reason",
    "transient_retry_status_message",
]
