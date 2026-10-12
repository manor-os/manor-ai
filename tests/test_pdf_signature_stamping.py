"""Pure regressions for preserving PDF content when applying signatures."""

import io
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException, Request, Response
from PIL import Image
from pypdf import PdfReader, PdfWriter
from pypdf.generic import ArrayObject, DictionaryObject, NameObject, StreamObject

from apps.api.routers.document_permissions import (
    ShareOtpRequest,
    ShareOtpVerifyRequest,
    SignaturePlacement,
    _normalize_share_otp_email,
    _normalize_signature_email,
    _stamp_pdf_signature,
    request_shared_doc_otp,
    verify_shared_doc_otp,
)
from packages.core.services.share_access import verify_share_access


def _source_pdf(contents_kind: str) -> bytes:
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject({
            NameObject("/F1"): writer._add_object(DictionaryObject({
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            })),
        }),
    })
    original_text = b"BT /F1 12 Tf 30 740 Td (Original contract terms) Tj ET\n"
    changed_state = b"1 0 0 rg\n2 0 0 2 120 80 cm\n"
    stream_bytes = (
        [original_text + changed_state]
        if contents_kind == "single"
        else [original_text, changed_state]
    )
    streams = ArrayObject()
    for data in stream_bytes:
        stream = StreamObject()
        stream._data = data
        streams.append(writer._add_object(stream))
    page[NameObject("/Contents")] = (
        streams[0]
        if contents_kind == "single"
        else writer._add_object(streams)
        if contents_kind == "indirect_array"
        else streams
    )
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


@pytest.mark.parametrize("contents_kind", ["single", "direct_array", "indirect_array"])
def test_signature_preserves_original_pdf_text_and_isolates_graphics_state(contents_kind):
    stamped = _stamp_pdf_signature(
        _source_pdf(contents_kind),
        Image.new("RGBA", (200, 60), (0, 0, 0, 255)),
        placement=SignaturePlacement(),
        signer_email="client@example.com",
        signed_at=datetime(2026, 9, 9, tzinfo=timezone.utc),
        request_id="regression-request",
    )
    page = PdfReader(io.BytesIO(stamped)).pages[0]
    text = page.extract_text()
    assert "Original contract terms" in text
    assert "Signed by client@example.com" in text

    contents = page["/Contents"]
    assert all(isinstance(item.get_object(), StreamObject) for item in contents)
    assert contents[0].get_object().get_data() == b"q\n"
    assert contents[-2].get_object().get_data() == b"Q\n"
    assert b"2 0 0 2 120 80 cm" in b"".join(
        item.get_object().get_data() for item in contents[1:-2]
    )

    # Walk the relevant graphics operators: neither the original red fill nor
    # its transform may leak into the caption or signature image.
    state = {"color": (0, 0, 0), "transforms": []}
    stack = []
    signature_drawn = False
    caption_drawn = False
    for operands, operator in page.get_contents().operations:
        if operator == b"q":
            stack.append({"color": state["color"], "transforms": list(state["transforms"])})
        elif operator == b"Q":
            state = stack.pop()
        elif operator == b"rg":
            state["color"] = tuple(operands)
        elif operator == b"cm":
            state["transforms"].append(tuple(operands))
        elif operator == b"Do" and str(operands[0]).startswith("/ManorSignature"):
            signature_drawn = True
            assert state["color"] == (0, 0, 0)
            assert len(state["transforms"]) == 1
        elif operator == b"Tj" and str(operands[0]).startswith("Signed by"):
            caption_drawn = True
            assert state == {"color": (0, 0, 0), "transforms": []}
    assert signature_drawn and caption_drawn


def test_signature_email_normalizes_international_domain():
    email = _normalize_signature_email(" Client@Bücher.Example ")
    assert email == "client@xn--bcher-kva.example"
    assert email.isascii()


@pytest.mark.parametrize("email", ["客户@example.com", "élise@example.com"])
def test_signature_email_rejects_non_ascii_local_part(email):
    with pytest.raises(HTTPException) as error:
        _normalize_signature_email(email)
    assert error.value.status_code == 400


def _otp_route_fixture(monkeypatch):
    share = SimpleNamespace(
        id="signature-otp-regression",
        entity_id="signature-otp-entity",
        audience="email:client@xn--bcher-kva.example",
        metadata_={"kind": "signature_request"},
        require_otp=True,
        expires_at=None,
    )
    db = SimpleNamespace(rollback=AsyncMock(), commit=AsyncMock())
    monkeypatch.setattr(
        "apps.api.routers.document_permissions._load_public_document_share",
        AsyncMock(return_value=(share, None, datetime.now(timezone.utc))),
    )
    monkeypatch.setattr("apps.api.routers.document_permissions._limit_share_otp", AsyncMock())
    send = AsyncMock(return_value=True)
    monkeypatch.setattr("apps.api.routers.document_permissions._send_signature_mail", send)
    request = Request({
        "type": "http", "scheme": "http", "server": ("localhost", 80), "path": "/", "headers": [],
    })
    return share, db, send, request


@pytest.mark.asyncio
async def test_signature_otp_accepts_unicode_domain_for_request_and_verify(monkeypatch):
    share, db, send, request = _otp_route_fixture(monkeypatch)
    result = await request_shared_doc_otp(
        "opaque-token", ShareOtpRequest(email="Client@Bücher.Example"), request, db
    )
    assert result == {"status": "sent"}
    import re
    email = send.await_args.kwargs["to"]
    code = re.search(r"\b\d{6}\b", send.await_args.kwargs["content"]).group(0)
    assert email == "client@xn--bcher-kva.example"
    response = Response()
    verified = await verify_shared_doc_otp(
        "opaque-token",
        ShareOtpVerifyRequest(email="Client@Bücher.Example", code=code),
        request,
        response,
        db,
    )
    assert verify_share_access(share, verified["access_token"]) == email
    assert "httponly" in response.headers["set-cookie"].lower()


@pytest.mark.asyncio
@pytest.mark.parametrize("email", ["客户@bücher.example", "other@bücher.example", "invalid"])
async def test_signature_otp_request_keeps_invalid_and_nonmember_email_private(monkeypatch, email):
    share, db, send, request = _otp_route_fixture(monkeypatch)
    result = await request_shared_doc_otp(
        "opaque-token", ShareOtpRequest(email=email), request, db
    )
    assert result == {"status": "sent"}
    send.assert_not_awaited()
    assert "otp_challenges" not in share.metadata_
    with pytest.raises(HTTPException) as error:
        await verify_shared_doc_otp(
            "opaque-token",
            ShareOtpVerifyRequest(email=email, code="000000"),
            request,
            Response(),
            db,
        )
    assert error.value.status_code == 400
    assert error.value.detail == "Verification code is invalid or expired"


def test_signature_otp_normalization_does_not_change_ordinary_shares():
    share = SimpleNamespace(metadata_={})
    original_email = " Client@Bücher.Example "
    assert _normalize_share_otp_email(share, original_email) == original_email
