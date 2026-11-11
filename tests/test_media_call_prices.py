"""Image and audio calls are priced on the provider's own billing unit."""

import pytest

from packages.core.services import model_pricing

GEMINI_IMAGE = "google/gemini-3.1-flash-image"


@pytest.mark.parametrize(
    "usage",
    [
        # Gemini usageMetadata (native route).
        {"promptTokenCount": 20, "candidatesTokenCount": 1120,
         "candidatesTokensDetails": [{"modality": "IMAGE", "tokenCount": 1120}]},
        # OpenRouter chat shape.
        {"prompt_tokens": 20, "completion_tokens": 1120},
    ],
)
def test_gemini_image_output_tokens_bill_at_the_image_rate(usage):
    cost, basis = model_pricing.image_call_cost_usd(GEMINI_IMAGE, usage)

    # 20 input tokens at $0.50/M + 1,120 image tokens at $60/M, not the $3/M text rate.
    assert cost == pytest.approx((20 * 0.50 + 1120 * 60.0) / 1_000_000)
    assert basis == "official_tokens"


def test_gateway_camel_case_tokens_are_read_for_every_image():
    cost, basis = model_pricing.image_call_cost_usd("openai/gpt-image-2", {"inputTokens": 50, "outputTokens": 4160})

    assert cost == pytest.approx((50 * 5.0 + 4160 * 30.0) / 1_000_000)
    assert basis == "official_tokens"


def test_reference_images_bill_at_the_image_input_rate():
    usage = {
        "input_tokens": 1050,
        "output_tokens": 4160,
        "input_tokens_details": {"text_tokens": 50, "image_tokens": 1000},
    }
    cost, _basis = model_pricing.image_call_cost_usd("openai/gpt-image-2", usage)

    assert cost == pytest.approx((50 * 5.0 + 1000 * 8.0 + 4160 * 30.0) / 1_000_000)


def test_an_image_without_tokens_falls_back_to_the_official_list_price():
    assert model_pricing.image_call_cost_usd("xai/grok-imagine-image", {}) == (0.02, "official_per_image")


def test_an_unknown_image_model_is_unpriced_not_guessed():
    assert model_pricing.image_call_cost_usd("acme/img", {"input_tokens": 1, "output_tokens": 1}) == (None, "unpriced")


def test_music_bills_the_measured_seconds_not_a_full_song():
    assert model_pricing.audio_call_cost_usd("elevenlabs/music-v2.5", seconds=30) == (
        pytest.approx(30 * 0.00125), "official_seconds",
    )
    # Unmeasured length is never billed as the 180-second default.
    assert model_pricing.audio_call_cost_usd("elevenlabs/music-v2.5") == (None, "unpriced")
