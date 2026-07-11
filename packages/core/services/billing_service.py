"""OSS-safe billing facade.

"""
from __future__ import annotations

import os
from typing import Any

from packages.core.constants.plans import is_cloud


CREDITS_PER_USD = int(os.getenv("MANOR_OSS_CREDITS_PER_USD", "1000"))
AI_MARGIN = 0.0


def _cloud_impl():
    if not is_cloud():
        return None
    return None


def _cloud_attr(name: str):
    impl = _cloud_impl()
    if impl is None:
        raise RuntimeError("Manor Cloud billing is not available in OSS mode")
    return getattr(impl, name)


def __getattr__(name: str) -> Any:
    if name.startswith("__"):
        raise AttributeError(name)
    return _cloud_attr(name)


def estimate_provider_cost(*args: Any, **kwargs: Any) -> float:
    impl = _cloud_impl()
    if impl is None:
        return 0.0
    return float(impl.estimate_provider_cost(*args, **kwargs))


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
