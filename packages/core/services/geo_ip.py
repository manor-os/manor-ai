"""Geo-IP enrichment for session records.

Uses ip-api.com with a Redis cache. GEO_IP_API_KEY selects the commercial
HTTPS endpoint. Legacy session consumers retain their existing HTTP fallback;
new registration lookups require HTTPS and never fall back to plaintext.
The free endpoint does not permit commercial use; configure the key before
enabling this integration in a commercial deployment.

Provider failures return ``None``; Redis failures bypass the cache. Signup
bounds enrichment to two seconds and retains UTC if a timezone is unavailable.
"""
from __future__ import annotations

import ipaddress
import json
import logging
import os
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

logger = logging.getLogger(__name__)


_REDIS_KEY_FMT = "geo:{ip}"
_TTL_SECONDS = int(os.getenv("GEO_IP_CACHE_TTL_SECONDS", str(60 * 60 * 24 * 30)))
_HTTP_TIMEOUT = float(os.getenv("GEO_IP_HTTP_TIMEOUT", "3.0"))
_ENDPOINT = (
    "http://ip-api.com/json/{ip}"
    "?fields=status,country,countryCode,city,lat,lon,timezone"
)


def valid_timezone(value: object) -> str | None:
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        ZoneInfo(value)
        return value
    except (ValueError, ZoneInfoNotFoundError):
        return None


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


async def lookup_geo(ip: Optional[str], *, require_https: bool = False) -> Optional[dict]:
    """Resolve IP to coarse location, coordinates and a validated IANA timezone.

    Returns ``None`` for: missing input, private/loopback IPs, cache + API
    misses, or provider errors. Positive results are cached for 30 days by
    default; misses for at most five minutes, so transient failures can recover.
    """
    if not ip:
        return None
    ip = ip.strip()
    if not ip or _is_private(ip):
        return None

    from packages.core.config import get_settings

    api_key = get_settings().GEO_IP_API_KEY
    if require_https and not api_key:
        return None
    # Never use a plaintext-derived cache entry for registration defaults.
    cache_key = f"geo:https:v2:{ip}" if api_key else _REDIS_KEY_FMT.format(ip=ip)
    client = _redis()
    try:
        return await _lookup_with_cache(ip, api_key, cache_key, client)
    finally:
        # Registration's total timeout can cancel during Redis or HTTP IO.
        await _close(client)


async def _lookup_with_cache(ip: str, api_key: str, cache_key: str, client) -> Optional[dict]:
    cached: Optional[str] = None
    if client is not None:
        try:
            cached = await client.get(cache_key)
        except Exception as exc:
            logger.debug("geo_ip: cache get failed (%s)", type(exc).__name__)

    if cached:
        try:
            parsed = json.loads(cached)
            # Negative cache row → return None without hitting API again.
            if parsed is None or (isinstance(parsed, dict) and parsed.get("__miss")):
                return None
            if isinstance(parsed, dict):
                parsed["timezone"] = valid_timezone(parsed.get("timezone"))
                return parsed
        except json.JSONDecodeError:
            # Corrupted entry — fall through and refetch.
            pass

    payload: Optional[dict] = None
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as http:
            if api_key:
                resp = await http.get(
                    f"https://pro.ip-api.com/json/{ip}",
                    params={"key": api_key, "fields": "status,country,countryCode,city,lat,lon,timezone"},
                )
            else:
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
                    "timezone": valid_timezone(data.get("timezone")),
                }
    except Exception as exc:
        # Don't escalate: callers expect None on any failure.
        # HTTP exceptions can contain the full URL, including an IP and key.
        logger.warning("geo_ip: lookup failed (%s)", type(exc).__name__)

    if client is not None:
        try:
            to_store = json.dumps(payload if payload else {"__miss": True})
            await client.setex(cache_key, _TTL_SECONDS if payload else min(_TTL_SECONDS, 300), to_store)
        except Exception as exc:
            logger.debug("geo_ip: cache write failed (%s)", type(exc).__name__)

    return payload
