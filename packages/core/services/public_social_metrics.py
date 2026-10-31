"""Best-effort, unauthenticated public-post metric collection.

This module deliberately reads only the public HTML delivered by the platform.
It does not run JavaScript, use cookies, or attempt to bypass login, captcha, or
regional gates. Callers must treat unavailable public counters as unavailable,
not as zero.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import re
from typing import Any
from urllib.parse import urlsplit

import httpx

from packages.core.services.dashboard_http import (
    DashboardHttpError,
    create_public_https_transport,
    resolve_public_https_target,
)
from packages.core.services.youtube_public_video import fetch_youtube_public_metrics


_PARSER_VERSION = "public-social-v1"
_INSTAGRAM_HOSTS = frozenset({"instagram.com", "www.instagram.com"})
_TIKTOK_HOSTS = frozenset({"tiktok.com", "www.tiktok.com"})
_YOUTUBE_HOSTS = frozenset({"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"})
_COUNT_RE = re.compile(r"(?P<number>\d+(?:\.\d+)?)\s*(?P<suffix>[kKmMbB])?\Z")


class PublicSocialMetricsError(ValueError):
    """A stable error for unsafe URLs or failed public page reads."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _metric(
    value: int | None,
    *,
    raw_value: str | None = None,
    availability: str = "available",
    reason: str | None = None,
    provider_metric_name: str,
    precision: str = "exact",
) -> dict[str, Any]:
    return {
        "value": value,
        "precision": precision,
        "raw_value": raw_value,
        "availability": availability,
        "reason": reason,
        "provider_metric_name": provider_metric_name,
    }


def _unavailable(name: str, reason: str, *, blocked: bool = False) -> dict[str, Any]:
    return _metric(
        None,
        availability="blocked" if blocked else "unavailable",
        reason=reason,
        provider_metric_name=name,
    )


def _parse_count(value: Any) -> tuple[int | None, str, str | None]:
    if isinstance(value, bool) or value is None:
        return None, "exact", None
    if isinstance(value, int):
        return (value, "exact", str(value)) if value >= 0 else (None, "exact", None)
    text = str(value).strip().replace(",", "")
    match = _COUNT_RE.fullmatch(text)
    if match is None:
        return None, "exact", text or None
    amount = float(match.group("number"))
    suffix = (match.group("suffix") or "").lower()
    multiplier = {"": 1, "k": 1_000, "m": 1_000_000, "b": 1_000_000_000}[suffix]
    return max(0, int(round(amount * multiplier))), ("rounded" if suffix else "exact"), text


def canonicalize_public_post_url(platform: str, public_url: str) -> tuple[str, str]:
    """Validate a supported public post URL and return its stable identity."""
    parsed = urlsplit(str(public_url or "").strip())
    try:
        port = parsed.port
    except ValueError as exc:
        raise PublicSocialMetricsError("invalid_url", "A public post URL has an invalid port") from exc
    if parsed.username is not None or parsed.password is not None or port is not None:
        raise PublicSocialMetricsError("invalid_url", "A public post URL must not include credentials or a port")
    host = (parsed.hostname or "").lower()
    parts = [part for part in parsed.path.split("/") if part]
    if parsed.scheme != "https":
        raise PublicSocialMetricsError("invalid_url", "A public HTTPS post URL is required")

    if platform == "instagram":
        # Instagram also links posts under the author: /<username>/reel/<code>/.
        # The post code is global, so both forms share one canonical URL.
        if len(parts) == 3 and re.fullmatch(r"[A-Za-z0-9._]{1,30}", parts[0]) and parts[1] in {"p", "reel"}:
            parts = parts[1:]
        if host not in _INSTAGRAM_HOSTS or len(parts) != 2 or parts[0] not in {"p", "reel"}:
            raise PublicSocialMetricsError("invalid_url", "An Instagram post or Reel URL is required")
        post_id = parts[1]
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", post_id):
            raise PublicSocialMetricsError("invalid_post_id", "The Instagram post ID is invalid")
        return f"https://www.instagram.com/{parts[0]}/{post_id}/", post_id

    if platform == "tiktok":
        if host not in _TIKTOK_HOSTS or len(parts) != 3 or not parts[0].startswith("@") or parts[1] not in {"video", "photo"}:
            raise PublicSocialMetricsError("invalid_url", "A TikTok video or photo post URL is required")
        post_id = parts[2]
        if not re.fullmatch(r"\d{1,32}", post_id):
            raise PublicSocialMetricsError("invalid_post_id", "The TikTok post ID is invalid")
        return f"https://www.tiktok.com/{parts[0]}/{parts[1]}/{post_id}", post_id

    if platform == "youtube":
        if host not in _YOUTUBE_HOSTS:
            raise PublicSocialMetricsError("invalid_url", "A YouTube URL is required")
        from packages.core.services.youtube_public_video import canonicalize_youtube_public_url

        return canonicalize_youtube_public_url(public_url)
    raise PublicSocialMetricsError("unsupported_platform", "Only youtube, instagram, and tiktok are supported")


async def _fetch_public_html(url: str) -> tuple[str, str]:
    try:
        target = await resolve_public_https_target(url)
    except DashboardHttpError as exc:
        raise PublicSocialMetricsError("unsafe_url", "Only public HTTPS pages are available") from exc
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(15.0, connect=7.0),
            follow_redirects=False,
            trust_env=False,
            transport=create_public_https_transport(target),
            headers={
                "Accept": "text/html,application/xhtml+xml",
                "User-Agent": "Manor-Public-Metrics/1.0",
            },
        ) as client:
            async with client.stream("GET", target.url) as response:
                if 300 <= response.status_code < 400:
                    raise PublicSocialMetricsError("redirect", "Public post redirects are not followed")
                if response.status_code in {401, 403, 429}:
                    raise PublicSocialMetricsError("access_blocked", "The public post cannot be read without access")
                response.raise_for_status()
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > 1_000_000:
                        raise PublicSocialMetricsError("response_too_large", "Public post page is too large")
                    chunks.append(chunk)
                return b"".join(chunks).decode(response.encoding or "utf-8", errors="replace"), target.url
    except PublicSocialMetricsError:
        raise
    except (httpx.HTTPError, OSError) as exc:
        raise PublicSocialMetricsError("fetch_failed", "Public post page could not be fetched") from exc


def _iter_json_values(html: str) -> list[Any]:
    """Decode JSON roots embedded in a public page without evaluating page script."""
    decoder = json.JSONDecoder()
    values: list[Any] = []
    offset = 0
    while offset < len(html):
        start = min((index for index in (html.find("{", offset), html.find("[", offset)) if index >= 0), default=-1)
        if start < 0:
            break
        try:
            value, end = decoder.raw_decode(html, start)
        except json.JSONDecodeError:
            offset = start + 1
            continue
        values.append(value)
        offset = end
    return values


def _iter_dicts(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _iter_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _iter_dicts(child)


def _url_contains_post_id(value: Any, canonical_url: str) -> bool:
    if not isinstance(value, str):
        return False
    try:
        candidate = urlsplit(value)
        target = urlsplit(canonical_url)
    except ValueError:
        return False
    return (
        candidate.scheme == "https"
        and (candidate.hostname or "").lower() == (target.hostname or "").lower()
        and candidate.path.rstrip("/") == target.path.rstrip("/")
    )


def _is_target_post_object(platform: str, item: dict[str, Any], post_id: str, canonical_url: str) -> bool:
    if platform == "instagram":
        return any(str(item.get(key) or "") == post_id for key in ("shortcode", "code", "media_code")) or any(
            _url_contains_post_id(item.get(key), canonical_url) for key in ("permalink", "url", "share_url")
        )
    return any(str(item.get(key) or "") == post_id for key in ("id", "itemId", "item_id", "video_id", "aweme_id"))


def _target_post_object(platform: str, post_id: str, canonical_url: str, html: str) -> dict[str, Any] | None:
    for root in _iter_json_values(html):
        for item in _iter_dicts(root):
            if _is_target_post_object(platform, item, post_id, canonical_url):
                return item
    return None


def _first_object_count(item: dict[str, Any], names: tuple[str, ...]) -> tuple[int | None, str, str | None, str]:
    """Read only the matched post and its direct metric containers."""
    containers = [item]
    containers.extend(
        child
        for child in item.values()
        if isinstance(child, dict) and child is not item.get("related") and child is not item.get("recommendations")
    )
    for name in names:
        for container in containers:
            raw = container.get(name)
            if isinstance(raw, dict):
                raw = raw.get("count")
            value, precision, raw_text = _parse_count(raw)
            if value is not None:
                return value, precision, raw_text, name
    return None, "exact", None, names[0]


def _account_evidence(platform: str, item: dict[str, Any]) -> tuple[str | None, str | None]:
    owner = next(
        (item.get(key) for key in ("owner", "author", "user", "userInfo") if isinstance(item.get(key), dict)), {}
    )
    account_id = next(
        (
            str(value)
            for value in (
                item.get("owner_id"),
                item.get("author_id"),
                item.get("ig_user_id"),
                owner.get("id"),
                owner.get("uid"),
                owner.get("uniqueId"),
            )
            if value is not None and str(value)
        ),
        None,
    )
    handle = next(
        (
            str(value).lstrip("@")
            for value in (
                item.get("username"),
                item.get("user_name"),
                item.get("uniqueId"),
                owner.get("username"),
                owner.get("user_name"),
                owner.get("uniqueId"),
            )
            if value is not None and str(value)
        ),
        None,
    )
    return account_id, handle


def _blocked_page(html: str) -> str | None:
    lower = html.lower()
    if "captcha" in lower or "verify you are human" in lower:
        return "captcha_required"
    if "log in" in lower and ("instagram" in lower or "tiktok" in lower):
        return "login_required"
    if "private account" in lower or "video unavailable" in lower or "content unavailable" in lower:
        return "post_unavailable"
    return None


def parse_public_post_page(
    *,
    platform: str,
    post_id: str,
    canonical_url: str,
    html: str,
    expected_account_id: str | None,
    expected_account_handle: str | None,
) -> dict[str, Any]:
    blocked = None
    evidence = {
        "page_sha256": hashlib.sha256(html.encode("utf-8", errors="replace")).hexdigest(),
        "identity_verified": False,
        "account_identity_verified": None,
    }
    item = _target_post_object(platform, post_id, canonical_url, html)
    if item is None:
        # A public page may include a Log in navigation link while still
        # exposing this post. A banner alone does not invalidate its data.
        blocked = _blocked_page(html) or "post_identity_unverified"
    else:
        evidence["identity_verified"] = True
        account_id, account_handle = _account_evidence(platform, item)
        if expected_account_id:
            evidence["expected_account_id"] = expected_account_id
            evidence["account_identity_verified"] = account_id == expected_account_id
        if expected_account_handle:
            evidence["expected_account_handle"] = expected_account_handle.lstrip("@")
            handle_matches = (
                account_handle is not None and account_handle.lower() == expected_account_handle.lstrip("@").lower()
            )
            evidence["account_identity_verified"] = (
                handle_matches
                if evidence["account_identity_verified"] is None
                else evidence["account_identity_verified"] and handle_matches
            )
        if expected_account_id or expected_account_handle:
            if evidence["account_identity_verified"] is not True:
                blocked = blocked or "account_identity_unverified"
        if account_id:
            evidence["account_id"] = account_id
        if account_handle:
            evidence["account_handle"] = account_handle

    if platform == "instagram":
        definitions = {
            "views": ("play_count", "video_view_count", "view_count"),
            "likes": ("like_count", "edge_media_preview_like"),
            "comments": ("comment_count", "edge_media_to_comment"),
        }
    else:
        definitions = {
            "views": ("playCount", "viewCount"),
            "likes": ("diggCount", "likeCount"),
            "comments": ("commentCount",),
            "shares": ("shareCount",),
        }
    metrics: dict[str, dict[str, Any]] = {}
    for key, names in definitions.items():
        if blocked:
            metrics[key] = _unavailable(names[0], blocked, blocked=True)
            continue
        value, precision, raw, source_name = (
            _first_object_count(item, names) if item else (None, "exact", None, names[0])
        )
        metrics[key] = (
            _metric(value, raw_value=raw, precision=precision, provider_metric_name=source_name)
            if value is not None
            else _unavailable(source_name, "not_publicly_exposed")
        )
    return {
        "platform": platform,
        "external_post_id": post_id,
        "canonical_url": canonical_url,
        "collected_at": _now(),
        "source": "public_page",
        "parser_version": _PARSER_VERSION,
        "evidence": evidence,
        "metrics": metrics,
    }


def _unreadable_result(*, platform: str, post_id: str, canonical_url: str, reason: str) -> dict[str, Any]:
    names = (
        {"views": "play_count", "likes": "like_count", "comments": "comment_count"}
        if platform == "instagram"
        else {"views": "playCount", "likes": "diggCount", "comments": "commentCount", "shares": "shareCount"}
    )
    return {
        "platform": platform,
        "external_post_id": post_id,
        "canonical_url": canonical_url,
        "collected_at": _now(),
        "source": "public_page",
        "parser_version": _PARSER_VERSION,
        "evidence": {"identity_verified": False},
        "metrics": {key: _unavailable(name, reason, blocked=reason == "access_blocked") for key, name in names.items()},
    }


async def collect_public_post_metrics(
    *,
    platform: str,
    public_url: str,
    expected_post_id: str,
    expected_account_id: str | None = None,
    expected_account_handle: str | None = None,
) -> dict[str, Any]:
    """Collect best-effort public counters for one verified post URL."""
    platform = str(platform or "").lower()
    canonical_url, post_id = canonicalize_public_post_url(platform, public_url)
    if str(expected_post_id or "") != post_id:
        raise PublicSocialMetricsError("identity_mismatch", "Public URL does not match the expected post ID")
    if platform == "youtube":
        metrics = await fetch_youtube_public_metrics(canonical_url, expected_video_id=post_id)
        channel_id = str(metrics.channel_id or "")
        channel_name = str(metrics.channel_name or "").lstrip("@")
        if expected_account_id and channel_id != expected_account_id:
            raise PublicSocialMetricsError(
                "identity_mismatch", "YouTube video channel does not match the selected account"
            )
        if expected_account_handle and channel_name.lower() != expected_account_handle.lstrip("@").lower():
            raise PublicSocialMetricsError(
                "identity_mismatch", "YouTube video channel does not match the selected handle"
            )
        return {
            "platform": platform,
            "external_post_id": post_id,
            "canonical_url": canonical_url,
            "collected_at": metrics.collected_at,
            "source": "public_page",
            "parser_version": _PARSER_VERSION,
            "evidence": {
                "channel_id": metrics.channel_id,
                "channel_name": metrics.channel_name,
                "identity_verified": True,
                "account_identity_verified": (True if expected_account_id or expected_account_handle else None),
            },
            "metrics": {
                key: _metric(
                    getattr(metrics, key),
                    raw_value=str(getattr(metrics, key)) if getattr(metrics, key) is not None else None,
                    availability="available" if getattr(metrics, key) is not None else "unavailable",
                    reason=None if getattr(metrics, key) is not None else "not_publicly_exposed",
                    provider_metric_name=key,
                    precision=metrics.metric_precision.get(key, "exact"),
                )
                for key in ("views", "likes", "comments")
            },
        }
    try:
        html, _ = await _fetch_public_html(canonical_url)
    except PublicSocialMetricsError as exc:
        if exc.code in {"access_blocked", "fetch_failed", "response_too_large"}:
            return _unreadable_result(
                platform=platform,
                post_id=post_id,
                canonical_url=canonical_url,
                reason=exc.code,
            )
        raise
    return parse_public_post_page(
        platform=platform,
        post_id=post_id,
        canonical_url=canonical_url,
        html=html,
        expected_account_id=expected_account_id,
        expected_account_handle=expected_account_handle,
    )
