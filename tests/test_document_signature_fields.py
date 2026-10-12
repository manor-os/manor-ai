"""Visual PDF fields keep the original signing authority and atomic version boundary."""

import asyncio
import base64
import hashlib
import io
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from PIL import Image, ImageDraw
from pydantic import ValidationError
from pypdf import PdfReader, PdfWriter
from pypdf.generic import ArrayObject, DictionaryObject, NameObject, StreamObject

from apps.api.routers.document_permissions import (
    CreateSignatureRequest,
    SubmitSignatureRequest,
    _load_public_document_share,
    _signature_fields,
    _validate_signature_values,
    submit_signature,
)
from packages.core.services.pdf_signature_fields import (
    SignatureField,
    _text_field_image,
    stamp_pdf_fields,
    validate_signature_fields,
)


def _field(id="signature", type="signature", **overrides):
    return dict(id=id, type=type, label=id, required=True, page=1, x=0.2, y=0.2, width=0.4, height=0.06) | overrides


def _fields():
    return [
        _field(height=0.1), _field("name", "name", page=2, y=0.3),
        _field("date", "date", page=2, y=0.4), _field("text", "text", page=2, y=0.5),
        _field("checkbox", "checkbox", page=2, y=0.6, width=0.03, height=0.03),
        _field("optional", "text", page=1, y=0.8, required=False),
    ]


def _signature():
    image = Image.new("RGBA", (240, 72))
    ImageDraw.Draw(image).line([(8, 45), (40, 5), (35, 60), (230, 25)], fill="black", width=3)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode()


def _values():
    return dict(signature=_signature(), name="Adrian Wang", date="2026-09-09", text="Approved - confirmed", checkbox=True)


@pytest.mark.asyncio
async def test_signature_completion_aliases_fail_closed_before_database_access():
    db = AsyncMock()
    token = "sc1." + "A" * 26 + "." + "0" * 64
    with pytest.raises(HTTPException) as read_error:
        await _load_public_document_share(db, token)
    assert read_error.value.status_code == 404
    with pytest.raises(HTTPException) as sign_error:
        await submit_signature(token, SubmitSignatureRequest(consent=True), None, db=db)
    assert sign_error.value.status_code == 409
    db.begin_nested.assert_not_called()
    db.execute.assert_not_called()


@pytest.mark.parametrize("change", [
    {"x": float("nan")}, {"y": float("inf")}, {"width": 0}, {"height": -0.1},
    {"x": 0.9}, {"y": 0.99}, {"page": 0}, {"page": 1.5}, {"id": "bad/id"}, {"label": " "},
    {"x": True}, {"width": "0.3"}, {"required": "false"},
])
def test_signature_field_rejects_invalid_definitions(change):
    with pytest.raises(ValidationError):
        SignatureField.model_validate(_field(**change))


@pytest.mark.parametrize("definitions", [[], [_field(), _field()], [_field(required=False)], [_field(type="text")]])
def test_signature_field_set_requires_unique_ids_and_required_signature(definitions):
    with pytest.raises(HTTPException):
        validate_signature_fields([SignatureField.model_validate(item) for item in definitions])


def test_signature_field_request_bounded_and_submission_types_strict():
    with pytest.raises(ValidationError):
        CreateSignatureRequest(recipient_name="Client", recipient_email="client@example.com", fields=[_field()] * 51)
    with pytest.raises(ValidationError):
        SubmitSignatureRequest(consent=True, field_values={"checkbox": 1})
    with pytest.raises(ValidationError):
        SubmitSignatureRequest(consent="true", field_values={})


@pytest.mark.parametrize("change", [
    {"signature": ""}, {"signature": False}, {"name": " "}, {"name": True},
    {"checkbox": False}, {"checkbox": "true"}, {"date": "2026-02-30"},
    {"date": "20260909"}, {"text": "x" * 2001}, {"text": "bad\x00text"}, {"unknown": "value"},
])
def test_signature_field_values_fail_closed(change):
    with pytest.raises(HTTPException):
        _validate_signature_values({"fields": _fields()}, SubmitSignatureRequest(consent=True, field_values=_values() | change))


def test_signature_field_required_values_cannot_be_bypassed_with_legacy_image():
    with pytest.raises(HTTPException):
        _validate_signature_values({"fields": _fields()}, SubmitSignatureRequest(consent=True, signature_data_url=_signature()))


def test_signature_field_rejects_an_opaque_empty_canvas():
    buffer = io.BytesIO()
    Image.new("RGBA", (200, 60), "white").save(buffer, format="PNG")
    blank = "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode()
    with pytest.raises(HTTPException):
        _validate_signature_values({"fields": [_field()]}, SubmitSignatureRequest(consent=True, field_values={"signature": blank}))


def test_signature_field_evidence_contains_hashes_not_signature_image():
    _definitions, values, images, evidence = _validate_signature_values(
        {"fields": _fields()}, SubmitSignatureRequest(consent=True, field_values=_values() | {"name": "Adrián 王"}),
    )
    assert images["signature"].getbbox()
    assert evidence["name"] == "Adrián 王"
    assert evidence["checkbox"] is True
    assert set(evidence["signature"]) == {"sha256"}
    assert len(evidence["signature"]["sha256"]) == 64
    assert "optional" not in values


def test_signature_fields_legacy_fallback_accepts_old_or_new_payload():
    metadata = {"placement": {"page": 3, "x": 0.1, "y": 0.3, "width": 0.3, "height": 0.1}}
    assert _signature_fields(metadata)[0].id == "signature"
    assert _signature_fields(metadata)[0].page == 3
    for payload in ({"signature_data_url": _signature()}, {"field_values": {"signature": _signature()}}):
        assert _validate_signature_values(metadata, SubmitSignatureRequest(consent=True, **payload))[2]["signature"]


def _source_pdf(rotation=0):
    writer = PdfWriter()
    for number in range(2):
        page = writer.add_blank_page(width=612, height=792)
        font = writer._add_object(DictionaryObject({
            NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }))
        page[NameObject("/Resources")] = writer._add_object(DictionaryObject({
            NameObject("/Font"): DictionaryObject({NameObject("/F1"): font}),
            NameObject("/XObject"): writer._add_object(DictionaryObject()),
        }))
        stream = StreamObject()
        stream._data = f"BT /F1 12 Tf 30 700 Td (Original page {number + 1}) Tj ET\n2 0 0 2 20 20 cm".encode()
        page[NameObject("/Contents")] = writer._add_object(ArrayObject([writer._add_object(stream)]))
        page.cropbox.lower_left = (20, 30)
        page.cropbox.upper_right = (580, 760)
        if rotation:
            page.rotate(rotation)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_signature_fields_stamp_all_pages_preserve_contents_and_visible_coordinates(rotation):
    fields, values, images, _ = _validate_signature_values(
        {"fields": _fields()}, SubmitSignatureRequest(consent=True, field_values=_values()),
    )
    output = stamp_pdf_fields(
        PdfReader(io.BytesIO(_source_pdf(rotation))), fields, values, images,
        signer_email="client@example.com", signed_at=datetime.now(timezone.utc), request_id="fields-test",
    )
    reader = PdfReader(io.BytesIO(output))
    assert len(reader.pages) == 2
    assert reader.metadata["/ManorSignatureStatus"] == "completed"
    for number, page in enumerate(reader.pages):
        assert f"Original page {number + 1}" in page.extract_text()
        assert page.rotation == 0
        assert len(page["/Resources"]["/XObject"]) == (1 if number == 0 else 4)
        contents = page["/Contents"]
        assert contents[0].get_object().get_data() == b"q\n"
        assert contents[-2].get_object().get_data() == b"Q\n"
        operations = page.get_contents().operations
        for index, (operands, operator) in enumerate(operations):
            if operator != b"Do" or not str(operands[0]).startswith("/ManorField"):
                continue
            matrix, op = operations[index - 1]
            assert op == b"cm"
            w, _, _, h, x, y = [float(value) for value in matrix]
            assert float(page.cropbox.left) <= x < x + w <= float(page.cropbox.right) + 0.001
            assert float(page.cropbox.bottom) <= y < y + h <= float(page.cropbox.top) + 0.001


def test_signature_unicode_is_visible_and_never_silently_substituted():
    from pathlib import Path
    from packages.core.services.pdf_signature_fields import _TEXT_FONTS

    latin = _text_field_image("Adrian", 180, 30)
    assert latin.getbbox()
    if any(Path(path).is_file() for path in _TEXT_FONTS):
        assert _text_field_image("Adrián", 180, 30).getbbox()
    else:
        with pytest.raises(HTTPException):
            _text_field_image("Adrián", 180, 30)
    if any(Path(path).is_file() for path in (
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    )):
        chinese = _text_field_image("客户王小明", 180, 30)
        assert chinese.getbbox()
        assert latin.tobytes() != chinese.tobytes()
    else:
        # Minimal hosts fail explicitly; the production API image includes CJK.
        with pytest.raises(HTTPException) as error:
            _text_field_image("客户王小明", 180, 30)
        assert "unsupported" in error.value.detail
    with pytest.raises(HTTPException) as error:
        _text_field_image("unsupported\U0010ffff", 180, 30)
    assert "unsupported" in error.value.detail
    with pytest.raises(HTTPException):
        _text_field_image("Cannot fit this complete sentence", 8, 8)


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", [None, "fields", "revoked", "version_failure"])
async def test_signature_fields_api_validates_before_email_and_signs_one_version(client, db_session, monkeypatch, tmp_path, mutation):
    from pathlib import Path
    from sqlalchemy import func, select
    from packages.core.config import get_settings
    from packages.core.models.document import Document
    from packages.core.models.document_version import DocumentVersion
    from packages.core.models.permission import Share
    from packages.core.services.entity_fs import resolve_path
    from tests.test_document_permissions import _auth, _connect_signature_mailbox

    monkeypatch.setattr(get_settings(), "MANOR_FS_ROOT", str(tmp_path))
    monkeypatch.setattr(get_settings(), "MANOR_FS_ENABLED", True)
    headers = await _auth(client, "fieldowner")
    await _connect_signature_mailbox(client, headers)
    delivery = AsyncMock(return_value=True)
    monkeypatch.setattr("packages.core.services.email_service.send_connected_email", delivery)
    uploaded = await client.post("/api/v1/documents/upload", headers=headers, files={"file": ("agreement.pdf", _source_pdf(), "application/pdf")})
    assert uploaded.status_code == 201, uploaded.text
    document_id = uploaded.json()["id"]
    create_url = f"/api/v1/documents/{document_id}/signature-requests"
    payload = dict(recipient_name="Adrian", recipient_email="client@example.com", fields=_fields())
    for definitions in ([_field(page=3)], [_field(), _field()], [_field(type="text")]):
        invalid = await client.post(create_url, headers=headers, json=payload | {"fields": definitions})
        assert invalid.status_code == 400, invalid.text
    assert (await client.get(create_url, headers=headers)).json() == []
    delivery.assert_not_awaited()
    created = await client.post(create_url, headers=headers, json=payload)
    assert created.status_code == 201, created.text
    assert created.json()["fields"] == _fields()
    token = created.json()["url"].rsplit("/", 1)[-1]
    await client.post(f"/api/v1/shared-doc/{token}/request-otp", json={"email": "client@example.com"})
    import re
    code = re.search(r"\b\d{6}\b", delivery.await_args.kwargs["content"]).group(0)
    verified = await client.post(f"/api/v1/shared-doc/{token}/verify-otp", json={"email": "client@example.com", "code": code})
    assert verified.status_code == 200, verified.text
    preview = await client.get(f"/api/v1/shared-doc/{token}/signature-request")
    assert preview.json()["fields"] == _fields()
    submit_url = f"/api/v1/shared-doc/{token}/signature"
    for values in ({}, _values() | {"checkbox": False}, _values() | {"extra": "bad"}):
        invalid = await client.post(submit_url, json={"consent": True, "field_values": values})
        assert invalid.status_code == 400, invalid.text
    version_count = (await db_session.execute(select(func.count()).select_from(DocumentVersion).where(DocumentVersion.document_id == document_id))).scalar_one()
    # End our read before competing mutation transactions.
    await db_session.rollback()
    if mutation == "version_failure":
        async def fail_version(*_args, **_kwargs):
            raise RuntimeError("simulated version failure")

        monkeypatch.setattr(
            "packages.core.services.version_service.create_version",
            fail_version,
        )
        with pytest.raises(RuntimeError, match="simulated version failure"):
            await client.post(
                submit_url,
                json={"consent": True, "field_values": _values()},
            )
        await db_session.rollback()
        source = await db_session.get(Document, document_id)
        assert Path(resolve_path(source.entity_id, source.fs_path)).read_bytes() == _source_pdf()
        assert not Path(tmp_path, source.entity_id, "agreement-signed.pdf").exists()
        assert (await db_session.execute(
            select(Document).where(
                Document.entity_id == source.entity_id,
                Document.source == "signature",
            )
        )).scalars().all() == []
        share = await db_session.get(Share, created.json()["id"])
        assert share.metadata_["signature_status"] == "pending"
        return
    if mutation:
        import threading
        from apps.api.routers import document_permissions

        started, proceed = threading.Event(), threading.Event()
        stamp = document_permissions.stamp_pdf_fields

        def paused_stamp(*args, **kwargs):
            started.set()
            assert proceed.wait(timeout=10)
            return stamp(*args, **kwargs)

        monkeypatch.setattr(document_permissions, "stamp_pdf_fields", paused_stamp)
        completion = asyncio.create_task(client.post(submit_url, json={"consent": True, "field_values": _values()}))
        try:
            assert await asyncio.to_thread(started.wait, 10)
            share = await db_session.get(Share, created.json()["id"])
            if mutation == "fields":
                updated = dict(share.metadata_)
                updated["fields"] = [dict(field) for field in updated["fields"]]
                updated["fields"][0]["x"] = 0.1
                share.metadata_ = updated
            else:
                share.status = "revoked"
            await db_session.commit()
        finally:
            proceed.set()
        rejected = await asyncio.wait_for(completion, timeout=10)
        assert rejected.status_code == (409 if mutation == "fields" else 404), rejected.text
        row = await db_session.get(Document, document_id)
        assert not (row.metadata_ or {}).get("signature_lock")
        assert Path(resolve_path(row.entity_id, row.fs_path)).read_bytes() == _source_pdf()
        after = (await db_session.execute(select(func.count()).select_from(DocumentVersion).where(DocumentVersion.document_id == document_id))).scalar_one()
        assert after == version_count
        return
    collision_bytes = _source_pdf(rotation=90)
    collision = await client.post(
        "/api/v1/documents/upload",
        headers=headers,
        files={"file": ("agreement-signed.pdf", collision_bytes, "application/pdf")},
    )
    assert collision.status_code == 201, collision.text
    responses = await asyncio.gather(*[
        client.post(submit_url, json={"consent": True, "field_values": _values()}) for _ in range(2)
    ])
    assert sorted(response.status_code for response in responses) == [200, 409], [response.text for response in responses]
    completed = next(response.json() for response in responses if response.status_code == 200)
    downloaded = await client.get(f"/api/v1/shared-doc/{token}/download")
    assert downloaded.status_code == 200, downloaded.text
    assert hashlib.sha256(downloaded.content).hexdigest() == completed["document_sha256"]
    assert len(PdfReader(io.BytesIO(downloaded.content)).pages) == 2
    assert completed["signed_document_name"] == "agreement-2-signed.pdf"
    source = await db_session.get(Document, document_id)
    occupied_copy = await db_session.get(Document, collision.json()["id"])
    signed_copy = await db_session.get(Document, completed["signed_document_id"])
    assert signed_copy is not None
    assert signed_copy.folder_id == source.folder_id
    assert Path(signed_copy.fs_path).parent == Path(source.fs_path).parent
    assert Path(resolve_path(source.entity_id, source.fs_path)).read_bytes() == _source_pdf()
    assert Path(resolve_path(occupied_copy.entity_id, occupied_copy.fs_path)).read_bytes() == collision_bytes
    assert Path(resolve_path(signed_copy.entity_id, signed_copy.fs_path)).read_bytes() == downloaded.content
    source_versions = (await db_session.execute(
        select(func.count()).select_from(DocumentVersion).where(
            DocumentVersion.document_id == document_id
        )
    )).scalar_one()
    signed_versions = (await db_session.execute(
        select(func.count()).select_from(DocumentVersion).where(
            DocumentVersion.document_id == signed_copy.id
        )
    )).scalar_one()
    assert source_versions == version_count
    assert signed_versions == 1
    share = await db_session.get(Share, created.json()["id"])
    assert share.metadata_["field_values"]["name"] == "Adrian Wang"
    assert "data:image" not in str(share.metadata_)
    assert len(share.metadata_["field_values_sha256"]) == 64
