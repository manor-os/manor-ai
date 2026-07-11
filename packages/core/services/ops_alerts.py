"""Ops alert rule engine.

Evaluates a snapshot from ``ops_service.collect_snapshot`` against a
fixed rule set, dedups via Redis cooldown keys, and dispatches alerts
to email + the in-app notification system.

Day 3 ships only the four CRITICAL rules — they're the ones that
warrant paging humans. Warning/info rules and a daily digest land in
later stages.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import httpx

logger = logging.getLogger(__name__)

# Cooldown keys live in Redis under this prefix; TTL is per-rule.
_REDIS_COOLDOWN_PREFIX = "ops:alert:cooldown"
_REDIS_RULE_STATE_PREFIX = "ops:alert:state"

# Per-target state for delta-based rules (restart counts, consecutive
# tick counters). Plain strings — checked once per tick, no TTL.
_REDIS_PREV_RESTART = "ops:alert:prev_restart"   # hash {container: count}
_REDIS_MEM_CONSEC = "ops:alert:mem_consec"        # int counter

# Log spike detection state. ``log_baseline:{container}`` is a sorted
# set of (epoch_score → error_count_string), kept rolling for ~1h.
# ``log_spikes`` is the latest detection map written by the log scanner
# task and read back by the rule check.
_REDIS_LOG_BASELINE = "ops:log_baseline"          # prefix
_REDIS_LOG_SPIKES = "ops:log_spikes"              # hash {container: spike_payload_json}

# Suppressed warning/info events get pushed here; the daily digest
# task drains it.
_REDIS_DIGEST_QUEUE = "ops:digest:queue"

# Auto-resolve: don't fire a "resolved" email until the rule has been
# clear for at least this long (avoids flapping). Same threshold for
# all rules — keep it simple.
_RESOLVE_STABLE_S = 600   # 10 min

# Container name suffixes that exit cleanly by design (one-shot init
# jobs in docker-compose). We never alert on these.
_INIT_SUFFIXES = ("-init",)

# Recipient list is now sourced from the admin-editable Redis config
# (``ops:config``). The OPS_EMAIL_RECIPIENTS env var still works as a
# fallback when the config has no recipients — see ops_config.py.

# Where to ping for health_deep_down — defaults to the in-process API.
# Override via OPS_HEALTH_URL when the alerter is co-located with the
# API but the route differs.
_HEALTH_URL = os.environ.get("OPS_HEALTH_URL", "http://localhost:8000/health/deep")


@dataclass(frozen=True)
class Rule:
    id: str
    severity: str           # critical | warning | info
    description: str
    cooldown_s: int         # how long after a fire before we re-alert
    # ``check`` returns ``(triggered, target, context)``:
    #   - triggered: True if alert should fire
    #   - target: a string used in the cooldown key (container name, "host", etc.)
    #   - context: dict merged into the alert payload (used in email body)
    check: Callable[[dict[str, Any]], list[tuple[str, dict[str, Any]]]]


# ── Rule predicates ────────────────────────────────────────────────────

def _check_container_down(snap: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    out: list[tuple[str, dict[str, Any]]] = []
    for c in snap.get("containers") or []:
        name = c.get("name", "")
        if any(name.endswith(s) for s in _INIT_SUFFIXES):
            continue  # init job — exits cleanly by design
        if c.get("status") != "running":
            # Skip containers that exited cleanly (code 0) AND have never restarted —
            # likely a one-shot job or intentional shutdown.
            if c.get("status") == "exited" and (c.get("exit_code") or 0) == 0 and (c.get("restart_count") or 0) == 0:
                continue
            out.append((name, {
                "status": c.get("status"),
                "exit_code": c.get("exit_code"),
                "restart_count": c.get("restart_count"),
                "image": c.get("image"),
            }))
    return out


def _check_oom_killed(snap: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    out = []
    for c in snap.get("containers") or []:
        if c.get("oom_killed"):
            out.append((c.get("name", "?"), {
                "image": c.get("image"),
                "mem_limit_mb": c.get("mem_limit_mb"),
            }))
    return out


def _check_disk_full(snap: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    host = snap.get("host") or {}
    disk = host.get("disk") or {}
    pct = disk.get("pct") or 0
    threshold = float(_rule_cfg(snap, "disk_full").get("threshold_pct", 95))
    if pct >= threshold:
        return [("host", {"disk_pct": pct, "threshold": threshold, "free_gb": disk.get("free_gb"), "total_gb": disk.get("total_gb")})]
    return []


def _check_restart_loop(snap: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """Container restart_count increased since the last tick.

    Uses ``snap['__state__']['prev_restart']`` (hash {container: count})
    pre-loaded by the dispatcher, and writes the new map back to
    ``snap['__state_writes__']['prev_restart']`` for the dispatcher to
    persist after evaluation. Keeps rule functions sync + I/O-free.
    """
    state = snap.get("__state__") or {}
    prev_map: dict[str, str] = state.get("prev_restart") or {}

    out: list[tuple[str, dict[str, Any]]] = []
    new_map: dict[str, str] = {}
    for c in snap.get("containers") or []:
        name = c.get("name", "")
        if not name or any(name.endswith(s) for s in _INIT_SUFFIXES):
            continue
        cur = int(c.get("restart_count") or 0)
        new_map[name] = str(cur)
        prev_str = prev_map.get(name)
        if prev_str is not None:
            prev = int(prev_str)
            if cur > prev:
                out.append((name, {
                    "delta": cur - prev,
                    "current": cur,
                    "previous": prev,
                    "image": c.get("image"),
                }))
    snap.setdefault("__state_writes__", {})["prev_restart"] = new_map
    return out


def _check_queue_lag(snap: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    threshold = int(_rule_cfg(snap, "queue_lag").get("threshold", 500))
    out = []
    for name, depth in (snap.get("queues") or {}).items():
        if int(depth or 0) >= threshold:
            out.append((f"queue:{name}", {"depth": depth, "threshold": threshold}))
    return out


def _check_memory_pressure(snap: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """Host memory ≥ threshold% for N consecutive ticks. Both knobs
    come from config. Counter pre-loaded at ``snap['__state__']['mem_consec']``;
    writes new value to ``snap['__state_writes__']['mem_consec']``.
    """
    cfg = _rule_cfg(snap, "memory_pressure")
    threshold = float(cfg.get("threshold_pct", 90))
    needed = int(cfg.get("consecutive_ticks", 3))
    state = snap.get("__state__") or {}
    cur = int(state.get("mem_consec") or 0)
    pct = ((snap.get("host") or {}).get("mem") or {}).get("pct") or 0
    new_val = cur + 1 if pct >= threshold else 0
    snap.setdefault("__state_writes__", {})["mem_consec"] = new_val
    if new_val >= needed:
        return [("host", {"mem_pct": pct, "threshold": threshold, "consecutive_ticks": new_val})]
    return []


def _check_error_log_spike(snap: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """Container error rate ≥ 3× the rolling 1h baseline.

    Spike payload is precomputed by ``ops.log_scan`` and pre-loaded
    into ``snap['__state__']['log_spikes']`` by ``_preload_state``,
    keyed by container name.
    """
    state = snap.get("__state__") or {}
    spikes_raw = state.get("log_spikes") or {}
    out = []
    for name, payload_str in spikes_raw.items():
        try:
            p = json.loads(payload_str)
        except Exception:
            continue
        out.append((name, p))
    return out


def _check_health_deep_down(_snap: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """Sync-style probe of /health/deep. Runs inside the alerter Celery
    task on its own thread, so we use the sync httpx client rather than
    creating an async session."""
    try:
        with httpx.Client(timeout=5.0) as client:
            r = client.get(_HEALTH_URL)
            if r.status_code >= 500:
                payload = {}
                try:
                    payload = r.json()
                except Exception:
                    payload = {"body": r.text[:500]}
                return [("api", {"status_code": r.status_code, **payload})]
    except Exception as exc:
        # Endpoint unreachable counts as down.
        return [("api", {"error": str(exc)})]
    return []


CRITICAL_RULES: list[Rule] = [
    Rule(
        id="container_down",
        severity="critical",
        description="A non-init container is not running",
        cooldown_s=1800,  # 30 min — don't spam during long outages
        check=_check_container_down,
    ),
    Rule(
        id="oom_killed",
        severity="critical",
        description="Container was OOM-killed",
        cooldown_s=1800,
        check=_check_oom_killed,
    ),
    Rule(
        id="disk_full",
        severity="critical",
        description="Host disk usage at or above 95%",
        cooldown_s=3600,
        check=_check_disk_full,
    ),
    Rule(
        id="health_deep_down",
        severity="critical",
        description="/health/deep returned 5xx (DB / Redis / FS broken)",
        cooldown_s=600,
        check=_check_health_deep_down,
    ),
]

WARNING_RULES: list[Rule] = [
    Rule(
        id="restart_loop",
        severity="warning",
        description="Container restarted since last tick",
        cooldown_s=900,
        check=_check_restart_loop,
    ),
    Rule(
        id="queue_lag",
        severity="warning",
        description="Celery queue depth ≥ 500",
        cooldown_s=600,
        check=_check_queue_lag,
    ),
    Rule(
        id="memory_pressure",
        severity="warning",
        description="Host memory ≥ 90% for 3 consecutive ticks",
        cooldown_s=600,
        check=_check_memory_pressure,
    ),
    Rule(
        id="error_log_spike",
        severity="warning",
        description="Container error rate ≥ 3× rolling 1h baseline",
        cooldown_s=900,
        check=_check_error_log_spike,
    ),
]

ALL_RULES: list[Rule] = CRITICAL_RULES + WARNING_RULES


# ── Alert dispatch ─────────────────────────────────────────────────────

async def _redis():
    """Open a fresh Redis client for the current event loop.

    The shared ``cache._get_redis()`` singleton caches its client across
    ``asyncio.run()`` calls. Inside Celery each task gets a new event
    loop, so the cached client raises ``Event loop is closed`` on the
    second tick. A fresh per-call client avoids that — caller closes
    via ``aclose()``.
    """
    try:
        import redis.asyncio as aioredis
        from packages.core.config import get_settings
        return aioredis.from_url(get_settings().REDIS_URL, decode_responses=True)
    except Exception as exc:
        logger.debug("ops_alerts: redis unavailable: %s", exc)
        return None


async def _close(client) -> None:
    if client is None:
        return
    try:
        await client.aclose()
    except Exception:
        pass


async def _is_in_cooldown(rule_id: str, target: str) -> bool:
    r = await _redis()
    if r is None:
        return False
    try:
        return bool(await r.get(f"{_REDIS_COOLDOWN_PREFIX}:{rule_id}:{target}"))
    except Exception:
        return False
    finally:
        await _close(r)


async def _set_cooldown(rule_id: str, target: str, ttl: int) -> None:
    r = await _redis()
    if r is None:
        return
    try:
        await r.setex(f"{_REDIS_COOLDOWN_PREFIX}:{rule_id}:{target}", ttl, "1")
        # Also record the firing state so a follow-up tick can detect resolve.
        await r.hset(f"{_REDIS_RULE_STATE_PREFIX}:{rule_id}:{target}",
                     mapping={"firing": "1", "since": str(int(time.time()))})
    except Exception:
        pass
    finally:
        await _close(r)


def _format_subject(rule: Rule, target: str) -> str:
    return f"[Manor Ops] {rule.severity.upper()} — {rule.id} on {target}"


def _format_body(rule: Rule, target: str, context: dict[str, Any], snap: dict[str, Any]) -> tuple[str, str]:
    """Returns (text_body, html_body)."""
    lines = [
        f"Severity:    {rule.severity.upper()}",
        f"Rule:        {rule.id}",
        f"Target:      {target}",
        f"Detected:    {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}",
        "",
        rule.description,
        "",
        "Context:",
    ]
    for k, v in context.items():
        lines.append(f"  {k}: {v}")
    host = snap.get("host") or {}
    lines.extend([
        "",
        "Host:",
        f"  cpu_pct={host.get('cpu_pct')}  mem_pct={(host.get('mem') or {}).get('pct')}  disk_pct={(host.get('disk') or {}).get('pct')}",
    ])
    text = "\n".join(lines)
    html = "<pre style='font-family: ui-monospace, monospace; font-size: 13px;'>" + text + "</pre>"
    return text, html


def _config_recipients(snap: dict[str, Any]) -> list[str]:
    """Pull recipients from the preloaded config, falling back to []."""
    cfg = (snap.get("__state__") or {}).get("config") or {}
    return cfg.get("recipients") or []


async def _send_email_alert(rule: Rule, target: str, context: dict[str, Any], snap: dict[str, Any]) -> None:
    recipients = _config_recipients(snap)
    if not recipients:
        logger.debug("ops config has no recipients — skipping email for %s/%s", rule.id, target)
        return
    try:
        from packages.core.services.email_service import send_email
    except Exception as exc:
        logger.warning("email_service unavailable: %s", exc)
        return

    subject = _format_subject(rule, target)
    text, html = _format_body(rule, target, context, snap)
    for addr in recipients:
        try:
            ok = await send_email(addr, subject, html, text_body=text)
            if not ok:
                logger.warning("ops alert email to %s failed (send_email returned False)", addr)
        except Exception as exc:
            logger.warning("ops alert email to %s failed: %s", addr, exc)


async def _record_event(rule: Rule, target: str, context: dict[str, Any]) -> None:
    """Append to a rolling 7-day Redis sorted set for the alerts UI."""
    r = await _redis()
    if r is None:
        return
    try:
        now = int(time.time())
        await r.zadd(
            f"ops:events:{target}:{rule.id}",
            {json.dumps({"ts": now, "severity": rule.severity, "context": context}): now},
        )
        # Trim to last 7 days
        await r.zremrangebyscore(f"ops:events:{target}:{rule.id}", 0, now - 7 * 86400)
    except Exception as exc:
        logger.debug("record_event failed: %s", exc)
    finally:
        await _close(r)


async def _preload_state(snap: dict[str, Any]) -> None:
    """Read all state needed by stateful rules into ``snap['__state__']``
    so rule check functions stay sync + I/O-free."""
    state: dict[str, Any] = {}
    r = await _redis()
    if r is not None:
        try:
            state["prev_restart"] = await r.hgetall(_REDIS_PREV_RESTART) or {}
            state["mem_consec"] = await r.get(_REDIS_MEM_CONSEC) or "0"
            state["log_spikes"] = await r.hgetall(_REDIS_LOG_SPIKES) or {}
        except Exception as exc:
            logger.debug("state preload failed: %s", exc)
        finally:
            await _close(r)
    # Admin-editable rule + recipient config (UI-managed).
    try:
        from packages.core.services.ops_config import load_config
        state["config"] = await load_config()
    except Exception as exc:
        logger.debug("config load failed, using defaults: %s", exc)
        state["config"] = {"recipients": [], "rules": {}}
    snap["__state__"] = state


def _rule_cfg(snap: dict[str, Any], rule_id: str) -> dict[str, Any]:
    return ((snap.get("__state__") or {}).get("config") or {}).get("rules", {}).get(rule_id) or {}


# ── Log spike detection ────────────────────────────────────────────────

# Floor on raw count so a low-traffic container doesn't trip on
# 0-baseline → 1 ERROR (which is technically infinite-x).
_SPIKE_MIN_ABS = 5
_SPIKE_MULTIPLIER = 3.0
_BASELINE_WINDOW_S = 3600   # 1h rolling baseline


async def _config_log_spike_params() -> tuple[float, int]:
    """Pull the multiplier + min_abs from config (with defaults). Used
    by the log scanner task which runs outside the alert tick — it
    needs to fetch config itself rather than reading from snap."""
    try:
        from packages.core.services.ops_config import load_config
        cfg = await load_config()
        rule = (cfg.get("rules") or {}).get("error_log_spike") or {}
        return float(rule.get("multiplier", _SPIKE_MULTIPLIER)), int(rule.get("min_abs", _SPIKE_MIN_ABS))
    except Exception:
        return _SPIKE_MULTIPLIER, _SPIKE_MIN_ABS


async def update_log_baselines_and_publish_spikes(
    counts_per_container: dict[str, int],
) -> dict[str, dict[str, Any]]:
    """Update rolling baselines + write a spike map to Redis for the
    rule engine to consume. Called by ``ops.log_scan`` Celery task.

    Returns the spike map for callers / logs (also written to
    ``ops:log_spikes`` for the rule check).
    """
    spikes: dict[str, dict[str, Any]] = {}
    multiplier, min_abs = await _config_log_spike_params()
    r = await _redis()
    if r is None:
        return spikes
    try:
        now = int(time.time())
        cutoff = now - _BASELINE_WINDOW_S
        for name, count in counts_per_container.items():
            key = f"{_REDIS_LOG_BASELINE}:{name}"
            # Trim the window
            await r.zremrangebyscore(key, 0, cutoff)
            # Read prior counts (excluding this tick) for baseline
            prior = await r.zrange(key, 0, -1, withscores=False) or []
            prior_counts = []
            for item in prior:
                # values are strings like "5" — sometimes "5:tickid"
                try:
                    prior_counts.append(int(str(item).split(":")[0]))
                except Exception:
                    continue
            mean = (sum(prior_counts) / len(prior_counts)) if prior_counts else 0.0

            # Append this tick to the rolling set. We tag the value
            # with ``count:now`` so duplicate counts don't collide
            # (sorted-set members must be unique).
            await r.zadd(key, {f"{count}:{now}": now})
            await r.expire(key, _BASELINE_WINDOW_S * 2)

            # Spike detection
            spike_threshold = max(mean * multiplier, float(min_abs))
            if count >= min_abs and count >= spike_threshold and mean > 0:
                spikes[name] = {
                    "count": count,
                    "baseline_mean": round(mean, 2),
                    "multiplier": round(count / mean, 2) if mean else None,
                    "samples": len(prior_counts),
                }

        # Publish the spike map (replaces any prior — only the latest tick matters)
        await r.delete(_REDIS_LOG_SPIKES)
        if spikes:
            await r.hset(
                _REDIS_LOG_SPIKES,
                mapping={name: json.dumps(payload) for name, payload in spikes.items()},
            )
            # TTL so a stale spike doesn't keep firing if the scanner stops
            await r.expire(_REDIS_LOG_SPIKES, 300)
    except Exception as exc:
        logger.debug("update_log_baselines failed: %s", exc)
    finally:
        await _close(r)

    return spikes


def update_log_baselines_sync(counts: dict[str, int]) -> dict[str, dict[str, Any]]:
    return asyncio.run(update_log_baselines_and_publish_spikes(counts))


# ── Ack ────────────────────────────────────────────────────────────────

# When a user acks an alert, extend the cooldown to this many seconds
# so they stop getting emails for that target/rule. Auto-resolve still
# fires when the underlying condition clears.
_ACK_DURATION_S = 4 * 3600


async def ack_alert(rule_id: str, target: str) -> bool:
    """Extend cooldown to ``_ACK_DURATION_S`` and mark as acked.
    Returns True if the alert existed."""
    r = await _redis()
    if r is None:
        return False
    try:
        cooldown_key = f"{_REDIS_COOLDOWN_PREFIX}:{rule_id}:{target}"
        state_key = f"{_REDIS_RULE_STATE_PREFIX}:{rule_id}:{target}"
        # Only ack things that are currently firing (state key present)
        exists = await r.exists(state_key)
        if not exists:
            return False
        await r.setex(cooldown_key, _ACK_DURATION_S, "1")
        await r.hset(state_key, mapping={"acked": "1", "acked_at": str(int(time.time()))})
        return True
    finally:
        await _close(r)


async def list_active_alerts() -> list[dict[str, Any]]:
    """All currently-firing alerts (one per ``ops:alert:state:*`` key)."""
    out: list[dict[str, Any]] = []
    r = await _redis()
    if r is None:
        return out
    try:
        async for key in r.scan_iter(f"{_REDIS_RULE_STATE_PREFIX}:*"):
            parts = key.split(":")
            if len(parts) < 5:
                continue
            rule_id = parts[3]
            target = ":".join(parts[4:])
            data = await r.hgetall(key) or {}
            cooldown_ttl = await r.ttl(f"{_REDIS_COOLDOWN_PREFIX}:{rule_id}:{target}")
            out.append({
                "id": f"{rule_id}:{target}",
                "rule_id": rule_id,
                "target": target,
                "since": int(data.get("since") or 0),
                "acked": data.get("acked") == "1",
                "acked_at": int(data.get("acked_at") or 0) or None,
                "cooldown_seconds_left": int(cooldown_ttl) if cooldown_ttl and cooldown_ttl > 0 else 0,
            })
    finally:
        await _close(r)
    out.sort(key=lambda a: a["since"], reverse=True)
    return out


async def _persist_state_writes(snap: dict[str, Any]) -> None:
    writes = snap.get("__state_writes__") or {}
    if not writes:
        return
    r = await _redis()
    if r is None:
        return
    try:
        if "prev_restart" in writes:
            new_map = writes["prev_restart"]
            # Clear old hash + write fresh — handles containers that
            # disappeared between ticks.
            await r.delete(_REDIS_PREV_RESTART)
            if new_map:
                await r.hset(_REDIS_PREV_RESTART, mapping=new_map)
        if "mem_consec" in writes:
            await r.set(_REDIS_MEM_CONSEC, str(writes["mem_consec"]))
    except Exception as exc:
        logger.debug("state persist failed: %s", exc)
    finally:
        await _close(r)


async def _push_to_digest(rule: Rule, target: str, context: dict[str, Any]) -> None:
    """Append a suppressed firing to the daily-digest queue. Critical
    rules don't go here — they always email immediately. Only warning/
    info rules feed the digest."""
    if rule.severity == "critical":
        return
    r = await _redis()
    if r is None:
        return
    try:
        payload = json.dumps({
            "ts": int(time.time()),
            "rule_id": rule.id,
            "severity": rule.severity,
            "target": target,
            "context": context,
        })
        await r.rpush(_REDIS_DIGEST_QUEUE, payload)
        # Cap the queue to keep the digest sendable in one email.
        await r.ltrim(_REDIS_DIGEST_QUEUE, -500, -1)
    except Exception as exc:
        logger.debug("digest push failed: %s", exc)
    finally:
        await _close(r)


async def _detect_resolves(snap: dict[str, Any], rules: list[Rule]) -> list[str]:
    """For every (rule, target) currently flagged as firing in Redis,
    check whether the rule still matches that target. If not, AND the
    rule has been firing for ≥ ``_RESOLVE_STABLE_S``, send a resolve
    email and clear the firing/cooldown state."""
    resolved: list[str] = []
    r = await _redis()
    if r is None:
        return resolved

    # Build a fast {(rule_id, target)} set for currently-matching rules
    matching: set[tuple[str, str]] = set()
    rule_by_id: dict[str, Rule] = {rule.id: rule for rule in rules}
    for rule in rules:
        try:
            for target, _ctx in rule.check(snap):
                matching.add((rule.id, target))
        except Exception:
            continue  # don't let a flaky rule block resolve detection

    try:
        keys = []
        async for key in r.scan_iter(f"{_REDIS_RULE_STATE_PREFIX}:*"):
            keys.append(key)
        now = int(time.time())
        for key in keys:
            # key format: ops:alert:state:{rule_id}:{target}
            parts = key.split(":")
            if len(parts) < 5:
                continue
            rule_id = parts[3]
            target = ":".join(parts[4:])
            if (rule_id, target) in matching:
                continue  # still firing
            rule = rule_by_id.get(rule_id)
            if rule is None:
                # Rule retired — silently clean up.
                await r.delete(key, f"{_REDIS_COOLDOWN_PREFIX}:{rule_id}:{target}")
                continue
            try:
                state_data = await r.hgetall(key) or {}
                since = int(state_data.get("since") or 0)
            except Exception:
                since = 0
            if since == 0 or (now - since) < _RESOLVE_STABLE_S:
                continue
            # Resolved.
            await r.delete(key, f"{_REDIS_COOLDOWN_PREFIX}:{rule_id}:{target}")
            await _send_resolve_email(rule, target, snap, since)
            resolved.append(f"{rule_id}/{target}")
    except Exception as exc:
        logger.debug("resolve scan failed: %s", exc)
    finally:
        await _close(r)
    return resolved


async def _send_resolve_email(rule: Rule, target: str, snap: dict[str, Any], since: int) -> None:
    recipients = _config_recipients(snap)
    if not recipients:
        return
    try:
        from packages.core.services.email_service import send_email
    except Exception:
        return
    duration_min = max(1, (int(time.time()) - since) // 60)
    subject = f"[Manor Ops] RESOLVED — {rule.id} on {target}"
    text = (
        f"Rule {rule.id} on {target} has cleared.\n\n"
        f"Duration: ~{duration_min} min\n"
        f"Resolved at: {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}\n"
    )
    html = "<pre style='font-family: ui-monospace, monospace; font-size: 13px;'>" + text + "</pre>"
    for addr in recipients:
        try:
            await send_email(addr, subject, html, text_body=text)
        except Exception:
            pass


async def evaluate_and_dispatch(snap: dict[str, Any], rules: list[Rule] | None = None) -> dict[str, Any]:
    """Walk the rule set against ``snap``. For each match:
      - if in cooldown → push to digest queue (warning/info only)
      - else → set cooldown, record event, send email immediately

    After firing-phase, scan currently-firing rules and emit resolves
    for any that have cleared for ≥ 10 min.
    """
    rules = rules if rules is not None else ALL_RULES
    fired: list[str] = []
    suppressed: list[str] = []
    errors: list[str] = []

    await _preload_state(snap)

    for rule in rules:
        cfg = _rule_cfg(snap, rule.id)
        if cfg.get("enabled") is False:
            continue   # admin disabled this rule via /admin/ops settings
        cooldown_s = int(cfg.get("cooldown_s", rule.cooldown_s))
        try:
            matches = rule.check(snap)
        except Exception as exc:
            logger.warning("rule %s check failed: %s", rule.id, exc, exc_info=True)
            errors.append(f"{rule.id}:{exc}")
            continue
        for target, context in matches:
            if await _is_in_cooldown(rule.id, target):
                suppressed.append(f"{rule.id}/{target}")
                await _push_to_digest(rule, target, context)
                continue
            await _set_cooldown(rule.id, target, cooldown_s)
            await _record_event(rule, target, context)
            await _send_email_alert(rule, target, context, snap)
            fired.append(f"{rule.id}/{target}")

    await _persist_state_writes(snap)

    resolved = await _detect_resolves(snap, rules)

    return {"fired": fired, "suppressed": suppressed, "resolved": resolved, "errors": errors}


def evaluate_and_dispatch_sync(snap: dict[str, Any]) -> dict[str, Any]:
    """Sync entry point for Celery tasks (which can't await directly)."""
    return asyncio.run(evaluate_and_dispatch(snap))


# ── Daily digest ───────────────────────────────────────────────────────

async def send_digest() -> dict[str, Any]:
    """Drain the digest queue + email a summary. Called daily by
    ``ops.send_digest`` Celery beat. Empty queue → no email sent.
    """
    r = await _redis()
    if r is None:
        return {"ok": False, "error": "redis_unavailable"}
    try:
        # LRANGE then DELETE — atomic enough for our purposes (digest
        # is daily; one missed event in a beat-skew window is fine).
        raw_items = await r.lrange(_REDIS_DIGEST_QUEUE, 0, -1) or []
        if raw_items:
            await r.delete(_REDIS_DIGEST_QUEUE)
    except Exception as exc:
        logger.warning("digest drain failed: %s", exc)
        return {"ok": False, "error": str(exc)}
    finally:
        await _close(r)

    if not raw_items:
        logger.info("ops digest: empty queue, no email sent")
        return {"ok": True, "events": 0, "emailed": False}

    if not _OPS_RECIPIENTS:
        logger.info("ops digest: %d events, OPS_EMAIL_RECIPIENTS unset — skipping email", len(raw_items))
        return {"ok": True, "events": len(raw_items), "emailed": False}

    # Bucket by severity → rule_id → list of targets
    buckets: dict[str, dict[str, list[dict[str, Any]]]] = {"warning": {}, "info": {}}
    for raw in raw_items:
        try:
            ev = json.loads(raw)
        except Exception:
            continue
        sev = ev.get("severity", "warning")
        sev_bucket = buckets.setdefault(sev, {})
        sev_bucket.setdefault(ev.get("rule_id", "?"), []).append(ev)

    lines = [f"Digest of suppressed ops alerts (last 24h): {len(raw_items)} events", ""]
    for sev in ("warning", "info"):
        sev_bucket = buckets.get(sev) or {}
        total = sum(len(v) for v in sev_bucket.values())
        lines.append(f"{sev.upper()} ({total})")
        for rule_id, events in sorted(sev_bucket.items()):
            targets = sorted({e.get("target", "?") for e in events})
            lines.append(f"  • {rule_id}: {len(events)} suppressed across {len(targets)} target(s)")
            for t in targets[:5]:
                lines.append(f"      - {t}")
        lines.append("")

    text = "\n".join(lines)
    html = "<pre style='font-family: ui-monospace, monospace; font-size: 13px;'>" + text + "</pre>"
    subject = f"[Manor Ops] Daily Digest — {len(raw_items)} suppressed events"

    try:
        from packages.core.services.email_service import send_email
        for addr in _OPS_RECIPIENTS:
            await send_email(addr, subject, html, text_body=text)
    except Exception as exc:
        logger.warning("digest email failed: %s", exc)
        return {"ok": False, "events": len(raw_items), "error": str(exc)}

    return {"ok": True, "events": len(raw_items), "emailed": True}


def send_digest_sync() -> dict[str, Any]:
    return asyncio.run(send_digest())
