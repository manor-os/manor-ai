from __future__ import annotations

import asyncio
import json
import time

import httpx
import pytest

from packages.core.services import open_music_catalog as catalog


@pytest.fixture(autouse=True)
def track_cache(monkeypatch):
    """The search -> import track cache, kept in memory for each test."""
    from packages.core import cache as cache_module

    stored: dict[str, str] = {}

    async def cache_get(key):
        return json.loads(stored[key]) if key in stored else None

    async def cache_set(key, value, ttl=300):
        assert ttl == catalog.OPEN_MUSIC_TRACK_CACHE_TTL_SECONDS
        stored[key] = json.dumps(value)
        return True

    monkeypatch.setattr(cache_module.cache, "get", cache_get)
    monkeypatch.setattr(cache_module.cache, "set", cache_set)
    return stored


def _track_payload(**overrides):
    payload = {
        "id": "track-123",
        "title": "Cinematic Signal",
        "creator": "Example Artist",
        "duration": 125500,
        "url": "https://cdn.example.com/track.mp3",
        "foreign_landing_url": "https://example.com/tracks/123",
        "license": "by",
        "license_version": "4.0",
        "category": "music",
        "source": "jamendo",
        "provider": "jamendo",
        "genres": ["cinematic", "ambient", "cinematic"],
    }
    payload.update(overrides)
    return payload


def test_normalize_open_music_track_preserves_license_and_credit():
    track = catalog.normalize_open_music_track(_track_payload())

    assert track.id == "track-123"
    assert track.duration_seconds == 125.5
    assert track.license == "by"
    assert track.license_name == "CC BY 4.0"
    assert track.license_url == "https://creativecommons.org/licenses/by/4.0/"
    assert track.genres == ("cinematic", "ambient")
    assert "Example Artist" in track.attribution


@pytest.mark.parametrize("license_code", ["by-nc", "by-nd", "by-sa", "by-nc-sa"])
def test_normalize_open_music_track_rejects_restricted_licenses(license_code: str):
    with pytest.raises(catalog.OpenMusicLicenseError):
        catalog.normalize_open_music_track(_track_payload(license=license_code))


def test_normalize_open_music_track_rejects_non_music_audio():
    with pytest.raises(catalog.OpenMusicCatalogUnavailable):
        catalog.normalize_open_music_track(_track_payload(category="sound_effect"))


@pytest.mark.asyncio
async def test_search_open_music_forces_commercial_music_filters(monkeypatch):
    observed: dict = {}

    async def fake_catalog_get(path: str, *, params=None):
        observed["path"] = path
        observed["params"] = params
        return {
            "page_count": 2,
            "result_count": 3,
            "results": [
                _track_payload(),
                _track_payload(id="blocked", license="by-nc"),
                _track_payload(id="sfx", category="sound_effect"),
            ],
        }

    monkeypatch.setattr(catalog, "_catalog_get", fake_catalog_get)
    result = await catalog.search_open_music("cinematic technology", page=1, page_size=99)

    assert observed["path"] == ""
    assert observed["params"]["category"] == "music"
    assert observed["params"]["license"] == "pdm,cc0,by"
    assert observed["params"]["mature"] == "false"
    assert observed["params"]["page_size"] == 20
    assert [item["id"] for item in result["items"]] == ["track-123"]
    assert result["total"] == 3


@pytest.mark.asyncio
async def test_search_open_music_retries_leading_keyword_when_phrase_is_empty(monkeypatch):
    observed_queries: list[str] = []

    async def fake_catalog_get(path: str, *, params=None):
        observed_queries.append(params["q"])
        if params["q"] == "technology corporate":
            return {"page_count": 0, "result_count": 0, "results": []}
        return {
            "page_count": 1,
            "result_count": 1,
            "results": [_track_payload(title="Inspiring Technology")],
        }

    monkeypatch.setattr(catalog, "_catalog_get", fake_catalog_get)
    result = await catalog.search_open_music("technology corporate", page=1, page_size=18)

    assert observed_queries == ["technology corporate", "technology"]
    assert result["total"] == 1
    assert [item["title"] for item in result["items"]] == ["Inspiring Technology"]


@pytest.mark.asyncio
async def test_get_open_music_track_reloads_catalog_item(monkeypatch):
    observed: list[str] = []

    async def fake_catalog_get(path: str, *, params=None):
        observed.append(path)
        return _track_payload(id="fresh-track", license="cc0", license_version="1.0")

    monkeypatch.setattr(catalog, "_catalog_get", fake_catalog_get)
    track = await catalog.get_open_music_track("fresh-track")

    assert observed == ["fresh-track/"]
    assert track.id == "fresh-track"
    assert track.license_name == "CC0"


@pytest.mark.asyncio
async def test_import_resolves_a_searched_track_without_a_second_openverse_call(monkeypatch, track_cache):
    # 2026-10-20 eval: search answered from Openverse, then the import's detail
    # call timed out and the run got no music.
    calls: list[str] = []

    async def openverse(path: str, *, params=None):
        calls.append(path)
        if path:
            raise catalog.OpenMusicCatalogUnavailable("Openverse timed out", retryable=True)
        return {"page_count": 1, "result_count": 2, "results": [
            _track_payload(), _track_payload(id="track-456", license="cc0"),
        ]}

    monkeypatch.setattr(catalog, "_catalog_get", openverse)
    listed = (await catalog.search_open_music("cinematic", page=1, page_size=18))["items"][0]
    track = await catalog.get_open_music_track(listed["id"])

    assert calls == [""]
    assert track.to_dict() == listed
    # The cached record still passes the license policy at import.
    record_key = next(key for key in track_cache if key.endswith("track-456"))
    tampered = json.loads(track_cache[record_key])
    tampered["payload"]["license"] = "by-nc"
    track_cache[record_key] = json.dumps(tampered)
    with pytest.raises(catalog.OpenMusicLicenseError):
        await catalog.get_open_music_track("track-456")


@pytest.mark.asyncio
async def test_import_retries_a_timeout_once_then_reports_retryable_unavailability(monkeypatch):
    attempts: list[str] = []

    def handler(request: httpx.Request):
        attempts.append(request.url.path)
        raise httpx.ReadTimeout("hang", request=request)

    _mock_http(monkeypatch, handler)
    monkeypatch.setattr(catalog, "IMPORT_RETRY_BACKOFF_SECONDS", 0)
    with pytest.raises(catalog.OpenMusicCatalogUnavailable) as raised:
        await catalog.get_open_music_track("uncached-track")

    assert attempts == ["/v1/audio/uncached-track/"] * 2
    assert raised.value.retryable is True
    assert "commons:" in str(raised.value) and "wikimedia_commons" in str(raised.value)

    def not_found(request: httpx.Request):
        attempts.append(request.url.path)
        return httpx.Response(404)

    attempts.clear()
    _mock_http(monkeypatch, not_found)
    with pytest.raises(catalog.OpenMusicCatalogUnavailable) as raised:
        await catalog.get_open_music_track("gone-track")
    assert len(attempts) == 1
    assert raised.value.retryable is False


# --- Wikimedia Commons fallback -------------------------------------------
# 2026-10-19/20: Openverse's search endpoints hung for 30-60 min at a time and
# every video run in those windows got no licensed music.


def _commons_page(page_id: int, *, license_code: str = "cc-by-4.0", mime: str = "audio/mpeg",
                  duration: float = 150.0, index: int = 1, derivatives=None):
    name = f"Track_{page_id}"
    return {
        "pageid": page_id,
        "ns": 6,
        "title": f"File:{name}.mp3",
        "index": index,
        "videoinfo": [{
            "duration": duration,
            "mime": mime,
            "url": f"https://upload.wikimedia.org/wikipedia/commons/a/ab/{name}.mp3?utm_source=commons",
            "descriptionurl": f"https://commons.wikimedia.org/wiki/File:{name}.mp3",
            "derivatives": derivatives or [],
            "extmetadata": {
                "License": {"value": license_code},
                "LicenseUrl": {"value": "https://creativecommons.org/licenses/by/4.0"},
                "Artist": {"value": '<a href="https://example.org/artist">Jane &amp; Co</a>'},
                "ObjectName": {"value": f"Track {page_id}"},
            },
        }],
    }


_REAL_ASYNC_CLIENT = httpx.AsyncClient


def _mock_http(monkeypatch, handler):
    """Route the catalog's real httpx clients through ``handler``."""
    transport = httpx.MockTransport(handler)

    class MockClient(_REAL_ASYNC_CLIENT):
        def __init__(self, **kwargs):
            super().__init__(transport=transport, **kwargs)

    monkeypatch.setattr(catalog.httpx, "AsyncClient", MockClient)


def _commons_search_response(pages):
    return httpx.Response(200, json={"query": {"searchinfo": {"totalhits": 81}, "pages": pages}})


@pytest.mark.asyncio
async def test_search_answers_from_commons_within_budget_when_openverse_hangs(monkeypatch):
    commons_queries: list[dict] = []

    async def handler(request: httpx.Request):
        if request.url.host == "api.openverse.org":
            await asyncio.sleep(30)
        commons_queries.append(dict(request.url.params))
        return _commons_search_response([
            _commons_page(103, license_code="cc-by-sa-4.0", index=1),
            _commons_page(104, license_code="cc-by-nc-4.0", index=2),
            _commons_page(105, mime="audio/midi", index=3),
            _commons_page(106, duration=4.0, index=4),  # a pronunciation clip, not music
            _commons_page(102, license_code="cc0", mime="audio/wav", index=6, derivatives=[
                {"src": "https://upload.wikimedia.org/transcoded/x/Track_102.wav.mp3", "type": "audio/mpeg"},
            ]),
            _commons_page(101, index=5),
        ])

    _mock_http(monkeypatch, handler)
    monkeypatch.setattr(catalog, "OPENVERSE_SEARCH_BUDGET_SECONDS", 0.2)
    started = time.monotonic()
    result = await catalog.search_open_music("warm piano", page=2, page_size=18)

    assert time.monotonic() - started < 5
    assert result["catalog"] == "wikimedia_commons"
    assert result["catalog_failures"] == [{"catalog": "openverse", "error": "timed out after 0.2s"}]
    assert [item["id"] for item in result["items"]] == ["commons:101", "commons:102"]
    first, wav = result["items"]
    assert first["license"] == "by"
    assert first["license_name"] == "CC BY 4.0"
    assert first["creator"] == "Jane & Co"
    assert first["preview_url"] == "https://upload.wikimedia.org/wikipedia/commons/a/ab/Track_101.mp3"
    assert "Jane & Co" in first["attribution"] and "Wikimedia Commons" in first["attribution"]
    assert wav["license"] == "cc0"
    assert wav["preview_url"] == "https://upload.wikimedia.org/transcoded/x/Track_102.wav.mp3"
    assert result["total"] == 81
    assert commons_queries[0]["gsrsearch"] == "filetype:audio warm piano"
    assert commons_queries[0]["gsroffset"] == str(catalog.COMMONS_SEARCH_BATCH)


@pytest.mark.asyncio
@pytest.mark.parametrize("openverse", ["http_503", "no_results"])
async def test_search_uses_commons_when_openverse_errors_or_is_empty(monkeypatch, openverse):
    def handler(request: httpx.Request):
        if request.url.host == "api.openverse.org":
            if openverse == "http_503":
                return httpx.Response(503)
            return httpx.Response(200, json={"results": [], "page_count": 0, "result_count": 0})
        return _commons_search_response([_commons_page(101)])

    _mock_http(monkeypatch, handler)
    result = await catalog.search_open_music("ambient", page=1, page_size=18)

    assert result["catalog"] == "wikimedia_commons"
    assert [item["id"] for item in result["items"]] == ["commons:101"]
    expected_failures = [{"catalog": "openverse", "error": "Openverse HTTP 503"}] if openverse == "http_503" else []
    assert result["catalog_failures"] == expected_failures


@pytest.mark.asyncio
async def test_search_fails_only_when_every_catalog_fails(monkeypatch):
    def handler(request: httpx.Request):
        if request.url.host == "api.openverse.org":
            raise httpx.ConnectTimeout("hang", request=request)
        return httpx.Response(502)

    _mock_http(monkeypatch, handler)
    with pytest.raises(catalog.OpenMusicSearchUnavailable) as raised:
        await catalog.search_open_music("ambient", page=1, page_size=18)

    assert raised.value.failures == [
        {"catalog": "openverse", "error": "Openverse timed out"},
        {"catalog": "wikimedia_commons", "error": "Wikimedia Commons HTTP 502"},
    ]
    assert "openverse" in str(raised.value) and "wikimedia_commons" in str(raised.value)


@pytest.mark.parametrize(("code", "expected"), [
    ("cc0", ("cc0", "CC0")),
    ("pd", ("pdm", "Public Domain")),
    ("cc-by-3.0-de", ("by", "CC BY 3.0")),
])
def test_commons_license_policy_accepts_only_pd_cc0_and_by(code, expected):
    track = catalog.normalize_commons_track(_commons_page(1, license_code=code))
    assert (track.license, track.license_name) == expected


@pytest.mark.parametrize("code", ["cc-by-sa-4.0", "cc-by-nc-4.0", "cc-by-nd-3.0", "cc-by-nc-sa-2.0", "gfdl", ""])
def test_commons_license_policy_rejects_restricted_licenses(code):
    with pytest.raises(catalog.OpenMusicLicenseError):
        catalog.normalize_commons_track(_commons_page(1, license_code=code))


@pytest.mark.asyncio
async def test_commons_track_id_round_trips_to_its_catalog(monkeypatch, track_cache):
    lookups: list[dict] = []

    def handler(request: httpx.Request):
        assert request.url.host == "commons.wikimedia.org"
        params = dict(request.url.params)
        lookups.append(params)
        if "pageids" in params:
            return httpx.Response(200, json={"query": {"pages": [_commons_page(int(params["pageids"]))]}})
        return _commons_search_response([_commons_page(4242)])

    async def openverse_down(*_args, **_kwargs):
        raise catalog.OpenMusicCatalogUnavailable("Openverse timed out")

    _mock_http(monkeypatch, handler)
    monkeypatch.setattr(catalog, "_catalog_get", openverse_down)
    listed = (await catalog.search_open_music("ambient", page=1, page_size=18))["items"][0]
    track_cache.clear()  # an import after the search record expired
    track = await catalog.get_open_music_track(listed["id"])

    assert lookups[-1]["pageids"] == "4242"
    assert track.to_dict() == listed
    assert track.catalog == "wikimedia_commons"
    for bad_id in ("jamendo:1", "commons:abc", "commons:"):
        with pytest.raises(catalog.OpenMusicCatalogUnavailable):
            await catalog.get_open_music_track(bad_id)


@pytest.mark.asyncio
async def test_commons_download_stays_on_the_wikimedia_upload_host(monkeypatch, tmp_path):
    async def public_host(_url):
        return None

    monkeypatch.setattr(catalog, "_resolve_public_audio_host", public_host)
    track = catalog.normalize_commons_track(_commons_page(7))

    def handler(request: httpx.Request):
        assert request.url.host == "upload.wikimedia.org"
        if request.url.path.endswith("Track_7.mp3") and "mirror" not in request.url.path:
            return httpx.Response(302, headers={"location": "https://upload.wikimedia.org/mirror/Track_7.mp3"})
        return httpx.Response(200, content=b"ID3music", headers={"content-type": "audio/mpeg"})

    _mock_http(monkeypatch, handler)
    downloaded = await catalog.download_open_music_track(track, str(tmp_path / "ok"), max_bytes=1024)
    assert (downloaded.file_size, downloaded.extension) == (8, "mp3")

    def off_host(request: httpx.Request):
        assert request.url.host == "upload.wikimedia.org"
        return httpx.Response(302, headers={"location": "https://elsewhere.example.com/Track_7.mp3"})

    _mock_http(monkeypatch, off_host)
    with pytest.raises(catalog.OpenMusicDownloadError, match="download host"):
        await catalog.download_open_music_track(track, str(tmp_path / "bad"), max_bytes=1024)
