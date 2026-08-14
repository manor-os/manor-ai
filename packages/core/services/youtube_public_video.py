"""Read-only access to metrics embedded in a public YouTube watch page."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import re
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx


_ALLOWED_YOUTUBE_HOSTS = frozenset(
    {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"}
)
_VIDEO_ID_RE = re.compile(r"[A-Za-z0-9_-]{11}")
PUBLIC_YOUTUBE_HEADERS = {
    "Accept-Language": "en-US,en;q=0.8",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/127.0.0.0 Safari/537.36"
    ),
}


@dataclass(frozen=True)
class YouTubePublicVideoMetrics:
    video_id: str
    public_url: str
    title: str
    channel_id: str
    channel_name: str
    subscribers: int | None
    published_at: str
    views: int | None
    likes: int | None
    comments: int | None
    unavailable_fields: tuple[str, ...]
    collected_at: str


class YouTubePublicVideoError(ValueError):
    """A stable, user-reportable public-page failure."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def canonicalize_youtube_public_url(url: str) -> tuple[str, str]:
    """Validate a public YouTube URL and return its canonical watch URL and ID."""

    parsed = urlsplit(str(url or "").strip())
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or host not in _ALLOWED_YOUTUBE_HOSTS:
        raise YouTubePublicVideoError(
            "invalid_url",
            "A public HTTPS YouTube watch URL is required",
        )

    if host == "youtu.be":
        video_id = parsed.path.strip("/")
    elif parsed.path.rstrip("/") == "/watch":
        video_id = parse_qs(parsed.query).get("v", [""])[0]
    else:
        video_id = ""

    if not _VIDEO_ID_RE.fullmatch(video_id):
        raise YouTubePublicVideoError(
            "invalid_video_id",
            "The YouTube video ID is invalid",
        )
    return f"https://www.youtube.com/watch?v={video_id}", video_id


def _extract_assigned_json(html: str, variable_name: str) -> dict[str, Any]:
    decoder = json.JSONDecoder()
    markers = (f"var {variable_name} =", f"{variable_name} =")
    starts = sorted(
        index
        for marker in markers
        if (index := html.find(marker)) >= 0
    )
    for index in starts:
        object_start = html.find("{", index)
        if object_start < 0:
            continue
        try:
            value, _ = decoder.raw_decode(html[object_start:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise YouTubePublicVideoError(
        "missing_page_data",
        f"YouTube page is missing {variable_name}",
    )


def _find_first_key(value: Any, key: str) -> Any:
    if isinstance(value, dict):
        if key in value:
            return value[key]
        for child in value.values():
            found = _find_first_key(child, key)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_first_key(child, key)
            if found is not None:
                return found
    return None


def _non_negative_integer(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, str) and value.isascii() and value.isdigit():
        return int(value)
    return None


def _renderer_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if not isinstance(value, dict):
        return ""
    simple = str(value.get("simpleText") or "").strip()
    if simple:
        return simple
    runs = value.get("runs")
    if isinstance(runs, list):
        return "".join(
            str(run.get("text") or "")
            for run in runs
            if isinstance(run, dict)
        ).strip()
    return ""


def _compact_public_count(value: Any) -> int | None:
    """Parse an en-US public count such as ``1.23K subscribers``."""

    text = _renderer_text(value).replace(",", "").strip().lower()
    if not text:
        return None
    if text.startswith("no subscriber"):
        return 0
    match = re.search(r"(?P<number>\d+(?:\.\d+)?)\s*(?P<suffix>[kmb])?", text)
    if match is None:
        return None
    multiplier = {"": 1, "k": 1_000, "m": 1_000_000, "b": 1_000_000_000}
    number = float(match.group("number"))
    return max(0, int(round(number * multiplier[match.group("suffix") or ""])))


def _required_text(value: Any, *, code: str, message: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise YouTubePublicVideoError(code, message)
    return text


def parse_youtube_public_page(
    html: str,
    *,
    expected_video_id: str,
    public_url: str | None = None,
    collected_at: str | None = None,
) -> YouTubePublicVideoMetrics:
    """Parse stable identity and metric fields from a public watch-page payload."""

    player = _extract_assigned_json(html, "ytInitialPlayerResponse")
    initial = _extract_assigned_json(html, "ytInitialData")
    playability = player.get("playabilityStatus")
    playability_status = (
        str(playability.get("status") or "").strip()
        if isinstance(playability, dict)
        else ""
    )
    if playability_status != "OK":
        raise YouTubePublicVideoError(
            "not_playable",
            f"YouTube video is not publicly playable ({playability_status or 'unknown'})",
        )

    details = player.get("videoDetails")
    if not isinstance(details, dict):
        raise YouTubePublicVideoError(
            "missing_identity",
            "YouTube page is missing video identity",
        )
    if details.get("isPrivate") is True:
        raise YouTubePublicVideoError(
            "private_video",
            "YouTube video is private",
        )

    video_id = _required_text(
        details.get("videoId"),
        code="missing_identity",
        message="YouTube page is missing the video ID",
    )
    if video_id != expected_video_id:
        raise YouTubePublicVideoError(
            "video_mismatch",
            "YouTube page video ID does not match the publication receipt",
        )

    microformat = player.get("microformat")
    renderer = (
        microformat.get("playerMicroformatRenderer")
        if isinstance(microformat, dict)
        else None
    )
    published_at = _required_text(
        renderer.get("publishDate") if isinstance(renderer, dict) else None,
        code="missing_identity",
        message="YouTube page is missing the publication timestamp",
    )

    views = _non_negative_integer(details.get("viewCount"))
    likes = _non_negative_integer(
        _find_first_key(initial, "likeCountIfIndifferentNumber")
    )
    comments = _non_negative_integer(_find_first_key(initial, "commentCount"))
    subscribers = _compact_public_count(
        _find_first_key(initial, "subscriberCountText")
    )
    metrics = {
        "views": views,
        "likes": likes,
        "comments": comments,
        "subscribers": subscribers,
    }
    unavailable_fields = tuple(
        name for name, value in metrics.items() if value is None
    )
    canonical_url = public_url or f"https://www.youtube.com/watch?v={video_id}"
    return YouTubePublicVideoMetrics(
        video_id=video_id,
        public_url=canonical_url,
        title=_required_text(
            details.get("title"),
            code="missing_identity",
            message="YouTube page is missing the video title",
        ),
        channel_id=_required_text(
            details.get("channelId"),
            code="missing_identity",
            message="YouTube page is missing the channel ID",
        ),
        channel_name=_required_text(
            details.get("author"),
            code="missing_identity",
            message="YouTube page is missing the channel name",
        ),
        subscribers=subscribers,
        published_at=published_at,
        views=views,
        likes=likes,
        comments=comments,
        unavailable_fields=unavailable_fields,
        collected_at=collected_at or datetime.now(timezone.utc).isoformat(),
    )


async def _fetch_with_client(
    client: httpx.AsyncClient,
    *,
    canonical_url: str,
    expected_video_id: str,
    collected_at: str | None,
) -> YouTubePublicVideoMetrics:
    for attempt in range(2):
        try:
            response = await client.get(
                canonical_url,
                headers=PUBLIC_YOUTUBE_HEADERS,
            )
        except httpx.TimeoutException as exc:
            if attempt == 0:
                continue
            raise YouTubePublicVideoError(
                "timeout",
                "YouTube public page timed out",
            ) from exc
        except httpx.RequestError as exc:
            raise YouTubePublicVideoError(
                "network_error",
                "YouTube public page could not be reached",
            ) from exc

        if response.status_code >= 500 and attempt == 0:
            continue
        if response.status_code != 200:
            raise YouTubePublicVideoError(
                "http_error",
                f"YouTube returned HTTP {response.status_code}",
            )
        return parse_youtube_public_page(
            response.text,
            expected_video_id=expected_video_id,
            public_url=canonical_url,
            collected_at=collected_at,
        )
    raise YouTubePublicVideoError(
        "fetch_failed",
        "YouTube public page could not be fetched",
    )


async def fetch_youtube_public_metrics(
    public_url: str,
    *,
    expected_video_id: str | None = None,
    client: httpx.AsyncClient | None = None,
    collected_at: str | None = None,
) -> YouTubePublicVideoMetrics:
    """Fetch and parse one public YouTube page without browser or auth state."""

    canonical_url, url_video_id = canonicalize_youtube_public_url(public_url)
    expected = str(expected_video_id or url_video_id).strip()
    if expected != url_video_id:
        raise YouTubePublicVideoError(
            "video_mismatch",
            "YouTube URL video ID does not match the publication receipt",
        )

    if client is not None:
        return await _fetch_with_client(
            client,
            canonical_url=canonical_url,
            expected_video_id=expected,
            collected_at=collected_at,
        )

    timeout = httpx.Timeout(15.0, connect=7.0)
    async with httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=False,
    ) as owned_client:
        return await _fetch_with_client(
            owned_client,
            canonical_url=canonical_url,
            expected_video_id=expected,
            collected_at=collected_at,
        )
