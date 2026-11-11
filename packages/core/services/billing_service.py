"""OSS-safe billing facade.

"""
from __future__ import annotations

import os
from decimal import ROUND_CEILING, Decimal
from typing import Any

from packages.core.constants.plans import is_cloud


_OSS_CREDITS_PER_USD = int(os.getenv("MANOR_OSS_CREDITS_PER_USD", "1000"))


def _cloud_impl():
    if not is_cloud():
        return None
    return None


def _cloud_attr(name: str):
    impl = _cloud_impl()
    if impl is None:
        raise RuntimeError("Manor Cloud billing is not available in OSS mode")
    return getattr(impl, name)


def credit_rate() -> tuple[int, float]:
    """(credits per USD, AI margin) that every USD-to-credit conversion uses.

    The cloud values are the only real ones. A module-level copy here would
    shadow them, so ``CREDITS_PER_USD`` and ``AI_MARGIN`` resolve through this
    function instead.
    """
    impl = _cloud_impl()
    if impl is None:
        return _OSS_CREDITS_PER_USD, 0.0
    return int(impl.CREDITS_PER_USD), float(impl.AI_MARGIN)


def usd_to_credits(cost_usd: float | Decimal | None) -> int:
    """Credits for a USD cost: ceil(cost × (1 + margin) × credits per USD).

    Exact decimal arithmetic, so a float product never rounds one credit up.
    A zero or missing cost is zero credits; any positive cost is at least one.
    """
    cost = Decimal(str(float(cost_usd or 0)))
    if cost <= 0:
        return 0
    credits_per_usd, ai_margin = credit_rate()
    exact = cost * (1 + Decimal(str(ai_margin))) * credits_per_usd
    return int(exact.to_integral_value(rounding=ROUND_CEILING))


def __getattr__(name: str) -> Any:
    if name.startswith("__"):
        raise AttributeError(name)
    if name == "CREDITS_PER_USD":
        return credit_rate()[0]
    if name == "AI_MARGIN":
        return credit_rate()[1]
    return _cloud_attr(name)


def estimate_provider_cost(*args: Any, **kwargs: Any) -> float | None:
    impl = _cloud_impl()
    if impl is None:
        return 0.0
    cost = impl.estimate_provider_cost(*args, **kwargs)
    # None only with require_price: the model has no price to bill on.
    return None if cost is None else float(cost)


def tokens_to_credits(*args: Any, **kwargs: Any) -> int:
    impl = _cloud_impl()
    if impl is None:
        return 0
    return int(impl.tokens_to_credits(*args, **kwargs))


def cost_to_credits(cost_usd: float, *args: Any, **kwargs: Any) -> int:
    impl = _cloud_impl()
    if impl is None:
        return 0
    return int(impl.cost_to_credits(cost_usd, *args, **kwargs))


def estimate_video_cost(*args: Any, **kwargs: Any) -> float:
    impl = _cloud_impl()
    if impl is None:
        return 0.0
    return float(impl.estimate_video_cost(*args, **kwargs))


def video_to_credits(*args: Any, **kwargs: Any) -> int:
    impl = _cloud_impl()
    if impl is None:
        return 0
    return int(impl.video_to_credits(*args, **kwargs))


async def record_token_usage(*args: Any, **kwargs: Any):
    impl = _cloud_impl()
    if impl is None:
        return None
    return await impl.record_token_usage(*args, **kwargs)


async def get_credit_balance(*args: Any, **kwargs: Any):
    impl = _cloud_impl()
    if impl is None:
        return {"balance": 0, "credits": 0}
    return await impl.get_credit_balance(*args, **kwargs)


async def get_token_balance(*args: Any, **kwargs: Any):
    impl = _cloud_impl()
    if impl is None:
        return {"available": 0, "reserved": 0}
    return await impl.get_token_balance(*args, **kwargs)


async def check_and_auto_recharge(*args: Any, **kwargs: Any) -> bool:
    impl = _cloud_impl()
    if impl is None:
        return False
    return bool(await impl.check_and_auto_recharge(*args, **kwargs))


async def process_stripe_webhook(*args: Any, **kwargs: Any):
    return await _cloud_attr("process_stripe_webhook")(*args, **kwargs)
