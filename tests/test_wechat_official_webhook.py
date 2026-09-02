"""WeChat Official Account callback and integration wiring regressions."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import func, select
from fastapi import HTTPException

from packages.core.models.base import generate_ulid
from packages.core.models.channel import ChannelConfig, MessageLog
from packages.core.services.channels.wechat_adapter import WeChatAdapter
from tests.test_document_permissions import _auth


def _xml_message(*, message_id: str = "wechat-message-1") -> bytes:
    return f"""<xml>
    <ToUserName><![CDATA[gh-manor]]></ToUserName>
    <FromUserName><![CDATA[openid-1]]></FromUserName>
    <CreateTime>1724457600</CreateTime>
    <MsgType><![CDATA[text]]></MsgType>
    <Content><![CDATA[hello]]></Content>
    <MsgId>{message_id}</MsgId>
    </xml>""".encode()


def _xml_event() -> bytes:
    return b"""<xml>
    <ToUserName><![CDATA[gh-manor]]></ToUserName>
    <FromUserName><![CDATA[openid-1]]></FromUserName>
    <CreateTime>1724457600</CreateTime>
    <MsgType><![CDATA[event]]></MsgType>
    <Event><![CDATA[subscribe]]></Event>
    <EventKey></EventKey>
    </xml>"""


@pytest.mark.asyncio
async def test_wechat_official_callback_deduplicates_and_queues_receipt(
    client, db_session, monkeypatch: pytest.MonkeyPatch,
) -> None:
    headers = await _auth(client, "wechat_official_callback_owner")
    owner = (await client.get("/api/v1/auth/me", headers=headers)).json()
    config = ChannelConfig(
        id=generate_ulid(),
        entity_id=owner["entity_id"],
        owner_user_id=owner["id"],
        channel_type="wechat",
        provider="wechat_oa",
        credentials={
            "app_id": "wx-app",
            "app_secret": "wx-secret",
            "token": "wx-callback-token",
        },
        config={},
        status="active",
    )
    db_session.add(config)
    await db_session.commit()

    queued: list[dict] = []
    from packages.core.tasks.channel_tasks import dispatch_inbound_task

    monkeypatch.setattr(
        dispatch_inbound_task,
        "delay",
        lambda **kwargs: queued.append(kwargs),
    )
    body = _xml_message()
    params = {
        "config_id": config.id,
        "signature": WeChatAdapter._sign("wx-callback-token", "123", "nonce"),
        "timestamp": "123",
        "nonce": "nonce",
    }

    first = await client.post(
        "/api/v1/channels/wechat/callback",
        params=params,
        content=body,
    )
    duplicate = await client.post(
        "/api/v1/channels/wechat/callback",
        params=params,
        content=body,
    )

    assert first.status_code == 200, first.text
    assert first.text == "success"
    assert duplicate.status_code == 200, duplicate.text
    assert duplicate.text == "success"
    assert len(queued) == 1
    assert queued[0]["inbound_message_log_id"]
    count = await db_session.scalar(
        select(func.count(MessageLog.id)).where(
            MessageLog.channel_config_id == config.id,
            MessageLog.direction == "inbound",
            MessageLog.external_id == "wechat-message-1",
        )
    )
    assert count == 1


def test_wechat_official_receipts_have_a_provider_unique_index() -> None:
    from packages.core.models.channel import MessageLog

    names = {
        index.name
        for index in MessageLog.__table__.indexes
    }
    assert "uq_message_logs_wechat_inbound_message" in names


def test_wechat_official_rejects_encrypted_callback_credentials() -> None:
    from apps.api.routers.integrations import _prepare_channel_credentials

    with pytest.raises(HTTPException) as exc_info:
        _prepare_channel_credentials(
            "wechat_official",
            {
                "app_id": "wx-app",
                "app_secret": "wx-secret",
                "token": "wx-callback-token",
                "encoding_aes_key": "unsupported-aes-key",
            },
        )

    assert exc_info.value.status_code == 422
    assert "plain-text mode" in str(exc_info.value.detail)


@pytest.mark.asyncio
async def test_wechat_events_without_msg_id_get_a_stable_receipt_key() -> None:
    parsed = await WeChatAdapter(
        app_id="wx-app",
        app_secret="wx-secret",
        token="wx-callback-token",
    ).handle_message(_xml_event().decode())

    assert parsed["msg_id"].startswith("event:")


def test_wechat_official_is_grouped_with_social_integrations() -> None:
    from apps.api.routers.integrations import _PROVIDER_DISPLAY
    from packages.core.integrations import canonical_integration_key

    assert _PROVIDER_DISPLAY["wechat_official"]["category"] == "Social"
    assert canonical_integration_key("wechat") == "wechat_official"


@pytest.mark.asyncio
async def test_wechat_official_health_requires_callback_token() -> None:
    from packages.core.services import integration_health

    result = await integration_health.test_wechat_official(
        {"app_id": "wx-app", "app_secret": "wx-secret"},
    )

    assert result["ok"] is False
    assert "token" in result["detail"].lower()


@pytest.mark.asyncio
async def test_wechat_official_health_does_not_publish_relative_callback_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import packages.core.config as config_module
    from packages.core.services import integration_health

    channel_config = SimpleNamespace(id="cc-wechat")

    class Result:
        def scalar_one_or_none(self):
            return channel_config

    class FakeDb:
        async def execute(self, _query):
            return Result()

    monkeypatch.setattr(
        config_module,
        "get_settings",
        lambda: SimpleNamespace(PUBLIC_BASE_URL=""),
    )

    wiring = await integration_health._wiring_ctx_for_integration(
        FakeDb(),
        SimpleNamespace(
            id="integration-wechat",
            entity_id="entity-1",
            provider="wechat_official",
        ),
    )

    assert wiring == {"expected_url": "", "channel_config_id": "cc-wechat"}


@pytest.mark.asyncio
async def test_wechat_official_health_exposes_unverifiable_callback_url(monkeypatch) -> None:
    from packages.core.services import integration_health

    class Response:
        is_success = True
        status_code = 200
        text = "{}"

        def json(self):
            return {"access_token": "access-token"}

    async def fake_get(*_args, **_kwargs):
        return Response()

    monkeypatch.setattr(integration_health, "_http_get", fake_get)
    result = await integration_health.test_wechat_official(
        {
            "app_id": "wx-app",
            "app_secret": "wx-secret",
            "token": "wx-callback-token",
        },
        wiring_ctx={
            "expected_url": (
                "https://staging.example/api/v1/channels/wechat/callback"
                "?config_id=cc-wechat"
            ),
            "channel_config_id": "cc-wechat",
        },
    )

    assert result["ok"] is True
    assert result["wiring"]["ok"] is None
    assert result["wiring"]["expected_url"].endswith("config_id=cc-wechat")
    assert "cannot verify" in result["wiring"]["detail"].lower()


def test_wechat_official_blueprint_only_references_available_tools() -> None:
    from packages.core.ai.mcp import wechat_official
    from packages.core.blueprints.solo_company import get_solo_company_blueprint

    available = {tool["name"] for tool in wechat_official.list_tools()}
    workflow = next(
        workflow
        for workflow in get_solo_company_blueprint(
            "solo-content-distribution-studio-v1"
        )["recipe"]["workflows"]
        if workflow["slug"] == "opc-publish-wechat-official-v1"
    )
    references = {
        step["tool"].removeprefix("mcp__wechat_official__")
        for step in workflow["steps"]
        if step.get("tool", "").startswith("mcp__wechat_official__")
    }
    assert references <= available
