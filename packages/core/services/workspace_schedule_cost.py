"""What frequent schedules cost in model calls, so the person can decide how often they run.

A schedule whose Workflow reaches an Agent step before any branch pays for the
model on every run, even when nothing is new (a watch on a source with no feed
to read). The Architect shows these numbers when it asks how often to look and
again before the draft is ready. Nothing here limits a schedule: the person
decides.
"""
from __future__ import annotations

from calendar import monthrange
from dataclasses import dataclass
from datetime import date
from math import isfinite
from typing import Any

from packages.core.cron import cron_field_matches

# One look by an Agent: 9 real source-watch runs on 2026-11-03/04, none connected to an account
# (3-9 model calls, 15k-69k prompt tokens). Reading a real inbox can only add to it.
LOOK_PROMPT_TOKENS = 28_000
LOOK_CACHED_TOKENS = 9_000
LOOK_COMPLETION_TOKENS = 300
# Below this a schedule's few runs are not what decides the bill.
FREQUENT_RUNS_PER_DAY = 1.0
EXAMPLE_CADENCES = (("every 15 minutes", 96), ("every hour", 24), ("every 4 hours", 6), ("twice a day", 2))


def _cron_values(field: str, low: int, high: int) -> set[int]:
    return {value for value in range(low, high + 1)
            if cron_field_matches(value, field, minimum=low, maximum=high)}


def runs_per_day(*, cron_expr: Any = None, every_seconds: Any = None) -> float:
    """Daily frequency over the Gregorian calendar, using the scheduler's grammar; 0 when unknown."""
    try:
        if every_seconds:
            seconds = float(every_seconds)
            return 86_400 / seconds if isfinite(seconds) and seconds > 0 else 0.0
        fields = str(cron_expr or "").split()
        if len(fields) != 5:
            return 0.0
        minute, hour, days, months, weekdays = [
            _cron_values(field, low, high)
            for field, (low, high) in zip(fields, ((0, 59), (0, 23), (1, 31), (1, 12), (0, 6)))
        ]
        if not all((minute, hour, days, months, weekdays)):
            return 0.0
        per_day = len(minute) * len(hour)
        if len(days) == 31 and len(months) == 12:
            return per_day * len(weekdays) / 7
        # One 400-year cycle includes leap years and every weekday alignment.
        matching_days = sum(
            day <= monthrange(year, month)[1] and (date(year, month, day).weekday() + 1) % 7 in weekdays
            for year in range(2000, 2400) for month in months for day in days
        )
        return per_day * matching_days / 146_097
    except (TypeError, ValueError, ZeroDivisionError):
        return 0.0


def runs_model_every_time(slug: str, workflows: list[dict[str, Any]], *, seen: frozenset[str] = frozenset()) -> bool:
    """Whether every run of a Workflow reaches an Agent step, through sub-Flows, before any branch."""
    return _workflow_model_path(slug, workflows, seen) is True


def _workflow_model_path(slug: str, workflows: list[dict[str, Any]], seen: frozenset[str]) -> bool | None:
    from packages.core.services.workflow_service import entry_step_id

    workflow = next((row for row in workflows if row.get("slug") == slug), None)
    if workflow is None or slug in seen or not workflow.get("steps"):
        return False
    return _model_path(workflow["steps"], entry_step_id(workflow["steps"]), workflows, seen | {slug})


def _model_path(nodes: list[dict], entry: str | None, workflows: list[dict], seen: frozenset[str],
                routes: dict | None = None) -> bool | str | None:
    """True at a model, False at a branch/unknown path, None at completion, or a Stage exit name."""
    if entry is None:
        return False
    steps = {step.get("id"): step for step in nodes}
    current = entry
    visited: set[Any] = set()
    while current is not None:
        if current in (routes or {}):
            return current
        if current in visited or current not in steps:
            return False
        visited.add(current)
        step = steps[current]
        kind = step.get("type")
        if kind == "agent":
            return True
        config = step.get("config") or {}
        if kind == "subworkflow":
            outcome = _workflow_model_path(str(config.get("workflow_id") or ""), workflows, seen)
            if outcome is not None:
                return outcome
        if kind == "stage":
            exits = config.get("routes") or {}
            outcome = _model_path(config.get("operations") or [], config.get("entry_operation_id"), workflows, seen, exits)
            if isinstance(outcome, bool):
                return outcome
            if isinstance(outcome, str):
                current = exits[outcome]
                continue
        following = step.get("next") or []
        if kind in {"condition", "switch"} or len(following) > 1:
            return False
        current = following[0] if following else None
    return None


def frequent_model_schedules(fields: dict[str, Any]) -> list[dict[str, Any]]:
    """The scheduled jobs that pay for the model on every run and run at least daily."""
    workflows = [row for row in fields.get("workflows") or [] if isinstance(row, dict)]
    rows = []
    for job in fields.get("scheduled_jobs") or []:
        if not isinstance(job, dict):
            continue
        slug = str((job.get("execution_target") or {}).get("workflow_slug") or "")
        runs = runs_per_day(cron_expr=job.get("cron_expr"), every_seconds=job.get("every_seconds"))
        if runs >= FREQUENT_RUNS_PER_DAY and (not slug or runs_model_every_time(slug, workflows)):
            rows.append({"job": job.get("job_id"), "workflow": slug or None, "looks_per_day": round(runs)})
    return rows


@dataclass(frozen=True)
class LookPrice:
    """What one look costs for this account: in credits, or in dollars on the person's own model key."""

    model: str
    byok: bool
    credits_per_look: float
    usd_per_look: float

    def per_day(self, looks: float) -> str:
        if self.byok or not self.credits_per_look:
            return f"about ${self.usd_per_look * looks:.2f} a day on the model key"
        return f"about {max(round(self.credits_per_look * looks), 1):,} credits a day"


async def look_price(db: Any, *, entity_id: str, user_id: str | None) -> LookPrice:
    """Price a creation draft, before it has a persisted Workspace id."""
    from packages.core.services import billing_service, model_pricing_gateway
    from packages.core.services.model_resolver import resolve_llm_metadata_for_user, resolve_model_for_user

    model = await resolve_model_for_user("primary", user_id=user_id, entity_id=entity_id, workspace_id=None, db=db)
    byok = bool(await resolve_llm_metadata_for_user("primary", user_id=user_id, entity_id=entity_id, workspace_id=None, db=db))
    # Priced over many looks: credits round up per call, which would overstate a single look.
    credits = billing_service.tokens_to_credits(LOOK_PROMPT_TOKENS * 100, LOOK_COMPLETION_TOKENS * 100, model,
                                                cache_read_tokens=LOOK_CACHED_TOKENS * 100)
    usd = model_pricing_gateway.estimate_token_cost_usd(LOOK_PROMPT_TOKENS, LOOK_COMPLETION_TOKENS, model,
                                                        cache_read_tokens=LOOK_CACHED_TOKENS)
    return LookPrice(model=model, byok=byok, credits_per_look=credits / 100, usd_per_look=usd)


def schedule_cost_block(price: LookPrice) -> str:
    """The numbers the Architect quotes when it asks how often to look in a connected account."""
    from packages.core.blueprints.workflow_patterns import SOURCE_FEEDS
    from packages.core.services.integration_service import coming_soon_servers

    read_directly = ", ".join(feed.label for feed in SOURCE_FEEDS if feed.provider not in coming_soon_servers())
    options = "; ".join(f"{name}: {looks} looks, {price.per_day(looks)}" for name, looks in EXAMPLE_CADENCES)
    return (f"<schedule_cost>A look at {read_directly} reads the account directly and calls no model unless something "
            f"is new, so how often only decides how soon new things are picked up. Any other source, including one "
            f"reached through the browser, runs its Agent on every look, about {LOOK_PROMPT_TOKENS:,} tokens on "
            f"{price.model}, even when nothing is new. Per such source: {options}. These are estimates from runs with "
            f"nothing to read.</schedule_cost>\n")


async def schedule_cost_summary(db: Any, fields: dict[str, Any], *, entity_id: str, user_id: str | None) -> dict[str, Any] | None:
    """What the draft's frequent schedules cost a day, for the Architect to tell the person."""
    schedules = frequent_model_schedules(fields)
    if not schedules:
        return None
    price = await look_price(db, entity_id=entity_id, user_id=user_id)
    looks = sum(row["looks_per_day"] for row in schedules)
    return {"looks_per_day": looks, "tokens_per_day": looks * LOOK_PROMPT_TOKENS, "cost_per_day": price.per_day(looks),
            "schedules": schedules}
