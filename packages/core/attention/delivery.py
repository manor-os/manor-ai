"""Tell people about attention items: once per stage, in Workspace chat.

An item is told when it opens or its stage moves up, if its kind is told at
that stage. Each sweep posts one message per Workspace in its chat and sends
each recipient one notice per Workspace through their own notification
settings (``notify()``). ``fyi`` items are never told one by one.

Some kinds replace an older sender, whose own marker records what was sent:
a request's ``context.reminders`` (hitl_reminders, also a Strategist cohort's
card), an incident's ``notified_at`` (account_incident_delivery) and an
automation's pause notice (scheduler_service). Delivery reads those markers
too, and writes the ones the older sender reads, so switching the
``attention_delivery`` flag on or back off never repeats a stage the other
side already told.
"""
from __future__ import annotations

import hashlib
import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import and_, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.constants.attention import AttentionKind, AttentionState, AttentionUrgency
from packages.core.database import async_session
from packages.core.i18n import t
from packages.core.models.attention import AttentionItem
from packages.core.services.notify import notify

logger = logging.getLogger(__name__)

FLAG = "attention_delivery"


@dataclass(frozen=True)
class KindDelivery:
    #: The first stage people are told about. An approval card is already in
    #: the chat when it opens, so needs_input is told from its first reminder.
    first_stage: int = 0
    #: The notification type recipients' settings are read for; None = chat only.
    notification_type: Optional[str] = None
    #: An item first seen older than this is marked told without telling,
    #: matching the reminder window it replaces.
    max_first_age_hours: Optional[int] = None


def _kinds() -> dict[str, KindDelivery]:
    from packages.core.services.hitl_reminders import MAX_AGE_HOURS

    return {
        AttentionKind.NEEDS_INPUT.value: KindDelivery(
            first_stage=1, notification_type="task_hitl_reminder", max_first_age_hours=MAX_AGE_HOURS,
        ),
        # The proposal card is in the chat when it opens, like an approval card.
        AttentionKind.DECISION_PENDING.value: KindDelivery(
            first_stage=1, notification_type="proposal", max_first_age_hours=MAX_AGE_HOURS,
        ),
        AttentionKind.ACCOUNT_BROKEN.value: KindDelivery(notification_type="integration_account_issue"),
        AttentionKind.SETUP_INCOMPLETE.value: KindDelivery(notification_type="workspace_setup_required"),
        AttentionKind.AUTOMATION_PAUSED.value: KindDelivery(
            notification_type="automation_paused", max_first_age_hours=MAX_AGE_HOURS,
        ),
        AttentionKind.GOAL_OFF_PACE.value: KindDelivery(),
        # The Task's own outcome says it is blocked; these are its reminders.
        AttentionKind.WORK_BLOCKED.value: KindDelivery(
            first_stage=1, notification_type="task_hitl_reminder", max_first_age_hours=MAX_AGE_HOURS,
        ),
        AttentionKind.WORK_OVERDUE.value: KindDelivery(
            notification_type="task_overdue", max_first_age_hours=MAX_AGE_HOURS,
        ),
    }


async def delivery_enabled(db: AsyncSession, entity_id: str) -> bool:
    """Whether attention delivery replaces the older senders for this entity."""
    from packages.core.services.feature_flags import is_enabled

    return await is_enabled(db, FLAG, entity_id=entity_id, fallback=False)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


# ── What each subject needs: recipients, link, prior marker ──────────────

@dataclass
class _Told:
    item: AttentionItem
    stage: int
    recipients: list[str]
    link: str
    params: dict[str, Any] = field(default_factory=dict)


async def _workspace_people(db, workspace_id: Optional[str], now: datetime) -> list[str]:
    from packages.core.services.account_incident_delivery import _workspace_owner_ids

    return await _workspace_owner_ids(db, workspace_id, now) if workspace_id else []


async def _entity_admins(db, entity_id: str) -> list[str]:
    from packages.core.models.user import User

    return [str(value) for value in (await db.execute(
        select(User.id).where(
            User.entity_id == entity_id, User.status == "active", User.role.in_(("owner", "admin")),
        )
    )).scalars().all()]


def _reminded_request_id(item: AttentionItem) -> Optional[str]:
    """The card whose reminders this item replaces: its own, or a cohort's."""
    if item.subject_type == "hitl_request":
        return item.subject_id
    if item.subject_type == "proposal_review":
        return (item.evidence or {}).get("hitl_request_id")
    return None


async def _hitl_request(db, item: AttentionItem):
    from packages.core.models.hitl_request import HitlRequest

    request_id = _reminded_request_id(item)
    if not request_id:
        return None
    return (await db.execute(select(HitlRequest).where(HitlRequest.id == request_id))).scalar_one_or_none()


async def _prior_told(db, item: AttentionItem) -> Optional[int]:
    """The stage an older sender already told for this subject, if any."""
    from packages.core.models.notification import Notification
    from packages.core.services.account_incidents import INCIDENT_KEY

    if _reminded_request_id(item):
        request = await _hitl_request(db, item)
        sent = int((((request.context or {}) if request else {}).get("reminders") or {}).get("sent") or 0)
        return sent or None
    if item.subject_type == "scheduled_job":
        # scheduler_service.notify_scheduled_job_auto_paused, for this pause.
        noticed = (await db.execute(
            select(Notification.id).where(
                Notification.entity_id == item.entity_id,
                Notification.idempotency_key.like(f"scheduled-job:auto-paused:{item.subject_id}:%"),
                Notification.created_at >= item.opened_at,
            ).limit(1)
        )).scalar_one_or_none()
        return 0 if noticed else None
    state = await _incident_state(db, item)
    incident = (state or {}).get(INCIDENT_KEY)
    if isinstance(incident, dict) and incident.get("notified_at"):
        return 0
    return None


async def _incident_state(db, item: AttentionItem) -> Optional[dict]:
    from packages.core.models.document import Integration
    from packages.core.models.user import OAuthAccount

    if item.subject_type == "integration":
        row = await db.get(Integration, item.subject_id)
        return dict(row.config or {}) if row is not None else None
    if item.subject_type == "oauth_account":
        row = await db.get(OAuthAccount, item.subject_id)
        return dict(row.profile or {}) if row is not None else None
    return None


async def _mark_subject(db, item: AttentionItem, *, stage: int, prior: Optional[int], now: datetime) -> bool:
    """Write the older sender's marker for this stage. False if the subject closed."""
    from packages.core.services.account_incident_delivery import stamp_incident_notified
    from packages.core.services.hitl_reminders import claim_reminder_stage

    request_id = _reminded_request_id(item)
    if request_id:
        return await claim_reminder_stage(
            db, request_id, prev_sent=prior or 0,
            state={"sent": stage, "last_at": now.isoformat(), "by": "attention"},
        )
    if item.subject_type in ("integration", "oauth_account"):
        await stamp_incident_notified(db, item.subject_type, item.subject_id, now=now)
    return True


async def _task_people(db, item: AttentionItem) -> list[str]:
    """A Task's people, else the Workspace's approvers (as for a Task's card)."""
    from packages.core.services.notification_workspace_callbacks import resolve_workspace_hitl_recipients
    from packages.core.services.task_event_notifications import task_event_recipient_users

    _task, users = await task_event_recipient_users(
        db, item.entity_id, "task.hitl_reminder", {"task_id": (item.evidence or {}).get("task_id")},
        admin_fallback=False,
    )
    return [str(user.id) for user in users] or await resolve_workspace_hitl_recipients(
        db, entity_id=item.entity_id, workspace_id=item.workspace_id,
    )


async def prepare_told(db, item: AttentionItem, *, stage: int, now: datetime) -> Optional[_Told]:
    """Who an open item would be told to, and its link; the digest reads the same."""
    return await _prepare(db, item, stage, now)


async def _prepare(db, item: AttentionItem, stage: int, now: datetime) -> Optional[_Told]:
    """Recipients, link and copy parameters; None when nobody can be told."""
    from packages.core.models.document import Integration
    from packages.core.models.goal import Goal
    from packages.core.models.participant import HumanCommitment, ParticipantProfile
    from packages.core.models.user import OAuthAccount
    from packages.core.services.account_incident_delivery import provider_label
    from packages.core.services.hitl_reminders import card_link, reminder_recipients
    from packages.core.services.notification_workspace_callbacks import resolve_workspace_hitl_recipients

    evidence = item.evidence or {}
    chat = f"/chat?workspace={item.workspace_id}" if item.workspace_id else "/"
    if item.kind == AttentionKind.NEEDS_INPUT.value:
        params = {"label": evidence.get("label") or item.title, "stage": stage}
        if item.subject_type == "hitl_request":
            request = await _hitl_request(db, item)
            if request is None:
                return None
            return _Told(item, stage, await reminder_recipients(db, request), await card_link(db, request), params)
        if item.subject_type == "workflow_run":
            # A Workflow paused at an approval or input wait: the Workspace's approvers answer it.
            run_id = evidence.get("workflow_run_id")
            users = await resolve_workspace_hitl_recipients(
                db, entity_id=item.entity_id, workspace_id=item.workspace_id,
            )
            return _Told(item, stage, users, f"/workspaces/{item.workspace_id}/workflow-runs/{run_id}", params)
        commitment = await db.get(HumanCommitment, item.subject_id)
        if commitment is None:
            return None
        users: list[str] = []
        if commitment.participant_id:
            profile = await db.get(ParticipantProfile, commitment.participant_id)
            users = [str(profile.user_id)] if profile is not None and profile.user_id else []
        users = users or await resolve_workspace_hitl_recipients(
            db, entity_id=item.entity_id, workspace_id=item.workspace_id,
        )
        return _Told(item, stage, users, chat, params)

    if item.kind == AttentionKind.DECISION_PENDING.value:
        params = {"label": evidence.get("label") or item.title, "stage": stage}
        request = await _hitl_request(db, item)
        if request is not None:
            return _Told(item, stage, await reminder_recipients(db, request), await card_link(db, request), params)
        users = await resolve_workspace_hitl_recipients(db, entity_id=item.entity_id, workspace_id=item.workspace_id)
        return _Told(item, stage, users, chat, params)

    if item.kind == AttentionKind.ACCOUNT_BROKEN.value:
        params = {"provider": provider_label(evidence.get("provider") or "")}
        if item.subject_type == "oauth_account":
            account = await db.get(OAuthAccount, item.subject_id)
            return _Told(item, stage, [str(account.user_id)] if account else [], "/integrations", params)
        integration = await db.get(Integration, item.subject_id)
        users = [str(integration.owner_user_id)] if integration is not None and integration.owner_user_id else []
        users += await _workspace_people(db, item.workspace_id, now)
        if not users:
            users = await _entity_admins(db, item.entity_id)
        return _Told(item, stage, list(dict.fromkeys(users)), "/integrations", params)

    if item.kind == AttentionKind.SETUP_INCOMPLETE.value:
        labels = [
            str(check.get("label") or check.get("provider") or check.get("key") or "")
            for check in evidence.get("incomplete") or [] if isinstance(check, dict)
        ]
        params = {"labels": ", ".join(label for label in labels if label)}
        return _Told(item, stage, await _workspace_people(db, item.workspace_id, now),
                     f"/workspaces/{item.workspace_id}?tab=overview", params)

    if item.kind == AttentionKind.AUTOMATION_PAUSED.value:
        users = [item.assignee_user_id] if item.assignee_user_id else await _workspace_people(db, item.workspace_id, now)
        return _Told(item, stage, users, f"/jobs?job={evidence.get('job_id') or ''}", {
            "label": evidence.get("label") or item.title, "count": evidence.get("consecutive_errors") or 0,
        })

    if item.kind == AttentionKind.GOAL_OFF_PACE.value:
        goal = await db.get(Goal, item.subject_id)
        if goal is None:
            return None
        return _Told(item, stage, [], chat, {
            "goal": goal.title, "current": evidence.get("current_value") or "?",
            "target": evidence.get("target_value") or "?",
        })

    if item.kind in (AttentionKind.WORK_BLOCKED.value, AttentionKind.WORK_OVERDUE.value):
        return _Told(item, stage, await _task_people(db, item), f"/tasks/{item.subject_id}", {
            "label": evidence.get("label") or item.title, "reason": item.summary or "", "stage": stage,
        })
    return None


# ── Copy ─────────────────────────────────────────────────────────────────

def _lines(told: _Told, locale: Optional[str]) -> tuple[str, str]:
    """(headline, what to do) for one item, in ``locale``."""
    kind, p = told.item.kind, told.params
    if kind == AttentionKind.NEEDS_INPUT.value:
        wait = t("attention.wait.final" if told.stage >= 2 else "attention.wait.first", locale=locale)
        return (t("attention.needs_input.headline", locale=locale, label=p["label"][:120], wait=wait),
                t("attention.needs_input.action", locale=locale, link=told.link))
    if kind == AttentionKind.ACCOUNT_BROKEN.value:
        return (t("attention.account_broken.headline", locale=locale, provider=p["provider"]),
                t("attention.account_broken.action", locale=locale, link=told.link))
    if kind == AttentionKind.SETUP_INCOMPLETE.value:
        return (t("attention.setup_incomplete.headline", locale=locale, labels=p["labels"][:160]),
                t("attention.setup_incomplete.action", locale=locale, link=told.link))
    if kind in (AttentionKind.DECISION_PENDING.value, AttentionKind.WORK_BLOCKED.value):
        wait = t("attention.wait.final" if told.stage >= 2 else "attention.wait.first", locale=locale)
        action = t(f"attention.{kind}.action", locale=locale, link=told.link)
        reason = (p.get("reason") or "")[:160].rstrip()
        if reason:
            # The agent's own words; end the sentence before the link.
            action = f"{reason}{'' if reason[-1] in '.。!?！？' else '.'} {action}"
        return t(f"attention.{kind}.headline", locale=locale, label=p["label"][:120], wait=wait), action
    if kind == AttentionKind.AUTOMATION_PAUSED.value:
        return (t("attention.automation_paused.headline", locale=locale, label=p["label"][:120], count=p["count"]),
                t("attention.automation_paused.action", locale=locale, link=told.link))
    if kind == AttentionKind.WORK_OVERDUE.value:
        return (t("attention.work_overdue.headline", locale=locale, label=p["label"][:120]),
                t("attention.work_overdue.action", locale=locale, link=told.link))
    return (t("attention.goal_off_pace.headline", locale=locale, goal=p["goal"][:120],
              current=p["current"], target=p["target"]),
            t("attention.goal_off_pace.action", locale=locale))


def _chat_body(items: list[_Told], locale: Optional[str]) -> str:
    if len(items) == 1:
        headline, action = _lines(items[0], locale)
        return f"**{headline}**\n\n{action}"
    lines = [f"- **{headline}** {action}" for headline, action in (_lines(i, locale) for i in items)]
    return t("attention.many.headline", locale=locale, count=len(items)) + "\n\n" + "\n".join(lines)


_MARKDOWN_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")


def _notice(items: list[_Told], workspace: str, locale: Optional[str]) -> tuple[str, str, str]:
    """(title, body, link) of one person's notice for a batch."""
    if len(items) == 1:
        headline, action = _lines(items[0], locale)
        plain = _MARKDOWN_LINK.sub(r"\1", action)
        return headline[:200], f"{workspace} · {plain}", items[0].link
    body = "\n".join(f"• {_lines(i, locale)[0][:120]}" for i in items)
    link = f"/chat?workspace={items[0].item.workspace_id}" if items[0].item.workspace_id else items[0].link
    return t("attention.many.notice", locale=locale, count=len(items), workspace=workspace), body, link


# ── The pass ─────────────────────────────────────────────────────────────

_CLAIM_SQL = text(
    "UPDATE attention_items SET told_stage = :stage, told_at = :now"
    " WHERE id = :id AND state = 'open' AND coalesce(told_stage, -1) < :stage"
    " RETURNING id"
)


async def _claim(db, item_id: str, stage: int, now: datetime) -> bool:
    """Record the told stage only if no other pass told it first."""
    return (await db.execute(_CLAIM_SQL, {"id": item_id, "stage": stage, "now": now})).scalar_one_or_none() is not None


async def deliver_attention(*, now: datetime | None = None, limit: int = 200) -> dict[str, int]:
    """Tell what is due. Returns counts of chat messages and notices sent.

    The claims and chat messages commit together; notices are handed to
    ``notify()`` after the commit, one per person and Workspace.
    """
    now = now or datetime.now(timezone.utc)
    async with async_session() as db:
        posted, notices = await _tell_due(db, now=now, limit=limit)
        await db.commit()
    sent = await _send_notices(notices)
    return {"chat_messages": posted, "notices": sent}


async def _tell_due(db, *, now: datetime, limit: int) -> tuple[int, list[tuple]]:
    from packages.core.ai.response_language import resolve_response_locale
    from packages.core.models.workspace import Workspace
    from packages.core.services.hitl_reminders import log_reminder_on_task

    kinds = _kinds()
    due = and_(
        AttentionItem.state == AttentionState.OPEN.value,
        AttentionItem.urgency != AttentionUrgency.FYI.value,
        or_(*(
            and_(AttentionItem.kind == kind, AttentionItem.stage >= spec.first_stage)
            for kind, spec in kinds.items()
        )),
        or_(AttentionItem.told_stage.is_(None), AttentionItem.told_stage < AttentionItem.stage),
    )
    # Only entities with delivery on, so the rest can never fill the batch.
    entity_ids = [
        entity_id for entity_id in (await db.execute(
            select(AttentionItem.entity_id).where(due).distinct()
        )).scalars().all()
        if await delivery_enabled(db, entity_id)
    ]
    if not entity_ids:
        return 0, []
    rows = (await db.execute(
        select(AttentionItem).where(due, AttentionItem.entity_id.in_(entity_ids))
        .order_by(AttentionItem.opened_at.asc()).limit(limit)
    )).scalars().all()

    # A subject can have one item per Workspace (account_broken). The marker
    # the first one writes in this pass is not an earlier telling.
    told_this_pass: set[tuple[str, str]] = set()
    by_workspace: dict[tuple[str, Optional[str]], list[_Told]] = defaultdict(list)
    for item in rows:
        spec = kinds[item.kind]
        subject = (item.subject_type, item.subject_id)
        prior = None if subject in told_this_pass else await _prior_told(db, item)
        told = max(value for value in (item.told_stage, prior, -1) if value is not None)
        too_old = (
            told < 0 and spec.max_first_age_hours is not None
            and now - _aware(item.opened_at) > timedelta(hours=spec.max_first_age_hours)
        )
        if told >= item.stage or too_old:
            # Already told by the older sender, or past the window it would
            # have told in: catch up the marker without telling.
            await _claim(db, item.id, item.stage, now)
            continue
        if not await _claim(db, item.id, item.stage, now):
            continue
        if subject not in told_this_pass and not await _mark_subject(db, item, stage=item.stage, prior=prior, now=now):
            continue
        told_this_pass.add(subject)
        prepared = await _prepare(db, item, item.stage, now)
        if prepared is None:
            continue
        if item.subject_type == "hitl_request" and (item.evidence or {}).get("task_id"):
            await log_reminder_on_task(db, {
                "id": item.subject_id, "task_id": item.evidence["task_id"], "stage": item.stage,
                "summary": prepared.params["label"],
                "wait_minutes": max(0, int((now - _aware(item.opened_at)).total_seconds() // 60)),
            }, now=now)
        by_workspace[(item.entity_id, item.workspace_id)].append(prepared)

    posted = 0
    notices: list[tuple] = []
    noticed: set[tuple[str, str, str]] = set()  # (person, subject type, subject id)
    for (entity_id, workspace_id), items in by_workspace.items():
        name = "Manor"
        if workspace_id:
            name = (await db.execute(select(Workspace.name).where(Workspace.id == workspace_id))).scalar_one_or_none() or "Workspace"
            first = next((user for told in items for user in told.recipients), None)
            await _post_chat(db, entity_id, workspace_id, items, await resolve_response_locale(first, db=db))
            posted += 1
        per_person: dict[tuple[str, str], list[_Told]] = defaultdict(list)
        for told in items:
            notification_type = kinds[told.item.kind].notification_type
            for user_id in dict.fromkeys(told.recipients) if notification_type else ():
                key = (user_id, told.item.subject_type, told.item.subject_id)
                if key not in noticed:
                    noticed.add(key)
                    per_person[(user_id, notification_type)].append(told)
        for (user_id, notification_type), batch in per_person.items():
            locale = await resolve_response_locale(user_id, db=db)
            title, body, link = _notice(batch, name, locale)
            digest = hashlib.sha256(
                "|".join(sorted(f"{told.item.id}:{told.stage}" for told in batch)).encode()
            ).hexdigest()[:32]
            notices.append((entity_id, user_id, notification_type, workspace_id, title, body, link, {
                "attention_item_ids": [told.item.id for told in batch],
            }, f"attention:{user_id}:{digest}"))
    return posted, notices


async def _post_chat(db, entity_id: str, workspace_id: str, items: list[_Told], locale: Optional[str]) -> None:
    from packages.core.workspace_chat import service as chat_service

    message = await chat_service.post_message(
        db, entity_id=entity_id, workspace_id=workspace_id,
        body=_chat_body(items, locale),
        message_kind="agent_update", author_kind="agent",
        meta={"attention_item_ids": [told.item.id for told in items]},
        publish_event=False,
    )
    chat_service.queue_workspace_chat_message_event_after_commit(
        db, entity_id=entity_id, workspace_id=workspace_id, message=message,
    )


async def _send_notices(notices: list[tuple]) -> int:
    sent = 0
    for entity_id, user_id, notification_type, workspace_id, title, body, link, meta, key in notices:
        try:
            await notify(
                entity_id=entity_id, user_id=user_id, type=notification_type, title=title, body=body,
                link=link, severity="info", workspace_id=workspace_id, meta=meta, idempotency_key=key,
            )
            sent += 1
        except Exception:
            logger.warning("attention notice failed for %s", user_id, exc_info=True)
    return sent


__all__ = ["FLAG", "deliver_attention", "delivery_enabled"]
