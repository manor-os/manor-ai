"""Inbox sources for the morning briefing.

Pluggable per provider. Each ``InboxSource`` returns a list of
``InboxSignal`` dicts the triage prompt can ingest. Adding Slack DMs,
Calendar conflicts, Stripe events, etc. = one new file here +
register() call.

For Demo B v0, only Gmail is wired. Sandbox workspaces use the Gmail
adapter's ``simulate_tool`` so the briefing can be exercised without
real OAuth + quota.
"""
from __future__ import annotations

import base64
import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional, Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.credentials import Requester, get_credential_service
from packages.core.models.document import Integration

logger = logging.getLogger(__name__)


# ── Source contract ──────────────────────────────────────────────────

InboxSignal = dict  # narrow alias — see fields produced below


class InboxSource(Protocol):
    """Implementations:

      * resolve their own integration row from (entity_id, provider)
      * lease credentials via CredentialService
      * call the relevant MCP adapter (live or simulate)
      * return a list of plain dicts:
          {source, source_ref, sender, subject, body_text, received_at, snippet}
        the triage prompt can read.
    """
    name: str
    """Source key — matches the integration ``provider``."""

    async def fetch(
        self,
        db: AsyncSession,
        entity_id: str,
        *,
        execution_mode: str = "live",
        max_n: int = 10,
    ) -> list[InboxSignal]: ...


_REGISTRY: dict[str, InboxSource] = {}


def register(source: InboxSource) -> None:
    _REGISTRY[source.name] = source


def get_source(name: str) -> Optional[InboxSource]:
    return _REGISTRY.get(name)


def supported_sources() -> list[str]:
    return sorted(_REGISTRY.keys())


# ── Gmail source ─────────────────────────────────────────────────────

class GmailInboxSource:
    """Gmail-backed inbox source. For sandbox / dry_run executions
    routes to the adapter's ``simulate_tool`` — operator can test the
    briefing pipeline without real Gmail."""

    name = "gmail"

    async def fetch(
        self,
        db: AsyncSession,
        entity_id: str,
        *,
        execution_mode: str = "live",
        max_n: int = 10,
    ) -> list[InboxSignal]:
        from packages.core.ai.mcp import gmail as adapter

        if execution_mode in ("dry_run", "sandbox"):
            list_envelope = await adapter.simulate_tool(
                "list_messages", {"query": "is:unread", "max_results": max_n},
            )
        else:
            integration = await _resolve_integration(db, entity_id, "gmail")
            if integration is None:
                logger.info("briefing: no Gmail integration for entity %s", entity_id)
                return []
            token = await _lease_token(integration)
            if not token:
                logger.warning("briefing: Gmail integration has no usable token")
                return []
            list_envelope = await adapter.call_tool(
                "list_messages",
                {"query": "is:unread", "max_results": max_n},
                token,
            )

        listing = _parse_envelope(list_envelope)
        if not listing or "messages" not in listing:
            return []

        signals: list[InboxSignal] = []
        for entry in listing["messages"][:max_n]:
            msg_id = entry["id"]
            if execution_mode in ("dry_run", "sandbox"):
                msg_envelope = await adapter.simulate_tool(
                    "get_message", {"message_id": msg_id},
                )
            else:
                msg_envelope = await adapter.call_tool(
                    "get_message", {"message_id": msg_id}, token,
                )
            msg = _parse_envelope(msg_envelope)
            if msg is None:
                continue
            signals.append(_normalise_gmail_message(msg))
        return signals


def _normalise_gmail_message(msg: dict) -> InboxSignal:
    """Extract sender/subject/body from a Gmail v1 ``get_message`` envelope."""
    headers = {
        h.get("name", "").lower(): h.get("value", "")
        for h in (msg.get("payload", {}).get("headers") or [])
    }
    body_b64 = msg.get("payload", {}).get("body", {}).get("data", "")
    body_text = ""
    if body_b64:
        try:
            body_text = base64.urlsafe_b64decode(body_b64.encode("ascii")).decode("utf-8")
        except Exception:
            body_text = ""

    return {
        "source": "gmail",
        "source_ref": msg["id"],
        "thread_ref": msg.get("threadId"),
        "sender": headers.get("from", ""),
        "subject": headers.get("subject", ""),
        "received_at": headers.get("date", ""),
        "snippet": msg.get("snippet", ""),
        "body_text": body_text,
        "labels": list(msg.get("labelIds") or []),
    }


# ── Helpers ──────────────────────────────────────────────────────────

def _parse_envelope(envelope: dict) -> Optional[dict]:
    """MCP envelopes wrap text content; the inner JSON is what we need."""
    if envelope.get("isError"):
        return None
    for block in envelope.get("content", []):
        text = block.get("text", "")
        if not text:
            continue
        try:
            return json.loads(text)
        except (TypeError, ValueError):
            return None
    return None


async def _resolve_integration(
    db: AsyncSession, entity_id: str, provider: str,
) -> Optional[Integration]:
    return (await db.execute(
        select(Integration).where(
            Integration.entity_id == entity_id,
            Integration.provider == provider,
            Integration.status == "active",
        ).order_by(Integration.created_at.desc()).limit(1)
    )).scalar_one_or_none()


async def _lease_token(integration: Integration) -> str:
    creds = get_credential_service().lease_integration(
        integration,
        requester=Requester(kind="briefing", id=integration.id),
        reason="briefing:fetch_inbox",
    )
    return (
        creds.get("access_token")
        or creds.get("bearer_token")
        or creds.get("token")
        or ""
    )


# Auto-register Gmail.
register(GmailInboxSource())
