"""Deployment-level configuration for the WhatsApp Business Meta app."""
from __future__ import annotations

from dataclasses import dataclass
import os

from packages.core.config import get_settings


@dataclass(frozen=True)
class WhatsAppBusinessConfig:
    app_id: str
    app_secret: str
    verify_token: str
    embedded_signup_config_id: str
    callback_url: str


def load_whatsapp_business_config() -> WhatsAppBusinessConfig:
    """Load the one Meta app configuration owned by this deployment."""
    public_base = get_settings().PUBLIC_BASE_URL.rstrip("/")
    values = {
        "app_id": os.getenv("NANGO_PROVIDER_WHATSAPP_CLIENT_ID", "").strip(),
        "app_secret": os.getenv(
            "NANGO_PROVIDER_WHATSAPP_CLIENT_SECRET",
            "",
        ).strip(),
        "verify_token": os.getenv("WHATSAPP_WEBHOOK_VERIFY_TOKEN", "").strip(),
        "embedded_signup_config_id": os.getenv(
            "NANGO_PROVIDER_WHATSAPP_CONFIG_ID",
            "",
        ).strip(),
        "callback_url": (
            f"{public_base}/api/v1/channels/whatsapp/webhook"
            if public_base
            else ""
        ),
    }
    missing = [key for key, value in values.items() if not value]
    if missing:
        raise RuntimeError(
            "WhatsApp Business deployment configuration is incomplete: "
            + ", ".join(sorted(missing))
        )
    return WhatsAppBusinessConfig(**values)
