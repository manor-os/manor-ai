"""Real image bytes, upload safety and durable conversion receipts."""
from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from PIL import Image
from starlette.datastructures import Headers, UploadFile

from packages.core.ai.runtime import file_actions, generated_files
from packages.core.ai.tools import file_tools
from packages.core.ai.tools.generate_file import document, tool
from packages.core.contracts.file_engine import file_type_capability
from packages.core.models.base import generate_ulid
from packages.core.services import file_context, file_conversion, upload_security
from packages.core.services.entity_fs import get_entity_root
from packages.core.services.image_formats import heif_mime_from_header
from packages.core.services.office_editing import OfficeConversionLimitError
from tests.test_knowledge_file_consistency import _doc_for, _seed_workspace, fs_enabled  # noqa: F401
from tests.test_chat_attachment_read_access import attachment_fs, seed_attachment  # noqa: F401

HEIC = Path(__file__).resolve().parents[1] / "apps/web/e2e/fixtures/chat-upload.heic"


def image_bytes(format="PNG", mode="RGBA", orientation=None):
    output = io.BytesIO()
    image = Image.new(mode, (12, 8), (240, 20, 40, 128) if mode == "RGBA" else (240, 20, 40))
    exif = Image.Exif()
    if orientation:
        exif[274] = orientation
    image.save(output, format=format, exif=exif)
    return output.getvalue()


@pytest.mark.parametrize("source", ["png", "jpg", "jpeg", "webp", "gif", "bmp", "tif", "tiff", "avif", "heic", "heif"])
def test_image_conversion_is_discoverable(source):
    assert "png" in file_type_capability(source)["conversion_targets"]


@pytest.mark.parametrize("source,target,format", [("heic", "png", "PNG"), ("png", "webp", "WEBP"), ("png", "jpg", "JPEG"), ("png", "png", "PNG")])
def test_conversion_produces_requested_format_at_full_resolution(source, target, format):
    original = HEIC.read_bytes() if source == "heic" else image_bytes()
    output = file_conversion._convert_image_file_sync(original, source, target)
    with Image.open(io.BytesIO(output)) as converted:
        assert converted.format == format
        assert converted.size == ((96, 64) if source == "heic" else (12, 8))
        if source == "png" and target in {"png", "webp"}:
            assert converted.convert("RGBA").getpixel((0, 0)) == (240, 20, 40, 128)
        if target == "jpg":
            assert converted.mode == "RGB"
            assert converted.getpixel((0, 0))[1] > 100  # alpha flattened onto white


def test_conversion_applies_exif_orientation_once():
    output = file_conversion._convert_image_file_sync(image_bytes(mode="RGB", orientation=6), "png", "png")
    with Image.open(io.BytesIO(output)) as converted:
        assert converted.size == (8, 12)
        assert not converted.getexif().get(274)


def test_invalid_multiframe_and_oversized_images_are_rejected(monkeypatch):
    with pytest.raises(Exception):
        file_conversion._convert_image_file_sync(b"invalid", "png", "png")
    output = io.BytesIO()
    Image.new("RGB", (12, 8), "red").save(output, format="GIF", save_all=True, append_images=[Image.new("RGB", (12, 8), "blue")])
    with pytest.raises(ValueError, match="frames"):
        file_conversion._convert_image_file_sync(output.getvalue(), "gif", "png")
    monkeypatch.setattr(file_conversion, "IMAGE_CONVERSION_MAX_PIXELS", 95)
    with pytest.raises(OfficeConversionLimitError, match="megapixel"):
        file_conversion._convert_image_file_sync(image_bytes(), "png", "png")


def test_input_and_output_byte_limits(monkeypatch):
    monkeypatch.setattr(file_conversion, "FILE_CONVERSION_MAX_INPUT_BYTES", 10)
    with pytest.raises(OfficeConversionLimitError, match="50 MiB"):
        file_conversion._convert_image_file_sync(image_bytes(), "png", "png")
    monkeypatch.setattr(file_conversion, "FILE_CONVERSION_MAX_INPUT_BYTES", 1024)
    monkeypatch.setattr(file_conversion, "FILE_CONVERSION_MAX_OUTPUT_BYTES", 10)
    with pytest.raises(OfficeConversionLimitError, match="100 MiB"):
        file_conversion._convert_image_file_sync(image_bytes(), "png", "png")


@pytest.mark.asyncio
async def test_misnamed_heic_keeps_security_scan_and_gets_provider_compatible_vision(monkeypatch):
    original = HEIC.read_bytes()
    scanned = []
    monkeypatch.setattr(upload_security, "_clamav_scan", lambda stream: scanned.append(stream.read()))
    assert await upload_security.inspect_upload_content(original, filename="phone.png", declared_content_type="image/png") == "image/heic"
    assert scanned == [original]
    assert file_context._safe_upload_ext("phone.png", "image/heic") == ".heic"
    inline, mime = file_context._prepare_inline_image(original, "image/png")
    assert mime == "image/jpeg"
    with Image.open(io.BytesIO(inline)) as converted:
        assert converted.format == "JPEG" and converted.size == (96, 64)
    for unsafe in (b"MZ" + original, b"not an image"):
        with pytest.raises(upload_security.UploadSecurityError):
            upload_security.validate_upload_header(filename="phone.png", header=unsafe)
    with pytest.raises(upload_security.UploadSecurityError):
        upload_security.validate_upload_header(filename="phone.heic", header=b"invalid")
    assert heif_mime_from_header(b"\x00\x00\x00\x18ftypavif\x00\x00\x00\x00mif1avif") is None


@pytest.mark.asyncio
async def test_heic_still_fails_closed_without_required_scanner(monkeypatch):
    monkeypatch.setattr(upload_security, "_av_required", lambda: True)
    monkeypatch.delenv("CLAMAV_HOST", raising=False)
    with pytest.raises(upload_security.UploadSecurityError) as error:
        await upload_security.inspect_upload_content(HEIC.read_bytes(), filename="phone.png")
    assert error.value.status_code == 503


def _gps_exif() -> Image.Exif:
    """EXIF with GPS coordinates and an orientation that rotates the picture 90 degrees."""
    exif = Image.Exif()
    exif[274] = 6
    gps = exif.get_ifd(0x8825)
    gps[1], gps[2] = "N", (37.0, 46.0, 30.0)
    gps[3], gps[4] = "W", (122.0, 25.0, 0.0)
    exif[0x8825] = gps
    return exif


def _photo_with_gps_and_orientation() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (12, 8), (240, 20, 40)).save(output, format="JPEG", exif=_gps_exif().tobytes())
    return output.getvalue()


async def _store_through_knowledge_upload(client, photo: bytes) -> bytes:
    registered = await client.post(
        "/api/v1/auth/register",
        json={"username": "photo_privacy", "email": "photo-privacy@test.com", "password": "pass123"},
    )
    headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}
    entity_id = (await client.get("/api/v1/auth/me", headers=headers)).json()["entity_id"]
    upload = await client.post(
        "/api/v1/documents/upload", headers=headers,
        files={"file": ("IMG_0001.jpg", photo, "image/jpeg")},
    )
    assert upload.status_code == 201, upload.text
    stored = (Path(get_entity_root(entity_id)) / upload.json()["fs_path"]).read_bytes()
    assert upload.json()["file_size"] == len(stored)
    return stored


async def _store_through_chat_attachment(photo: bytes) -> bytes:
    entity_id = generate_ulid()
    attachment = UploadFile(
        filename="IMG_0001.jpg", file=io.BytesIO(photo), headers=Headers({"content-type": "image/jpeg"}),
    )
    attachments = await file_context.build_file_context([attachment], [], entity_id, db=None)
    reference = attachments.attachment_refs[0]["path"]
    stored = (Path(get_entity_root(entity_id)) / reference).read_bytes()
    # Chat files are named by the digest of the bytes actually saved.
    assert reference == f"uploads/chat/{hashlib.sha256(stored).hexdigest()[:16]}.jpg"
    return stored


@pytest.mark.asyncio
@pytest.mark.usefixtures("fs_enabled")
@pytest.mark.parametrize("entry", ["knowledge", "chat"])
async def test_stored_photo_drops_gps_metadata_and_keeps_orientation(client, entry):
    photo = _photo_with_gps_and_orientation()
    if entry == "knowledge":
        stored = await _store_through_knowledge_upload(client, photo)
    else:
        stored = await _store_through_chat_attachment(photo)
    assert b"Exif\x00\x00" not in stored
    with Image.open(io.BytesIO(stored)) as photo_out:
        assert photo_out.format == "JPEG"
        assert photo_out.size == (8, 12)  # the orientation was applied to the pixels
        assert 0x8825 not in photo_out.getexif()
        assert not photo_out.getexif().get(274)


@pytest.mark.parametrize("format_name,media_type", [
    ("JPEG", "image/jpeg"), ("PNG", "image/png"), ("WEBP", "image/webp"), ("HEIF", "image/heic"),
])
def test_every_supported_photo_format_drops_embedded_exif(format_name, media_type):
    # HEIF is the iPhone default and carries EXIF in info, which encoders can
    # write back out; each format must come out without its GPS tags.
    from packages.core.services.image_formats import register_heif_decoder
    from packages.core.services.image_metadata import strip_image_metadata

    register_heif_decoder()
    source = io.BytesIO()
    kwargs = {"exif": _gps_exif().tobytes()}
    if format_name == "WEBP":
        kwargs["lossless"] = True
    if format_name == "HEIF":
        kwargs["quality"] = 90
    Image.new("RGB", (12, 8), (240, 20, 40)).save(source, format=format_name, **kwargs)

    stored = strip_image_metadata(source.getvalue(), media_type=media_type)

    with Image.open(io.BytesIO(stored)) as photo_out:
        assert 0x8825 not in photo_out.getexif()
        assert not photo_out.info.get("exif")


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,nested", [("image", False), ("image", True), ("document", False)])
async def test_source_conversion_never_calls_an_image_model(monkeypatch, kind, nested):
    runtime = AsyncMock(return_value='{"converted":true}')
    monkeypatch.setattr(document, "runtime_generate_document_file", runtime)
    monkeypatch.setattr(tool, "handle_image", AsyncMock(side_effect=AssertionError("Image provider was called")))
    args = {"source": {"path": "uploads/chat/photo.heic", "expected_sha256": "a" * 64}, "file_type": "png"}
    result = json.loads(await tool._generate_file_handler("entity", kind=kind, name="photo.png", **({"params": args} if nested else args)))
    assert result["converted"]
    assert runtime.call_args.kwargs["source"] == args["source"]
    # The typed bucket only applies inside an execution archive; the handler
    # never prefixes the name itself (that diverged from the receipt).
    assert runtime.call_args.kwargs["default_subdir"] == ("images" if kind == "image" else "documents")
    assert runtime.call_args.kwargs["name"] == "photo.png"


def test_conversion_authorizes_one_exact_output_without_provider_formats(tmp_path):
    from packages.core.ai.runtime.media_outputs import prepare_runtime_media_output_plan
    from packages.core.ai.runtime.authorization_receipts import WorkspaceFileAuthorizationResourceFactory

    args = {"kind": "image", "source": {"path": "phone.heic", "expected_sha256": "a" * 64}, "name": "phone.png", "file_type": "png"}
    assert prepare_runtime_media_output_plan(tool_name="generate_file", arguments=args, artifact_base_dir="", entity_root=str(tmp_path)) is None
    resources = WorkspaceFileAuthorizationResourceFactory.generate_file_resources(args, "phone.png")
    assert [resource.resource_id for resource in resources] == ["images/phone.png"]


@pytest.mark.asyncio
@pytest.mark.usefixtures("fs_enabled")
@pytest.mark.parametrize("outcome", ["success", "read_denied", "approval_denied", "stale", "projection_failure"])
async def test_image_conversion_creates_canonical_knowledge_document(db_session, monkeypatch, outcome):
    from packages.core.services.knowledge_sync import sync_file_to_knowledge

    entity_id, workspace_id = generate_ulid(), generate_ulid()
    await _seed_workspace(db_session, entity_id, workspace_id)
    root = Path(file_tools._get_entity_root(entity_id))
    original = HEIC.read_bytes()
    source_path = root / "photo.heic"
    source_path.write_bytes(original)
    await sync_file_to_knowledge(entity_id=entity_id, abs_path=str(source_path), entity_root=str(root), source="upload", created_by="test")
    source_doc = await _doc_for(db_session, entity_id, "photo.heic")
    source_id = source_doc.id
    source = {"path": "photo.heic", "expected_sha256": "a" * 64 if outcome == "stale" else hashlib.sha256(original).hexdigest()}
    read_guard = AsyncMock(return_value='{"error":"file_permission_denied"}' if outcome == "read_denied" else None)
    write_guard = AsyncMock(return_value='{"error":"approval_required"}' if outcome == "approval_denied" else None)
    monkeypatch.setattr(file_actions, "runtime_guard_file_read_access", read_guard)
    monkeypatch.setattr(file_actions, "runtime_guard_file_mutation", write_guard)
    if outcome == "projection_failure":
        monkeypatch.setattr(generated_files, "runtime_generated_file_metadata", AsyncMock(side_effect=OSError("injected projection failure")))
    result = json.loads(await tool._generate_file_handler(entity_id, kind="image", name="converted.png", file_type="png", source=source, workspace_id=workspace_id))
    assert source_path.read_bytes() == original
    assert (await _doc_for(db_session, entity_id, "photo.heic")).id == source_id
    target = await _doc_for(db_session, entity_id, "converted.png")
    if outcome == "success":
        assert result["created"] and result["converted"], result
        assert target.id == result["document"]["document_id"]
        assert target.file_type == "png"
        assert target.metadata_["origin"]["workspace_id"] == workspace_id
        converted = (root / target.fs_path).read_bytes()
        assert result["document"]["source_sha256"] == hashlib.sha256(converted).hexdigest()
        with Image.open(io.BytesIO(converted)) as image:
            assert image.format == "PNG" and image.size == (96, 64)
    else:
        assert result.get("error"), result
        assert target is None
        assert not list(root.rglob("converted.png"))


@pytest.mark.asyncio
@pytest.mark.parametrize("include_image", [False, True])
async def test_large_uploaded_png_retains_source_hash_and_converts(db_session, attachment_fs, monkeypatch, include_image):  # noqa: F811
    original = image_bytes() + b"\x00" * (file_tools.READ_FILE_MAX_IMAGE_BYTES + 1)
    user, conv, _msg, _ws, source, path = await seed_attachment(db_session, attachment_fs, data=original, extension="png")
    args = {"entity_id": user.entity_id, "user_id": user.id, "conversation_id": conv.id}
    read = json.loads(await file_tools._read_file(**args, path=path, include_image=include_image))
    assert read["read_mode"] == "metadata" and "error" not in read
    # Never silently metadata: the reason the pixels were withheld is explicit.
    assert read["image"]["delivered"] is False and "data_url" not in read["image"]
    assert read["image"]["reason"] == ("include_image_false" if include_image is False else "image_too_large")
    assert read["source_sha256"] == hashlib.sha256(original).hexdigest()
    assert read["size"] == len(original)
    monkeypatch.setattr(file_actions, "runtime_guard_file_mutation", AsyncMock(return_value=None))
    result = json.loads(await tool._generate_file_handler(**args, kind="image", name="converted.png", file_type="png", source={"path": read["resolved_path"], "expected_sha256": read["source_sha256"]}))
    assert result["created"] and result["converted"], result
    assert source.read_bytes() == original
    target = await _doc_for(db_session, user.entity_id, "converted.png")
    with Image.open(attachment_fs / args["entity_id"] / target.fs_path) as image:
        assert image.format == "PNG" and image.size == (12, 8)


@pytest.mark.asyncio
@pytest.mark.usefixtures("fs_enabled")
@pytest.mark.parametrize("outcome", ["jpeg", "not_image", "http", "document_kind"])
async def test_public_image_url_is_saved_as_a_real_image(db_session, monkeypatch, outcome):
    """A post cover is fetched once and stored only if its bytes decode as an image."""
    from packages.core.services import code_projects

    entity_id, workspace_id = generate_ulid(), generate_ulid()
    await _seed_workspace(db_session, entity_id, workspace_id)
    root = Path(file_tools._get_entity_root(entity_id))
    body = b"<html>login</html>" if outcome == "not_image" else image_bytes("JPEG", mode="RGB")
    download = AsyncMock(return_value=body)
    monkeypatch.setattr(code_projects, "download_public_https", download)
    monkeypatch.setattr(file_actions, "runtime_guard_file_mutation", AsyncMock(return_value=None))
    url = ("http" if outcome == "http" else "https") + "://cdn.example.com/cover.jpg?sig=1"
    kind = "document" if outcome == "document_kind" else "image"
    result = json.loads(await tool._generate_file_handler(
        entity_id, kind=kind, name="cover.jpg", file_type="jpg", source={"url": url}, workspace_id=workspace_id,
    ))
    target = await _doc_for(db_session, entity_id, "cover.jpg")
    if outcome == "jpeg":
        assert result["created"] and result["converted"], result
        assert target.id == result["document"]["document_id"] and target.file_type == "jpg"
        with Image.open(io.BytesIO((root / target.fs_path).read_bytes())) as image:
            assert image.format == "JPEG" and image.size == (12, 8)
        download.assert_awaited_once()
    else:
        assert result.get("error"), result
        assert target is None
        if outcome in {"http", "document_kind"}:
            download.assert_not_awaited()
