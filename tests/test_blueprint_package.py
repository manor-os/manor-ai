"""A Blueprint package carries the Workspace's own files its App points at.

What these protect: an exported App whose cover is the Workspace's upload
opens with that picture on another account; the platform's own pictures and
https links stay addresses; a package is the same bytes for the same content
and is refused when any file does not match its digest.
"""
from __future__ import annotations

import gzip
import io
import tarfile

import pytest

from packages.core.blueprints.exporter import ExportContext, export_workspace
from packages.core.blueprints.installer import InstallMode, install_blueprint
from packages.core.blueprints.package import (
    FileEmbedder,
    PackageError,
    asset_from_bytes,
    embed_assets,
    pack,
    restore_assets,
    store_assets,
    unpack,
)
from packages.core.governance.approval_policy_service import initialize_workspace_approval
from packages.core.models.base import generate_ulid
from packages.core.models.workspace import Workspace
from packages.core.services.workspace_app_view import normalize_app_view_config

PICTURE = b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"\x00" * 24


def _payload(cover: str) -> dict:
    return {
        "manifest": {"blueprint_version": "1.1", "title": "Studio"},
        "recipe": {"operating_model": {"app_view": {"cover_image_url": cover}}},
    }


def test_a_package_is_the_same_bytes_for_the_same_content_and_refuses_tampering():
    asset = asset_from_bytes(PICTURE, name="cover.webp", mime_type="image/webp")
    payload, assets = embed_assets(_payload("/api/v1/fs/e/cover.webp"), lambda text: asset if text.endswith("cover.webp") else None)
    assert payload["recipe"]["operating_model"]["app_view"]["cover_image_url"] == asset.placeholder
    assert pack(payload, assets) == pack(payload, assets)

    unpacked, carried = unpack(pack(payload, assets))
    assert [item.data for item in carried] == [PICTURE]
    restored = restore_assets(unpacked, {asset.sha256: "/api/v1/fs/other/pic.webp"})
    assert restored["recipe"]["operating_model"]["app_view"]["cover_image_url"] == "/api/v1/fs/other/pic.webp"

    tampered = io.BytesIO()
    with tarfile.open(fileobj=io.BytesIO(gzip.decompress(pack(payload, assets)))) as source, \
            tarfile.open(fileobj=tampered, mode="w") as target:
        for member in source.getmembers():
            data = source.extractfile(member).read()
            if member.name.startswith("package/assets/"):
                data = data[::-1]
            member.size = len(data)
            target.addfile(member, io.BytesIO(data))
    with pytest.raises(PackageError, match="does not match"):
        unpack(gzip.compress(tampered.getvalue()))
    with pytest.raises(PackageError, match="names files"):
        unpack(pack(payload, []))


@pytest.mark.asyncio
async def test_an_uploaded_cover_travels_in_the_package_and_installs_on_another_account(db_session, tmp_path, monkeypatch):
    db = db_session
    entity = generate_ulid()
    (tmp_path / "Knowledge").mkdir()
    (tmp_path / "Knowledge" / "cover.webp").write_bytes(PICTURE)
    stored: dict[str, bytes] = {}
    monkeypatch.setattr("packages.core.services.entity_fs.resolve_path",
                        lambda entity_id, rel: str(tmp_path / rel) if entity_id == entity else None)
    monkeypatch.setattr("packages.core.services.entity_fs.write_entity_file_atomic",
                        lambda entity_id, rel, data, **_kw: stored.setdefault(f"{entity_id}/{rel}", data))

    uploaded = f"/api/v1/fs/{entity}/Knowledge/cover.webp"
    ws = Workspace(
        id=generate_ulid(), entity_id=entity, name="JR Music Studio", kind="music",
        settings={"access_mode": "members_only"}, cover_image_url=uploaded,
        operating_model={"app_view": {
            "visual": "motion",
            "motion_cover_url": "/blueprints/motion/jr-ai-music-wave.mp4?v=2",
            "motion_cover_source_url": uploaded,
        }},
    )
    db.add(ws)
    await db.flush()
    await initialize_workspace_approval(db, ws)
    user = type("User", (), {"id": generate_ulid(), "role": "owner"})()

    # Without a package the upload cannot travel: the cover and the motion
    # cover made for it are left out rather than failing the export.
    plain = await export_workspace(db, ws.id, title=ws.name)
    plain_app = plain["recipe"]["operating_model"]["app_view"]
    assert "cover_image_url" not in plain_app
    assert plain_app["motion_cover_url"] is plain_app["motion_cover_source_url"] is None

    embedder = FileEmbedder(db, entity_id=entity, user=user, workspace_id=ws.id)
    payload = await export_workspace(db, ws.id, title=ws.name, context=ExportContext(file_embedder=embedder))
    [asset] = embedder.assets.values()
    app = payload["recipe"]["operating_model"]["app_view"]
    assert app["cover_image_url"] == app["motion_cover_source_url"] == asset.placeholder
    assert app["motion_cover_url"] == "/blueprints/motion/jr-ai-music-wave.mp4?v=2"

    other = generate_ulid()
    unpacked, carried = unpack(pack(payload, [asset]))
    restored = restore_assets(unpacked, await store_assets(other, carried))
    result = await install_blueprint(db, payload=restored, entity_id=other, mode=InstallMode.SIMULATE)
    installed = normalize_app_view_config((await db.get(Workspace, result.workspace_id)).operating_model["app_view"])
    address = f"/api/v1/fs/{other}/marketplace/blueprint-assets/{asset.sha256}.webp"
    assert installed["cover_image_url"] == installed["motion_cover_source_url"] == address
    assert stored[f"{other}/marketplace/blueprint-assets/{asset.sha256}.webp"] == PICTURE
