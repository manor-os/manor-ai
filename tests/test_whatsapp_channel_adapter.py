"""WhatsApp adapter webhook and media receipt regressions."""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.requests import Request


def test_whatsapp_signature_requires_the_app_secret_and_exact_payload_bytes():
    from packages.core.services.channels.whatsapp_adapter import WhatsAppAdapter

    body = b'{"entry":[{"id":"waba-1"}]}'
    signature = hmac.new(b"app-secret", body, hashlib.sha256).hexdigest()
    adapter = WhatsAppAdapter(
        phone_number_id="phone-id",
        access_token="access-token",
        verify_token="verify-token",
        app_secret="app-secret",
    )

    assert adapter.verify_inbound_signature(
        headers={"X-Hub-Signature-256": f"sha256={signature}"}, body=body,
    )
    assert not adapter.verify_inbound_signature(
        headers={"X-Hub-Signature-256": f"sha256={signature}"},
        body=body + b" ",
    )
    assert not WhatsAppAdapter(
        phone_number_id="phone-id",
        access_token="access-token",
        verify_token="verify-token",
    ).verify_inbound_signature(
        headers={"X-Hub-Signature-256": f"sha256={signature}"}, body=body,
    )


@pytest.mark.asyncio
async def test_whatsapp_webhook_checks_signature_before_database_work(monkeypatch):
    from apps.api.routers.channels import whatsapp as whatsapp_router
    from packages.core.services.channels.whatsapp_adapter import WhatsAppAdapter

    adapter = WhatsAppAdapter(
        phone_number_id="phone-id",
        access_token="access-token",
        verify_token="verify-token",
        app_secret="app-secret",
    )

    async def resolve_config(_config_id):
        return adapter, SimpleNamespace(id="config-id", entity_id="entity-id")

    async def receive():
        return {"type": "http.request", "body": b'{"entry":[]}', "more_body": False}

    monkeypatch.setattr(whatsapp_router, "_get_adapter_and_config", resolve_config)
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/channels/whatsapp/webhook",
            "headers": [(b"content-type", b"application/json")],
        },
        receive=receive,
    )

    with pytest.raises(HTTPException, match="signature"):
        await whatsapp_router.whatsapp_receive(request, config_id="config-id")


@pytest.mark.asyncio
@pytest.mark.parametrize("event_name", ["messages", "statuses"])
async def test_whatsapp_webhook_rejects_signed_event_for_another_phone_number(
    monkeypatch,
    event_name: str,
):
    from apps.api.routers.channels import whatsapp as whatsapp_router
    from packages.core.services.channels.whatsapp_adapter import WhatsAppAdapter

    adapter = WhatsAppAdapter(
        phone_number_id="configured-phone-id",
        access_token="access-token",
        verify_token="verify-token",
        app_secret="app-secret",
    )
    body = json.dumps({
        "object": "whatsapp_business_account",
        "entry": [{
            "changes": [{
                "field": "messages",
                "value": {
                    "metadata": {"phone_number_id": "other-phone-id"},
                    event_name: [{"id": "wamid.cross-config"}],
                },
            }],
        }],
    }, separators=(",", ":")).encode()
    signature = hmac.new(b"app-secret", body, hashlib.sha256).hexdigest()

    async def resolve_config(_config_id):
        return adapter, SimpleNamespace(id="config-id", entity_id="entity-id")

    async def should_not_parse(_body):
        pytest.fail("a mismatched webhook must be rejected before event parsing")

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    monkeypatch.setattr(whatsapp_router, "_get_adapter_and_config", resolve_config)
    monkeypatch.setattr(adapter, "handle_webhook", should_not_parse)
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/channels/whatsapp/webhook",
            "headers": [
                (b"content-type", b"application/json"),
                (b"x-hub-signature-256", f"sha256={signature}".encode()),
            ],
        },
        receive=receive,
    )

    with pytest.raises(HTTPException, match="phone number") as exc_info:
        await whatsapp_router.whatsapp_receive(request, config_id="config-id")

    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_whatsapp_attachment_returns_meta_message_id(monkeypatch):
    from packages.core.services.channels import whatsapp_adapter
    from packages.core.services.channels.whatsapp_adapter import (
        WhatsAppAdapter,
        WhatsAppChannelAdapter,
    )

    class Response:
        is_success = True
        status_code = 200
        text = ""

        @staticmethod
        def json():
            return {"messages": [{"id": "wamid.media.1"}]}

    class Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, *_args, **_kwargs):
            return Response()

    async def build(self, _cc, *, reason):
        assert reason == "channel.whatsapp.send_attachment"
        return WhatsAppAdapter("phone-id", "access-token", "verify-token")

    monkeypatch.setattr(whatsapp_adapter, "httpx", SimpleNamespace(AsyncClient=Client))
    monkeypatch.setattr(WhatsAppChannelAdapter, "_build", build)

    result = await WhatsAppChannelAdapter().send_attachment(
        SimpleNamespace(),
        "15550001111",
        url="https://example.test/report.pdf",
        kind="document",
    )

    assert result["external_id"] == "wamid.media.1"


@pytest.mark.asyncio
async def test_whatsapp_text_outside_customer_window_requires_template(
    monkeypatch,
    caplog,
):
    from packages.core.services.channels import whatsapp_adapter
    from packages.core.services.channels.base import (
        ChannelTextSendError,
        ChannelTextSendFailureDisposition,
    )
    from packages.core.services.channels.whatsapp_adapter import WhatsAppAdapter

    class Response:
        status_code = 400
        text = "provider rejection"

        @staticmethod
        def json():
            return {
                "error": {
                    "message": "Re-engagement message",
                    "type": "OAuthException",
                    "code": 131047,
                    "error_subcode": 2018278,
                }
            }

    class Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, *_args, **_kwargs):
            return Response()

    monkeypatch.setattr(whatsapp_adapter, "httpx", SimpleNamespace(AsyncClient=Client))
    adapter = WhatsAppAdapter(
        phone_number_id="phone-id",
        access_token="customer-access-token",
        verify_token="verify-token",
    )

    with caplog.at_level(logging.ERROR), pytest.raises(ChannelTextSendError) as raised:
        await adapter.send_text("15550001111", "late reply")

    assert raised.value.reason_code == "whatsapp_template_required"
    assert (
        raised.value.disposition
        is ChannelTextSendFailureDisposition.DETERMINATE
    )
    error_detail = str(raised.value).lower()
    assert "approved" in error_detail
    assert "template" in error_detail
    assert "customer-access-token" not in caplog.text
    assert "15550001111" not in caplog.text
