"""Credit display helpers shared by OSS and Cloud.

OSS does not price provider tokens or implement Manor Cloud credit economics.
"""
import math
import os

from packages.core.constants.plans import is_cloud


OSS_CREDITS_PER_USD = int(os.getenv("MANOR_OSS_CREDITS_PER_USD", "1000"))


def _cloud_rates() -> tuple[int, float] | None:
    if not is_cloud():
        return None
    return None


def get_rates() -> dict:
    """Return current credit rates for compatibility displays."""
    cloud_rates = _cloud_rates()
    if cloud_rates:
        credits_per_usd, ai_margin = cloud_rates
        return {
            "credits_per_usd": credits_per_usd,
            "ai_margin": ai_margin,
        }
    return {
        "credits_per_usd": OSS_CREDITS_PER_USD,
        "ai_margin": 0,
    }


def usd_to_credits(usd: float) -> int:
    """USD → display credits. Token/provider conversion is Cloud-only."""
    if usd is None or usd <= 0:
        return 0
    credits_per_usd = get_rates()["credits_per_usd"]
    return math.ceil(float(usd) * credits_per_usd)


def credits_to_usd(credits: int) -> float:
    """Display credits → USD."""
    if credits is None or credits <= 0:
        return 0.0
    credits_per_usd = get_rates()["credits_per_usd"]
    return float(credits) / credits_per_usd
