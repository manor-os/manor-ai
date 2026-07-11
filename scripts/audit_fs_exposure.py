#!/usr/bin/env python3
"""Audit and optionally remediate entity-FS URL/document exposure.

Dry-run by default:
  PYTHONPATH=. python scripts/audit_fs_exposure.py

Apply targeted remediation:
  PYTHONPATH=. python scripts/audit_fs_exposure.py --apply --quarantine-hidden-docs --revoke-shares-with-fs-links
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import String, cast, or_, select

from packages.core.database import async_session
from packages.core.models.conversation_share import ConversationShare
from packages.core.models.document import Document
from packages.core.models.task import Message
from packages.core.services.knowledge_visibility import is_user_visible_path


FS_URL_MARKER = "/api/v1/fs/"


def _contains_fs_url(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return FS_URL_MARKER in value
    if isinstance(value, list):
        return any(_contains_fs_url(item) for item in value)
    if isinstance(value, dict):
        return any(_contains_fs_url(item) for item in value.values())
    return False


async def audit(
    *,
    entity_id: str | None,
    apply: bool,
    quarantine_hidden_docs: bool,
    revoke_shares_with_fs_links: bool,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    async with async_session() as db:
        doc_q = select(Document).where(Document.fs_path.isnot(None))
        if entity_id:
            doc_q = doc_q.where(Document.entity_id == entity_id)
        docs = list((await db.execute(doc_q)).scalars().all())
        hidden_docs = [d for d in docs if d.fs_path and not is_user_visible_path(d.fs_path)]

        msg_q = select(Message).where(
            or_(
                Message.content.ilike(f"%{FS_URL_MARKER}%"),
                cast(Message.tool_calls, String).ilike(f"%{FS_URL_MARKER}%"),
                cast(Message.attachments, String).ilike(f"%{FS_URL_MARKER}%"),
            )
        )
        fs_messages = list((await db.execute(msg_q)).scalars().all())

        share_q = select(ConversationShare).where(ConversationShare.is_active.is_(True))
        if entity_id:
            share_q = share_q.where(ConversationShare.entity_id == entity_id)
        shares = list((await db.execute(share_q)).scalars().all())

        shares_with_fs_links: list[ConversationShare] = []
        for share in shares:
            rows = list((await db.execute(
                select(Message).where(Message.conversation_id == share.conversation_id)
            )).scalars().all())
            if any(
                _contains_fs_url(m.content)
                or _contains_fs_url(m.tool_calls)
                or _contains_fs_url(m.attachments)
                for m in rows
            ):
                shares_with_fs_links.append(share)

        print("FS exposure audit")
        print(f"  entity_id: {entity_id or '(all)'}")
        print(f"  mode: {'apply' if apply else 'dry-run'}")
        print(f"  hidden Document rows: {len(hidden_docs)}")
        for doc in hidden_docs[:50]:
            print(f"    doc={doc.id} entity={doc.entity_id} path={doc.fs_path!r} trashed={doc.is_trashed}")
        if len(hidden_docs) > 50:
            print(f"    ... {len(hidden_docs) - 50} more")
        print(f"  messages containing {FS_URL_MARKER}: {len(fs_messages)}")
        print(f"  active shares containing {FS_URL_MARKER}: {len(shares_with_fs_links)}")
        for share in shares_with_fs_links[:50]:
            print(f"    share={share.id} entity={share.entity_id} conversation={share.conversation_id}")
        if len(shares_with_fs_links) > 50:
            print(f"    ... {len(shares_with_fs_links) - 50} more")

        if not apply:
            return

        if quarantine_hidden_docs:
            for doc in hidden_docs:
                meta = dict(doc.metadata_ or {})
                meta["security_quarantined_at"] = now
                meta["security_quarantine_reason"] = "hidden_fs_path_projected_to_document"
                doc.metadata_ = meta
                doc.is_trashed = True
                doc.trashed_at = datetime.now(timezone.utc)
                doc.trashed_by = "security-audit"

        if revoke_shares_with_fs_links:
            for share in shares_with_fs_links:
                share.is_active = False

        await db.commit()
        print("  remediation committed")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--entity-id", help="Limit audit/remediation to one entity")
    parser.add_argument("--apply", action="store_true", help="Commit remediation changes")
    parser.add_argument("--quarantine-hidden-docs", action="store_true", help="Trash Document rows pointing at hidden/system paths")
    parser.add_argument("--revoke-shares-with-fs-links", action="store_true", help="Disable active shared chats containing local FS URLs")
    args = parser.parse_args()
    asyncio.run(audit(
        entity_id=args.entity_id,
        apply=args.apply,
        quarantine_hidden_docs=args.quarantine_hidden_docs,
        revoke_shares_with_fs_links=args.revoke_shares_with_fs_links,
    ))


if __name__ == "__main__":
    main()
