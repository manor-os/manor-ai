"""One person's daily Workspace digest: what changed, what waits on them, broken accounts.

Built only from shared sources: the Workspace event ledger, open HitlRequests
(with the reminder sweep's recipient rule), and the connected-account incident
markers. Scope is the person's own entity and the Workspaces they can read.
Rendering is plain text; scheduling and delivery live elsewhere.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import Text, func, literal, select, update
from sqlalchemy.dialects.postgresql import ARRAY, JSONB

from packages.core.attention import delivery as attention_delivery
from packages.core.constants.approvals import APPROVAL_OPEN_STATUSES
from packages.core.constants.attention import AttentionKind, AttentionState
from packages.core.database import async_session
from packages.core.ledger import event_types as et
from packages.core.models.attention import AttentionItem
from packages.core.models.document import Integration
from packages.core.models.goal import Goal
from packages.core.models.hitl_request import HitlRequest
from packages.core.models.notification import Notification
from packages.core.models.task import Task
from packages.core.models.user import OAuthAccount, User
from packages.core.models.workspace import Workspace
from packages.core.models.workspace_event import WorkspaceEvent
from packages.core.services import account_incident_delivery, hitl_reminders
from packages.core.services.account_incidents import INCIDENT_KEY
from packages.core.services.notify import notify
from packages.core.services.provider_keys import canonical_provider_key
from packages.core.services.timezone_utils import load_user_timezone

logger = logging.getLogger(__name__)

DIGEST_HOUR = 9
DIGEST_CATCH_UP_HOURS = 3
NOTIFICATION_KIND = "workspace_digest"
FIRST_DIGEST_LOOKBACK = timedelta(hours=24)
MAX_LOOKBACK = timedelta(days=7)
# The person's digest state: User.preferences["digest_state"]. A top-level key,
# so the notification settings save (which rewrites "notifications") keeps it.
STATE_KEY = "digest_state"

CHANGED_LIMIT = 5
WAITING_LIMIT = 10
# Open cards read before the per-person recipient filter; past this the
# overflow count is a lower bound.
WAITING_SCAN = 200
CHANGED_KEYS = ("completed", "goals", "auto_started")
ACCOUNTS_LINK = "/integrations"

_PACE_TEXT = {
    "ahead": "ahead of pace",
    "on_track": "on track",
    "behind": "behind pace",
    "at_risk": "at risk",
    "achieved": "achieved",
    "unknown": "not measurable yet",
}


@dataclass(frozen=True)
class DigestItem:
    workspace: str
    text: str
    link: str | None = None


@dataclass(frozen=True)
class Digest:
    changed: dict[str, list[DigestItem]]
    changed_more: dict[str, int]
    waiting: list[DigestItem]
    waiting_more: int
    accounts: list[DigestItem]
    next_steps: list[DigestItem] = field(default_factory=list)
    # The open-card scan hit its limit, so more cards may wait than were counted.
    waiting_more_is_lower_bound: bool = False

    def is_empty(self) -> bool:
        return not (any(self.changed.values()) or self.waiting or self.accounts or self.next_steps)


async def _readable_workspaces(db, user) -> dict[str, str]:
    """Active, readable Workspaces in the person's own entity: id → name."""
    from packages.core.permissions import resolve_effective_user_role_name
    from packages.core.services.workspace_access import user_readable_workspace_ids

    role = await resolve_effective_user_role_name(
        db, user_id=user.id, entity_id=user.entity_id, legacy_role=user.role,
    )
    ids = await user_readable_workspace_ids(db, entity_id=user.entity_id, user_id=user.id, role=role)
    if not ids:
        return {}
    rows = (await db.execute(
        select(Workspace.id, Workspace.name).where(
            Workspace.id.in_(ids),
            Workspace.status == "active",
            Workspace.deleted_at.is_(None),
        )
    )).all()
    return {str(row.id): row.name or "Workspace" for row in rows}


def _window(user, workspace_ids, since: datetime, now: datetime) -> list[Any]:
    return [
        WorkspaceEvent.entity_id == user.entity_id,
        WorkspaceEvent.workspace_id.in_(workspace_ids),
        WorkspaceEvent.occurred_at > since,
        WorkspaceEvent.occurred_at <= now,
    ]


async def _latest(
    db, conditions: list[Any], *, limit: int = CHANGED_LIMIT, distinct_source: bool = False,
) -> tuple[list[WorkspaceEvent], int]:
    """The newest ``limit`` events matching ``conditions`` and their total count.

    With ``distinct_source`` each source counts once and appears once, as its
    newest event: a re-run Task is one finished Task.
    """
    counted = func.count(func.distinct(WorkspaceEvent.source_id)) if distinct_source else func.count()
    total = (await db.execute(select(counted).where(*conditions))).scalar_one()
    if not total:
        return [], 0
    matching = list(conditions)
    if distinct_source:
        newest_per_source = (
            select(WorkspaceEvent.id).where(*conditions)
            .distinct(WorkspaceEvent.source_id)
            .order_by(WorkspaceEvent.source_id, WorkspaceEvent.occurred_at.desc(), WorkspaceEvent.id.desc())
        )
        matching = [WorkspaceEvent.id.in_(newest_per_source)]
    rows = (await db.execute(
        select(WorkspaceEvent).where(*matching)
        .order_by(WorkspaceEvent.occurred_at.desc(), WorkspaceEvent.id.desc())
        .limit(limit)
    )).scalars().all()
    return list(rows), int(total)


async def _titles(db, model, ids: set[str]) -> dict[str, str]:
    if not ids:
        return {}
    rows = (await db.execute(select(model.id, model.title).where(model.id.in_(ids)))).all()
    return {str(row.id): row.title for row in rows}


def _payload(event: Any) -> dict[str, Any]:
    """An event's (or event row's) payload as a dict."""
    return event.payload if isinstance(event.payload, dict) else {}


async def _changed(
    db, user, names: dict[str, str], *, since: datetime, now: datetime,
) -> tuple[dict[str, list[DigestItem]], dict[str, int]]:
    window = _window(user, list(names), since, now)
    completed, completed_total = await _latest(
        db, [*window, WorkspaceEvent.event_type == et.EXECUTION_COMPLETED], distinct_source=True,
    )
    # Approved by the system, not a person: the Strategist started it under a
    # standing "Always approve" grant. Human-request items are auto-approved
    # by design (they never mint a card), so they are not started work.
    auto, auto_total = await _latest(db, [
        *window,
        WorkspaceEvent.event_type == et.PROPOSAL_ITEM_APPROVED,
        WorkspaceEvent.actor_kind == "system",
        func.coalesce(WorkspaceEvent.payload["kind"].astext, "") != "human_request",
    ])

    task_ids = {e.source_id for e in completed}
    task_ids |= {e.source_id for e in auto if not _payload(e).get("title")}
    task_titles = await _titles(db, Task, task_ids)

    def _task_title(event: WorkspaceEvent) -> str:
        return str(_payload(event).get("title") or task_titles.get(event.source_id) or "A Task")

    completed_items = [
        DigestItem(names[e.workspace_id], f"Finished “{_task_title(e)}”", f"/tasks/{e.source_id}")
        for e in completed
    ]
    auto_items = [
        DigestItem(names[e.workspace_id], f"Started “{_task_title(e)}” automatically", f"/tasks/{e.source_id}")
        for e in auto
    ]
    goal_items = await _goal_items(db, window, names)
    goals_shown = goal_items[:CHANGED_LIMIT]
    changed = {"completed": completed_items, "goals": goals_shown, "auto_started": auto_items}
    more = {
        "completed": completed_total - len(completed_items),
        "goals": len(goal_items) - len(goals_shown),
        "auto_started": auto_total - len(auto_items),
    }
    return changed, more


def _pace_endpoint(window: list[Any], *, first: bool):
    """Each Goal's first (or last) pace change in the window, one row per Goal."""
    order = (lambda column: column.asc()) if first else (lambda column: column.desc())
    return (
        select(WorkspaceEvent.source_id, WorkspaceEvent.workspace_id, WorkspaceEvent.payload,
               WorkspaceEvent.occurred_at)
        .where(*window, WorkspaceEvent.event_type == et.GOAL_PACE_CHANGED)
        .distinct(WorkspaceEvent.source_id)
        .order_by(WorkspaceEvent.source_id, order(WorkspaceEvent.occurred_at), order(WorkspaceEvent.id))
    )


async def _goal_items(db, window: list[Any], names: dict[str, str]) -> list[DigestItem]:
    """One line per Goal that achieved or changed pace on net, newest first.

    Every Goal with events in the window is inspected, through its first and
    last pace change, so the overflow counts only Goals that would be shown.
    """
    first = {row.source_id: row for row in (await db.execute(_pace_endpoint(window, first=True))).all()}
    last = {row.source_id: row for row in (await db.execute(_pace_endpoint(window, first=False))).all()}
    achieved = {row.source_id: row for row in (await db.execute(
        select(WorkspaceEvent.source_id, WorkspaceEvent.workspace_id,
               func.max(WorkspaceEvent.occurred_at).label("occurred_at"))
        .where(*window, WorkspaceEvent.event_type == et.GOAL_ACHIEVED)
        .group_by(WorkspaceEvent.source_id, WorkspaceEvent.workspace_id)
    )).all()}
    titles = await _titles(db, Goal, set(last) | set(achieved))
    lines: list[tuple[datetime, str, DigestItem]] = []
    for goal_id in set(last) | set(achieved):
        title = titles.get(goal_id) or "A goal"
        latest = max((row for row in (last.get(goal_id), achieved.get(goal_id)) if row is not None),
                     key=lambda row: row.occurred_at)
        if goal_id in achieved:
            text = f"Goal “{title}” achieved"
        else:
            new_pace = _payload(last[goal_id]).get("new_pace")
            old_pace = _payload(first[goal_id]).get("old_pace")
            if not new_pace or new_pace == old_pace:
                continue  # moved and came back: no net change to report
            text = f"Goal “{title}” is now {_PACE_TEXT.get(new_pace, new_pace)}"
            if old_pace:
                text += f" (was {_PACE_TEXT.get(old_pace, old_pace)})"
        workspace_id = latest.workspace_id
        link = f"/workspaces/{workspace_id}?tab=overview"
        lines.append((latest.occurred_at, goal_id, DigestItem(names[workspace_id], text, link)))
    lines.sort(key=lambda line: (line[0], line[1]), reverse=True)
    return [item for _at, _goal_id, item in lines]


# reminder_recipients reads only these fields of a card.
RecipientKey = tuple[str, str | None, str | None]  # (entity id, Workspace id, origin Task id)


async def _waiting(
    db,
    user,
    names: dict[str, str],
    *,
    now: datetime,
    recipients: dict[RecipientKey, frozenset[str]],
    links: dict[str, str],
    prepared: dict[str, Any],
) -> tuple[list[DigestItem], int, bool]:
    """(oldest things waiting on the person, how many more, whether that count is a lower bound).

    Open cards, and what has no card: a Workflow paused at its approval and a
    Strategist proposal. Staging 2026-11-03: the digest named one of the four
    things waiting because it read only the cards.
    """
    requests = (await db.execute(
        select(HitlRequest).where(
            HitlRequest.entity_id == user.entity_id,
            HitlRequest.workspace_id.in_(list(names)),
            HitlRequest.status.in_(APPROVAL_OPEN_STATUSES),
            HitlRequest.created_at < now,
        ).order_by(HitlRequest.created_at.asc(), HitlRequest.id.asc()).limit(WAITING_SCAN)
    )).scalars().all()
    mine: list[HitlRequest] = []
    for request in requests:
        key = (request.entity_id, request.workspace_id, request.origin_task_id)
        if key not in recipients:
            recipients[key] = frozenset(await hitl_reminders.reminder_recipients(db, request))
        if str(user.id) in recipients[key]:
            mine.append(request)
    others = (await db.execute(
        select(AttentionItem).where(
            AttentionItem.entity_id == user.entity_id,
            AttentionItem.workspace_id.in_(list(names)),
            AttentionItem.state == AttentionState.OPEN.value,
            AttentionItem.kind.in_([AttentionKind.NEEDS_INPUT.value, AttentionKind.DECISION_PENDING.value]),
            AttentionItem.subject_type != "hitl_request",
            AttentionItem.opened_at < now,
        ).order_by(AttentionItem.opened_at.asc(), AttentionItem.id.asc()).limit(WAITING_SCAN)
    )).scalars().all()
    waiting: list[tuple[datetime, HitlRequest | AttentionItem]] = [(request.created_at, request) for request in mine]
    for other in others:
        if other.id not in prepared:
            prepared[other.id] = await attention_delivery.prepare_told(db, other, stage=other.stage, now=now)
        told = prepared[other.id]
        if told is not None and str(user.id) in told.recipients:
            waiting.append((other.opened_at, other))
    # A decision a run or proposal is held on comes first; on staging 2026-11-04, months-old
    # open questions filled the list and pushed an article's approval out of it.
    waiting.sort(key=lambda row: (
        isinstance(row[1], AttentionItem) and row[1].subject_type == "human_commitment", _utc(row[0]),
    ))
    items: list[DigestItem] = []
    for _at, row in waiting[:WAITING_LIMIT]:
        if isinstance(row, AttentionItem):
            items.append(DigestItem(names[row.workspace_id], row.title, prepared[row.id].link))
            continue
        if row.id not in links:
            links[row.id] = await hitl_reminders.card_link(db, row)
        items.append(DigestItem(names[row.workspace_id], hitl_reminders.summary(row), links[row.id]))
    lower_bound = len(requests) >= WAITING_SCAN or len(others) >= WAITING_SCAN
    return items, len(waiting) - len(items), lower_bound


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _open_incident(state: Any) -> bool:
    return isinstance(state, dict) and isinstance(state.get(INCIDENT_KEY), dict)


def _account_item(provider: str) -> DigestItem:
    label = account_incident_delivery.provider_label(provider)
    return DigestItem("Accounts", f"{label} needs reconnecting", ACCOUNTS_LINK)


async def _accounts(
    db, user, *, now: datetime, scans: dict[str, account_incident_delivery.WorkspaceScan],
) -> list[DigestItem]:
    items: list[DigestItem] = []
    personal = (await db.execute(
        select(OAuthAccount).where(
            OAuthAccount.user_id == user.id,
            OAuthAccount.profile[INCIDENT_KEY].is_not(None),
        ).order_by(OAuthAccount.provider.asc(), OAuthAccount.id.asc())
    )).scalars().all()
    items.extend(_account_item(a.provider) for a in personal if _open_incident(a.profile))

    integrations = [
        row for row in (await db.execute(
            select(Integration).where(
                Integration.entity_id == user.entity_id,
                Integration.status == "active",
                Integration.config[INCIDENT_KEY].is_not(None),
            ).order_by(Integration.provider.asc(), Integration.id.asc())
        )).scalars().all()
        if _open_incident(row.config)
    ]
    owned_providers: set[str] | None = None
    for integration in integrations:
        if integration.owner_user_id and str(integration.owner_user_id) == str(user.id):
            items.append(_account_item(integration.provider))
            continue
        if owned_providers is None:
            # Providers used by Workspaces this person owns: the incident
            # delivery's scan, computed once per entity and shareable.
            if user.entity_id not in scans:
                scans[user.entity_id] = await account_incident_delivery.scan_entity_workspaces(
                    db, user.entity_id, now,
                )
            owned_providers = set()
            for _workspace_id, _name, owners, providers in scans[user.entity_id]:
                if str(user.id) in owners:
                    owned_providers |= providers
        if canonical_provider_key(integration.provider) in owned_providers:
            items.append(_account_item(integration.provider))
    return items


async def build_user_digest(
    db,
    user,
    *,
    since: datetime,
    now: datetime,
    scans: dict[str, account_incident_delivery.WorkspaceScan] | None = None,
    recipients: dict[RecipientKey, frozenset[str]] | None = None,
    links: dict[str, str] | None = None,
    prepared: dict[str, Any] | None = None,
) -> Digest:
    """Build one person's digest for ``(since, now]``.

    ``scans`` caches each entity's Workspace owner/provider scan,
    ``recipients`` each card's recipients by ``RecipientKey``, ``prepared``
    each attention item's recipients and link by id, and ``links``
    each card's link by id. Pass one dict of each across the people of a run
    so each is computed once per run.
    """
    names = await _readable_workspaces(db, user)
    if names:
        changed, changed_more = await _changed(db, user, names, since=since, now=now)
        waiting, waiting_more, waiting_lower_bound = await _waiting(
            db, user, names, now=now,
            recipients=recipients if recipients is not None else {},
            links=links if links is not None else {},
            prepared=prepared if prepared is not None else {},
        )
    else:
        changed = {key: [] for key in CHANGED_KEYS}
        changed_more = {key: 0 for key in CHANGED_KEYS}
        waiting, waiting_more, waiting_lower_bound = [], 0, False
    accounts = await _accounts(db, user, now=now, scans=scans if scans is not None else {})
    return Digest(
        changed=changed,
        changed_more=changed_more,
        waiting=waiting,
        waiting_more=waiting_more,
        accounts=accounts,
        next_steps=[],
        waiting_more_is_lower_bound=waiting_lower_bound,
    )


def _lines(items: list[DigestItem], more: int, *, more_is_lower_bound: bool = False) -> list[str]:
    lines = [f"• {item.workspace}: {item.text}" for item in items]
    if more > 0:
        lines.append("…and more" if more_is_lower_bound else f"…and {more} more")
    return lines


def render_digest(digest: Digest, *, local_date: date) -> tuple[str, str]:
    """(title, plain-text body). Empty sections are left out."""
    title = f"Your Workspaces · {local_date.strftime('%b')} {local_date.day}"
    changed: list[str] = []
    for key in CHANGED_KEYS:
        if digest.changed.get(key):
            changed.extend(_lines(digest.changed[key], digest.changed_more.get(key, 0)))
    sections = [
        ("What changed", changed),
        ("Waiting on you", _lines(
            digest.waiting, digest.waiting_more, more_is_lower_bound=digest.waiting_more_is_lower_bound,
        ) if digest.waiting else []),
        ("Accounts", _lines(digest.accounts, 0)),
        ("Suggested next steps", _lines(digest.next_steps, 0)),
    ]
    body = "\n\n".join(f"{heading}\n" + "\n".join(lines) for heading, lines in sections if lines)
    return title, body


def _local_now(user: User, now: datetime) -> datetime | None:
    """The person's local time when it is inside the digest hours, else None."""
    local = now.astimezone(load_user_timezone(user.timezone))
    if DIGEST_HOUR <= local.hour < DIGEST_HOUR + DIGEST_CATCH_UP_HOURS:
        return local
    return None


async def _last_digest_at(db, user: User) -> datetime | None:
    return (await db.execute(
        select(func.max(Notification.created_at)).where(
            Notification.user_id == user.id, Notification.type == NOTIFICATION_KIND,
        )
    )).scalar_one_or_none()


def _stored_state(user: User) -> tuple[str | None, datetime | None]:
    """(local date of the last digest sent, end of its window) from the person's preferences."""
    state = (user.preferences or {}).get(STATE_KEY)
    if not isinstance(state, dict):
        return None, None
    last_local_date = state.get("last_local_date")
    until: datetime | None = None
    if isinstance(state.get("until"), str):
        try:
            until = datetime.fromisoformat(state["until"])
        except ValueError:
            until = None
        if until is not None and until.tzinfo is None:
            until = until.replace(tzinfo=timezone.utc)
    return (last_local_date if isinstance(last_local_date, str) else None), until


async def _digest_since(db, user: User, *, local: datetime, now: datetime) -> datetime | None:
    """Where this person's next digest window starts, or None when today's was sent.

    The state stored on the person decides. The latest digest Notification is
    a fallback for a send whose state was never stored; deleting it does not
    reopen the day.
    """
    today = local.date()
    last_local_date, until = _stored_state(user)
    if last_local_date == today.isoformat():
        return None
    last = await _last_digest_at(db, user)
    if last is not None and last.astimezone(local.tzinfo).date() == today:
        return None
    return max(until or last or now - FIRST_DIGEST_LOOKBACK, now - MAX_LOOKBACK)


async def _record_digest_sent(db, user_id: str, *, local_date: date, until: datetime) -> None:
    """Store ``preferences.digest_state`` on the person in one atomic UPDATE.

    The merge runs against the row as it is at write time, so preference keys
    another session wrote since this person was loaded are kept.
    """
    state = {"last_local_date": local_date.isoformat(), "until": until.isoformat()}
    await db.execute(
        update(User).where(User.id == user_id).values(preferences=func.jsonb_set(
            func.coalesce(User.preferences, literal({}, type_=JSONB)),
            literal([STATE_KEY], type_=ARRAY(Text)),
            literal(state, type_=JSONB),
            literal(True),
            type_=JSONB,
        )).execution_options(synchronize_session=False)
    )


async def run_workspace_digest_sweep(*, now: datetime | None = None, batch: int = 500) -> int:
    """Send today's digest to every active user from 09:00 local until noon.

    Returns the number of digests handed to ``notify``. A person gets at most
    one per local date: the date and window end of the last digest are stored
    on the person once ``notify`` took it, someone who already has one today is
    skipped before anything is built, and the notification key carries the
    date as a backstop. Each window is ``(previous window end, this sweep's
    now]``, so nothing falls between two digests. The hours up to noon are a
    catch-up: a tick that was dropped or ran late does not cost anyone the
    day's digest. The accepted cost is that a person with nothing to report
    has their digest rebuilt (and found empty) on every tick until noon, about
    12 times a day.
    """
    now = now or datetime.now(timezone.utc)
    scans: dict[str, account_incident_delivery.WorkspaceScan] = {}
    recipients: dict[RecipientKey, frozenset[str]] = {}
    links: dict[str, str] = {}
    prepared: dict[str, Any] = {}
    sent = 0
    after = ""
    while True:
        outgoing: list[tuple[User, date, str, str]] = []
        async with async_session() as db:
            users = (await db.execute(
                select(User).where(
                    User.id > after, User.status == "active", User.deleted_at.is_(None),
                ).order_by(User.id.asc()).limit(batch)
            )).scalars().all()
            for user in users:
                local = _local_now(user, now)
                if local is None:
                    continue
                try:
                    # A savepoint per person: a database error rolls back only
                    # this person's reads, and the page's session stays usable.
                    async with db.begin_nested():
                        since = await _digest_since(db, user, local=local, now=now)
                        if since is None:
                            continue  # already sent today
                        digest = await build_user_digest(
                            db, user, since=since, now=now, scans=scans, recipients=recipients, links=links, prepared=prepared,
                        )
                    if digest.is_empty():
                        continue
                    title, body = render_digest(digest, local_date=local.date())
                except Exception:
                    logger.exception("workspace digest: building for user %s failed", user.id)
                    continue
                outgoing.append((user, local.date(), title, body))
            if users:
                after = users[-1].id
            more = len(users) == batch
        for user, local_date, title, body in outgoing:
            key = f"workspace-digest:{user.id}:{local_date.isoformat()}"
            try:
                await notify(
                    entity_id=user.entity_id, user_id=user.id, type=NOTIFICATION_KIND,
                    title=title, body=body, link="/notifications", severity="info",
                    idempotency_key=key,
                )
                sent += 1
            except ValueError:
                logger.info("workspace digest: %s already sent", key)
            except Exception:
                logger.exception("workspace digest: sending to user %s failed", user.id)
                continue
            try:
                async with async_session() as db:
                    await _record_digest_sent(db, user.id, local_date=local_date, until=now)
                    await db.commit()
            except Exception:
                logger.exception("workspace digest: recording the digest for user %s failed", user.id)
        if not more:
            return sent
