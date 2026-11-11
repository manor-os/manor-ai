"""Video cost from the provider's official output tokens."""

import pytest

from packages.core.services import model_pricing
from packages.core.tasks import video_adapters

SEEDANCE_20 = "bytedance/seedance-2.0"
SEEDANCE_25 = "bytedance/seedance-2.5"


@pytest.mark.parametrize(
    ("model", "resolution", "official_per_m"),
    [
        (SEEDANCE_25, "720p", 10.7),
        (SEEDANCE_25, "1080p", 11.7),
        (SEEDANCE_20, "720p", 7.0),
    ],
)
def test_derived_token_price_matches_the_official_rate(model, resolution, official_per_m):
    assert model_pricing.video_token_price_per_m(model, resolution) == pytest.approx(official_per_m, rel=0.01)


def test_official_tokens_price_a_clip_like_its_list_estimate():
    # 10 s at 720p is 216,000 Seedance tokens; billed at the token price it equals the list estimate.
    tokens = 216_000
    assert model_pricing.video_cost_from_tokens(SEEDANCE_20, tokens, "720p") == pytest.approx(
        model_pricing.priced_video_cost_usd(SEEDANCE_20, 10, "720p"), rel=0.01,
    )


def test_input_video_lowers_the_token_price_by_the_table_factor():
    plain = model_pricing.video_token_price_per_m(SEEDANCE_25, "720p")
    with_input = model_pricing.video_token_price_per_m(SEEDANCE_25, "720p", has_video_input=True)

    assert with_input == pytest.approx(plain * model_pricing._SEEDANCE_25_VIDEO_INPUT_FACTOR)


def test_per_second_models_have_no_token_price():
    assert model_pricing.video_token_price_per_m("kwaivgi/kling-v3.0", "720p") is None
    assert model_pricing.video_cost_from_tokens("kwaivgi/kling-v3.0", 1000, "720p") is None


def test_no_reported_tokens_has_no_token_cost():
    assert model_pricing.video_cost_from_tokens(SEEDANCE_20, None, "720p") is None


def test_seedance_reports_its_completion_tokens_as_usage():
    usage = video_adapters.provider_video_usage({"usage": {"completion_tokens": 216_000, "total_tokens": 216_000}})

    assert usage["completion_tokens"] == 216_000
    assert usage["prompt_tokens"] == 0
