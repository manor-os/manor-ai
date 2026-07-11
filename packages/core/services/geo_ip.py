"""Geo-IP enrichment for session records.

Uses ip-api.com's free tier (45 req/min/IP, no key, no commercial-use
restriction in this read-only diagnostic context) with a Redis cache
keyed by IP. The cache is the workhorse: once a user's IP is seen the
result lives for 30 days, so steady-state load is one external call
per new visitor IP per month.

Failure-tolerant by design — every consumer gets ``None`` on timeout,
rate-limit, parse error, or missing Redis. Geo data is never on the
critical path; missing it just means the admin column reads "—".
"""
from __future__ import annotations

import ipaddress
import json
import logging
import os
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


_REDIS_KEY_FMT = "geo:{ip}"
_TTL_SECONDS = int(os.getenv("GEO_IP_CACHE_TTL_SECONDS", str(60 * 60 * 24 * 30)))
_HTTP_TIMEOUT = float(os.getenv("GEO_IP_HTTP_TIMEOUT", "3.0"))
_ENDPOINT = (
    "http://ip-api.com/json/{ip}"
    "?fields=status,country,countryCode,city,lat,lon"
)


def _redis():
    try:
        import redis.asyncio as aioredis
        from packages.core.config import get_settings
        return aioredis.from_url(get_settings().REDIS_URL, decode_responses=True)
    except Exception as exc:
        logger.debug("geo_ip: redis unavailable: %s", exc)
        return None


async def _close(client) -> None:
    if client is None:
        return
    try:
        await client.aclose()
    except Exception:
        pass


def _is_private(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip.strip())
    except ValueError:
        return True
    # Includes loopback, link-local, multicast, unique-local, etc.
    return (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_reserved
        or addr.is_multicast
        or addr.is_unspecified
    )


async def lookup_geo(ip: Optional[str]) -> Optional[dict]:
    """Resolve ``ip`` to ``{country_code, country, city}`` (any field may be empty).

    Returns ``None`` for: missing input, private/loopback IPs, cache + API
    misses, or any error. Result is cached in Redis for 30 days (positive
    AND negative — a negative cache row avoids re-hammering the API for
    the same junk IP).
    """
    if not ip:
        return None
    ip = ip.strip()
    if not ip or _is_private(ip):
        return None

    cache_key = _REDIS_KEY_FMT.format(ip=ip)
    client = _redis()
    cached: Optional[str] = None
    if client is not None:
        try:
            cached = await client.get(cache_key)
        except Exception as exc:
            logger.debug("geo_ip: cache get failed for %s: %s", ip, exc)

    if cached:
        try:
            parsed = json.loads(cached)
            # Negative cache row → return None without hitting API again.
            if parsed is None or parsed.get("__miss"):
                await _close(client)
                return None
            await _close(client)
            return parsed
        except json.JSONDecodeError:
            # Corrupted entry — fall through and refetch.
            pass

    payload: Optional[dict] = None
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as http:
            resp = await http.get(_ENDPOINT.format(ip=ip))
        if resp.status_code == 200:
            data = resp.json()
            if data.get("status") == "success":
                # ip-api returns numeric lat/lon; coerce to float
                # defensively in case they ever change formats.
                try:
                    lat = float(data["lat"]) if data.get("lat") is not None else None
                except (TypeError, ValueError):
                    lat = None
                try:
                    lon = float(data["lon"]) if data.get("lon") is not None else None
                except (TypeError, ValueError):
                    lon = None
                payload = {
                    "country_code": (data.get("countryCode") or "")[:2].upper() or None,
                    "country": (data.get("country") or None),
                    "city": (data.get("city") or None),
                    "latitude": lat,
                    "longitude": lon,
                }
    except Exception as exc:
        # Don't escalate: callers expect None on any failure.
        logger.warning("geo_ip: lookup failed for %s: %s", ip, exc)

    if client is not None:
        try:
            to_store = json.dumps(payload if payload else {"__miss": True})
            await client.setex(cache_key, _TTL_SECONDS, to_store)
        except Exception as exc:
            logger.debug("geo_ip: cache write failed for %s: %s", ip, exc)
        await _close(client)

    return payload
