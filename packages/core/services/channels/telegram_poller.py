"""Telegram long-polling runner.

Keeps one asyncio task per active Telegram bot alive for the life of
the API process. Each task:

  1. Calls ``deleteWebhook`` once so ``getUpdates`` can run (Telegram
     returns 409 if a webhook is registered).
  2. Loops ``getUpdates?offset=<last+1>&timeout=25`` — Telegram holds
     the connection open for up to 25s and returns whatever updates
     arrive. Lightweight: roughly one request per minute when idle.
  3. For each update, enqueues a Celery ``dispatch_inbound_task.delay``
     so the message lands on the same agent pipeline webhook-mode uses.
  4. Persists the max ``update_id + 1`` into
     ``ChannelConfig.config.last_update_id`` so restarts don't replay.

Mode selection (``settings.TELEGRAM_MODE``):
  - ``webhook``  → poller is disabled; webhook path is used
  - ``polling``  → poller manages every active Telegram ChannelConfig
  - ``auto``     → polling when PUBLIC_BASE_URL is not HTTPS, else webhook

This lives in the same process as the API. Single-replica safe; for
multi-replica deployments you'd want leader election or a dedicated
runner service — but at that scale, switch to webhook mode.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, Optional

import httpx
from sqlalchemy import select

from packages.core.config import get_settings
from packages.core.database import async_session
from packages.core.models.channel import ChannelConfig

logger = logging.getLogger(__name__)


_TELEGRAM_API = "https://api.telegram.org"
_POLL_TIMEOUT = 25  # seconds — Telegram's recommended long-poll window


# ── Mode resolution ─────────────────────────────────────────────────────────

def polling_mode_enabled() -> bool:
    """Should this deployment run the polling runner?"""
    s = get_settings()
    mode = (s.TELEGRAM_MODE or "auto").lower()
    if mode == "polling":
        return True
    if mode == "webhook":
        return False
    # auto
    return not s.PUBLIC_BASE_URL.lower().startswith("https://")


# ── Per-bot polling task ────────────────────────────────────────────────────

async def _poll_one_bot(cc_id: str, bot_token: str, stop_event: asyncio.Event) -> None:
    """Long-poll one bot until stop_event is set."""
    # Clear any stale webhook first, otherwise getUpdates returns 409.
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            await c.post(f"{_TELEGRAM_API}/bot{bot_token}/deleteWebhook")
    except Exception:
        logger.debug("Telegram poller: deleteWebhook failed (may be benign)", exc_info=True)

    offset = await _load_offset(cc_id)
    logger.info("Telegram poller: starting config=%s offset=%d", cc_id, offset)

    consecutive_errors = 0
    while not stop_event.is_set():
        try:
            url = (
                f"{_TELEGRAM_API}/bot{bot_token}/getUpdates"
                f"?timeout={_POLL_TIMEOUT}&offset={offset}"
            )
            # Use a timeout slightly longer than Telegram's long-poll so
            # a quiet bot doesn't spin in an error loop.
            async with httpx.AsyncClient(timeout=_POLL_TIMEOUT + 10) as c:
                resp = await c.get(url)
        except (httpx.ReadTimeout, httpx.ConnectTimeout):
            continue
        except Exception as e:
            consecutive_errors += 1
            backoff = min(60.0, 2.0 * consecutive_errors)
            logger.warning(
                "Telegram poller config=%s error: %s — backoff %.1fs",
                cc_id, e, backoff,
            )
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass
            continue

        if resp.status_code != 200:
            consecutive_errors += 1
            backoff = min(60.0, 2.0 * consecutive_errors)
            logger.warning(
                "Telegram poller config=%s HTTP %s: %s — backoff %.1fs",
                cc_id, resp.status_code, resp.text[:200], backoff,
            )
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass
            continue

        consecutive_errors = 0
        body = resp.json() or {}
        if not body.get("ok"):
            logger.warning("Telegram poller config=%s response not ok: %s", cc_id, body)
            continue

        updates = body.get("result") or []
        for upd in updates:
            await _handle_update(cc_id, upd)
            offset = max(offset, int(upd.get("update_id", 0)) + 1)

        if updates:
            await _save_offset(cc_id, offset)


async def _handle_update(cc_id: str, update: Dict[str, Any]) -> None:
    """Normalise a Telegram update and enqueue it via the same path the
    webhook router uses. Reuses TelegramAdapter.handle_update for parsing."""
    try:
        async with async_session() as db:
            cc = (await db.execute(
                select(ChannelConfig).where(ChannelConfig.id == cc_id)
            )).scalar_one_or_none()
        if not cc:
            return

        from packages.core.services.channels.telegram_adapter import TelegramAdapter
        token = (cc.credentials or {}).get("bot_token", "")
        adapter = TelegramAdapter(bot_token=token)
        parsed = await adapter.handle_update(update)
        if not parsed:
            return

        # Log the inbound row exactly like the webhook path does
        from packages.core.services.channel_service import handle_inbound_message
        try:
            async with async_session() as db:
                await handle_inbound_message(
                    db,
                    entity_id=cc.entity_id,
                    channel_config_id=cc.id,
                    payload={
                        "from": parsed["sender_id"],
                        "to": str(parsed.get("chat_id", "")),
                        "content": parsed["content"],
                        "message_id": parsed.get("msg_id"),
                        "channel_type": "telegram",
                        "metadata": {
                            "message_type": parsed["message_type"],
                            "sender_name": parsed.get("sender_name", ""),
                            "chat_id": parsed.get("chat_id"),
                            "source": "long_poll",
                        },
                    },
                )
                await db.commit()
        except Exception:
            logger.exception("Telegram poller: failed to log inbound")

        # Hand to the gateway via the Celery task, same as webhook path
        try:
            from packages.core.tasks.channel_tasks import dispatch_inbound_task
            dispatch_inbound_task.delay(
                entity_id=cc.entity_id,
                channel_config_id=cc.id,
                channel_type="telegram",
                sender_id=str(parsed["sender_id"]),
                sender_name=parsed.get("sender_name"),
                chat_id=str(parsed.get("chat_id") or parsed["sender_id"]),
                content=parsed["content"] or "",
            )
        except Exception:
            logger.exception(
                "Telegram poller: could not enqueue dispatch — Celery down?"
            )
    except Exception:
        logger.exception("Telegram poller: _handle_update crashed")


# ── Offset persistence ──────────────────────────────────────────────────────

async def _load_offset(cc_id: str) -> int:
    async with async_session() as db:
        cc = (await db.execute(
            select(ChannelConfig).where(ChannelConfig.id == cc_id)
        )).scalar_one_or_none()
    if not cc:
        return 0
    try:
        return int((cc.config or {}).get("last_update_id") or 0)
    except Exception:
        return 0


async def _save_offset(cc_id: str, offset: int) -> None:
    async with async_session() as db:
        cc = (await db.execute(
            select(ChannelConfig).where(ChannelConfig.id == cc_id)
        )).scalar_one_or_none()
        if not cc:
            return
        cfg = dict(cc.config or {})
        cfg["last_update_id"] = offset
        cc.config = cfg
        await db.commit()


# ── Runner lifecycle ────────────────────────────────────────────────────────

class TelegramPoller:
    def __init__(self) -> None:
        self._stop = asyncio.Event()
        self._tasks: Dict[str, asyncio.Task] = {}   # cc_id → task
        self._supervisor: Optional[asyncio.Task] = None

    async def start(self) -> None:
        if not polling_mode_enabled():
            logger.info("Telegram poller disabled — TELEGRAM_MODE resolves to webhook.")
            return
        self._stop.clear()
        logger.info("Telegram poller: supervisor starting")
        self._supervisor = asyncio.create_task(self._supervise())

    async def stop(self) -> None:
        self._stop.set()
        tasks = list(self._tasks.values())
        if self._supervisor:
            tasks.append(self._supervisor)
        for t in tasks:
            t.cancel()
        for t in tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        self._tasks.clear()
        logger.info("Telegram poller: stopped")

    async def _supervise(self) -> None:
        """Every 30s, reconcile running tasks with active ChannelConfigs."""
        try:
            while not self._stop.is_set():
                await self._reconcile()
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=30)
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            pass

    async def _reconcile(self) -> None:
        async with async_session() as db:
            rows = (await db.execute(
                select(ChannelConfig).where(
                    ChannelConfig.channel_type == "telegram",
                    ChannelConfig.status == "active",
                )
            )).scalars().all()

        desired: Dict[str, str] = {}
        for cc in rows:
            token = (cc.credentials or {}).get("bot_token")
            if token:
                desired[cc.id] = token

        # Start new, stop removed
        for cc_id, token in desired.items():
            if cc_id not in self._tasks or self._tasks[cc_id].done():
                self._tasks[cc_id] = asyncio.create_task(
                    _poll_one_bot(cc_id, token, self._stop)
                )
        for cc_id in list(self._tasks.keys()):
            if cc_id not in desired:
                self._tasks[cc_id].cancel()
                del self._tasks[cc_id]

    def is_polling(self, cc_id: str) -> bool:
        task = self._tasks.get(cc_id)
        return bool(task and not task.done())


# Global singleton — started/stopped in FastAPI lifespan
poller = TelegramPoller()

# Unused re-export
_ = json
