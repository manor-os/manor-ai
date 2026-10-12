"""One signer may verify any invited address and complete exactly one signature."""
import asyncio
import base64
import io
import re
from types import SimpleNamespace

import pytest
from PIL import Image, ImageDraw
from pypdf import PdfReader, PdfWriter
from sqlalchemy import select

import packages.core.database as db_module
from packages.core.models import Document, Share
from packages.core.services.share_access import (
    ShareAccessError, audience_allows_email, create_otp_challenge,
    create_share_access_token, verify_otp_challenge, verify_share_access,
)
from test_document_permissions import _auth, _connect_signature_mailbox


@pytest.mark.parametrize("signature", [False, True])
def test_alternate_email_authority_is_signature_only(signature):
    share = SimpleNamespace(
        id="request", audience="email:primary@example.test", require_otp=True, expires_at=None,
        metadata_={"kind": "signature_request" if signature else "ordinary_share", "recipient_emails": ["alternate@example.test"]},
    )
    assert audience_allows_email(share, "primary@example.test")
    assert audience_allows_email(share, "alternate@example.test") is signature
    assert not audience_allows_email(share, "outsider@example.test")
    if signature:
        email, code = create_otp_challenge(share, "alternate@example.test")
        token = verify_otp_challenge(share, email, code)
        assert verify_share_access(share, token) == email
        share.metadata_["recipient_emails"] = []
        with pytest.raises(ShareAccessError):
            verify_share_access(share, token)
    else:
        with pytest.raises(ShareAccessError):
            create_otp_challenge(share, "alternate@example.test")
    outsider = create_share_access_token(share=share, email="outsider@example.test")
    with pytest.raises(ShareAccessError):
        verify_share_access(share, outsider)


@pytest.fixture
async def invitation_setup(client, monkeypatch, tmp_path):
    from apps.api.routers import documents
    from packages.core.config import get_settings

    settings = get_settings()
    # Earlier configuration tests may have reset the cached Settings instance.
    # Bind the uploader to this fixture's filesystem configuration as well.
    monkeypatch.setattr(documents, "settings", settings)
    monkeypatch.setattr(settings, "MANOR_FS_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "MANOR_FS_ENABLED", True)
    headers = await _auth(client, "alternate_signer_owner")
    await _connect_signature_mailbox(client, headers)
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    pdf = io.BytesIO()
    writer.write(pdf)
    uploaded = await client.post("/api/v1/documents/upload?visibility=private", headers=headers,
                                files={"file": ("one-signature.pdf", pdf.getvalue(), "application/pdf")})
    assert uploaded.status_code == 201, uploaded.text
    assert uploaded.json()["fs_path"], uploaded.text
    deliveries, codes, failures = [], {}, set()

    async def capture(_credentials, **kwargs):
        deliveries.append(kwargs)
        if kwargs["subject"].startswith("Verify your email"):
            codes[kwargs["to"]] = re.search(r"\b\d{6}\b", kwargs["content"]).group(0)
        return kwargs["to"] not in failures

    monkeypatch.setattr("packages.core.services.email_service.send_connected_email", capture)
    image = Image.new("RGBA", (600, 180), (0, 0, 0, 0))
    ImageDraw.Draw(image).line([(20, 130), (160, 30), (300, 145), (560, 45)], fill=(25, 25, 25, 255), width=10)
    png = io.BytesIO()
    image.save(png, format="PNG")
    return {
        "headers": headers, "doc": uploaded.json(), "deliveries": deliveries, "codes": codes, "failures": failures,
        "signature": "data:image/png;base64," + base64.b64encode(png.getvalue()).decode("ascii"),
        "payload": {"recipient_name": "One signer", "recipient_email": "primary@example.test",
                    "recipient_emails": ["PRIMARY@example.test", "alternate@example.test", "alternate@example.test"]},
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("sign_with", ["primary", "alternate", "concurrently"])
async def test_any_invited_email_completes_one_signature(client, invitation_setup, sign_with):
    setup = invitation_setup
    base = f"/api/v1/documents/{setup['doc']['id']}"
    created = await client.post(f"{base}/signature-requests", headers=setup["headers"], json=setup["payload"])
    assert created.status_code == 201, created.text
    emails = ["primary@example.test", "alternate@example.test"]
    assert created.json()["recipient_emails"] == emails
    assert created.json()["group_size"] == 1
    assert created.json()["group_id"] is None
    assert [mail["to"] for mail in setup["deliveries"]] == emails
    links = [re.search(r'href="([^"]+)"', mail["content"]).group(1) for mail in setup["deliveries"]]
    assert links == [created.json()["url"]] * 2
    token = created.json()["url"].rsplit("/", 1)[-1]
    public = f"/api/v1/shared-doc/{token}"
    assert (await client.get(f"{public}/signature-request")).status_code == 401
    outsider = await client.post(f"{public}/request-otp", json={"email": "outsider@example.test"})
    assert outsider.status_code == 200
    assert len(setup["deliveries"]) == 2
    rejected = await client.post(f"{public}/verify-otp", json={"email": "outsider@example.test", "code": "000000"})
    assert rejected.status_code == 400, rejected.text
    assert "access_token" not in rejected.json()
    access = {}
    for email in emails:
        requested = await client.post(f"{public}/request-otp", json={"email": email})
        assert requested.status_code == 200, requested.text
        verified = await client.post(f"{public}/verify-otp", json={"email": email, "code": setup["codes"][email]})
        assert verified.status_code == 200, verified.text
        access[email] = verified.json()["access_token"]
        preview = await client.get(f"{public}/signature-request", params={"access_token": access[email]})
        assert preview.status_code == 200, preview.text
        assert preview.json()["recipient_email"] == email
        assert "recipient_emails" not in preview.json()

    async def sign(email):
        return await client.post(f"{public}/signature", params={"access_token": access[email]},
                                 json={"signature_data_url": setup["signature"], "consent": True})

    if sign_with == "concurrently":
        outcomes = await asyncio.gather(*(sign(email) for email in emails))
        assert sorted(response.status_code for response in outcomes) == [200, 409], [r.text for r in outcomes]
        signed = next(response for response in outcomes if response.status_code == 200)
        signer_email = signed.json()["signer_email"]
    else:
        signer_email = f"{sign_with}@example.test"
        signed = await sign(signer_email)
        assert signed.status_code == 200, signed.text
        assert (await sign(next(email for email in emails if email != signer_email))).status_code == 409
    assert signed.json()["signer_email"] == signer_email
    rows = (await client.get(f"{base}/signature-requests", headers=setup["headers"])).json()
    assert len(rows) == 1
    assert rows[0]["status"] == "completed"
    assert rows[0]["signed_by_email"] == signer_email
    for email in emails:
        preview = await client.get(f"{public}/signature-request", params={"access_token": access[email]})
        assert preview.json()["status"] == "completed"
        downloaded = await client.get(f"{public}/download", params={"access_token": access[email]})
        assert downloaded.status_code == 200, downloaded.text
        assert PdfReader(io.BytesIO(downloaded.content)).metadata["/ManorSigner"] == signer_email
    async with db_module.async_session() as session:
        signed_copies = (await session.execute(select(Document).where(Document.source == "signature"))).scalars().all()
        assert len(signed_copies) == 1


@pytest.mark.asyncio
async def test_partial_email_delivery_resend_and_revocation_share_one_invitation(client, invitation_setup):
    setup = invitation_setup
    base = f"/api/v1/documents/{setup['doc']['id']}"
    setup["failures"].add("alternate@example.test")
    created = await client.post(f"{base}/signature-requests", headers=setup["headers"], json=setup["payload"])
    assert created.status_code == 201, created.text
    assert created.json()["delivery_status"] == "failed"
    async with db_module.async_session() as session:
        share = await session.get(Share, created.json()["id"])
        assert share.metadata_["recipient_delivery_status"] == {"primary@example.test": "sent", "alternate@example.test": "failed"}
    setup["failures"].clear()
    resent = await client.post(f"{base}/signature-requests/{created.json()['id']}/resend", headers=setup["headers"])
    assert resent.status_code == 200, resent.text
    assert resent.json()["delivery_status"] == "sent"
    assert resent.json()["id"] == created.json()["id"]
    assert resent.json()["expires_at"] == created.json()["expires_at"]
    assert resent.json()["url"] != created.json()["url"]
    assert len(setup["deliveries"]) == 4
    assert [mail["to"] for mail in setup["deliveries"][-2:]] == ["primary@example.test", "alternate@example.test"]
    old_token = created.json()["url"].rsplit("/", 1)[-1]
    assert (await client.get(f"/api/v1/shared-doc/{old_token}/signature-request")).status_code == 404
    revoked = await client.delete(f"{base}/signature-requests/{created.json()['id']}", headers=setup["headers"])
    assert revoked.status_code == 204, revoked.text
    new_token = resent.json()["url"].rsplit("/", 1)[-1]
    for email in resent.json()["recipient_emails"]:
        denied = await client.post(f"/api/v1/shared-doc/{new_token}/request-otp", json={"email": email})
        assert denied.status_code in (404, 410), denied.text


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["address", "too_many", "shared_signer_email"])
async def test_invalid_recipient_email_lists_send_nothing(client, invitation_setup, invalid):
    setup = invitation_setup
    payload = dict(setup["payload"])
    if invalid == "address":
        payload["recipient_emails"] = ["invalid@example.test,other@example.test"]
    elif invalid == "too_many":
        payload["recipient_emails"] = [f"alternate{i}@example.test" for i in range(10)]
    else:
        payload["additional_recipients"] = [{
            "recipient_name": "Different signer", "recipient_email": "alternate@example.test",
            "fields": [{"id": "signature", "type": "signature", "label": "Signature", "required": True,
                        "page": 1, "x": .1, "y": .2, "width": .3, "height": .1}],
        }]
    base = f"/api/v1/documents/{setup['doc']['id']}"
    rejected = await client.post(f"{base}/signature-requests", headers=setup["headers"], json=payload)
    assert rejected.status_code == 400, rejected.text
    assert setup["deliveries"] == []
    assert (await client.get(f"{base}/signature-requests", headers=setup["headers"])).json() == []
