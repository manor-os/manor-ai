from __future__ import annotations

import pytest

from packages.core.services import open_music_catalog as catalog


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
