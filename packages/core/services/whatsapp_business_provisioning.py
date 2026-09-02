"""Provider-side provisioning for customer-owned WhatsApp Business numbers."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any
from urllib.parse import quote

import httpx

from packages.core.ai.mcp.nango import _NANGO_BASE
from packages.core.external_api_versions import META_GRAPH


class WhatsAppPhoneState(str, Enum):
    CONNECTED = "connected"
    REGISTRATION_REQUIRED = "registration_required"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class WhatsAppProvisioningResult:
    ok: bool
    phone_state: WhatsAppPhoneState
    exact_app_subscribed: bool
    phone_number_id: str
    waba_id: str
    app_id: str
    detail: str


@dataclass(frozen=True)
class WhatsAppCallbackResult:
    ok: bool
    configured_url: str | None
    expected_url: str
    detail: str


async def _nango_graph_json(
    method: str,
    *,
    nango_secret: str,
    provider_config_key: str,
    connection_id: str,
    path: str,
    params: dict[str, str] | None = None,
    payload: dict[str, str] | None = None,
) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.request(
            method,
            f"{_NANGO_BASE}/proxy/{META_GRAPH.value}/{path.lstrip('/')}",
            params=params,
            json=payload,
            headers={
                "Authorization": f"Bearer {nango_secret}",
                "Provider-Config-Key": provider_config_key,
                "Connection-Id": connection_id,
                "Nango-Proxy-Accept": "application/json",
            },
        )
    response.raise_for_status()
    body = response.json()
    return body if isinstance(body, dict) else {}


async def inspect_whatsapp_business_number(
    *,
    nango_secret: str,
    provider_config_key: str,
    connection_id: str,
    phone_number_id: str,
    waba_id: str,
    expected_app_id: str,
) -> WhatsAppProvisioningResult:
    common = {
        "nango_secret": nango_secret,
        "provider_config_key": provider_config_key,
        "connection_id": connection_id,
    }
    phone = await _nango_graph_json(
        "GET",
        **common,
        path=quote(phone_number_id, safe=""),
        params={
            "fields": (
                "id,status,code_verification_status,platform_type,is_on_biz_app"
            ),
        },
    )
    subscribed = await _nango_graph_json(
        "GET",
        **common,
        path=f"{quote(waba_id, safe='')}/subscribed_apps",
        params={"fields": "id,name"},
    )
    status = str(phone.get("status") or "").strip().upper()
    if status == "CONNECTED":
        phone_state = WhatsAppPhoneState.CONNECTED
    elif status:
        phone_state = WhatsAppPhoneState.REGISTRATION_REQUIRED
    else:
        phone_state = WhatsAppPhoneState.UNKNOWN
    app_ids = {
        str(item.get("id") or "").strip()
        for item in subscribed.get("data", [])
        if isinstance(item, dict)
    }
    exact_app_subscribed = expected_app_id in app_ids
    ok = (
        phone_state is WhatsAppPhoneState.CONNECTED
        and exact_app_subscribed
    )
    return WhatsAppProvisioningResult(
        ok=ok,
        phone_state=phone_state,
        exact_app_subscribed=exact_app_subscribed,
        phone_number_id=phone_number_id,
        waba_id=waba_id,
        app_id=expected_app_id,
        detail=(
            "WhatsApp Business route is provisioned"
            if ok
            else "WhatsApp phone registration or exact app subscription is missing"
        ),
    )


async def inspect_whatsapp_app_callback(
    *,
    app_id: str,
    app_secret: str,
    expected_callback_url: str,
) -> WhatsAppCallbackResult:
    """Inspect the deployment App's WhatsApp callback without retaining secrets."""
    graph_base = f"https://graph.facebook.com/{META_GRAPH.value}"
    async with httpx.AsyncClient(timeout=20.0) as client:
        token_response = await client.request(
            "GET",
            f"{graph_base}/oauth/access_token",
            params={
                "client_id": app_id,
                "client_secret": app_secret,
                "grant_type": "client_credentials",
            },
        )
        if token_response.status_code >= 400:
            raise RuntimeError(
                "Meta App access token request failed with HTTP "
                f"{token_response.status_code}"
            )
        token_body = token_response.json()
        app_access_token = (
            str(token_body.get("access_token") or "").strip()
            if isinstance(token_body, dict)
            else ""
        )
        if not app_access_token:
            raise RuntimeError("Meta App access token response was incomplete")

        subscriptions_response = await client.request(
            "GET",
            f"{graph_base}/{quote(app_id, safe='')}/subscriptions",
            params={"fields": "object,callback_url"},
            headers={"Authorization": f"Bearer {app_access_token}"},
        )
        if subscriptions_response.status_code >= 400:
            raise RuntimeError(
                "Meta App subscription inspection failed with HTTP "
                f"{subscriptions_response.status_code}"
            )
        subscriptions_body = subscriptions_response.json()

    rows = (
        subscriptions_body.get("data", [])
        if isinstance(subscriptions_body, dict)
        else []
    )
    whatsapp_rows = [
        item
        for item in rows
        if isinstance(item, dict)
        and str(item.get("object") or "").strip()
        == "whatsapp_business_account"
    ]
    configured_url = next(
        (
            str(item.get("callback_url") or "").strip()
            for item in whatsapp_rows
            if str(item.get("callback_url") or "").strip()
        ),
        None,
    )
    ok = any(
        str(item.get("callback_url") or "").strip() == expected_callback_url
        for item in whatsapp_rows
    )
    return WhatsAppCallbackResult(
        ok=ok,
        configured_url=configured_url,
        expected_url=expected_callback_url,
        detail=(
            "WhatsApp Business callback matches this deployment"
            if ok
            else "WhatsApp Business callback does not match this deployment"
        ),
    )


async def provision_whatsapp_business_number(
    *,
    nango_secret: str,
    provider_config_key: str,
    connection_id: str,
    phone_number_id: str,
    waba_id: str,
    expected_app_id: str,
    registration_pin: str | None = None,
) -> WhatsAppProvisioningResult:
    if registration_pin is not None and (
        len(registration_pin) != 6
        or not registration_pin.isascii()
        or not registration_pin.isdecimal()
    ):
        raise ValueError("WhatsApp registration PIN must be exactly six digits")

    common = {
        "nango_secret": nango_secret,
        "provider_config_key": provider_config_key,
        "connection_id": connection_id,
    }
    before = await inspect_whatsapp_business_number(
        **common,
        phone_number_id=phone_number_id,
        waba_id=waba_id,
        expected_app_id=expected_app_id,
    )
    if before.phone_state is not WhatsAppPhoneState.CONNECTED:
        if registration_pin is None:
            return before
        await _nango_graph_json(
            "POST",
            **common,
            path=f"{quote(phone_number_id, safe='')}/register",
            payload={
                "messaging_product": "whatsapp",
                "pin": registration_pin,
            },
        )
    await _nango_graph_json(
        "POST",
        **common,
        path=f"{quote(waba_id, safe='')}/subscribed_apps",
        payload={},
    )
    return await inspect_whatsapp_business_number(
        **common,
        phone_number_id=phone_number_id,
        waba_id=waba_id,
        expected_app_id=expected_app_id,
    )


async def unsubscribe_whatsapp_business_app(
    *,
    nango_secret: str,
    provider_config_key: str,
    connection_id: str,
    waba_id: str,
) -> None:
    await _nango_graph_json(
        "DELETE",
        nango_secret=nango_secret,
        provider_config_key=provider_config_key,
        connection_id=connection_id,
        path=f"{quote(waba_id, safe='')}/subscribed_apps",
    )


async def delete_nango_connection(
    *,
    nango_secret: str,
    provider_config_key: str,
    connection_id: str,
) -> None:
    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.request(
            "DELETE",
            f"{_NANGO_BASE}/connection/{quote(connection_id, safe='')}",
            params={"provider_config_key": provider_config_key},
            headers={"Authorization": f"Bearer {nango_secret}"},
        )
    if response.status_code == 404:
        return
    response.raise_for_status()


async def disconnect_whatsapp_business_account(
    *,
    nango_secret: str,
    provider_config_key: str,
    connection_id: str,
    waba_id: str,
) -> None:
    """Remove Manor's delegated access without deleting customer assets."""
    await unsubscribe_whatsapp_business_app(
        nango_secret=nango_secret,
        provider_config_key=provider_config_key,
        connection_id=connection_id,
        waba_id=waba_id,
    )
    await delete_nango_connection(
        nango_secret=nango_secret,
        provider_config_key=provider_config_key,
        connection_id=connection_id,
    )


__all__ = [
    "WhatsAppCallbackResult",
    "WhatsAppPhoneState",
    "WhatsAppProvisioningResult",
    "delete_nango_connection",
    "disconnect_whatsapp_business_account",
    "inspect_whatsapp_app_callback",
    "inspect_whatsapp_business_number",
    "provision_whatsapp_business_number",
    "unsubscribe_whatsapp_business_app",
]
