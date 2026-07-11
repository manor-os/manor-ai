"""Simple captcha verification service.

Supports multiple providers:
- hCaptcha (default)
- reCAPTCHA v2/v3
- Turnstile (Cloudflare)
"""
from __future__ import annotations

import logging
import os

import httpx

logger = logging.getLogger(__name__)

CAPTCHA_ENABLED = os.getenv("CAPTCHA_ENABLED", "false").lower() == "true"
CAPTCHA_PROVIDER = os.getenv("CAPTCHA_PROVIDER", "hcaptcha")  # hcaptcha, recaptcha, turnstile
CAPTCHA_SECRET = os.getenv("CAPTCHA_SECRET_KEY", "")

_VERIFY_URLS = {
    "hcaptcha": "https://hcaptcha.com/siteverify",
    "recaptcha": "https://www.google.com/recaptcha/api/siteverify",
    "turnstile": "https://challenges.cloudflare.com/turnstile/v0/siteverify",
}


async def verify_captcha(token: str, remote_ip: str | None = None) -> bool:
    """Verify captcha token. Returns True if valid or captcha disabled."""
    if not CAPTCHA_ENABLED or not CAPTCHA_SECRET:
        return True

    url = _VERIFY_URLS.get(CAPTCHA_PROVIDER)
    if not url:
        logger.warning("Unknown captcha provider: %s", CAPTCHA_PROVIDER)
        return True

    data = {"secret": CAPTCHA_SECRET, "response": token}
    if remote_ip:
        data["remoteip"] = remote_ip

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, data=data)
            result = resp.json()
        return result.get("success", False)
    except Exception as exc:
        logger.error("Captcha verification failed: %s", exc)
        # Fail open — don't block users if the captcha service is down
        return True
