"""Build the Strategist LLM prompt + parse the response into a Proposal.

The shape mirrors what the Planner uses (system prompt = stable rules
+ workspace context blocks; user prompt = the trigger + the JSON
schema). One repair retry on validation failure.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.ai.runtime import (
    runtime_execute_strategist_completion,
    runtime_strategist_system_prompt,
    runtime_strategist_user_prompt,
    runtime_strategist_user_prompt_v2,
    runtime_validation_retry_user_prompt,
)
from packages.core.ai.runtime.tool_error_contract import tool_error_payload
from packages.core.services.sensitive_data import sanitize_approval_credentials, sanitize_sensitive_payload
from packages.core.strategist.context import StrategistContext
from packages.core.strategist.proposal import Proposal

logger = logging.getLogger(__name__)


PLANNER_VERSION = "v0.1-demo-a"

STRATEGIST_PROMPT_SKILL_SLUG = "strategist_system_prompt"
"""If a Skill row with this slug exists for the workspace's entity, its
``system_prompt`` body replaces the hardcoded preamble. The dynamic
parts (services list, JSON schema) are still appended in code so an
edited skill body can't break the contract."""


class StrategistProposalGenerationError(RuntimeError):
    """The review produced no valid Proposal; never a successful empty cohort."""

    def __init__(self, message: str, *, code: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.details = sanitize_sensitive_payload(sanitize_approval_credentials(details or {}))


# ── LLM call orchestration ───────────────────────────────────────────

async def generate_proposal(
    ctx: StrategistContext,
    *,
    review_id: str,
    db: Optional[AsyncSession] = None,
    briefing_markdown: Optional[str] = None,
    user_id: str | None = None,
) -> Proposal:
    """Single Claude call → validated Proposal, with one repair retry.

    ``briefing_markdown`` (strategist_review_v2 only) swaps the legacy
    context sections for the deterministic ReviewBriefing markdown (M5/M6).
    Both paths include the same authoritative execution snapshot.

    The optional DB session is used only to resolve the Strategist prompt
    Skill. That read phase is committed before the external model call so a
    slow completion cannot leave the PostgreSQL connection idle in a
    transaction (staging enforces a 60-second idle transaction timeout).
    """
    override = (ctx.strategist_template or {}).get("system_prompt_override")
    if isinstance(override, str) and override.strip():
        preamble = override
    else:
        preamble = await _load_skill_preamble(db, ctx.workspace.entity_id)

    if db is not None and db.in_transaction():
        await db.commit()

    system_prompt = runtime_strategist_system_prompt(
        ctx,
        preamble=preamble,
        briefing_mode=briefing_markdown is not None,
    )
    if briefing_markdown is None:
        user_prompt = runtime_strategist_user_prompt(ctx, review_id=review_id)
    else:
        user_prompt = runtime_strategist_user_prompt_v2(
            briefing_markdown, ctx, review_id=review_id,
        )

    current_prompt = user_prompt
    for attempt in range(1, 3):
        raw = await _safe_runtime_completion(ctx, system_prompt, current_prompt, user_id=user_id)
        try:
            proposal = _parse_proposal(raw, review_id=review_id)
            _validate_execution_review(proposal, getattr(ctx, "execution_snapshot", {}) or {})
            return proposal
        except StrategistProposalGenerationError as exc:
            details = {**exc.details, "review_id": review_id, "attempts": attempt}
            logger.warning("Strategist Proposal validation failed: code=%s details=%s", exc.code, details)
            if attempt == 2:
                raise StrategistProposalGenerationError(str(exc), code=exc.code, details=details) from exc
            current_prompt = runtime_validation_retry_user_prompt(
                user_prompt,
                json.dumps(tool_error_payload(
                    tool_name="strategist_proposal", code=exc.code, message=str(exc), details=details,
                ), ensure_ascii=False),
            )
    raise AssertionError("Bounded Proposal generation did not return")


def _validate_execution_review(proposal: Proposal, snapshot: dict[str, Any]) -> None:
    """Validate model citations, never repair/infer another Task's evidence."""
    if not snapshot:
        # Historical readers and non-runtime callers have no snapshot to cite.
        if proposal.execution_review is None:
            return
    errors: list[dict[str, Any]] = []

    def mismatch(path: list, expected: Any, message: str) -> None:
        errors.append({"path": ["execution_review", *path],
                       "type": "execution_evidence_mismatch", "message": message, "expected": expected})

    review = proposal.execution_review
    if snapshot.get("status") != "available":
        mismatch([], None, "Execution snapshot unavailable; do not invent an execution review.")
    elif review is None:
        mismatch([], {"revision": snapshot["revision"], "references": []},
                 "Cite the supplied execution snapshot. Use references=[] only when no Task informs this decision.")
    else:
        if review.revision != snapshot["revision"]:
            mismatch(["revision"], snapshot["revision"], "Copy the exact supplied snapshot revision.")
        facts = {row["task_id"]: row for row in snapshot.get("tasks", [])}
        seen: set[str] = set()
        for index, reference in enumerate(review.references):
            path = ["references", index]
            fact = facts.get(reference.task_id)
            if fact is None:
                mismatch([*path, "task_id"], sorted(facts), "Task is not in this Workspace execution snapshot.")
                continue
            if reference.task_id in seen:
                mismatch([*path, "task_id"], "unique task_id", "Cite each Task once.")
            seen.add(reference.task_id)
            for key in ("plan_id", "observed_state"):
                if getattr(reference, key) != fact.get(key):
                    mismatch([*path, key], fact.get(key), f"{key} must come from this exact Task observation.")
            blocker = fact.get("current_blocker") or {}
            expected_ref = blocker.get("ref") if blocker.get("source_task_id") == reference.task_id else None
            if reference.blocker_ref is not None and reference.blocker_ref != expected_ref:
                mismatch([*path, "blocker_ref"], expected_ref, "Blocker must belong to this exact Task observation.")
    if errors:
        raise StrategistProposalGenerationError(
            "The proposal cites execution evidence that does not match the supplied Task facts.",
            code="proposal_execution_evidence_mismatch", details={"validation_errors": errors},
        )


def _is_transient_strategist_llm_error(exc: BaseException) -> bool:
    """True when the Strategist LLM failed in a way Celery should retry.

    Stream idle timeouts and provider stream errors keep their existing
    transport retry semantics instead of becoming Proposal validation errors.
    """
    if isinstance(exc, TimeoutError):
        return True
    try:
        from packages.core.ai.llm_client import LLMStreamProviderError
    except Exception:
        return False
    return isinstance(exc, LLMStreamProviderError)


async def _safe_runtime_completion(
    ctx: StrategistContext,
    system_prompt: str,
    user_prompt: str,
    *,
    user_id: str | None = None,
) -> str:
    from packages.core.ai.llm_client import CreditCheckUnavailableError, CreditExhaustedError
    from packages.core.budget.enforcement import BudgetExceeded
    from packages.core.services.credit_reservations import CreditReservationError

    try:
        workspace = getattr(ctx, "workspace", None)
        completion = await runtime_execute_strategist_completion(
            system_prompt,
            user_prompt,
            entity_id=getattr(workspace, "entity_id", None),
            workspace_id=getattr(workspace, "id", None),
            user_id=user_id,
        )
        return completion.content
    except (CreditExhaustedError, CreditCheckUnavailableError, CreditReservationError, BudgetExceeded):
        raise
    except Exception as exc:
        if _is_transient_strategist_llm_error(exc):
            raise
        failure = StrategistProposalGenerationError(
            "The Strategist model could not produce a proposal.", code="proposal_llm_unavailable",
            details={"cause": str(exc)[:2000], "exception_type": type(exc).__name__},
        )
        logger.warning("Strategist LLM call failed: code=%s details=%s", failure.code, failure.details)
        raise failure from exc


def _parse_proposal(text: str, *, review_id: str) -> Proposal:
    if not isinstance(text, str) or not text.strip():
        raise StrategistProposalGenerationError(
            "The Strategist model returned no proposal content.", code="proposal_invalid_output",
            details={"validation_errors": [{"path": [], "type": "empty_response", "message": "Expected a JSON Proposal object"}]},
        )
    candidate = _strip_code_fence(text).strip()
    try:
        # Inject review_id if the LLM omitted it (very common).
        data = json.loads(candidate)
        if isinstance(data, dict):
            data.setdefault("review_id", review_id)
        return Proposal.model_validate(data)
    except json.JSONDecodeError as exc:
        details = {"validation_errors": [{
            "path": [], "type": "invalid_json", "message": exc.msg,
            "line": exc.lineno, "column": exc.colno, "position": exc.pos,
        }]}
    except ValidationError as exc:
        errors = exc.errors(include_input=False, include_context=False, include_url=False)
        details = {"validation_errors": [{
            "path": list(error["loc"]), "type": error["type"], "message": error["msg"][:1000],
        } for error in errors[:20]]}
        if len(errors) > 20:
            details["omitted_errors"] = len(errors) - 20
    except (TypeError, ValueError, RecursionError) as exc:
        details = {"validation_errors": [{
            "path": [], "type": type(exc).__name__, "message": str(exc)[:1000],
        }]}
    raise StrategistProposalGenerationError(
        "The Strategist response does not match the Proposal contract.",
        code="proposal_invalid_output", details=details,
    )


def _strip_code_fence(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        first_nl = text.find("\n")
        if first_nl != -1:
            text = text[first_nl + 1:]
        if text.endswith("```"):
            text = text[:-3]
    return text.strip()


# ── Prompt building ───────────────────────────────────────────────────


async def _load_skill_preamble(
    db: Optional[AsyncSession], entity_id: Optional[str],
) -> Optional[str]:
    """Look up a per-entity Skill that overrides the system prompt.

    Returns the skill body (with ``{workspace_name}`` available as a
    format token) or None if no row exists / db isn't supplied.
    """
    if db is None or not entity_id:
        return None
    try:
        from packages.core.services.skill_service import get_skill_by_slug
        skill = await get_skill_by_slug(db, STRATEGIST_PROMPT_SKILL_SLUG, entity_id)
    except Exception:
        logger.debug("Strategist skill lookup failed", exc_info=True)
        return None
    if skill and (skill.system_prompt or "").strip():
        return skill.system_prompt
    return None
