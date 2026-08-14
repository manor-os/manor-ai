"""Vercel AI Gateway v4 model-protocol helpers.

Chat remains OpenAI-compatible at ``/v1/chat/completions``. Non-language
models use the AI SDK v4 wire protocol under ``/v4/ai/<type>-model`` with the
model ID and specification version carried in headers.
"""

from __future__ import annotations

from typing import Any

import httpx

from packages.core.services.model_provider_handlers import vercel_model_id


VERCEL_GATEWAY_DEFAULT_BASE_URL = "https://ai-gateway.vercel.sh/v4/ai"
VERCEL_GATEWAY_PROTOCOL_VERSION = "0.0.1"
VERCEL_MODEL_PROTOCOLS = frozenset({"embedding", "image", "speech", "transcription", "video"})


class VercelAIGatewayError(RuntimeError):
    """Raised when a Vercel v4 model-protocol request fails."""

    def __init__(self, *, protocol: str, status_code: int, detail: str) -> None:
        self.protocol = protocol
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"Vercel AI Gateway {protocol} request failed ({status_code}): {detail}")


def vercel_gateway_v4_base_url(base_url: str | None) -> str:
    """Normalize either the Gateway's OpenAI-compatible or v4 base URL."""

    base = str(base_url or VERCEL_GATEWAY_DEFAULT_BASE_URL).strip().rstrip("/")
    if base.endswith("/v4/ai"):
        return base
    if base.endswith("/v1"):
        base = base[:-3]
    return f"{base}/v4/ai"


def vercel_gateway_endpoint(base_url: str | None, protocol: str) -> str:
    normalized_protocol = str(protocol or "").strip().lower()
    if normalized_protocol not in VERCEL_MODEL_PROTOCOLS:
        raise ValueError(f"Unsupported Vercel AI Gateway protocol: {protocol}")
    return f"{vercel_gateway_v4_base_url(base_url)}/{normalized_protocol}-model"


def vercel_gateway_headers(
    *,
    api_key: str,
    model: str,
    protocol: str,
    auth_method: str = "api-key",
) -> dict[str, str]:
    normalized_protocol = str(protocol or "").strip().lower()
    if normalized_protocol not in VERCEL_MODEL_PROTOCOLS:
        raise ValueError(f"Unsupported Vercel AI Gateway protocol: {protocol}")
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "ai-gateway-protocol-version": VERCEL_GATEWAY_PROTOCOL_VERSION,
        "ai-gateway-auth-method": ("oidc" if str(auth_method or "").strip().lower() == "oidc" else "api-key"),
        f"ai-{normalized_protocol}-model-specification-version": "4",
        "ai-model-id": vercel_model_id(model),
    }


async def vercel_gateway_post(
    *,
    api_key: str,
    base_url: str | None,
    model: str,
    protocol: str,
    payload: dict[str, Any],
    auth_method: str = "api-key",
    endpoint_suffix: str = "",
    timeout: float = 180.0,
    extra_headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    """POST one v4 model-protocol envelope and return its JSON object."""

    endpoint = vercel_gateway_endpoint(base_url, protocol)
    if endpoint_suffix:
        endpoint = f"{endpoint}/{str(endpoint_suffix).strip('/')}"
    headers = vercel_gateway_headers(
        api_key=api_key,
        model=model,
        protocol=protocol,
        auth_method=auth_method,
    )
    headers.update(extra_headers or {})
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(endpoint, headers=headers, json=payload)
    try:
        body = response.json()
    except Exception:
        body = None
    if response.status_code >= 300:
        detail = (
            str(body)[:500] if body is not None else str(response.text or "Gateway returned a non-JSON response")[:500]
        )
        raise VercelAIGatewayError(
            protocol=protocol,
            status_code=response.status_code,
            detail=detail,
        )
    if not isinstance(body, dict):
        raise VercelAIGatewayError(
            protocol=protocol,
            status_code=response.status_code,
            detail="Gateway response was not a JSON object",
        )
    return body
