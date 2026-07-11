"""Ops alert config — admin-editable, Redis-backed.

Stored as a single JSON document at ``ops:config``. Defaults below
match the hardcoded behavior shipped in stage 2; the UI Settings
panel writes overrides on top. ``OPS_EMAIL_RECIPIENTS`` env var still
works as a fallback when ``recipients`` is empty in the config —
lets ops bootstrap email alerts before the UI is touched.

Why Redis (vs. PG): the rest of ops state lives in Redis, no migration
needed, and the surface area is small enough that a JSON blob is
fine. Promote to PG if you ever need audit history of who changed
what threshold when.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

_REDIS_KEY = "ops:config"


# ── Defaults ──────────────────────────────────────────────────────────
# Each rule entry MUST have ``enabled`` and ``cooldown_s``. Optional
# threshold keys per rule:
#
#   disk_full         → threshold_pct        (default 95)
#   queue_lag         → threshold            (default 500)
#   memory_pressure   → threshold_pct, consecutive_ticks (default 90 / 3)
#   error_log_spike   → multiplier, min_abs  (default 3.0 / 5)
#
# Rules without overrides ignore extra keys silently.
DEFAULT_CONFIG: dict[str, Any] = {
    "recipients": [],   # falls back to OPS_EMAIL_RECIPIENTS env var when empty
    "rules": {
        "container_down":   {"enabled": True, "cooldown_s": 1800},
        "oom_killed":       {"enabled": True, "cooldown_s": 1800},
        "disk_full":        {"enabled": True, "cooldown_s": 3600, "threshold_pct": 95},
        "health_deep_down": {"enabled": True, "cooldown_s": 600},
        "restart_loop":     {"enabled": True, "cooldown_s": 900},
        "queue_lag":        {"enabled": True, "cooldown_s": 600, "threshold": 500},
        "memory_pressure":  {"enabled": True, "cooldown_s": 600, "threshold_pct": 90, "consecutive_ticks": 3},
        "error_log_spike":  {"enabled": True, "cooldown_s": 900, "multiplier": 3.0, "min_abs": 5},
    },
}


def _redis():
    try:
        import redis.asyncio as aioredis
        from packages.core.config import get_settings
        return aioredis.from_url(get_settings().REDIS_URL, decode_responses=True)
    except Exception as exc:
        logger.debug("ops_config: redis unavailable: %s", exc)
        return None


async def _close(client) -> None:
    if client is None:
        return
    try:
        await client.aclose()
    except Exception:
        pass


async def load_config() -> dict[str, Any]:
    """Load full config (defaults + Redis overrides merged). Always
    returns a complete shape — UI never has to handle missing keys."""
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy

    r = _redis()
    if r is not None:
        try:
            raw = await r.get(_REDIS_KEY)
            if raw:
                stored = json.loads(raw)
                # Merge top-level recipients
                if isinstance(stored.get("recipients"), list):
                    cfg["recipients"] = stored["recipients"]
                # Merge per-rule overrides (preserve unknown keys for forward-compat)
                for rule_id, overrides in (stored.get("rules") or {}).items():
                    if not isinstance(overrides, dict):
                        continue
                    base = cfg["rules"].get(rule_id, {})
                    base.update(overrides)
                    cfg["rules"][rule_id] = base
        except Exception as exc:
            logger.warning("ops_config load failed, using defaults: %s", exc)
        finally:
            await _close(r)

    # Env-var fallback for recipients (useful before UI is wired)
    if not cfg["recipients"]:
        env = [e.strip() for e in os.environ.get("OPS_EMAIL_RECIPIENTS", "").split(",") if e.strip()]
        if env:
            cfg["recipients"] = env

    return cfg


async def save_config(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate + persist. Returns the merged config that will take
    effect (so the UI can re-render without a follow-up GET)."""
    if not isinstance(payload, dict):
        raise ValueError("config must be an object")

    # Validate recipients
    recipients = payload.get("recipients") or []
    if not isinstance(recipients, list):
        raise ValueError("recipients must be a list of email strings")
    cleaned_recipients = []
    for addr in recipients:
        if not isinstance(addr, str) or "@" not in addr:
            raise ValueError(f"invalid email: {addr!r}")
        cleaned_recipients.append(addr.strip())

    # Validate per-rule overrides — only known rule ids accepted
    rules_in = payload.get("rules") or {}
    if not isinstance(rules_in, dict):
        raise ValueError("rules must be an object keyed by rule id")
    cleaned_rules: dict[str, Any] = {}
    for rule_id, overrides in rules_in.items():
        if rule_id not in DEFAULT_CONFIG["rules"]:
            raise ValueError(f"unknown rule id: {rule_id}")
        if not isinstance(overrides, dict):
            raise ValueError(f"rule {rule_id}: overrides must be an object")
        clean: dict[str, Any] = {}
        if "enabled" in overrides:
            clean["enabled"] = bool(overrides["enabled"])
        if "cooldown_s" in overrides:
            cd = int(overrides["cooldown_s"])
            if cd < 30 or cd > 86400:
                raise ValueError(f"rule {rule_id}: cooldown_s must be 30..86400")
            clean["cooldown_s"] = cd
        # Optional thresholds — accept whatever's present
        for k in ("threshold", "threshold_pct", "consecutive_ticks", "multiplier", "min_abs"):
            if k in overrides:
                clean[k] = overrides[k]
        cleaned_rules[rule_id] = clean

    to_store = {"recipients": cleaned_recipients, "rules": cleaned_rules}

    r = _redis()
    if r is None:
        raise RuntimeError("redis unavailable; cannot persist config")
    try:
        await r.set(_REDIS_KEY, json.dumps(to_store))
    finally:
        await _close(r)

    return await load_config()
