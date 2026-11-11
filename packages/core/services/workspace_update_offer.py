"""Offer a delivered Workspace's new release where its people already are.

A provider sending a new release used to change nothing anyone could see: the
customer found it only by opening the Workspace overview. The offer is one
``blueprint_update`` card in the Workspace's main chat, plus a notification to
the people who own the Workspace. The card's one action applies the release
with the default choices — the author's changes are taken, the customer's own
edits are kept — through the ordinary Blueprint upgrade, so the result can be
reverted like any upgrade.

One open card per Workspace: a newer release closes the older card, and any
upgrade (from the card, the upgrade dialog or an agent) closes it as applied.
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.constants.pending_actions import PendingActionKind
from packages.core.models.task import Conversation, Message

logger = logging.getLogger(__name__)

NOTIFICATION_KIND = "workspace_update_available"
UPDATE_CHOICE = "update"
DISMISS_CHOICE = "dismiss"


#: Plan actions where the Workspace holds its own edit: one the author also
#: changed (``keep_yours``) or one the author left alone (``customized``).
_OWN_EDIT_ACTIONS = frozenset({"keep_yours", "customized"})


def default_resolutions(items: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Choices for an update nobody reviews item by item: take every change of
    the author's, but never replace an edit the Workspace made itself (the
    upgrade dialog is where someone decides to give one up)."""
    return [
        {
            "key": item["key"],
            "resolution": "keep_yours" if item.get("blocked_reason") or item["action"] in _OWN_EDIT_ACTIONS
            else "use_blueprint",
        }
        for item in items
        if item["action"] != "unchanged"
    ]


async def _open_cards(db: AsyncSession, *, entity_id: str, workspace_id: str) -> list[Message]:
    return list((await db.execute(
        select(Message)
        .join(Conversation, Message.conversation_id == Conversation.id)
        .where(
            Conversation.entity_id == entity_id,
            Conversation.workspace_id == workspace_id,
            Message.pending_action["kind"].as_string() == PendingActionKind.BLUEPRINT_UPDATE.value,
            Message.resolved_at.is_(None),
        )
        .with_for_update(of=Message)
    )).scalars().all())


async def close_update_cards(
    db: AsyncSession, *, entity_id: str, workspace_id: str, user_id: str | None, resolution: dict,
) -> int:
    """Resolve every open update card in the Workspace. Caller commits."""
    from packages.core.workspace_chat.service import resolve_pending_action

    cards = await _open_cards(db, entity_id=entity_id, workspace_id=workspace_id)
    for card in cards:
        await resolve_pending_action(db, message_id=card.id, user_id=user_id, resolution=dict(resolution))
    return len(cards)


async def mark_update_undone(db: AsyncSession, *, entity_id: str, workspace_id: str, version: str) -> None:
    """Record on the card that applied ``version`` that it was undone. Caller commits."""
    card = (await db.execute(
        select(Message)
        .join(Conversation, Message.conversation_id == Conversation.id)
        .where(
            Conversation.entity_id == entity_id,
            Conversation.workspace_id == workspace_id,
            Message.pending_action["kind"].as_string() == PendingActionKind.BLUEPRINT_UPDATE.value,
            Message.resolution["choice"].as_string() == UPDATE_CHOICE,
            Message.resolution["payload"]["version"].as_string() == version,
        )
        .order_by(Message.created_at.desc())
        .limit(1)
        .with_for_update(of=Message)
    )).scalar_one_or_none()
    if card is not None:
        resolution = dict(card.resolution)
        resolution["payload"] = {**(resolution.get("payload") or {}), "undone": True}
        card.resolution = resolution


async def _owner(db: AsyncSession, workspace):
    from packages.core.services.workspace_creators import workspace_creator_users

    return (await workspace_creator_users(db, [workspace])).get(workspace.id)


def _locale(owner) -> str:
    # The card and notice are read by the Workspace's own people; the owner's
    # language stands for theirs.
    return str(getattr(owner, "locale", None) or "en").split("-")[0].lower()


async def offer_update(
    db: AsyncSession,
    *,
    workspace,
    version: str,
    previous_version: str | None,
    release_notes: str | None,
    publisher_name: str | None,
) -> Message:
    """Post the card for ``version``, closing the card of any older release.
    Caller commits, then calls :func:`notify_update_owners`."""
    from packages.core.i18n import t
    from packages.core.services.decision_recommendation import recommendation
    from packages.core.workspace_chat.service import post_message

    await close_update_cards(
        db, entity_id=workspace.entity_id, workspace_id=workspace.id, user_id=None,
        resolution={"choice": "superseded", "payload": {"version": version}},
    )
    notes = (release_notes or "").strip()
    locale = _locale(await _owner(db, workspace))
    # The card says what happened in the viewer's language; the body carries
    # only the provider's own words.
    return await post_message(
        db,
        entity_id=workspace.entity_id,
        workspace_id=workspace.id,
        body=notes,
        message_kind="system",
        author_kind="system",
        refs=[{"type": "workspace", "id": workspace.id}],
        pending_action={
            "kind": PendingActionKind.BLUEPRINT_UPDATE.value,
            "title": t("workspace_update.title", locale, version=version),
            "version": version,
            "previous_version": previous_version,
            "release_notes": notes or None,
            "publisher_name": publisher_name,
            "workspace_name": workspace.name,
            "options": [UPDATE_CHOICE],
            "recommendation": recommendation(UPDATE_CHOICE, reason=t("workspace_update.keeps_edits", locale)),
        },
    )


async def notify_update_owners(
    db: AsyncSession, *, workspace, version: str, publisher_name: str | None,
) -> None:
    """Tell the Workspace's owner in the bell, and on the channels they chose
    for this kind. Best effort: the card is the durable offer."""
    from packages.core.i18n import t
    from packages.core.services.notify import notify

    owner = await _owner(db, workspace)
    if owner is None or getattr(owner, "status", "active") != "active":
        return
    locale = _locale(owner)
    try:
        await notify(
            workspace.entity_id, owner.id, NOTIFICATION_KIND,
            t("workspace_update.notify_title", locale, workspace=workspace.name, version=version),
            body=t("workspace_update.notify_body", locale,
                   publisher=publisher_name or t("workspace_update.provider", locale)),
            link=f"/workspaces/{workspace.id}",
            workspace_id=workspace.id,
            meta={"workspace_id": workspace.id, "version": version},
            idempotency_key=f"{NOTIFICATION_KIND}:{workspace.id}:{version}",
        )
    except Exception:
        logger.warning("workspace update notification failed workspace=%s", workspace.id, exc_info=True)
