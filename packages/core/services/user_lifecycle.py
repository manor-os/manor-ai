"""User account soft-delete + cascade lifecycle.

Mirrors the workspace soft-delete flow in ``entity_service`` but with
extra cleanup steps unique to user accounts:

  * Best-effort OAuth token revocation (Google + GitHub today; quiet
    no-op for any provider without a known revoke endpoint — token row
    is deleted on hard purge regardless).
  * Cascade-delete the parent Entity when the user being deleted is
    the sole remaining ``owner`` / ``admin`` (otherwise an org with no
    admins would be unmanageable). Stripe subscription is cancelled
    too; the customer record is preserved for audit + dispute history.
  * Anonymization on hard purge — every ``created_by`` / ``user_id``
    row pointing at the user is rewritten to ``__deleted_user__`` (for
    string-typed columns) or ``NULL`` (for FK-shaped String(26) ids),
    rather than blanket-deleting the row. This preserves workspace
    history while applying privacy-oriented pseudonymization.

Grace window
────────────
Soft-deleted users stay restorable for ``USER_PURGE_GRACE_DAYS`` days
(env-overridable, default 30). After that the
``ops.purge_soft_deleted_users`` Celery task runs the hard delete.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
from sqlalchemy import delete as sa_delete, func, or_, select, update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.user import Entity, OAuthAccount, User

logger = logging.getLogger(__name__)


USER_PURGE_GRACE_DAYS = int(os.getenv("USER_PURGE_GRACE_DAYS", "30"))

# Sentinel string columns get this when a user is purged.
DELETED_USER_SENTINEL = "__deleted_user__"


# ── Soft-delete + restore ───────────────────────────────────────────────────

async def soft_delete_user(
    db: AsyncSession, user_id: str,
) -> dict:
    """Mark a user account as deleted and trigger immediate side
    effects: revoke OAuth tokens, cancel Stripe subscription if this
    user is the sole admin of their entity, and cascade-soft-delete
    the entity + all its workspaces in that case.

    Returns ``{"user_id", "entity_cascaded": bool, "oauth_revoked": int}``.
    """
    user = await db.get(User, user_id)
    if not user or user.deleted_at is not None:
        return {"user_id": user_id, "entity_cascaded": False, "oauth_revoked": 0}

    now = datetime.now(timezone.utc)
    from packages.core.models.workspace_collaboration import WorkspaceCollaboration
    from packages.core.services.external_collaboration_access import (
        lock_access_grant_workspaces,
        sync_access_grants,
    )
    from sqlalchemy import or_
    grant_conditions = (
        WorkspaceCollaboration.user_id == user_id, WorkspaceCollaboration.authorized_by == user_id,
        WorkspaceCollaboration.requested_by == user_id, WorkspaceCollaboration.email == user.email.casefold(),
    )
    # Workspace locks precede the User and grant writes below; derived
    # membership/seat rows are cleared while user_id is still on the grant.
    access_grant_ids = await lock_access_grant_workspaces(db, *grant_conditions)
    user.deleted_at = now
    await db.execute(sa_update(WorkspaceCollaboration).where(or_(*grant_conditions))
        .values(status="revoked", snapshot={}))
    await sync_access_grants(db, access_grant_ids)

    # Best-effort OAuth revocation — running in foreground so the
    # caller (the API request handler) gets a deterministic answer.
    revoked = await _revoke_user_oauth_tokens(db, user_id)

    cascade_to_entity = await _is_sole_active_admin(db, user)
    if cascade_to_entity:
        await _cascade_delete_entity(db, user.entity_id, now)

    await db.flush()
    return {
        "user_id": user_id,
        "entity_cascaded": cascade_to_entity,
        "oauth_revoked": revoked,
    }


async def restore_user(
    db: AsyncSession, user_id: str,
) -> Optional[User]:
    """Undo a soft-delete inside the grace window. Returns the
    restored User row or ``None`` if not found / already purged.

    Note: an Entity that was cascade-soft-deleted alongside this user
    is also restored as part of this call — without it the user would
    log back in to a missing org.
    """
    result = await db.execute(
        select(User).where(User.id == user_id, User.deleted_at.is_not(None))
    )
    user = result.scalar_one_or_none()
    if not user:
        return None
    user.deleted_at = None

    entity = await db.get(Entity, user.entity_id)
    if entity and entity.deleted_at is not None:
        # Same logic as workspaces: restoring brings the cascaded
        # entity (and its workspaces) back too. Anything that was
        # hard-purged is gone for good.
        entity.deleted_at = None
        from packages.core.models.workspace import Workspace
        await db.execute(
            sa_update(Workspace)
            .where(
                Workspace.entity_id == entity.id,
                Workspace.deleted_at.is_not(None),
            )
            .values(deleted_at=None)
        )

    await db.flush()
    await db.refresh(user)
    return user


# ── List helpers ────────────────────────────────────────────────────────────

async def list_users_due_for_purge(
    db: AsyncSession, *, grace_days: int = USER_PURGE_GRACE_DAYS,
) -> list[User]:
    """Soft-deleted users past the grace window — fed to the nightly
    purge Celery task."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=grace_days)
    result = await db.execute(
        select(User).where(
            User.deleted_at.is_not(None),
            User.deleted_at < cutoff,
        )
    )
    return list(result.scalars().all())


# ── Hard purge ──────────────────────────────────────────────────────────────

async def purge_user(db: AsyncSession, user_id: str) -> bool:
    """Hard-delete a user and their attribution. Anonymizes content
    rows (so team history is preserved), drops OAuth tokens, then
    deletes the user row itself.

    If the user was the sole admin and the entity was cascade-soft-
    deleted, the nightly workspace purge task will hard-delete the
    entity's workspaces. Entity rows themselves get hard-deleted
    here when no active users remain.
    """
    user = await db.get(User, user_id)
    if not user:
        return False

    entity_id = user.entity_id
    from packages.core.models.apify_collection import ApifyCollection
    from packages.core.services.apify_collections import guard_collection_purge

    # User credentials and provider run identities cannot disappear before
    # outstanding collection costs have been reconciled. Include company
    # memberships, not just this user's primary entity.
    collection_entities = set((await db.execute(
        select(ApifyCollection.entity_id).where(ApifyCollection.user_id == user_id)
    )).scalars())
    remaining_active = (await db.execute(select(func.count(User.id)).where(
        User.entity_id == entity_id, User.id != user_id, User.deleted_at.is_(None),
    ))).scalar_one() or 0
    entity = await db.get(Entity, entity_id)
    deleting_entity = remaining_active == 0 and entity is not None and entity.deleted_at is not None
    # A user account must not disappear while recoverable personal Knowledge
    # still points at it. Entity owners/global transfer managers first move the
    # content to entity ownership through the explicit offboarding action.
    # The only exception is a fully deleted entity, whose data is being purged
    # as one lifecycle unit.
    from packages.core.models.document import Document, DocumentFolder

    document_owner_query = select(func.count(Document.id)).where(
        Document.owner_id == user_id,
    )
    folder_owner_query = select(func.count(DocumentFolder.id)).where(
        DocumentFolder.owner_id == user_id,
    )
    if deleting_entity:
        document_owner_query = document_owner_query.where(Document.entity_id != entity_id)
        folder_owner_query = folder_owner_query.where(DocumentFolder.entity_id != entity_id)
    owned_document_count = (await db.execute(document_owner_query)).scalar_one() or 0
    owned_folder_count = (await db.execute(folder_owner_query)).scalar_one() or 0
    if owned_document_count or owned_folder_count:
        logger.warning(
            "Deferring user purge for %s: %d documents and %d folders still require ownership transfer",
            user_id,
            owned_document_count,
            owned_folder_count,
        )
        return False
    if deleting_entity:
        collection_entities.add(entity_id)
    for collection_entity_id in sorted(collection_entities):
        if not await guard_collection_purge(
            db, entity_id=collection_entity_id,
            user_id=None if deleting_entity and collection_entity_id == entity_id else user_id,
        ):
            return False
    from packages.core.models.chat_feedback import ChatMessageFeedback
    from packages.core.models.channel import TwilioVoiceCallSession
    from packages.core.models.product_growth import ProductGrowthEvent
    from packages.core.models.runtime_learning import RuntimeEvidence
    from packages.core.models.task import Conversation, Message
    from packages.core.models.agent_delegation_session import AgentDelegationSession
    from packages.core.models.agent_delegation_session import AgentContextArchive
    from packages.core.models.runtime_run import RuntimeRun
    await db.execute(sa_update(RuntimeRun).where(RuntimeRun.user_id == user_id, RuntimeRun.entity_id == entity_id).values(context_state=None))
    await db.execute(
        sa_delete(AgentContextArchive).where(
            AgentContextArchive.runtime_run_id.in_(select(RuntimeRun.id).where(RuntimeRun.user_id == user_id, RuntimeRun.entity_id == entity_id))
        )
    )
    from packages.core.services.chat_feedback import (
        COMPLETION_FEEDBACK_EVIDENCE_TYPES,
    )

    feedback_message_ids = select(ChatMessageFeedback.message_id).where(
        ChatMessageFeedback.user_id == user_id
    )
    await db.execute(
        select(Message.id)
        .join(Conversation, Conversation.id == Message.conversation_id)
        .where(
            Conversation.entity_id == entity_id,
            Message.id.in_(feedback_message_ids),
        )
        .order_by(Message.id)
        .with_for_update(of=Message)
    )
    # Match feedback persistence's Message → feedback → evidence lock order.
    await db.execute(
        sa_delete(ChatMessageFeedback).where(ChatMessageFeedback.user_id == user_id)
    )
    await db.execute(
        sa_delete(RuntimeEvidence).where(
            RuntimeEvidence.user_id == user_id,
            RuntimeEvidence.evidence_type.in_(
                COMPLETION_FEEDBACK_EVIDENCE_TYPES
            ),
        )
    )
    await db.execute(
        sa_delete(ProductGrowthEvent).where(ProductGrowthEvent.user_id == user_id)
    )
    await db.execute(
        sa_delete(AgentDelegationSession).where(AgentDelegationSession.user_id == user_id)
    )
    await db.execute(
        sa_delete(TwilioVoiceCallSession).where(
            TwilioVoiceCallSession.owner_user_id == user_id
        )
    )
    await _anonymize_user_references(db, user_id)
    from packages.core.models.webchat_resident import WebchatResidentBinding
    await db.execute(sa_delete(WebchatResidentBinding).where(
        (WebchatResidentBinding.manor_user_id == user_id)
        | WebchatResidentBinding.target_account_id.in_(
            select(OAuthAccount.id).where(OAuthAccount.user_id == user_id)
        )
    ))
    await db.execute(sa_delete(OAuthAccount).where(OAuthAccount.user_id == user_id))
    await db.delete(user)
    await db.flush()

    # If the entity has no remaining active users, hard-delete it too
    # (it was already soft-deleted at the cascade step).
    remaining = (await db.execute(
        select(func.count(User.id)).where(
            User.entity_id == entity_id,
            User.deleted_at.is_(None),
        )
    )).scalar_one() or 0
    if remaining == 0:
        entity = (await db.execute(
            select(Entity)
            .where(Entity.id == entity_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )).scalar_one_or_none()
        if entity is not None and entity.deleted_at is not None:
            # Workspace purge retains Knowledge. Terminal Entity deletion
            # removes that Knowledge together with the company's integrations.
            from packages.core.models.document import Integration
            await db.execute(
                sa_delete(TwilioVoiceCallSession).where(
                    TwilioVoiceCallSession.entity_id == entity_id
                )
            )
            await db.execute(
                sa_delete(Integration).where(Integration.entity_id == entity_id)
            )
            # Only actual Entity hard deletion removes shared recovery state;
            # purging one member must leave other members' file journals intact.
            from packages.core.models.runtime_file_commit import RuntimeFileCommit
            await db.execute(sa_delete(RuntimeFileCommit).where(
                RuntimeFileCommit.entity_id == entity_id,
            ))
            from packages.core.services.workspace_artifact_purge import (
                purge_entity_knowledge,
            )
            await purge_entity_knowledge(db, entity_id)
            await db.delete(entity)
            await db.flush()
    return True


# ── OAuth revocation ────────────────────────────────────────────────────────

async def _revoke_user_oauth_tokens(
    db: AsyncSession, user_id: str,
) -> int:
    """Best-effort revoke at each provider. Failures are logged but
    don't block the soft-delete — the row is also deleted on hard
    purge regardless."""
    accounts = list((await db.execute(
        select(OAuthAccount).where(OAuthAccount.user_id == user_id)
    )).scalars().all())
    if not accounts:
        return 0

    revoked = 0
    from packages.core.services.oauth_account_credentials import lease_oauth_account_tokens

    async with httpx.AsyncClient(timeout=8.0) as cx:
        for acct in accounts:
            try:
                creds = lease_oauth_account_tokens(
                    acct,
                    requester_id=user_id,
                    reason="oauth.account_delete_revoke",
                )
                ok = await _revoke_oauth_token(
                    cx, acct.provider, creds.get("access_token", ""),
                )
                if ok:
                    revoked += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "oauth revoke failed for user=%s provider=%s: %s",
                    user_id, acct.provider, exc,
                )
    return revoked


async def _revoke_oauth_token(
    cx: httpx.AsyncClient, provider: str, access_token: str,
) -> bool:
    """Hit the provider-specific revoke endpoint. Quiet no-op for
    providers we don't have a known endpoint for — the row gets dropped
    on hard purge regardless, so the worst case is a token that stays
    valid until natural expiry."""
    if not access_token:
        return False
    if provider in ("google", "google_calendar", "google_drive", "gmail"):
        r = await cx.post(
            "https://oauth2.googleapis.com/revoke",
            data={"token": access_token},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        return r.is_success
    if provider == "github":
        # GitHub revoke needs the OAuth app's basic auth, which lives
        # in env. If it's not configured, fall through silently.
        client_id = os.getenv("GITHUB_CLIENT_ID")
        client_secret = os.getenv("GITHUB_CLIENT_SECRET")
        if not (client_id and client_secret):
            return False
        r = await cx.delete(
            f"https://api.github.com/applications/{client_id}/grant",
            auth=(client_id, client_secret),
            json={"access_token": access_token},
        )
        return r.is_success
    if provider == "linkedin":
        r = await cx.post(
            "https://www.linkedin.com/oauth/v2/revoke",
            data={
                "token": access_token,
                "client_id": os.getenv("LINKEDIN_CLIENT_ID", ""),
                "client_secret": os.getenv("LINKEDIN_CLIENT_SECRET", ""),
            },
        )
        return r.is_success
    # Slack, Notion, X/Twitter, etc. — no standard revoke endpoint or
    # one we haven't wired up. Best-effort skip.
    return False


# ── Sole-admin / Entity cascade ─────────────────────────────────────────────

async def _is_sole_active_admin(db: AsyncSession, user: User) -> bool:
    """True if this user is the only remaining ``owner`` / ``admin``
    of their entity. We count peers that are NOT soft-deleted and have
    a privileged role. The user themselves is excluded from the count
    (they're the one being soft-deleted)."""
    from packages.core.models.user import UserMembership
    from packages.core.permissions import (
        effective_user_role_name,
        resolve_effective_user_role_name,
    )

    if await effective_user_role_name(db, user) not in ("owner", "admin"):
        return False
    peers = list((await db.execute(
        select(User).where(
            User.id != user.id,
            User.status == "active",
            User.deleted_at.is_(None),
            or_(
                User.entity_id == user.entity_id,
                User.id.in_(
                    select(UserMembership.user_id).where(
                        UserMembership.entity_id == user.entity_id,
                        UserMembership.status == "active",
                        UserMembership.deleted_at.is_(None),
                    )
                ),
            ),
        )
    )).scalars().all())
    for peer in peers:
        if await resolve_effective_user_role_name(
            db,
            user_id=peer.id,
            entity_id=user.entity_id,
        ) in ("owner", "admin"):
            return False
    return True


async def _cascade_delete_entity(
    db: AsyncSession, entity_id: str, now: datetime,
) -> None:
    """Soft-delete the entity and every workspace under it."""
    entity = await db.get(Entity, entity_id, populate_existing=True)
    if entity is None or entity.deleted_at is not None:
        return
    entity.deleted_at = now

    from packages.core.models.workspace import Workspace
    await db.execute(
        sa_update(Workspace)
        .where(
            Workspace.entity_id == entity_id,
            Workspace.deleted_at.is_(None),
        )
        .values(deleted_at=now)
    )




# ── Anonymization ───────────────────────────────────────────────────────────

async def _anonymize_user_references(
    db: AsyncSession, user_id: str,
) -> None:
    """Replace every reference to ``user_id`` across content tables
    with the deleted-user sentinel (or NULL on FK-shaped columns).
    Run during hard purge.

    Designed to be safe-to-rerun — UPDATE WHERE user_id = X is
    idempotent.

    Audit log rows are intentionally NOT anonymized: those are
    forensic records and a separate compliance discussion. If privacy
    requirements get stricter we'll add a per-row policy later.
    """
    from packages.core.models.workspace_collaboration import WorkspaceCollaboration, WorkspaceCollaborationEntry
    from packages.core.services.external_collaboration_access import (
        lock_access_grant_workspaces,
        sync_access_grants,
    )
    from sqlalchemy import or_
    grant_conditions = (
        WorkspaceCollaboration.user_id == user_id, WorkspaceCollaboration.authorized_by == user_id,
        WorkspaceCollaboration.requested_by == user_id,
    )
    # Clear derived access before user_id is nulled below.
    access_grant_ids = await lock_access_grant_workspaces(db, *grant_conditions)
    await db.execute(sa_update(WorkspaceCollaboration).where(WorkspaceCollaboration.id.in_(access_grant_ids))
        .values(status="revoked"))
    await sync_access_grants(db, access_grant_ids)
    await db.execute(sa_update(WorkspaceCollaboration).where(or_(*grant_conditions)).values(status="revoked", snapshot={}, email="deleted-user@invalid.test", user_id=None, requested_by=None, authorized_by=None))
    await db.execute(sa_update(WorkspaceCollaborationEntry).where(WorkspaceCollaborationEntry.created_by == user_id)
        .values(created_by="deleted-user"))

    # ── String-typed display-name columns → sentinel ──
    # ``TaskLog.created_by`` is a String(100) author label (used for
    # both system + user actions); rewrite to the deleted-user sentinel
    # so audit trails read coherently.
    try:
        from packages.core.models.task import TaskLog
        await db.execute(
            sa_update(TaskLog)
            .where(TaskLog.created_by == user_id)
            .values(created_by=DELETED_USER_SENTINEL)
        )
    except ImportError:
        pass
    try:
        from packages.core.models.document import Document
        await db.execute(
            sa_update(Document)
            .where(Document.created_by == user_id)
            .values(created_by=DELETED_USER_SENTINEL)
        )
    except ImportError:
        pass
    try:
        from packages.core.models.document_version import DocumentVersion
        await db.execute(
            sa_update(DocumentVersion)
            .where(DocumentVersion.created_by == user_id)
            .values(created_by=DELETED_USER_SENTINEL)
        )
    except ImportError:
        pass

    # ── FK-shaped String(26) user-id columns → NULL ──
    # Importing inside the function keeps the module's import surface
    # light and avoids circulars during model registration.
    fk_columns = []
    try:
        from packages.core.models.task import Task as _T, Conversation as _C
        fk_columns.append((_T, "creator_id"))
        fk_columns.append((_T, "assignee_id"))
        fk_columns.append((_C, "user_id"))
    except ImportError:
        pass
    try:
        from packages.core.models.task import Message as _M
        fk_columns.append((_M, "resolved_by_user_id"))
    except ImportError:
        pass
    try:
        from packages.core.models.comment import Comment
        fk_columns.append((Comment, "user_id"))
    except ImportError:
        pass
    try:
        from packages.core.models.channel import Channel
        fk_columns.append((Channel, "user_id"))
    except ImportError:
        pass
    try:
        from packages.core.models.memory import AgentMemory
        fk_columns.append((AgentMemory, "user_id"))
    except ImportError:
        pass
    try:
        from packages.core.models.notification import Notification
        fk_columns.append((Notification, "user_id"))
    except ImportError:
        pass
    try:
        from packages.core.models.favorite import Favorite
        fk_columns.append((Favorite, "user_id"))
    except ImportError:
        pass
    try:
        from packages.core.models.scheduler import ScheduledJob
        fk_columns.append((ScheduledJob, "user_id"))
    except ImportError:
        pass
    try:
        from packages.core.models.workspace import WorkspaceActivity
        fk_columns.append((WorkspaceActivity, "user_id"))
    except ImportError:
        pass


    for model, col in fk_columns:
        col_attr = getattr(model, col, None)
        if col_attr is None:
            continue
        await db.execute(
            sa_update(model).where(col_attr == user_id).values({col: None})
        )
