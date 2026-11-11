"""Part designers: sub-agents that each design one part of a Workspace.

A lead (the Workspace Architect on a creation Draft, Workspace chat on a live
Workspace) commands a designer with a brief. The designer reads a snapshot of
what the Workspace has, in portable (Blueprint) keys, and submits a checked
candidate; it never writes the Draft. The lead accepts the candidate, which is
checked again against the latest Draft before it is written, or sends it back
with feedback.

Every designer is the same composition of functions; what it knows about its
part lives in its skill. Adding a part means adding a skill and one entry to
``part_designers``.
"""
from __future__ import annotations

import asyncio
import functools
import json
import logging
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Awaitable, Callable

from packages.core.ai.runtime.profiles import RuntimeProfile
from packages.core.ai.runtime.subagents import SubAgentSpec

logger = logging.getLogger(__name__)

SKILLS_DIR = Path(__file__).resolve().parents[1] / "ai" / "skills"
# Draft field holding each part's candidate until the lead accepts it.
CANDIDATES_FIELD = "_candidates"
# Draft field counting each part's designer runs that ended with no candidate.
DESIGN_FAILURES_FIELD = "_design_failures"
# Draft field set once the person has been told their Workspace starts without its own App page.
APP_GAP_TOLD_FIELD = "_app_gap_told"
# Runs the App designer gets before the Workspace starts on the standard page.
APP_DESIGN_TRIES = 2
# The lead's tool that runs one wave of designers; a part without a tool of its own uses it.
PARTS_COMMAND_TOOL = "ws_design_parts"

# Designers run at once in a wave; a provider that rate-limits makes the rest wait their turn.
DESIGN_CONCURRENCY = 3
# One wave, every attempt included: a stuck provider cannot hold the lead longer.
DESIGN_WAVE_TIMEOUT_SECONDS = 900.0
DESIGN_RETRY_DELAY_SECONDS = 2.0
DESIGN_STAGGER_SECONDS = 0.25
# How often a waiting lead renews its hold on the Draft.
DESIGN_KEEPALIVE_SECONDS = 60.0
# A designer's tools read its own context (the Workspace, the catalog, the page
# it keeps), meant whole: the loop's 12,000 cut the restaurant App's 40,000, so
# the designer could not keep a page it never saw.
DESIGNER_TOOL_RESULT_MAX_CHARS = 80_000

Check = Callable[[Any, Any], tuple[Any, list[str], list[str]]]
ToolExecutor = Callable[[str, dict[str, Any]], Awaitable[str]]


@dataclass
class PartDesignResult:
    """One designer run: its last passing candidate, if any."""

    candidate: Any
    warnings: list[str]
    notes: str
    attempts: int
    # Recorded beside the candidate and returned to the lead.
    details: dict[str, Any] = field(default_factory=dict)
    # The run's tool state, for a designer that reports more than the above.
    state: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PartDesigner:
    """How one part is designed, checked and accepted."""

    part: str
    title: str
    # Each surface sets its profile and round cap; see ``designer_subagent_specs``.
    subagent: SubAgentSpec
    skill: str
    # Private tools: read the context, use the part's patterns, submit a candidate.
    tool_schemas: tuple[dict[str, Any], ...]
    executor: Callable[[Any, dict[str, Any]], ToolExecutor]
    context_from_draft: Callable[[dict[str, Any]], Any]
    context_from_workspace: Callable[[Any, Any], Awaitable[Any]]
    # (context, value) -> (normalized, errors, warnings); run on submit and on accept.
    check: Check
    # Writes a checked candidate into the Draft fields; returns what the lead sees.
    accept: Callable[[dict[str, Any], Any], dict[str, Any]]
    # The candidate as the lead reviews it before accepting.
    summarize: Callable[[Any], dict[str, Any]]
    command_tool: str
    brief_hint: str
    # Why the part cannot be designed yet: {"error": ..., **detail}, or None.
    blockers: Callable[[Any], dict[str, Any] | None] = lambda _context: None
    # Sections the skill shares with other prompts, as (heading, text), kept in code.
    shared_guidance: tuple[tuple[str, str], ...] = ()
    # Said to the designer when the part already has a design to keep.
    revise_note: Callable[[Any], str | None] = lambda _context: None
    # A sent-back candidate -> (what the run reads of it, its tools' starting state); else it reads it all.
    revision: Callable[[Any], tuple[Any, dict[str, Any]]] | None = None
    # Runs the designer; ``run_part_designer`` unless the part wraps it.
    run: Callable[..., Awaitable[Any]] | None = None
    # Lint ``where`` prefixes this part owns.
    lint_where: tuple[str, ...] = ()
    depends_on: tuple[str, ...] = ()
    # (Draft field, value key) a candidate was kept in before ``_candidates``.
    legacy_slot: tuple[str, str] | None = None
    # What the lead does itself when the designer produces nothing.
    fallback: str | None = None
    # Writes a checked candidate through the lead's own write tools, for a part
    # whose writes need the database (``accept`` is then not called):
    # (db, record, entity_id=, user_id=, draft_id=, draft_phase=) -> (summary, error).
    apply: Callable[..., Awaitable[tuple[dict[str, Any] | None, dict[str, Any] | None]]] | None = None
    # Whether there is anything to design yet; ``False`` skips the part.
    needed: Callable[[Any], bool] = lambda _context: True

    async def design(self, context: Any, **kwargs: Any) -> Any:
        """Run the designer once; the result has candidate, details, warnings, notes, attempts."""
        if self.run is not None:
            return await self.run(context, **kwargs)
        return await run_part_designer(self, context, **kwargs)


@functools.cache
def part_designers() -> dict[str, PartDesigner]:
    from packages.core.services.workspace_agents_designer import AGENTS_DESIGNER
    from packages.core.services.workspace_app_designer import APP_DESIGNER
    from packages.core.services.workspace_automations_designer import AUTOMATIONS_DESIGNER
    from packages.core.services.workspace_knowledge_designer import KNOWLEDGE_DESIGNER
    from packages.core.services.workspace_workflow_designer import WORKFLOWS_DESIGNER

    return {designer.part: designer for designer in (
        AGENTS_DESIGNER, KNOWLEDGE_DESIGNER, WORKFLOWS_DESIGNER, AUTOMATIONS_DESIGNER, APP_DESIGNER)}


def designer_dependencies() -> dict[str, tuple[str, ...]]:
    return {part: designer.depends_on for part, designer in part_designers().items()}


def designer_lint_where() -> dict[str, tuple[str, ...]]:
    return {part: designer.lint_where for part, designer in part_designers().items()}


def designer_subagent_specs(profile: RuntimeProfile, max_steps: int) -> tuple[SubAgentSpec, ...]:
    return tuple(replace(designer.subagent, profile=profile, max_steps=max_steps)
                 for designer in part_designers().values())


def designer_command_tools() -> frozenset[str]:
    return frozenset(designer.command_tool for designer in part_designers().values())


# ── Candidate slots ─────────────────────────────────────────────────────────

def pending_candidates(fields: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Each part's waiting candidate, including one kept in its legacy field."""
    slots = fields.get(CANDIDATES_FIELD)
    pending = {part: dict(record) for part, record in (slots.items() if isinstance(slots, dict) else ())
               if part in part_designers() and isinstance(record, dict)}
    for part, designer in part_designers().items():
        if part in pending or designer.legacy_slot is None:
            continue
        name, value_key = designer.legacy_slot
        legacy = fields.get(name)
        if isinstance(legacy, dict):
            record = dict(legacy)
            record["value"] = record.pop(value_key, None)
            pending[part] = record
    return pending


def note_design_failure(fields: dict[str, Any], part: str) -> None:
    """Count a designer run that ended with no candidate."""
    counts = fields.get(DESIGN_FAILURES_FIELD)
    counts = dict(counts) if isinstance(counts, dict) else {}
    counts[part] = int(counts.get(part) or 0) + 1
    fields[DESIGN_FAILURES_FIELD] = counts


def _design_failures(fields: dict[str, Any], part: str) -> int:
    counts = fields.get(DESIGN_FAILURES_FIELD)
    return int(counts.get(part) or 0) if isinstance(counts, dict) else 0


def keep_candidate(fields: dict[str, Any], part: str, record: dict[str, Any]) -> None:
    drop_candidate(fields, part)
    counts = fields.get(DESIGN_FAILURES_FIELD)
    if isinstance(counts, dict) and part in counts:
        fields[DESIGN_FAILURES_FIELD] = {key: value for key, value in counts.items() if key != part}
    fields[CANDIDATES_FIELD] = {**(fields.get(CANDIDATES_FIELD) or {}), part: record}


def drop_candidate(fields: dict[str, Any], part: str) -> None:
    legacy_slot = part_designers()[part].legacy_slot
    if legacy_slot is not None:
        fields.pop(legacy_slot[0], None)
    slots = {key: value for key, value in (fields.get(CANDIDATES_FIELD) or {}).items() if key != part}
    if slots:
        fields[CANDIDATES_FIELD] = slots
    else:
        fields.pop(CANDIDATES_FIELD, None)


def accept_candidate(fields: dict[str, Any], part: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Check ``part``'s candidate against ``fields`` again and write it there.

    Returns ``(summary, None)``, or ``(None, {"error": ..., ...})`` leaving
    ``fields`` unchanged.
    """
    designer = part_designers().get(part)
    if designer is None:
        return None, {"error": f"unknown part {part!r}; parts are {sorted(part_designers())}"}
    record = pending_candidates(fields).get(part)
    if record is None:
        return None, {"error": f"no {designer.title} design is waiting; command one with {designer.command_tool}"}
    # The Draft may have changed since the design was checked.
    normalized, errors, _ = designer.check(designer.context_from_draft(fields), record.get("value"))
    if errors:
        return None, {"error": "the Draft changed since this design was checked; command a new design",
                      "errors": errors}
    summary = designer.accept(fields, normalized)
    drop_candidate(fields, part)
    return summary, None


def sendback_hint(designer: PartDesigner) -> str:
    """How the lead sends ``designer``'s candidate back."""
    if designer.command_tool == PARTS_COMMAND_TOOL:
        return f"{PARTS_COMMAND_TOOL}(parts=[{{part: '{designer.part}', feedback: ...}}])"
    return f"{designer.command_tool} with feedback"


def pending_candidate_issues(fields: dict[str, Any]) -> list[dict[str, str]]:
    return [{"severity": "P1", "where": f"{CANDIDATES_FIELD}.{part}", "message": (
        f"A {part_designers()[part].title} design is waiting for acceptance -- accept it with "
        f"ws_accept_parts(parts=['{part}']) or send it back with {sendback_hint(part_designers()[part])}.")}
        for part in pending_candidates(fields)]


def needs_app_page(fields: dict[str, Any]) -> bool:
    """A Workspace with a Workflow people run has work to start and watch from its App."""
    return not isinstance(fields.get("app_view"), dict) and any(
        isinstance(row, dict) and row.get("slug") and not row.get("internal") for row in fields.get("workflows") or [])


def app_design_gave_up(fields: dict[str, Any]) -> bool:
    """The App designer ran out of tries and the Workspace starts on the standard page."""
    return (needs_app_page(fields) and "app" not in pending_candidates(fields)
            and _design_failures(fields, "app") >= APP_DESIGN_TRIES)


def missing_app_issue(fields: dict[str, Any]) -> dict[str, str] | None:
    """Blocks a Workspace that needs an App page until it has one or the designer gave up."""
    if not needs_app_page(fields):
        return None
    if app_design_gave_up(fields):
        return {"severity": "P1", "where": "app_view", "message": (
            "The App page could not be designed, so this Workspace starts on the standard page. The person is told "
            "this with your reply: do not repeat it or explain why. You can try ws_design_app_view again.")}
    if "app" in pending_candidates(fields):
        return {"severity": "P0", "where": "app_view", "message": (
            "This Workspace has no App page yet and its design is waiting: accept it with ws_accept_app_view or "
            "send it back with feedback.")}
    return {"severity": "P0", "where": "app_view", "message": (
        "This Workspace has no App page yet: command one with ws_design_app_view and accept it. An earlier design "
        "that was interrupted left nothing behind.")}


# ── Sub-agent run ───────────────────────────────────────────────────────────

def revision_context(sent_back: dict[str, Any] | None, brief: str) -> str:
    """The sent-back design and the brief it answered, for a run that revises it."""
    if not isinstance(sent_back, dict) or sent_back.get("value") is None:
        return ""
    text = ""
    earlier = str(sent_back.get("brief") or "").strip()
    if earlier and earlier != brief.strip():
        text += f"\n\nThe brief your last design answered (still holds unless the feedback changes it):\n{earlier}"
    design = json.dumps(sent_back["value"], ensure_ascii=False, separators=(",", ":"), default=str)
    return text + ("\n\nYour last design, to revise: do what the feedback asks that your tools can do, keep the "
                   "rest, and submit the whole design again. What your tools cannot do stays undone and you name it "
                   "in your note: never remove, disable or block work the design already does to satisfy a check you "
                   f"cannot build.\n{design}")


def designer_system_prompt(designer: PartDesigner) -> str:
    text = (SKILLS_DIR / designer.skill / "SKILL.md").read_text(encoding="utf-8")
    if text.startswith("---"):
        text = text.split("---", 2)[2]
    return "\n\n".join([text.strip(), *(f"## {heading}\n\n{body}" for heading, body in designer.shared_guidance)])


async def run_part_designer(
    designer: PartDesigner,
    context: Any,
    *,
    brief: str,
    feedback: str | None,
    entity_id: str,
    user_id: str | None,
    workspace_id: str | None,
    surface: Any,
    sent_back: dict[str, Any] | None = None,
) -> PartDesignResult:
    """Run ``designer``'s sub-agent once and return its last passing candidate.

    ``sent_back`` is the design the feedback is about: ``{"value": candidate,
    "brief": the brief it answered}``. Each run is a new sub-agent, so without
    it a revision starts over from a brief the lead may have shortened.
    """
    from packages.core.ai.runtime import (
        runtime_execute_subagent_loop,
        runtime_prepare_local_tool_surface_for_turn,
        runtime_request_for_surface_turn,
        runtime_usage_subsource_scope,
    )

    request = runtime_request_for_surface_turn(
        surface=surface, entity_id=entity_id, user_id=user_id, workspace_id=workspace_id,
        message=brief, legacy_path=f"services.{designer.subagent.name}",
    )
    surface_result = runtime_prepare_local_tool_surface_for_turn(request, tool_schemas=list(designer.tool_schemas))
    schemas = {schema["function"]["name"]: schema for schema in surface_result.tool_schemas}
    state: dict[str, Any] = {}
    message = f"Brief:\n{brief.strip()}"
    if feedback and feedback.strip():
        message += f"\n\nYour last design was sent back with this feedback:\n{feedback.strip()}"
        if designer.revision is not None and isinstance(sent_back, dict) and sent_back.get("value") is not None:
            view, state = designer.revision(sent_back["value"])
            sent_back = {**sent_back, "value": view}
        message += revision_context(sent_back, brief)
    note = designer.revise_note(context)
    if note:
        message += f"\n\n{note}"
    # Logged as the caller's source plus ".<part>_designer"; the caller still pays.
    with runtime_usage_subsource_scope(f"{designer.part}_designer"):
        loop = await runtime_execute_subagent_loop(
            runtime_envelope=surface_result.envelope,
            requested_name=designer.subagent.name,
            system_prompt=designer_system_prompt(designer),
            user_message=message,
            tools=surface_result.tool_schemas,
            allowed_tool_names=surface_result.allowed_tool_names,
            tool_executor=designer.executor(context, state),
            tool_schema_resolver=lambda name: schemas.get(str(name or "")),
            entity_id=entity_id,
            user_id=user_id,
            agent_id=None,
            workspace_id=workspace_id,
            conversation_id=None,
            active_user_message=brief,
            tool_result_max_chars=DESIGNER_TOOL_RESULT_MAX_CHARS,
        )
    return PartDesignResult(
        candidate=state.get("candidate"),
        warnings=state.get("warnings") or [],
        notes=str(getattr(loop.result, "content", "") or "").strip(),
        attempts=int(state.get("attempts") or 0),
        details=dict(state.get("details") or {}),
        state=state,
    )


def candidate_reply(designer: PartDesigner, result: Any) -> dict[str, Any]:
    """What the lead sees of a kept candidate."""
    return {
        "candidate": designer.summarize(result.candidate), **result.details,
        "warnings": result.warnings, "design_notes": result.notes,
        "next": (f"Accept with ws_accept_parts(parts=['{designer.part}']), or call {designer.command_tool} "
                 "again with feedback."),
    }


# ── Waves: several designers at once ────────────────────────────────────────

@dataclass
class DesignJob:
    """One designer to run in a wave, with what ``PartDesigner.design`` takes."""

    designer: PartDesigner
    context: Any
    brief: str
    feedback: str | None = None
    sent_back: dict[str, Any] | None = None


@dataclass
class DesignOutcome:
    """A wave member's result: ``result`` after a run, else the technical ``error``."""

    job: DesignJob
    result: Any = None
    error: str | None = None
    attempts: int = 0
    seconds: float = 0.0


def _rate_limited(exc: BaseException) -> bool:
    text = f"{type(exc).__name__} {exc}".lower()
    return "429" in text or "rate limit" in text or "ratelimit" in text or "too many requests" in text


async def design_wave(
    jobs: list[DesignJob],
    *,
    entity_id: str,
    user_id: str | None,
    workspace_id: str | None,
    surface: Any,
    keepalive: Callable[[], Awaitable[None]] | None = None,
    concurrency: int = DESIGN_CONCURRENCY,
    timeout: float = DESIGN_WAVE_TIMEOUT_SECONDS,
) -> list[DesignOutcome]:
    """Run ``jobs``' designers at once and return their outcomes in order.

    At most ``concurrency`` run together; after a provider rate-limits one,
    the rest run one at a time. A run that fails for a technical reason is
    tried once more after a pause; ``timeout`` bounds the whole wave, so a
    provider that never answers ends as a failed outcome instead of holding
    the lead. One member's failure never stops another. Their database work
    queues as one caller, and ``keepalive`` runs every
    ``DESIGN_KEEPALIVE_SECONDS`` meanwhile (the lead renews its hold on the
    Draft); an error it raises stops the wave and is raised.
    """
    from packages.core.database_runtime import DatabaseAdmissionGroup, database_admission_group

    deadline = time.monotonic() + timeout
    slots = asyncio.Semaphore(max(1, concurrency))
    serial = asyncio.Lock()
    rate_limited = False

    async def attempt(job: DesignJob) -> Any:
        return await job.designer.design(
            job.context, brief=job.brief, feedback=job.feedback, entity_id=entity_id, user_id=user_id,
            workspace_id=workspace_id, surface=surface, sent_back=job.sent_back,
        )

    async def run(index: int, job: DesignJob) -> DesignOutcome:
        nonlocal rate_limited
        outcome = DesignOutcome(job=job)
        started = time.monotonic()
        # A short stagger spreads the designers' first model and database calls.
        await asyncio.sleep(index * DESIGN_STAGGER_SECONDS)
        for tries in (1, 2):
            remaining = deadline - time.monotonic()
            if remaining <= 1:
                outcome.error = outcome.error or "the wave ran out of time"
                break
            outcome.attempts = tries
            try:
                async with asyncio.timeout(remaining):
                    async with slots:
                        if rate_limited:
                            async with serial:
                                outcome.result = await attempt(job)
                        else:
                            outcome.result = await attempt(job)
                outcome.error = None
                break
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - a provider fault is retried, then reported
                outcome.error = "timed out" if isinstance(exc, (TimeoutError, asyncio.TimeoutError)) else repr(exc)
                rate_limited = rate_limited or _rate_limited(exc)
                logger.warning("%s designer run %d failed: %s", job.designer.part, tries, outcome.error,
                               exc_info=not isinstance(exc, (TimeoutError, asyncio.TimeoutError)))
                if tries == 1:
                    await asyncio.sleep(min(DESIGN_RETRY_DELAY_SECONDS * (4 if rate_limited else 1),
                                            max(0.0, deadline - time.monotonic() - 1)))
        outcome.seconds = time.monotonic() - started
        if outcome.error is not None:
            # Seen by operators, never by the person: the lead builds the part itself.
            logger.error("Part designer failed for a technical reason: part=%s entity=%s attempts=%d error=%s",
                         job.designer.part, entity_id, outcome.attempts, outcome.error)
        return outcome

    group_token = database_admission_group.set(DatabaseAdmissionGroup(limit=1))
    try:
        tasks = [asyncio.create_task(run(index, job)) for index, job in enumerate(jobs)]
    finally:
        database_admission_group.reset(group_token)
    keeper = None
    if keepalive is not None:
        async def keep() -> None:
            while True:
                await asyncio.sleep(DESIGN_KEEPALIVE_SECONDS)
                await keepalive()
        keeper = asyncio.create_task(keep())
    try:
        waiting = set(tasks) | ({keeper} if keeper is not None else set())
        while any(not task.done() for task in tasks):
            done, waiting = await asyncio.wait(waiting, return_when=asyncio.FIRST_COMPLETED)
            if keeper is not None and keeper in done:
                keeper.result()  # raises what stopped the keepalive
        return [task.result() for task in tasks]
    finally:
        for task in [*tasks, *([keeper] if keeper is not None else [])]:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, *([keeper] if keeper is not None else []), return_exceptions=True)
