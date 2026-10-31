"""Search and freeze commercially reusable music from open catalogs.

Openverse is the primary catalog; Wikimedia Commons answers when Openverse is
down or has nothing for a query.  Both are queried over their public HTTP APIs
(no SDK, no API key).  Only Public Domain, CC0, and CC BY tracks are accepted.
Track IDs from Commons carry a ``commons:`` prefix so the importer re-resolves
each item from the catalog that listed it.  The importer resolves the catalog
item again server-side before downloading so a browser cannot turn the
endpoint into an arbitrary URL fetcher.
"""
from __future__ import annotations

import asyncio
import html
import ipaddress
import logging
import math
import os
import re
import socket
from dataclasses import asdict, dataclass
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

logger = logging.getLogger(__name__)

OPENVERSE_AUDIO_API = "https://api.openverse.org/v1/audio/"
WIKIMEDIA_COMMONS_API = "https://commons.wikimedia.org/w/api.php"
OPEN_MUSIC_LICENSES = frozenset({"pdm", "cc0", "by"})
OPEN_MUSIC_LICENSE_FILTER = "pdm,cc0,by"
OPEN_MUSIC_USER_AGENT = "Manor-AI/1.0 (https://github.com/manor-os)"
MAX_OPEN_MUSIC_REDIRECTS = 4

OPENVERSE_CATALOG = "openverse"
COMMONS_CATALOG = "wikimedia_commons"
COMMONS_TRACK_PREFIX = "commons:"
# Per-request HTTP timeouts, plus a wall-clock budget for each catalog's whole
# search (Openverse may issue a second, broader query).  Openverse answers in
# well under a second when healthy; during its 2026-10 outages requests hung
# until the timeout, so a tight budget lets Commons answer within the time a
# single Openverse request used to take.
OPENVERSE_HTTP_TIMEOUT = httpx.Timeout(6.0, connect=3.0)
COMMONS_HTTP_TIMEOUT = httpx.Timeout(8.0, connect=3.0)
OPENVERSE_SEARCH_BUDGET_SECONDS = 7.0
COMMONS_SEARCH_BUDGET_SECONDS = 9.0
COMMONS_SEARCH_BATCH = 40
COMMONS_MIN_MUSIC_SECONDS = 15.0
COMMONS_MAX_MUSIC_SECONDS = 1200.0
COMMONS_TRACK_PROPS = {
    "prop": "videoinfo",
    "viprop": "url|mime|size|extmetadata|derivatives",
    "viextmetadatafilter": "License|LicenseShortName|LicenseUrl|Artist|Attribution|ObjectName",
}
# Downloads for a catalog whose files live on one known CDN never leave it,
# not even through a redirect.  Openverse aggregates many origins, so its
# downloads rely on the public-address checks alone.
OPEN_MUSIC_DOWNLOAD_HOSTS = {
    COMMONS_CATALOG: frozenset({"upload.wikimedia.org"}),
}


class OpenMusicCatalogError(RuntimeError):
    """Base error for the open-music catalog.

    ``retryable`` marks a transient availability failure (timeout, connection
    error, upstream 5xx/429) as opposed to a refusal or an invalid item.
    """

    def __init__(self, message: str = "", *, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


class OpenMusicCatalogUnavailable(OpenMusicCatalogError):
    """The upstream catalog or track file could not be reached."""


class OpenMusicSearchUnavailable(OpenMusicCatalogUnavailable):
    """Every configured catalog failed; ``failures`` names each one's cause."""

    def __init__(self, failures: list[dict[str, str]]):
        self.failures = failures
        detail = "; ".join(f"{item['catalog']}: {item['error']}" for item in failures)
        super().__init__(f"Open music search is temporarily unavailable ({detail})", retryable=True)


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
    catalog: str = OPENVERSE_CATALOG

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


def _http_failure_reason(exc: Exception) -> str:
    if isinstance(exc, httpx.TimeoutException):
        return "timed out"
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    if isinstance(exc, httpx.HTTPError):
        return "unreachable"
    return "returned an invalid response"


def _http_failure_is_transient(exc: Exception) -> bool:
    """Whether a later attempt can succeed: timeouts, dropped connections, 5xx/429."""
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        return status == 429 or status >= 500
    return isinstance(exc, httpx.TransportError)


async def _json_get(url: str, *, params: dict | None, timeout: httpx.Timeout, label: str) -> dict:
    try:
        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
            headers={"Accept": "application/json", "User-Agent": OPEN_MUSIC_USER_AGENT},
        ) as client:
            response = await client.get(url, params=params)
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise OpenMusicCatalogUnavailable(
            f"{label} {_http_failure_reason(exc)}",
            retryable=_http_failure_is_transient(exc),
        ) from exc
    if not isinstance(payload, dict):
        raise OpenMusicCatalogUnavailable(f"{label} returned an invalid response")
    return payload


async def _catalog_get(path: str, *, params: dict | None = None) -> dict:
    return await _json_get(
        urljoin(OPENVERSE_AUDIO_API, path), params=params,
        timeout=OPENVERSE_HTTP_TIMEOUT, label="Openverse",
    )


async def _commons_get(params: dict) -> dict:
    payload = await _json_get(
        WIKIMEDIA_COMMONS_API,
        params={"action": "query", "format": "json", "formatversion": "2", **params},
        timeout=COMMONS_HTTP_TIMEOUT, label="Wikimedia Commons",
    )
    if payload.get("error"):
        raise OpenMusicCatalogUnavailable("Wikimedia Commons rejected the query")
    return payload


async def search_open_music(
    query: str,
    *,
    page: int = 1,
    page_size: int = 18,
    catalog: str | None = None,
) -> dict:
    """Search Openverse, falling back to Wikimedia Commons.

    Commons answers when Openverse fails or finds nothing.  Each catalog runs
    under its own wall-clock budget, so one hanging provider cannot multiply
    the latency.  The result names the catalog that answered and any catalog
    that failed before it; only when every catalog fails is it an error.
    ``catalog`` limits the search to one catalog, e.g. Commons when Openverse
    answers searches but cannot serve an import.

    Each returned track's catalog record is remembered server-side, so the
    import that follows does not depend on a second round-trip to a catalog
    that has just become unavailable.
    """
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
    catalogs = (
        (OPENVERSE_CATALOG, _search_openverse, OPENVERSE_SEARCH_BUDGET_SECONDS),
        (COMMONS_CATALOG, _search_commons, COMMONS_SEARCH_BUDGET_SECONDS),
    )
    selected_catalog = _clean_text(catalog).casefold()
    if selected_catalog:
        catalogs = tuple(entry for entry in catalogs if entry[0] == selected_catalog)
        if not catalogs:
            raise ValueError(f"Unknown music catalog; use {OPENVERSE_CATALOG} or {COMMONS_CATALOG}")
    failures: list[dict[str, str]] = []
    empty_result: dict | None = None
    for catalog_name, search, budget in catalogs:
        try:
            result = await asyncio.wait_for(
                search(cleaned_query, page=safe_page, page_size=safe_page_size), timeout=budget,
            )
        except TimeoutError:
            failures.append({"catalog": catalog_name, "error": f"timed out after {budget:g}s"})
            continue
        except OpenMusicCatalogUnavailable as exc:
            failures.append({"catalog": catalog_name, "error": str(exc)})
            continue
        await _remember_track_records(result.pop("_records", None) or [])
        result["catalog"] = catalog_name
        if result["items"]:
            return {**result, "catalog_failures": failures}
        if empty_result is None:
            empty_result = result
    if empty_result is not None:
        return {**empty_result, "catalog_failures": failures}
    raise OpenMusicSearchUnavailable(failures)


# Search results stay importable without another catalog call for this long.
# The cached value is the catalog's own record, re-normalized at import, so
# the license policy and download-host rules still run on it.
OPEN_MUSIC_TRACK_CACHE_TTL_SECONDS = 6 * 60 * 60
_TRACK_CACHE_PREFIX = "open_music:track:"


async def _remember_track_records(records: list[dict]) -> None:
    if not records:
        return
    try:
        from packages.core.cache import cache

        await asyncio.gather(*(
            cache.set(f"{_TRACK_CACHE_PREFIX}{record['id']}", record, ttl=OPEN_MUSIC_TRACK_CACHE_TTL_SECONDS)
            for record in records
        ))
    except Exception:
        # The cache only saves a round-trip; import still re-resolves without it.
        logger.debug("Open music track cache write failed", exc_info=True)


async def _recall_track_record(track_id: str) -> dict | None:
    try:
        from packages.core.cache import cache

        record = await cache.get(f"{_TRACK_CACHE_PREFIX}{track_id}")
    except Exception:
        logger.debug("Open music track cache read failed", exc_info=True)
        return None
    return record if isinstance(record, dict) else None


async def _search_openverse(cleaned_query: str, *, page: int, page_size: int) -> dict:
    safe_page, safe_page_size = page, page_size
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
    records: list[dict] = []
    for item in payload.get("results") or []:
        try:
            track = normalize_open_music_track(item)
        except OpenMusicCatalogError:
            continue
        tracks.append(track)
        records.append({"id": track.id, "payload": item})
    return {
        "items": [track.to_dict() for track in tracks],
        "page": safe_page,
        "page_count": max(0, int(payload.get("page_count") or 0)),
        "total": max(0, int(payload.get("result_count") or 0)),
        "_records": records,
    }


_COMMONS_BY_LICENSE = re.compile(r"cc-by-(\d(?:\.\d)?)(?:-[a-z]{2,3})?")
_COMMONS_ORIGINAL_MIMES = frozenset({"audio/mpeg", "audio/mp3", "audio/ogg", "application/ogg"})
_COMMONS_REJECTED_MIMES = frozenset({"audio/midi", "audio/x-midi", "audio/mid"})


def _commons_license(value: str) -> tuple[str, str]:
    """Map a Commons ``License`` template code onto Manor's license policy."""
    normalized = value.casefold().strip()
    if normalized == "cc0":
        return "cc0", "1.0"
    if normalized == "pd":
        return "pdm", "1.0"
    match = _COMMONS_BY_LICENSE.fullmatch(normalized)
    if match:
        return "by", match.group(1)
    raise OpenMusicLicenseError("Track license is not approved for commercial video use")


def _html_text(value: object) -> str:
    return _clean_text(html.unescape(re.sub(r"<[^>]*>", " ", str(value or ""))))


def _commons_upload_url(value: object) -> str:
    """An upload.wikimedia.org HTTPS URL without its tracking query, or ''."""
    parsed = urlsplit(_clean_text(value))
    allowed = OPEN_MUSIC_DOWNLOAD_HOSTS[COMMONS_CATALOG]
    if parsed.scheme != "https" or (parsed.hostname or "").casefold() not in allowed:
        return ""
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def normalize_commons_track(payload: object) -> OpenMusicTrack:
    if not isinstance(payload, dict) or payload.get("missing") or payload.get("ns") != 6:
        raise OpenMusicCatalogUnavailable("Wikimedia Commons returned an invalid music item")
    infos = payload.get("videoinfo") or payload.get("imageinfo") or []
    info = infos[0] if isinstance(infos, list) and infos and isinstance(infos[0], dict) else {}
    metadata = info.get("extmetadata") if isinstance(info.get("extmetadata"), dict) else {}

    def meta(key: str) -> str:
        entry = metadata.get(key)
        return entry.get("value", "") if isinstance(entry, dict) else ""

    license_code, version = _commons_license(_clean_text(meta("License")))
    mime = _clean_text(info.get("mime")).casefold()
    if mime in _COMMONS_REJECTED_MIMES or not (mime.startswith("audio/") or mime == "application/ogg"):
        raise OpenMusicCatalogUnavailable("Wikimedia Commons item is not an audio recording")
    raw_duration = info.get("duration")
    if not isinstance(raw_duration, (int, float)) or not (
        COMMONS_MIN_MUSIC_SECONDS <= raw_duration <= COMMONS_MAX_MUSIC_SECONDS
    ):
        raise OpenMusicCatalogUnavailable("Wikimedia Commons item is not a music-length recording")

    page_id = payload.get("pageid")
    original_url = _commons_upload_url(info.get("url"))
    source_url = _clean_text(info.get("descriptionurl"))
    if not isinstance(page_id, int) or page_id <= 0 or not original_url or not source_url.startswith(
        "https://commons.wikimedia.org/"
    ):
        raise OpenMusicCatalogUnavailable("Wikimedia Commons music item is missing its source")
    # WAV/FLAC/WebM originals can be very large or poorly supported by the
    # editor; Commons publishes an MP3 transcode for them.
    audio_url = original_url
    if mime not in _COMMONS_ORIGINAL_MIMES:
        for derivative in info.get("derivatives") or []:
            if not isinstance(derivative, dict):
                continue
            derivative_type = _clean_text(derivative.get("type")).split(";", 1)[0].casefold()
            derivative_url = _commons_upload_url(derivative.get("src"))
            if derivative_type == "audio/mpeg" and derivative_url:
                audio_url = derivative_url
                break

    license_name, fallback_license_url = _license_details(license_code, version)
    license_url = _clean_text(meta("LicenseUrl"))
    if not license_url.startswith(("https://creativecommons.org/", "http://creativecommons.org/")):
        license_url = fallback_license_url
    raw_title = _clean_text(payload.get("title")).removeprefix("File:")
    title = (_html_text(meta("ObjectName")) or os.path.splitext(raw_title)[0] or "Untitled music")[:200]
    creator = (_html_text(meta("Artist")) or _html_text(meta("Attribution")) or "Unknown creator")[:200]
    attribution = (
        f'"{title}" by {creator}, via Wikimedia Commons ({source_url}) — {license_name} ({license_url})'
    )
    return OpenMusicTrack(
        id=f"{COMMONS_TRACK_PREFIX}{page_id}",
        title=title,
        creator=creator,
        duration_seconds=round(float(raw_duration), 3),
        preview_url=audio_url,
        source_url=source_url,
        license=license_code,
        license_name=license_name,
        license_url=license_url,
        attribution=attribution,
        source=COMMONS_CATALOG,
        provider=COMMONS_CATALOG,
        genres=(),
        catalog=COMMONS_CATALOG,
    )


def _commons_search_terms(query: str) -> str:
    # Plain words only: CirrusSearch keywords (insource:, intitle:, regexes)
    # stay out of a user-supplied query.
    return " ".join(re.findall(r"[\w'-]+", query))[:120]


async def _search_commons(cleaned_query: str, *, page: int, page_size: int) -> dict:
    terms = _commons_search_terms(cleaned_query)
    if not terms:
        return {"items": [], "page": page, "page_count": 0, "total": 0}
    params = {
        "generator": "search",
        "gsrnamespace": "6",
        "gsrlimit": str(COMMONS_SEARCH_BATCH),
        "gsroffset": str((page - 1) * COMMONS_SEARCH_BATCH),
        "gsrinfo": "totalhits",
        "gsrsearch": f"filetype:audio {terms}",
        **COMMONS_TRACK_PROPS,
    }
    payload = await _commons_get(params)
    # Same broadening as Openverse: a multi-word phrase with no hits retries
    # its leading subject once.
    leading = terms.split(maxsplit=1)[0]
    if leading != terms and not ((payload.get("query") or {}).get("pages") or []):
        params["gsrsearch"] = f"filetype:audio {leading}"
        payload = await _commons_get(params)
    query_payload = payload.get("query") or {}
    pages = [item for item in query_payload.get("pages") or [] if isinstance(item, dict)]
    pages.sort(key=lambda item: item.get("index") if isinstance(item.get("index"), int) else 0)
    tracks: list[OpenMusicTrack] = []
    records: list[dict] = []
    for item in pages:
        try:
            track = normalize_commons_track(item)
        except OpenMusicCatalogError:
            continue
        tracks.append(track)
        records.append({"id": track.id, "payload": item})
        if len(tracks) >= page_size:
            break
    total_hits = (query_payload.get("searchinfo") or {}).get("totalhits")
    total = max(0, int(total_hits)) if isinstance(total_hits, int) else len(tracks)
    return {
        "items": [track.to_dict() for track in tracks],
        "page": page,
        "page_count": math.ceil(total / COMMONS_SEARCH_BATCH),
        "total": total,
        "_records": records,
    }


IMPORT_RETRY_BACKOFF_SECONDS = 1.0


def _normalize_catalog_record(track_id: str, payload: object) -> OpenMusicTrack:
    """Apply the catalog's normalizer (and so its license policy) to a record."""
    if track_id.startswith(COMMONS_TRACK_PREFIX):
        return normalize_commons_track(payload)
    return normalize_open_music_track(payload)


async def _fetch_open_music_track(track_id: str) -> OpenMusicTrack:
    if track_id.startswith(COMMONS_TRACK_PREFIX):
        page_id = track_id.removeprefix(COMMONS_TRACK_PREFIX)
        payload = await _commons_get({"pageids": page_id, **COMMONS_TRACK_PROPS})
        pages = (payload.get("query") or {}).get("pages") or []
        if not pages:
            raise OpenMusicCatalogUnavailable("Wikimedia Commons music item was not found")
        return normalize_commons_track(pages[0])
    return normalize_open_music_track(await _catalog_get(f"{track_id}/"))


def _import_unavailable_message(track_id: str, exc: Exception) -> str:
    if track_id.startswith(COMMONS_TRACK_PREFIX):
        return (
            f"{exc} while re-checking this track's license (tried twice). "
            "Retry the import shortly, or pick another track."
        )
    return (
        f"{exc} while re-checking this track's license (tried twice). Openverse is temporarily "
        "unavailable: retry the import shortly, or search again with catalog "
        f"'{COMMONS_CATALOG}' and import a track whose id starts with '{COMMONS_TRACK_PREFIX}'."
    )


async def get_open_music_track(track_id: str) -> OpenMusicTrack:
    """Resolve a track for import from the catalog its (prefixed) ID names.

    A track a recent search returned is resolved from that search's catalog
    record; otherwise the catalog is asked again, retrying a transient failure
    once.  Either way the record passes the catalog's normalizer, so only
    approved licenses and the catalog's own download source come back.
    """
    cleaned_id = _clean_text(track_id)
    if not cleaned_id or len(cleaned_id) > 120 or "/" in cleaned_id:
        raise OpenMusicCatalogUnavailable("Invalid open-music track id")
    if cleaned_id.startswith(COMMONS_TRACK_PREFIX):
        page_id = cleaned_id.removeprefix(COMMONS_TRACK_PREFIX)
        if not page_id.isdigit() or len(page_id) > 12:
            raise OpenMusicCatalogUnavailable("Invalid open-music track id")
    elif ":" in cleaned_id:
        raise OpenMusicCatalogUnavailable("Unknown open-music catalog")

    record = await _recall_track_record(cleaned_id)
    if record is not None and record.get("id") == cleaned_id:
        try:
            track = _normalize_catalog_record(cleaned_id, record.get("payload"))
        except OpenMusicLicenseError:
            raise
        except OpenMusicCatalogError:
            track = None
        if track is not None and track.id == cleaned_id:
            return track

    for attempt in range(2):
        try:
            return await _fetch_open_music_track(cleaned_id)
        except OpenMusicCatalogUnavailable as exc:
            if not exc.retryable:
                raise
            if attempt == 0:
                await asyncio.sleep(IMPORT_RETRY_BACKOFF_SECONDS)
                continue
            raise OpenMusicCatalogUnavailable(
                _import_unavailable_message(cleaned_id, exc), retryable=True,
            ) from exc
    raise OpenMusicCatalogUnavailable("Open music track could not be resolved")


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
    allowed_hosts = OPEN_MUSIC_DOWNLOAD_HOSTS.get(track.catalog)
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(90.0, connect=10.0),
            follow_redirects=False,
            trust_env=False,
            headers={"Accept": "audio/*,application/octet-stream", "User-Agent": OPEN_MUSIC_USER_AGENT},
        ) as client:
            for redirect_index in range(MAX_OPEN_MUSIC_REDIRECTS + 1):
                current_url = _validate_public_audio_url(current_url)
                if allowed_hosts is not None and (
                    (urlsplit(current_url).hostname or "").rstrip(".").casefold() not in allowed_hosts
                ):
                    raise OpenMusicDownloadError("Music source left the catalog's download host")
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
                        raise OpenMusicDownloadError("Music source did not return audio")
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
                        raise OpenMusicDownloadError("Music file is empty")
                    return DownloadedOpenMusic(
                        file_size=total,
                        mime_type=normalized_type if normalized_type.startswith("audio/") else "audio/mpeg",
                        extension=_audio_extension(content_type, current_url),
                    )
    except OpenMusicCatalogError:
        raise
    except httpx.HTTPError as exc:
        raise OpenMusicDownloadError(
            f"Music file could not be downloaded (source {_http_failure_reason(exc)})",
            retryable=_http_failure_is_transient(exc),
        ) from exc
    except (OSError, ValueError) as exc:
        raise OpenMusicDownloadError("Music file could not be downloaded") from exc
    raise OpenMusicDownloadError("Music file could not be downloaded")
