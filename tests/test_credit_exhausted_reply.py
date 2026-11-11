"""A turn that credits stopped says so, in the limit's own words."""
from types import SimpleNamespace

from packages.core.ai.agentic_loop import _credit_exhausted_result
from packages.core.ai.llm_client import CreditExhaustedError
from packages.core.services.chat_service import CREDIT_EXHAUSTED_FALLBACK, credit_exhausted_reply

LIMIT = "You've used all 20,000 credits on the free plan. Purchase more credits or upgrade your plan to continue."


def test_the_loop_result_carries_the_limit_message_not_a_debug_bracket():
    result = _credit_exhausted_result(
        CreditExhaustedError(LIMIT), messages=[], usage={}, rounds=0, tool_calls_made=[],
    )
    assert result.stop_reason == "credit_exhausted"
    assert result.content == LIMIT


def test_an_emptied_reply_uses_the_limit_message():
    """Live: an empty credit-stopped reply was stored as "the model returned an
    empty response… reset your API key", sending users to the wrong fix."""
    result = SimpleNamespace(content="", error=LIMIT, error_detail={"message": LIMIT}, stop_reason="credit_exhausted")
    assert credit_exhausted_reply(result) == LIMIT
    bare = SimpleNamespace(content="", error=None, error_detail=None, stop_reason="credit_exhausted")
    assert credit_exhausted_reply(bare) == CREDIT_EXHAUSTED_FALLBACK
    assert "API key" not in CREDIT_EXHAUSTED_FALLBACK


def test_a_reservation_shortfall_reads_in_plain_words():
    """The machine form stays for clients that parse it; the chat reply does not show it."""
    raw = "insufficient credits: required 200, available 0"
    result = SimpleNamespace(content="", error=raw, error_detail={"message": raw}, stop_reason="credit_exhausted")
    assert credit_exhausted_reply(result) == (
        "Not enough AI credits for this request: it needs 200 credits and your balance is 0. "
        "Add credits or upgrade your plan to continue."
    )
