"""Personal read receipts for Workspace reminders; never approval resolutions."""
from __future__ import annotations

import json
from types import SimpleNamespace

from sqlalchemy import select, update
from sqlalchemy.orm.attributes import set_committed_value

from packages.core.models.task import Conversation, Message
from packages.core.workspace_chat.pending_actions import _sources

from packages.core.models.user import User
from packages.core.workspace_chat.pending_actions import pending_actions_by_workspace, pending_action_counts

PREFERENCE_KEY = "workspace_seen_attention"


def _seen_keys(preferences: dict, workspace_id: str) -> set[str]:
    workspaces = (preferences or {}).get(PREFERENCE_KEY)
    keys = workspaces.get(workspace_id) if isinstance(workspaces, dict) else None
    return {key for key in keys if isinstance(key, str)} if isinstance(keys, list) else set()


async def workspace_attention(db, *, user: User, workspace_ids: list[str]):
    decisions = await pending_actions_by_workspace(db, entity_id=user.entity_id, workspace_ids=workspace_ids)
    return {wid: _attention_payload(items, _seen_keys(user.preferences, wid)) for wid, items in decisions.items()}


def _attention_payload(decisions: dict, seen: set[str]):
    # Outstanding decisions without a chat card still belong to the pending
    # action count, but cannot have a personal chat read receipt.
    unseen = [decision for decision in decisions.values() if decision["messages"] and decision["key"] not in seen]
    messages = {}
    for decision in unseen:
        for message_id, sources in decision["messages"].items():
            messages.setdefault(message_id, {}).update({source: decision["key"] for source in sources})
    return {**pending_action_counts(decisions), "unseen_actions": len(unseen), "unseen_action_keys": messages}


async def mark_workspace_attention_seen(db, *, user: User, workspace_id: str, keys: list[str]):
    decisions = await pending_actions_by_workspace(db, entity_id=user.entity_id, workspace_ids=[workspace_id])
    active_keys = {decision["key"] for decision in decisions[workspace_id].values()}
    valid_keys = active_keys.intersection(keys)
    if valid_keys:
        # Serialize receipts from multiple tabs/devices and merge against the
        # current row, preserving other preferences and Workspace receipts.
        preferences = (await db.execute(select(User.preferences).where(User.id == user.id)
                       .with_for_update())).scalar_one() or {}
        branch = preferences.get(PREFERENCE_KEY)
        branch = dict(branch) if isinstance(branch, dict) else {}
        branch[workspace_id] = sorted((_seen_keys(preferences, workspace_id) & active_keys) | valid_keys)
        preferences = {**preferences, PREFERENCE_KEY: branch}
        await db.execute(update(User).where(User.id == user.id).values(preferences=preferences)
                         .execution_options(synchronize_session=False))
        set_committed_value(user, "preferences", preferences)
    return _attention_payload(decisions[workspace_id], _seen_keys(user.preferences, workspace_id))


async def list_workspace_attention(db, *, user: User, workspace_id: str, unread_only: bool = True):
    """List one chat card per canonical open decision, optionally filtering read receipts."""
    decisions = (await pending_actions_by_workspace(
        db, entity_id=user.entity_id, workspace_ids=[workspace_id],
    ))[workspace_id]
    seen = _seen_keys(user.preferences, workspace_id)
    visible = [d for d in decisions.values() if d['messages'] and (not unread_only or d['key'] not in seen)]
    ids = {mid for d in visible for mid in d['messages']}
    if not ids:
        return {'items': []}
    # Only the card fields: a Workflow card's metadata carries its whole step list.
    messages = [
        SimpleNamespace(
            id=message_id, pending_action=pending_action, meta={"hitl_requests": hitl_requests},
            content=content, created_at=created_at, resolved_at=resolved_at,
        )
        for message_id, pending_action, hitl_requests, content, created_at, resolved_at in (await db.execute(
            select(
                Message.id, Message.pending_action, Message.meta["hitl_requests"], Message.content,
                Message.created_at, Message.resolved_at,
            ).join(Conversation, Conversation.id == Message.conversation_id).where(
                Message.id.in_(ids), Conversation.workspace_id == workspace_id,
                Conversation.entity_id == user.entity_id,
            ).order_by(Message.created_at.desc(), Message.id.desc())
        )).all()
    ]
    items = []
    for decision in visible:
        message = next((m for m in messages if m.id in decision['messages']), None)
        if message is None:
            continue
        sources = decision['messages'][message.id]
        action, source_id = next(
            ((action, rid or message.id) for action, rid in _sources(message)
             if (rid or message.id) in sources),
            # A message standing in for a decision (an Agent's saved question) has no card fields.
            (decision.get('action') or {}, next(iter(sources), message.id)),
        )
        summary = next((value.strip() for field in (
            'title', 'review_title', 'prompt', 'task_title', 'summary', 'observed_problem', 'business_outcome',
        ) if isinstance(value := action.get(field), str) and value.strip()), message.content or '')
        if summary.lstrip().startswith('{'):
            try:
                structured = json.loads(summary)
            except (ValueError, TypeError):
                structured = None
            if isinstance(structured, dict):
                summary = next((value for field in ('summary', 'message', 'title')
                                if isinstance(value := structured.get(field), str) and value.strip()), '')
        items.append({
            'key': decision['key'], 'message_id': message.id, 'source_id': source_id,
            'kind': str(action.get('kind') or action.get('type') or 'input'),
            'summary': ' '.join(summary.split())[:240], 'created_at': message.created_at,
            # A connect request duplicates a connection requirement surfaces list.
            'connection_provider': (
                str(action.get('provider_key') or '') or None
                if action.get('connection_request') else None
            ),
            # The run that waits on this card, and the run that hosts it (a sub-flow's
            # parent), so a run's own page can offer the decision it is waiting on.
            'workflow_run_id': str(action.get('workflow_run_id') or '') or None,
            'workflow_host_run_id': str(action.get('workflow_chat_host_run_id') or '') or None,
        })
    items.sort(key=lambda item: (item['created_at'], item['message_id']), reverse=True)
    return {'items': items}
