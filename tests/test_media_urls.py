"""Signed display-media URLs: signing contract, eligibility, and the Knowledge list.

The Workspace App grid scenario is covered in tests/test_workspace_app_data.py
(inspection media).
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import os
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
from types import SimpleNamespace
from urllib.parse import parse_qsl, urlsplit

import pytest
from httpx import AsyncClient

from packages.core.services import media_urls
from packages.core.services.media_urls import (
    MAX_LIFETIME_SECONDS,
    MediaSigningKeyError,
    MediaUrlSigner,
    content_version,
    parse_signing_keys,
    request_is_browser_session,
    variant_version,
    verify_media_url,
    window_expiry,
    with_recorded_variants,
)

OLD = b"old-media-signing-secret-000000000"
NEW = b"new-media-signing-secret-111111111"
NOW = 1_800_000_123


def _doc(**overrides):
    values = dict(
        id="01JDOC0000000000000000000A", entity_id="01JENT0000000000000000000A", name="cover.png",
        file_type="png", mime_type="image/png", fs_path="Media/cover.png", file_size=42,
        content_sha256="ab" * 32, classification="internal", quarantine_status="clean", is_trashed=False,
        metadata_={},
    )
    values.update(overrides)
    doc = SimpleNamespace(**values)
    if "metadata_" not in overrides:
        doc.metadata_ = with_recorded_variants({}, version=content_version(doc), variants=["thumb"])
    return doc


def _split(url: str) -> tuple[str, dict[str, str]]:
    parts = urlsplit(url)
    return parts.path, dict(parse_qsl(parts.query))


def _signer(secret: bytes = NEW, key_id: str = "k2", now: int = NOW) -> MediaUrlSigner:
    return MediaUrlSigner(key_id=key_id, secret=secret, user_id="01JUSR0000000000000000000A", expires=window_expiry(now))


def test_signed_url_verifies_and_every_field_is_bound():
    path, query = _split(_signer().thumbnail_url(_doc()))
    keys = {"k1": OLD, "k2": NEW}
    assert path.startswith("/media/v1/thumb/01JENT0000000000000000000A/01JDOC0000000000000000000A/s")
    assert verify_media_url(path, query, keys, now=NOW)
    tampered_paths = [
        path.replace("01JDOC0000000000000000000A", "01JDOC0000000000000000000B"),
        path.replace("01JENT0000000000000000000A", "01JENT0000000000000000000B"),
        path.replace("/s", "/f", 1),
        path.replace("/thumb/", "/display/"),
    ]
    for tampered in tampered_paths:
        assert not verify_media_url(tampered, query, keys, now=NOW), tampered
    for field, value in {"e": str(int(query["e"]) - 600), "u": "01JUSR0000000000000000000B", "k": "k1", "s": query["s"][:-2] + "AA"}.items():
        assert not verify_media_url(path, {**query, field: value}, keys, now=NOW), field
    # Expired, unknown key, and a far-future expiry no signer could have issued.
    assert not verify_media_url(path, query, keys, now=int(query["e"]))
    assert not verify_media_url(path, query, {"k1": OLD}, now=NOW)
    future = MediaUrlSigner(key_id="k2", secret=NEW, user_id="01JUSR0000000000000000000A", expires=NOW + 3 * MAX_LIFETIME_SECONDS)
    future_path, future_query = _split(future.thumbnail_url(_doc()))
    assert not verify_media_url(future_path, future_query, keys, now=NOW)


def test_rotation_keeps_old_urls_valid_while_both_keys_are_listed():
    path, query = _split(_signer(OLD, "k1").thumbnail_url(_doc()))
    assert verify_media_url(path, query, parse_signing_keys(f"k2={NEW.decode()},k1={OLD.decode()}"), now=NOW)
    assert not verify_media_url(path, query, parse_signing_keys(f"k2={NEW.decode()}"), now=NOW)


def test_expiry_is_window_rounded_so_urls_repeat_inside_a_window():
    window_start = NOW - NOW % 600
    assert window_expiry(window_start) == window_expiry(window_start + 599) == window_start + 1800
    assert 1200 < window_expiry(window_start + 599) - (window_start + 599) <= MAX_LIFETIME_SECONDS
    assert _signer(now=window_start).thumbnail_url(_doc()) == _signer(now=window_start + 599).thumbnail_url(_doc())
    assert _signer(now=window_start).thumbnail_url(_doc()) != _signer(now=window_start + 600).thumbnail_url(_doc())


@pytest.mark.parametrize("raw", ["k1", "k1=short", "bad kid=" + "x" * 40, f"k1={'x' * 40},k1={'y' * 40}"])
def test_malformed_signing_keys_are_rejected(raw):
    with pytest.raises(MediaSigningKeyError):
        parse_signing_keys(raw)


def test_version_ignores_updated_at_and_falls_back_to_source_identity():
    doc = _doc(content_sha256=None, metadata_={"file_integrity": {"mtime_ns": 7}})
    fallback = content_version(doc)
    assert fallback and fallback.startswith("f")
    doc.updated_at = "later"
    doc.metadata_ = with_recorded_variants(doc.metadata_, version=fallback, variants=["thumb"])
    assert content_version(doc) == fallback
    doc.file_size = 43
    assert content_version(doc) != fallback
    assert content_version(_doc(content_sha256=None, metadata_={})) is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"classification": "restricted"},
        {"classification": "RESTRICTED"},
        {"is_trashed": True},
        {"quarantine_status": "pending_scan"},
        {"fs_path": None, "file_url": "https://cdn.example/cover.png"},
        {"name": "logo.svg", "file_type": "svg", "mime_type": "image/svg+xml"},
        {"name": "page.html", "file_type": "html", "mime_type": "text/html"},
        {"name": "report.pdf", "file_type": "pdf", "mime_type": "application/pdf"},
        # The worker has not written this version yet, or wrote an older one.
        {"metadata_": {}},
        {"metadata_": {"display_variants": {"version": "s" + "0" * 32, "variants": ["thumb"]}}},
    ],
)
def test_ineligible_documents_never_get_a_url(overrides):
    doc = _doc(**overrides)
    assert variant_version(doc, "thumb") is None
    assert _signer().thumbnail_url(doc) is None


def test_eligible_video_poster_is_signed():
    assert variant_version(_doc(name="clip.mp4", file_type="mp4", mime_type="video/mp4"), "thumb")


def _request(headers=None, **claims):
    return SimpleNamespace(
        headers=headers if headers is not None else {"x-manor-session": "1"},
        state=SimpleNamespace(auth_claims={"sub": "u", **claims}, auth_client=claims.get("azp"), impersonation=None),
    )


@pytest.mark.parametrize(
    ("request_", "allowed"),
    [
        (_request(), True),
        (_request(headers={}), False),
        (_request(azp="pat:01JPAT"), False),
        (_request(azp="mcp-client", scp=["workspaces:read"]), False),
        (_request(act={"sub": "agent"}), False),
        (_request(typ="collaborator_session"), False),
        (_request(typ="impersonation"), False),
        (None, False),
    ],
)
def test_only_first_party_browser_sessions_receive_urls(request_, allowed):
    assert request_is_browser_session(request_) is allowed


@pytest.fixture
def media_enabled(monkeypatch, tmp_path):
    from packages.core.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "MEDIA_SIGNED_URLS_ENABLED", True)
    monkeypatch.setattr(settings, "MEDIA_URL_SIGNING_KEYS", f"k1={OLD.decode()}")
    monkeypatch.setattr(settings, "MANOR_FS_ENABLED", True)
    monkeypatch.setattr(settings, "MANOR_FS_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "DEPLOYMENT_MODE", "oss")
    return tmp_path


def test_signing_is_off_by_default_and_without_keys(monkeypatch):
    from packages.core.config import Settings, get_settings

    assert Settings().MEDIA_SIGNED_URLS_ENABLED is False
    monkeypatch.setattr(get_settings(), "MEDIA_SIGNED_URLS_ENABLED", True)
    monkeypatch.setattr(get_settings(), "MEDIA_URL_SIGNING_KEYS", "")
    assert media_urls.signer_for_request(_request(), SimpleNamespace(id="01JUSR0000000000000000000A")) is None


def test_worker_and_api_can_publish_the_same_thumbnail_concurrently(tmp_path, monkeypatch):
    from PIL import Image

    from packages.core.services.media_variants import publish_variant_bytes, render_image_thumbnail

    source = tmp_path / "image.png"
    target = tmp_path / "thumb.jpg"
    api_thumbnail = io.BytesIO()
    Image.new("RGB", (64, 64), "teal").save(source)
    Image.new("RGB", (64, 64), "blue").save(api_thumbnail, format="JPEG")
    ready = Barrier(2, timeout=5)
    replace = os.replace

    def publish_together(temporary, destination):
        if destination == str(target):
            ready.wait()
        replace(temporary, destination)

    monkeypatch.setattr(os, "replace", publish_together)
    with ThreadPoolExecutor(max_workers=2) as workers:
        worker = workers.submit(render_image_thumbnail, str(source), str(target))
        api = workers.submit(publish_variant_bytes, api_thumbnail.getvalue(), str(target))
        worker.result()
        api.result()

    with Image.open(target) as image:
        image.load()
        assert image.size == (64, 64)
    assert set(tmp_path.iterdir()) == {source, target}


@pytest.mark.asyncio
@pytest.mark.integration
async def test_knowledge_browse_signs_worker_recorded_thumbnails_for_browser_sessions_only(
    client: AsyncClient, media_enabled, monkeypatch,
):
    from PIL import Image

    import packages.core.database as db_module
    from packages.core.models.document import Document
    from packages.core.services.media_variants import generate_display_variants, variant_path
    from packages.core.tasks import media_tasks

    dispatched: list[str] = []
    monkeypatch.setattr(media_tasks.generate_display_variants_task, "delay", dispatched.append)

    token = (await client.post("/api/v1/auth/register", json={
        "username": "mediabrowse", "email": "mediabrowse@test.com", "password": "pass123",
    })).json()["access_token"]
    bearer = {"Authorization": f"Bearer {token}"}
    browser = {**bearer, "X-Manor-Session": "1"}
    image = io.BytesIO()
    Image.new("RGB", (1200, 800), "teal").save(image, format="PNG")
    upload = await client.post(
        "/api/v1/documents/upload", headers=bearer,
        files={"file": ("cover.png", image.getvalue(), "image/png")},
    )
    assert upload.status_code in {200, 201}, upload.text
    doc_id, entity_id = upload.json()["id"], upload.json()["entity_id"]
    # The upload commits after Knowledge sync returns; the task is queued on that commit.
    assert dispatched == [doc_id]

    async def listed(headers):
        response = await client.get("/api/v1/documents/browse-page?scope=all", headers=headers)
        assert response.status_code == 200, response.text
        return next(item for item in response.json()["items"] if item["id"] == doc_id)

    # Nothing is signed before the worker has written and recorded the variant.
    assert (await listed(browser))["thumbnail_url"] is None
    assert await generate_display_variants(doc_id) == "ready"
    async with db_module.async_session() as db:
        stored = await db.get(Document, doc_id)
        version = content_version(stored)
        # The digest is of the uploaded bytes (not of a descriptor already read to its end).
        assert stored.content_sha256 == hashlib.sha256(image.getvalue()).hexdigest()
        assert version == "s" + stored.content_sha256[:32]
    thumb = variant_path(str(media_enabled / entity_id), doc_id, version)
    with Image.open(thumb) as rendered:
        assert rendered.format == "JPEG" and max(rendered.size) <= 640

    path, query = _split((await listed(browser))["thumbnail_url"])
    assert path == f"/media/v1/thumb/{entity_id}/{doc_id}/{version}.jpg"
    assert verify_media_url(path, query, {"k1": OLD})
    # Bearer callers without the browser session header (API keys, MCP adapters) get none.
    assert (await listed(bearer))["thumbnail_url"] is None
    # A recorded variant whose file went missing answers 404; the API thumbnail
    # the browser falls back to puts it back.
    os.remove(thumb)
    assert (await client.get(f"/api/v1/documents/{doc_id}/thumbnail", headers=bearer)).status_code == 200
    assert os.path.isfile(thumb)
    async with db_module.async_session() as db:
        stored = await db.get(Document, doc_id)
        stored.classification = "restricted"
        await db.commit()
    assert (await listed(browser))["thumbnail_url"] is None


@pytest.mark.asyncio
@pytest.mark.integration
async def test_backfill_reaches_older_media_until_each_has_a_record_without_touching_modified_dates(
    client: AsyncClient, media_enabled, monkeypatch,
):
    from PIL import Image

    import packages.core.database as db_module
    from packages.core.models.document import Document
    from packages.core.services import media_variants
    from packages.core.services.media_variants import backfill_display_variants, generate_display_variants
    from packages.core.tasks import media_tasks

    dispatched: list[str] = []
    monkeypatch.setattr(media_tasks.generate_display_variants_task, "delay", dispatched.append)
    token = (await client.post("/api/v1/auth/register", json={
        "username": "mediabackfill", "email": "mediabackfill@test.com", "password": "pass123",
    })).json()["access_token"]
    image = io.BytesIO()
    Image.new("RGB", (64, 64), "teal").save(image, format="PNG")

    doc_ids = []
    for name in ("present.png", "gone.png"):
        upload = await client.post(
            "/api/v1/documents/upload", headers={"Authorization": f"Bearer {token}"},
            files={"file": (name, image.getvalue(), "image/png")},
        )
        assert upload.status_code in {200, 201}, upload.text
        doc_ids.append(upload.json()["id"])
    present_id, gone_id = doc_ids

    async with db_module.async_session() as db:
        gone = await db.get(Document, gone_id)
        gone_path = media_enabled / gone.entity_id / gone.fs_path
    os.remove(gone_path)

    dispatched.clear()
    assert await backfill_display_variants(limit=10) == 2
    assert set(dispatched) == {present_id, gone_id}

    async def modified_at(document_id):
        async with db_module.async_session() as db:
            return (await db.get(Document, document_id)).updated_at

    before = await modified_at(present_id)
    loop = asyncio.get_running_loop()
    first_render_started = asyncio.Event()
    finish_failure = Event()
    render = media_variants.render_image_thumbnail

    def fail_first_render(_source, _target):
        loop.call_soon_threadsafe(first_render_started.set)
        if not finish_failure.wait(timeout=10):
            raise TimeoutError("second thumbnail render did not finish")
        raise OSError("first render failed")

    monkeypatch.setattr(media_variants, "render_image_thumbnail", fail_first_render)
    slow_render = asyncio.create_task(generate_display_variants(present_id))
    try:
        await asyncio.wait_for(first_render_started.wait(), timeout=10)
        monkeypatch.setattr(media_variants, "render_image_thumbnail", render)
        assert await generate_display_variants(present_id) == "ready"
    finally:
        finish_failure.set()
        slow_result = await slow_render
    assert slow_result == "render_failed"
    async with db_module.async_session() as db:
        present = await db.get(Document, present_id)
        version = content_version(present)
        assert version and variant_version(present, "thumb") == version

    assert await modified_at(present_id) == before
    assert await generate_display_variants(gone_id) == "missing"

    dispatched.clear()
    assert await backfill_display_variants(limit=10) == 0
    assert dispatched == []


@pytest.mark.asyncio
@pytest.mark.integration
async def test_every_writer_schedules_a_thumbnail_once_after_commit(client: AsyncClient, media_enabled, monkeypatch):
    # MCP files, generated media, video exports and sandbox rows insert or
    # upsert Documents directly, never through Knowledge sync.
    import packages.core.database as db_module
    from packages.core.models.document import Document
    from packages.core.services.media_urls import with_recorded_variants
    from packages.core.tasks import media_tasks

    dispatched: list[str] = []
    monkeypatch.setattr(media_tasks.generate_display_variants_task, "delay", dispatched.append)
    token = (await client.post("/api/v1/auth/register", json={
        "username": "mediawriters", "email": "mediawriters@test.com", "password": "pass123",
    })).json()["access_token"]
    entity_id = (await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})).json()["entity_id"]

    def row(name: str, mime: str, size: int = 10) -> Document:
        return Document(entity_id=entity_id, name=name, fs_path=f"generated/{name}", file_size=size,
                        mime_type=mime, source="mcp")

    async with db_module.async_session() as db:
        image, clip, text_file = row("frame.png", "image/png"), row("clip.mp4", "video/mp4"), row("notes.md", "text/markdown")
        db.add_all([image, clip, text_file])
        await db.flush()
        image_id, clip_id = image.id, clip.id
        # Nothing is queued before the rows exist for the worker to read.
        assert dispatched == []
        await db.commit()
        assert sorted(dispatched) == sorted([image_id, clip_id])

        dispatched.clear()
        rolled_back = row("discarded.png", "image/png")
        db.add(rolled_back)
        await db.flush()
        await db.rollback()
        assert dispatched == []

        # New bytes at the same path schedule again; unrelated edits do not.
        image = await db.get(Document, image_id)
        image.name = "renamed.png"
        await db.commit()
        assert dispatched == []
        image.file_size = 20
        await db.commit()
        assert dispatched == [image_id]
        image = await db.get(Document, image_id)

        # The worker recording the current version's thumb does not loop.
        dispatched.clear()
        image.content_sha256 = "ab" * 32
        image.metadata_ = with_recorded_variants(image.metadata_, version="s" + "ab" * 16, variants=["thumb"])
        await db.commit()
        assert dispatched == []


@pytest.mark.asyncio
@pytest.mark.integration
async def test_writers_schedule_nothing_while_signing_is_off(client: AsyncClient, monkeypatch):
    import packages.core.database as db_module
    from packages.core.config import get_settings
    from packages.core.models.document import Document
    from packages.core.tasks import media_tasks

    monkeypatch.setattr(get_settings(), "MEDIA_SIGNED_URLS_ENABLED", False)
    dispatched: list[str] = []
    monkeypatch.setattr(media_tasks.generate_display_variants_task, "delay", dispatched.append)
    token = (await client.post("/api/v1/auth/register", json={
        "username": "mediaoff", "email": "mediaoff@test.com", "password": "pass123",
    })).json()["access_token"]
    entity_id = (await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})).json()["entity_id"]
    async with db_module.async_session() as db:
        db.add(Document(entity_id=entity_id, name="a.png", fs_path="a.png", file_size=1, mime_type="image/png"))
        await db.commit()
    assert dispatched == []
