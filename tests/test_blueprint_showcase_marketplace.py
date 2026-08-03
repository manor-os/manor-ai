"""Marketplace Blueprint showcase, favorites, and remix provenance."""
from __future__ import annotations

from pathlib import Path

import pytest
from httpx import AsyncClient

from packages.core.config import get_settings
from packages.core.models.blueprint import WorkspaceBlueprint
from tests.marketplace_helpers import _force_fallback_verification, _register


pytestmark = pytest.mark.asyncio


async def _make_blueprint(
    db_session,
    entity_id: str,
    *,
    slug: str,
    status: str = "published",
) -> WorkspaceBlueprint:
    blueprint = WorkspaceBlueprint(
        entity_id=entity_id,
        slug=slug,
        title="Marketplace Blueprint",
        summary="A Blueprint with real outcomes.",
        tags=["marketplace"],
        payload={
            "manifest": {
                "blueprint_version": "1.1",
                "title": "Marketplace Blueprint",
                "author": {"display_name": "Blueprint Author"},
            },
        },
        payload_version="1.1",
        status=status,
    )
    db_session.add(blueprint)
    await db_session.commit()
    return blueprint


async def test_blueprint_favorites_are_cross_tenant_marketplace_signals(
    client: AsyncClient,
    db_session,
    monkeypatch,
):
    _force_fallback_verification(monkeypatch)
    owner_headers, owner_entity_id = await _register(client, "bp_market_owner")
    user_headers, _ = await _register(client, "bp_market_user")
    blueprint = await _make_blueprint(
        db_session,
        owner_entity_id,
        slug="marketplace-signals",
    )

    response = await client.post(
        f"/api/v1/blueprints/{blueprint.id}/favorite",
        headers=user_headers,
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"is_favorited": True, "favorite_count": 1}

    detail = await client.get(
        f"/api/v1/blueprints/{blueprint.id}",
        headers=owner_headers,
    )
    assert detail.status_code == 200, detail.text
    assert detail.json()["favorite_count"] == 1
    assert detail.json()["is_favorited"] is False

    listing = await client.get("/api/v1/blueprints", headers=user_headers)
    entry = next(item for item in listing.json() if item["id"] == blueprint.id)
    assert entry["favorite_count"] == 1
    assert entry["is_favorited"] is True

    response = await client.post(
        f"/api/v1/blueprints/{blueprint.id}/favorite",
        headers=user_headers,
    )
    assert response.json() == {"is_favorited": False, "favorite_count": 0}


async def test_draft_blueprint_rejects_favorite_action(
    client: AsyncClient,
    db_session,
    monkeypatch,
):
    _force_fallback_verification(monkeypatch)
    owner_headers, owner_entity_id = await _register(client, "bp_draft_owner")
    user_headers, _ = await _register(client, "bp_draft_user")
    blueprint = await _make_blueprint(
        db_session,
        owner_entity_id,
        slug="draft-marketplace",
        status="draft",
    )

    response = await client.post(
        f"/api/v1/blueprints/{blueprint.id}/favorite",
        headers=user_headers,
    )
    assert response.status_code == 404

    owner_detail = await client.get(
        f"/api/v1/blueprints/{blueprint.id}",
        headers=owner_headers,
    )
    assert owner_detail.status_code == 200


async def test_showcase_uploads_image_and_video_as_public_copies(
    client: AsyncClient,
    db_session,
    monkeypatch,
    tmp_path: Path,
):
    _force_fallback_verification(monkeypatch)
    settings = get_settings()
    old_enabled = settings.MANOR_FS_ENABLED
    old_root = settings.MANOR_FS_ROOT
    old_mode = settings.DEPLOYMENT_MODE
    settings.MANOR_FS_ENABLED = True
    settings.MANOR_FS_ROOT = str(tmp_path)
    settings.DEPLOYMENT_MODE = "oss"
    try:
        headers, entity_id = await _register(client, "bp_showcase_owner")
        blueprint = await _make_blueprint(
            db_session,
            entity_id,
            slug="showcase-upload",
            status="draft",
        )

        response = await client.post(
            f"/api/v1/blueprints/{blueprint.id}/showcase-assets",
            headers=headers,
            data={"caption": "Finished dashboard", "alt_text": "Dashboard result"},
            files={"file": ("result.png", b"\x89PNG\r\n\x1a\nshowcase", "image/png")},
        )
        assert response.status_code == 201, response.text
        payload = response.json()
        assert payload["status"] == "draft"
        assert payload["showcase_assets"][0]["kind"] == "image"
        assert payload["showcase_assets"][0]["caption"] == "Finished dashboard"
        assert payload["cover_image_url"] == payload["showcase_assets"][0]["url"]

        public_url = payload["showcase_assets"][0]["url"]
        public_response = await client.get(public_url)
        assert public_response.status_code == 200
        assert public_response.content.startswith(b"\x89PNG\r\n\x1a\n")

        stored = (
            tmp_path
            / entity_id
            / "marketplace"
            / "blueprints"
            / blueprint.id
        )
        assert [path.suffix for path in stored.iterdir()] == [".png"]

        asset_id = payload["showcase_assets"][0]["id"]
        response = await client.delete(
            f"/api/v1/blueprints/{blueprint.id}/showcase-assets/{asset_id}",
            headers=headers,
        )
        assert response.status_code == 200, response.text
        assert response.json()["showcase_assets"] == []
        assert not any(stored.iterdir())

        video_bytes = b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isommp41"
        response = await client.post(
            f"/api/v1/blueprints/{blueprint.id}/showcase-assets",
            headers=headers,
            data={"caption": "Finished automation walkthrough"},
            files={"file": ("walkthrough.mp4", video_bytes, "video/mp4")},
        )
        assert response.status_code == 201, response.text
        video_asset = response.json()["showcase_assets"][0]
        assert video_asset["kind"] == "video"
        assert video_asset["mime_type"] == "video/mp4"
        assert response.json()["cover_image_url"] is None

        public_response = await client.get(video_asset["url"])
        assert public_response.status_code == 200
        assert public_response.headers["content-type"].startswith("video/mp4")
        assert public_response.content == video_bytes
        assert [path.suffix for path in stored.iterdir()] == [".mp4"]

        response = await client.delete(
            f"/api/v1/blueprints/{blueprint.id}/showcase-assets/{video_asset['id']}",
            headers=headers,
        )
        assert response.status_code == 200, response.text
        assert not any(stored.iterdir())
    finally:
        settings.MANOR_FS_ENABLED = old_enabled
        settings.MANOR_FS_ROOT = old_root
        settings.DEPLOYMENT_MODE = old_mode


async def test_published_blueprint_content_and_record_are_immutable(
    client: AsyncClient,
    db_session,
    monkeypatch,
):
    _force_fallback_verification(monkeypatch)
    owner_headers, owner_entity_id = await _register(client, "bp_immutable_owner")
    blueprint = await _make_blueprint(
        db_session,
        owner_entity_id,
        slug="immutable-published-blueprint",
    )

    response = await client.put(
        f"/api/v1/blueprints/{blueprint.id}",
        headers=owner_headers,
        json={"title": "Changed after publication"},
    )
    assert response.status_code == 409, response.text

    response = await client.post(
        f"/api/v1/blueprints/{blueprint.id}/showcase-assets",
        headers=owner_headers,
        files={"file": ("result.png", b"\x89PNG\r\n\x1a\nshowcase", "image/png")},
    )
    assert response.status_code == 409, response.text

    response = await client.delete(
        f"/api/v1/blueprints/{blueprint.id}",
        headers=owner_headers,
    )
    assert response.status_code == 409, response.text

    detail = await client.get(
        f"/api/v1/blueprints/{blueprint.id}",
        headers=owner_headers,
    )
    assert detail.status_code == 200, detail.text
    assert detail.json()["title"] == "Marketplace Blueprint"
    assert detail.json()["status"] == "published"
