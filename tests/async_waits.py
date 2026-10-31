"""Bounded waits for tests that coordinate real database or service work."""
from __future__ import annotations

import asyncio

# A hang guard, not a latency budget: a loaded CI runner can stall a few real
# Postgres or Redis round trips for seconds. Ordering is proven by which side
# waits, never by how fast the other side finishes.
LIVENESS_TIMEOUT_S = 30


async def wait_for_signal(signal: asyncio.Event, producer: asyncio.Task) -> None:
    """Wait until ``producer`` sets ``signal``; surface its failure at once."""

    waiter = asyncio.ensure_future(signal.wait())
    try:
        await asyncio.wait(
            {waiter, producer},
            timeout=LIVENESS_TIMEOUT_S,
            return_when=asyncio.FIRST_COMPLETED,
        )
    finally:
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)
    if signal.is_set():
        return
    if producer.done():
        producer.result()
        raise AssertionError("producer finished without setting its signal")
    raise asyncio.TimeoutError(f"signal not set within {LIVENESS_TIMEOUT_S}s")
