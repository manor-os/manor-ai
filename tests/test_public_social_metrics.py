from __future__ import annotations

import pytest

from packages.core.services import public_social_metrics as metrics
from packages.core.services.youtube_public_video import YouTubePublicVideoMetrics

pytestmark = pytest.mark.oss_regression


@pytest.mark.parametrize(
    ("platform", "url", "post_id", "expected"),
    [
        (
            "instagram",
            "https://www.instagram.com/reel/CODE_123/",
            "CODE_123",
            "https://www.instagram.com/reel/CODE_123/",
        ),
        (
            "instagram",
            "https://instagram.com/p/CODE-123?utm_source=test",
            "CODE-123",
            "https://www.instagram.com/p/CODE-123/",
        ),
        (
            # Instagram's share flow links Reels under the author's username.
            "instagram",
            "https://www.instagram.com/simon.dao_1/reel/DespGqZt93L/",
            "DespGqZt93L",
            "https://www.instagram.com/reel/DespGqZt93L/",
        ),
        (
            "tiktok",
            "https://www.tiktok.com/@artist/video/1234567890?lang=en",
            "1234567890",
            "https://www.tiktok.com/@artist/video/1234567890",
        ),
        (
            "tiktok",
            "https://www.tiktok.com/@restaurant/photo/1234567890?lang=en",
            "1234567890",
            "https://www.tiktok.com/@restaurant/photo/1234567890",
        ),
    ],
)
def test_canonicalizes_supported_public_post_urls(platform: str, url: str, post_id: str, expected: str) -> None:
    canonical_url, actual_id = metrics.canonicalize_public_post_url(platform, url)

    assert canonical_url == expected
    assert actual_id == post_id


@pytest.mark.parametrize(
    ("platform", "url"),
    [
        ("instagram", "https://www.instagram.com/artist/"),
        ("instagram", "https://www.instagram.com/artist/stories/CODE_123/"),
        ("instagram", "https://evil.example/reel/CODE_123/"),
        ("tiktok", "https://www.tiktok.com/@artist/live/1234567890"),
        ("tiktok", "http://www.tiktok.com/@artist/video/1234567890"),
        ("instagram", "https://user:password@www.instagram.com/p/CODE_123/"),
        ("tiktok", "https://www.tiktok.com:8443/@artist/video/1234567890"),
    ],
)
def test_rejects_non_post_or_non_https_public_urls(platform: str, url: str) -> None:
    with pytest.raises(metrics.PublicSocialMetricsError):
        metrics.canonicalize_public_post_url(platform, url)


def test_parses_instagram_target_counts_and_ignores_recommendation_counts() -> None:
    result = metrics.parse_public_post_page(
        platform="instagram",
        post_id="CODE_123",
        canonical_url="https://www.instagram.com/reel/CODE_123/",
        html=(
            '<nav>Instagram <a href="/accounts/login/">Log in</a></nav>'
            '<script type="application/json">'
            '{"shortcode":"CODE_123","like_count":1234,"comment_count":"12",'
            '"recommendations":[{"shortcode":"OTHER","like_count":999999,"comment_count":999}]}'
            "</script>"
        ),
        expected_account_id=None,
        expected_account_handle=None,
    )

    assert result["metrics"]["likes"]["value"] == 1234
    assert result["metrics"]["likes"]["precision"] == "exact"
    assert result["metrics"]["comments"]["value"] == 12
    assert result["metrics"]["views"]["availability"] == "unavailable"


def test_parses_nested_instagram_public_counts() -> None:
    result = metrics.parse_public_post_page(
        platform="instagram",
        post_id="CODE_123",
        canonical_url="https://www.instagram.com/p/CODE_123/",
        html=(
            '<script>{"shortcode":"CODE_123","edge_media_preview_like":{"count":"1.2K"},'
            '"edge_media_to_comment":{"count":8}}</script>'
        ),
        expected_account_id=None,
        expected_account_handle=None,
    )

    assert result["metrics"]["likes"]["value"] == 1200
    assert result["metrics"]["likes"]["precision"] == "rounded"
    assert result["metrics"]["comments"]["value"] == 8


def test_identity_missing_from_page_never_returns_public_counts() -> None:
    result = metrics.parse_public_post_page(
        platform="tiktok",
        post_id="1234567890",
        canonical_url="https://www.tiktok.com/@artist/video/1234567890",
        html='<script>{"id":"different-video","stats":{"playCount":999999}}</script>',
        expected_account_id=None,
        expected_account_handle=None,
    )

    assert result["evidence"]["identity_verified"] is False
    assert result["metrics"]["views"]["availability"] == "blocked"
    assert result["metrics"]["views"]["reason"] == "post_identity_unverified"


@pytest.mark.parametrize("post_type", ["video", "photo"])
def test_tiktok_uses_matching_video_object_not_another_video(post_type: str) -> None:
    result = metrics.parse_public_post_page(
        platform="tiktok",
        post_id="1234567890",
        canonical_url=f"https://www.tiktok.com/@artist/{post_type}/1234567890",
        html=(
            '<script>{"items":['
            '{"id":"other","stats":{"playCount":999999}},'
            '{"id":"1234567890","stats":{"playCount":42,"diggCount":4,"commentCount":2,"shareCount":1}}'
            "]}</script>"
        ),
        expected_account_id=None,
        expected_account_handle=None,
    )

    assert result["evidence"]["identity_verified"] is True
    assert result["metrics"]["views"]["value"] == 42
    assert result["metrics"]["likes"]["value"] == 4


def test_account_identity_must_be_present_on_matching_post() -> None:
    result = metrics.parse_public_post_page(
        platform="instagram",
        post_id="CODE_123",
        canonical_url="https://www.instagram.com/p/CODE_123/",
        html='<script>{"shortcode":"CODE_123","like_count":1234}</script>',
        expected_account_id="artist-1",
        expected_account_handle=None,
    )

    assert result["evidence"]["account_identity_verified"] is False
    assert result["metrics"]["likes"]["availability"] == "blocked"
    assert result["metrics"]["likes"]["reason"] == "account_identity_unverified"


@pytest.mark.asyncio
async def test_youtube_channel_identity_mismatch_does_not_return_metrics(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_fetch(_: str, *, expected_video_id: str) -> YouTubePublicVideoMetrics:
        assert expected_video_id == "dQw4w9WgXcQ"
        return YouTubePublicVideoMetrics(
            video_id=expected_video_id,
            public_url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            title="Video",
            channel_id="different-channel",
            channel_name="Different Channel",
            subscribers=None,
            published_at="2026-10-12T00:00:00+00:00",
            views=42,
            likes=4,
            comments=2,
            unavailable_fields=(),
            collected_at="2026-10-12T00:00:00+00:00",
        )

    monkeypatch.setattr(metrics, "fetch_youtube_public_metrics", fake_fetch)

    with pytest.raises(metrics.PublicSocialMetricsError, match="channel"):
        await metrics.collect_public_post_metrics(
            platform="youtube",
            public_url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            expected_post_id="dQw4w9WgXcQ",
            expected_account_id="selected-channel",
        )


def test_marks_login_wall_blocked_without_making_up_metrics() -> None:
    result = metrics.parse_public_post_page(
        platform="tiktok",
        post_id="1234567890",
        canonical_url="https://www.tiktok.com/@artist/video/1234567890",
        html="TikTok Log in to continue",
        expected_account_id=None,
        expected_account_handle=None,
    )

    assert result["metrics"]["views"] == {
        "value": None,
        "precision": "exact",
        "raw_value": None,
        "availability": "blocked",
        "reason": "login_required",
        "provider_metric_name": "playCount",
    }
