from __future__ import annotations

import json

import httpx
import pytest

from packages.core.services import youtube_public_video


def _youtube_html(*, player: dict, initial: dict) -> str:
    return (
        "<html><script>var ytInitialPlayerResponse = "
        f"{json.dumps(player)};</script>"
        "<script>var ytInitialData = "
        f"{json.dumps(initial)};</script></html>"
    )


def _player(**overrides) -> dict:
    player = {
        "playabilityStatus": {"status": "OK"},
        "videoDetails": {
            "videoId": "tsJff6wc2XQ",
            "title": "The Two-Minute Shutdown Ritual | Stickman Productivity",
            "channelId": "UCbIyc_cOTGbOyC7CaIr_ZXg",
            "author": "Dao Simon",
            "viewCount": "0",
            "isPrivate": False,
        },
        "microformat": {
            "playerMicroformatRenderer": {
                "publishDate": "2026-08-10T05:01:29-07:00"
            }
        },
    }
    player.update(overrides)
    return player


@pytest.mark.parametrize(
    ("url", "expected_video_id"),
    [
        ("https://www.youtube.com/watch?v=tsJff6wc2XQ", "tsJff6wc2XQ"),
        ("https://youtu.be/tsJff6wc2XQ", "tsJff6wc2XQ"),
    ],
)
def test_canonicalizes_supported_public_youtube_urls(
    url: str,
    expected_video_id: str,
) -> None:
    canonical_url, video_id = youtube_public_video.canonicalize_youtube_public_url(url)

    assert canonical_url == (
        f"https://www.youtube.com/watch?v={expected_video_id}"
    )
    assert video_id == expected_video_id


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/watch?v=tsJff6wc2XQ",
        "http://www.youtube.com/watch?v=tsJff6wc2XQ",
        "https://www.youtube.com/watch?v=short",
    ],
)
def test_rejects_non_public_or_malformed_youtube_urls(url: str) -> None:
    with pytest.raises(ValueError):
        youtube_public_video.canonicalize_youtube_public_url(url)


def test_parses_zero_metrics_without_fabricating_missing_comments() -> None:
    html = _youtube_html(
        player=_player(),
        initial={
            "contents": [
                {
                    "segmentedLikeDislikeButtonViewModel": {
                        "likeCountEntity": {
                            "likeCountIfIndifferentNumber": "0"
                        }
                    }
                }
            ]
        },
    )

    result = youtube_public_video.parse_youtube_public_page(
        html,
        expected_video_id="tsJff6wc2XQ",
        public_url="https://www.youtube.com/watch?v=tsJff6wc2XQ",
        collected_at="2026-08-10T10:00:00+00:00",
    )

    assert result.video_id == "tsJff6wc2XQ"
    assert result.title == "The Two-Minute Shutdown Ritual | Stickman Productivity"
    assert result.channel_id == "UCbIyc_cOTGbOyC7CaIr_ZXg"
    assert result.channel_name == "Dao Simon"
    assert result.subscribers is None
    assert result.published_at == "2026-08-10T05:01:29-07:00"
    assert result.views == 0
    assert result.likes == 0
    assert result.comments is None
    assert result.unavailable_fields == ("comments", "subscribers")


def test_parses_numeric_comment_count_when_structured_data_exposes_it() -> None:
    html = _youtube_html(
        player=_player(),
        initial={
            "likeCountEntity": {"likeCountIfIndifferentNumber": "4"},
            "engagementPanel": {"commentCount": "12"},
            "videoOwnerRenderer": {
                "subscriberCountText": {"simpleText": "1.23K subscribers"}
            },
        },
    )

    result = youtube_public_video.parse_youtube_public_page(
        html,
        expected_video_id="tsJff6wc2XQ",
        public_url="https://www.youtube.com/watch?v=tsJff6wc2XQ",
        collected_at="2026-08-10T10:00:00+00:00",
    )

    assert result.likes == 4
    assert result.comments == 12
    assert result.subscribers == 1230
    assert result.unavailable_fields == ()


def test_parses_zero_public_subscribers() -> None:
    html = _youtube_html(
        player=_player(),
        initial={
            "videoOwnerRenderer": {
                "subscriberCountText": {"runs": [{"text": "No subscribers"}]}
            }
        },
    )

    result = youtube_public_video.parse_youtube_public_page(
        html,
        expected_video_id="tsJff6wc2XQ",
    )

    assert result.subscribers == 0


def test_rejects_a_public_page_for_a_different_video() -> None:
    html = _youtube_html(player=_player(), initial={})

    with pytest.raises(youtube_public_video.YouTubePublicVideoError) as exc_info:
        youtube_public_video.parse_youtube_public_page(
            html,
            expected_video_id="abcdefghijk",
            public_url="https://www.youtube.com/watch?v=abcdefghijk",
            collected_at="2026-08-10T10:00:00+00:00",
        )

    assert exc_info.value.code == "video_mismatch"


@pytest.mark.parametrize(
    "player",
    [
        _player(playabilityStatus={"status": "LOGIN_REQUIRED"}),
        _player(
            videoDetails={
                **_player()["videoDetails"],
                "isPrivate": True,
            }
        ),
    ],
)
def test_rejects_unplayable_or_private_video(player: dict) -> None:
    html = _youtube_html(player=player, initial={})

    with pytest.raises(youtube_public_video.YouTubePublicVideoError) as exc_info:
        youtube_public_video.parse_youtube_public_page(
            html,
            expected_video_id="tsJff6wc2XQ",
            public_url="https://www.youtube.com/watch?v=tsJff6wc2XQ",
            collected_at="2026-08-10T10:00:00+00:00",
        )

    assert exc_info.value.code in {"not_playable", "private_video"}


@pytest.mark.asyncio
async def test_fetches_the_canonical_public_page_without_authentication() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            text=_youtube_html(
                player=_player(),
                initial={
                    "likeCountEntity": {
                        "likeCountIfIndifferentNumber": "0"
                    }
                },
            ),
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
    ) as client:
        result = await youtube_public_video.fetch_youtube_public_metrics(
            "https://youtu.be/tsJff6wc2XQ",
            client=client,
            collected_at="2026-08-10T10:00:00+00:00",
        )

    assert result.video_id == "tsJff6wc2XQ"
    assert result.views == 0
    assert len(requests) == 1
    assert str(requests[0].url) == (
        "https://www.youtube.com/watch?v=tsJff6wc2XQ"
    )
    assert "cookie" not in requests[0].headers


@pytest.mark.asyncio
async def test_retries_one_server_error_then_parses_the_page() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(503, text="unavailable")
        return httpx.Response(
            200,
            text=_youtube_html(player=_player(), initial={}),
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
    ) as client:
        result = await youtube_public_video.fetch_youtube_public_metrics(
            "https://www.youtube.com/watch?v=tsJff6wc2XQ",
            client=client,
        )

    assert result.video_id == "tsJff6wc2XQ"
    assert attempts == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "expected_code"),
    [(403, "http_error"), (429, "http_error")],
)
async def test_reports_non_retryable_http_failures(
    status_code: int,
    expected_code: str,
) -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(status_code, text="blocked")
        ),
    ) as client:
        with pytest.raises(youtube_public_video.YouTubePublicVideoError) as exc_info:
            await youtube_public_video.fetch_youtube_public_metrics(
                "https://www.youtube.com/watch?v=tsJff6wc2XQ",
                client=client,
            )

    assert exc_info.value.code == expected_code


@pytest.mark.asyncio
async def test_reports_timeout_after_one_retry() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ReadTimeout("timed out", request=request)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(youtube_public_video.YouTubePublicVideoError) as exc_info:
            await youtube_public_video.fetch_youtube_public_metrics(
                "https://www.youtube.com/watch?v=tsJff6wc2XQ",
                client=client,
            )

    assert exc_info.value.code == "timeout"
    assert attempts == 2
