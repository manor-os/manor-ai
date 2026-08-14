"""Search and freeze commercially reusable music from the Openverse catalog.

The service intentionally uses Openverse's HTTP API directly instead of an SDK.
Only Public Domain, CC0, and CC BY tracks are accepted.  The importer resolves
the catalog item again server-side before downloading so a browser cannot turn
the endpoint into an arbitrary URL fetcher.
"""
from __future__ import annotations

import asyncio
import ipaddress
import os
import socket
from dataclasses import asdict, dataclass
from urllib.parse import urljoin, urlsplit

import httpx


OPENVERSE_AUDIO_API = "https://api.openverse.org/v1/audio/"
OPEN_MUSIC_LICENSES = frozenset({"pdm", "cc0", "by"})
OPEN_MUSIC_LICENSE_FILTER = "pdm,cc0,by"
OPEN_MUSIC_USER_AGENT = "Manor-AI/1.0 (https://github.com/manor-os)"
MAX_OPEN_MUSIC_REDIRECTS = 4


class OpenMusicCatalogError(RuntimeError):
    """Base error for the open-music catalog."""


class OpenMusicCatalogUnavailable(OpenMusicCatalogError):
    """The upstream catalog or track file could not be reached."""


class OpenMusicLicenseError(OpenMusicCatalogError):
    """The selected item is not covered by Manor's safe commercial filter."""


class OpenMusicDownloadError(OpenMusicCatalogError):
    """The selected audio file could not be safely frozen."""


@dataclass(frozen=True)
class OpenMusicTrack:
    id: str
    title: str
    creator: str
    duration_seconds: float | None
    preview_url: str
    source_url: str
    license: str
    license_name: str
    license_url: str
    attribution: str
    source: str
    provider: str
    genres: tuple[str, ...]

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["genres"] = list(self.genres)
        return payload


@dataclass(frozen=True)
class DownloadedOpenMusic:
    file_size: int
    mime_type: str
    extension: str


def _clean_text(value: object, fallback: str = "") -> str:
    return " ".join(str(value or fallback).replace("\x00", " ").split()).strip()


def _license_details(code: str, version: str) -> tuple[str, str]:
    normalized = code.casefold().strip()
    normalized_version = version.strip() or "1.0"
    if normalized == "pdm":
        return "Public Domain", "https://creativecommons.org/publicdomain/mark/1.0/"
    if normalized == "cc0":
        return "CC0", "https://creativecommons.org/publicdomain/zero/1.0/"
    if normalized == "by":
        return f"CC BY {normalized_version}", f"https://creativecommons.org/licenses/by/{normalized_version}/"
    raise OpenMusicLicenseError("Track license is not approved for commercial video use")


def normalize_open_music_track(payload: object) -> OpenMusicTrack:
    if not isinstance(payload, dict):
        raise OpenMusicCatalogUnavailable("Openverse returned an invalid music item")
    license_code = _clean_text(payload.get("license")).casefold()
    if license_code not in OPEN_MUSIC_LICENSES:
        raise OpenMusicLicenseError("Track license is not approved for commercial video use")
    category = _clean_text(payload.get("category")).casefold()
    if category and category != "music":
        raise OpenMusicCatalogUnavailable("Openverse item is not categorized as music")

    track_id = _clean_text(payload.get("id"))
    preview_url = _clean_text(payload.get("url"))
    source_url = _clean_text(payload.get("foreign_landing_url"))
    if not track_id or not preview_url or not source_url:
        raise OpenMusicCatalogUnavailable("Openverse music item is missing its source")

    version = _clean_text(payload.get("license_version"), "1.0")
    license_name, fallback_license_url = _license_details(license_code, version)
    license_url = _clean_text(payload.get("license_url"), fallback_license_url) or fallback_license_url
    title = _clean_text(payload.get("title"), "Untitled music") or "Untitled music"
    creator = _clean_text(payload.get("creator"), "Unknown creator") or "Unknown creator"
    attribution = _clean_text(payload.get("attribution"))
    if not attribution:
        attribution = f'"{title}" by {creator} — {license_name} ({license_url})'

    raw_duration = payload.get("duration")
    duration_seconds: float | None = None
    if isinstance(raw_duration, (int, float)) and raw_duration > 0:
        duration_seconds = round(float(raw_duration) / 1000.0, 3)

    raw_genres = payload.get("genres")
    genre_values = raw_genres if isinstance(raw_genres, list) else []
    genres = tuple(
        dict.fromkeys(
            genre
            for genre in (_clean_text(item) for item in genre_values)
            if genre
        )
    )[:8]
    return OpenMusicTrack(
        id=track_id,
        title=title,
        creator=creator,
        duration_seconds=duration_seconds,
        preview_url=preview_url,
        source_url=source_url,
        license=license_code,
        license_name=license_name,
        license_url=license_url,
        attribution=attribution,
        source=_clean_text(payload.get("source"), "openverse") or "openverse",
        provider=_clean_text(payload.get("provider"), "openverse") or "openverse",
        genres=genres,
    )


async def _catalog_get(path: str, *, params: dict | None = None) -> dict:
    url = urljoin(OPENVERSE_AUDIO_API, path)
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(15.0, connect=7.0),
            follow_redirects=False,
            trust_env=False,
            headers={"Accept": "application/json", "User-Agent": OPEN_MUSIC_USER_AGENT},
        ) as client:
            response = await client.get(url, params=params)
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise OpenMusicCatalogUnavailable("Open music search is temporarily unavailable") from exc
    if not isinstance(payload, dict):
        raise OpenMusicCatalogUnavailable("Openverse returned an invalid response")
    return payload


async def search_open_music(query: str, *, page: int = 1, page_size: int = 18) -> dict:
    cleaned_query = _clean_text(query)
    if len(cleaned_query) < 2:
        raise ValueError("Search needs at least 2 characters")
    if len(cleaned_query) > 120:
        raise ValueError("Search is too long")
    safe_page = max(1, min(100, int(page)))
    # Openverse's anonymous API accepts at most 20 audio results per page.
    # Keeping the catalog usable without an API key is part of this feature's
    # no-third-party-SDK contract.
    safe_page_size = max(1, min(20, int(page_size)))
    search_params = {
        "q": cleaned_query,
        "category": "music",
        "license": OPEN_MUSIC_LICENSE_FILTER,
        "mature": "false",
        "filter_dead": "true",
        "page": safe_page,
        "page_size": safe_page_size,
    }
    payload = await _catalog_get("", params=search_params)
    # Openverse full-text searches can be much narrower than users expect.
    # When a multi-word phrase has no matches, retry its leading subject once
    # so searches such as "technology corporate" still produce useful music.
    fallback_query = cleaned_query.split(maxsplit=1)[0]
    if fallback_query != cleaned_query and not (payload.get("results") or []):
        search_params["q"] = fallback_query
        payload = await _catalog_get("", params=search_params)
    tracks: list[OpenMusicTrack] = []
    for item in payload.get("results") or []:
        try:
            tracks.append(normalize_open_music_track(item))
        except OpenMusicCatalogError:
            continue
    return {
        "items": [track.to_dict() for track in tracks],
        "page": safe_page,
        "page_count": max(0, int(payload.get("page_count") or 0)),
        "total": max(0, int(payload.get("result_count") or 0)),
    }


async def get_open_music_track(track_id: str) -> OpenMusicTrack:
    cleaned_id = _clean_text(track_id)
    if not cleaned_id or len(cleaned_id) > 120 or "/" in cleaned_id:
        raise OpenMusicCatalogUnavailable("Invalid open-music track id")
    return normalize_open_music_track(await _catalog_get(f"{cleaned_id}/"))


def _validate_public_audio_url(value: str) -> str:
    parsed = urlsplit(_clean_text(value))
    if parsed.scheme.casefold() != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise OpenMusicDownloadError("Music source must be a public HTTPS URL")
    try:
        port = parsed.port
    except ValueError as exc:
        raise OpenMusicDownloadError("Music source port is invalid") from exc
    if port not in {None, 443}:
        raise OpenMusicDownloadError("Music source must use the standard HTTPS port")
    hostname = parsed.hostname.rstrip(".").casefold()
    if hostname in {"localhost", "localhost.localdomain"} or hostname.endswith((".local", ".internal", ".localhost")):
        raise OpenMusicDownloadError("Private music hosts are not available")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise OpenMusicDownloadError("Private music addresses are not available")
    return value


async def _resolve_public_audio_host(url: str) -> None:
    hostname = urlsplit(url).hostname or ""
    try:
        results = await asyncio.to_thread(socket.getaddrinfo, hostname, 443, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise OpenMusicDownloadError("Music source could not be resolved") from exc
    addresses = {str(item[4][0]) for item in results if item and len(item) > 4 and item[4]}
    if not addresses:
        raise OpenMusicDownloadError("Music source did not resolve")
    for raw in addresses:
        try:
            address = ipaddress.ip_address(raw)
        except ValueError as exc:
            raise OpenMusicDownloadError("Music source resolved unexpectedly") from exc
        if not address.is_global:
            raise OpenMusicDownloadError("Music source resolved to a private network")


def _audio_extension(content_type: str, url: str) -> str:
    normalized = content_type.split(";", 1)[0].strip().casefold()
    by_mime = {
        "audio/mpeg": "mp3",
        "audio/mp3": "mp3",
        "audio/wav": "wav",
        "audio/x-wav": "wav",
        "audio/ogg": "ogg",
        "application/ogg": "ogg",
        "audio/flac": "flac",
        "audio/x-flac": "flac",
        "audio/mp4": "m4a",
        "audio/aac": "aac",
    }
    if normalized in by_mime:
        return by_mime[normalized]
    suffix = os.path.splitext(urlsplit(url).path)[1].lower().lstrip(".")
    return suffix if suffix in {"mp3", "wav", "ogg", "flac", "m4a", "aac"} else "mp3"


async def download_open_music_track(
    track: OpenMusicTrack,
    destination: str,
    *,
    max_bytes: int,
) -> DownloadedOpenMusic:
    current_url = track.preview_url
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(90.0, connect=10.0),
            follow_redirects=False,
            trust_env=False,
            headers={"Accept": "audio/*,application/octet-stream", "User-Agent": OPEN_MUSIC_USER_AGENT},
        ) as client:
            for redirect_index in range(MAX_OPEN_MUSIC_REDIRECTS + 1):
                current_url = _validate_public_audio_url(current_url)
                await _resolve_public_audio_host(current_url)
                async with client.stream("GET", current_url) as response:
                    if response.is_redirect:
                        if redirect_index >= MAX_OPEN_MUSIC_REDIRECTS:
                            raise OpenMusicDownloadError("Music source redirected too many times")
                        location = response.headers.get("location")
                        if not location:
                            raise OpenMusicDownloadError("Music source returned an invalid redirect")
                        current_url = urljoin(current_url, location)
                        continue
                    response.raise_for_status()
                    content_type = response.headers.get("content-type", "application/octet-stream")
                    normalized_type = content_type.split(";", 1)[0].strip().casefold()
                    if normalized_type and not (
                        normalized_type.startswith("audio/")
                        or normalized_type in {"application/ogg", "application/octet-stream"}
                    ):
                        raise OpenMusicDownloadError("Openverse source did not return audio")
                    declared_size = int(response.headers.get("content-length") or 0)
                    if declared_size > max_bytes:
                        raise OpenMusicDownloadError("Music file exceeds the upload limit")
                    total = 0
                    with open(destination, "wb") as target:
                        async for chunk in response.aiter_bytes(256 * 1024):
                            if not chunk:
                                continue
                            total += len(chunk)
                            if total > max_bytes:
                                raise OpenMusicDownloadError("Music file exceeds the upload limit")
                            target.write(chunk)
                    if total <= 0:
                        raise OpenMusicDownloadError("Openverse music file is empty")
                    return DownloadedOpenMusic(
                        file_size=total,
                        mime_type=normalized_type if normalized_type.startswith("audio/") else "audio/mpeg",
                        extension=_audio_extension(content_type, current_url),
                    )
    except OpenMusicCatalogError:
        raise
    except (httpx.HTTPError, OSError, ValueError) as exc:
        raise OpenMusicDownloadError("Openverse music file could not be downloaded") from exc
    raise OpenMusicDownloadError("Openverse music file could not be downloaded")
