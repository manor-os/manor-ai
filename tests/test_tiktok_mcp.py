"""TikTok publication requires one exact server-owned human review."""

from contextlib import contextmanager
import json
from unittest.mock import AsyncMock

import pytest

from packages.core.ai.mcp import tiktok
from packages.core.ai.runtime.authorization_receipts import (
    RuntimeToolAuthorizationLease,
    RuntimeToolAuthorizationReceipt,
    TikTokPublishConsent,
    runtime_authorization_arguments_hash,
    runtime_begin_tool_authorization_scope,
    runtime_end_tool_authorization_scope,
)
from packages.core.services.runtime_authorization.domain import RuntimeAuthorizationAccess
from packages.core.services import tiktok_publish_review as reviews


CREATOR = {
    "creator_username": "example_creator",
    "creator_nickname": "Example Creator",
    "creator_avatar_url": "https://example.com/avatar.jpg",
    "privacy_level_options": ["PUBLIC_TO_EVERYONE", "SELF_ONLY"],
    "comment_disabled": False,
    "duet_disabled": False,
    "stitch_disabled": False,
    "max_video_post_duration_sec": 300,
}
MEDIA = {
    "rel_path": "uploads/chat/0123456789abcdef.mp4",
    "source_sha256": "a" * 64,
    "size_bytes": 1024,
    "duration_seconds": 45.25,
    "width": 1080,
    "height": 1920,
}
ARGS = {"video_url": MEDIA["rel_path"], "title": "Reviewed caption", "privacy_level": "PUBLIC_TO_EVERYONE"}


@contextmanager
def approved_call_context(args, *, context=None, media=None, evidence_changes=None, fields=None):
    """Exercise the real runtime lease while external requests remain mocked."""
    context = {"entity_id": "entity", "user_id": "actor", "conversation_id": "conversation", **(context or {})}
    context.setdefault("integration_account_id", "account")
    media = media or MEDIA
    evidence = {
        "version": 1,
        "review_id": "review",
        "tool_name": "mcp__tiktok__post_video",
        "workspace_id": None,
        **context,
        "creator_username": CREATOR["creator_username"],
        **{key: media[key] for key in ("rel_path", "source_sha256", "size_bytes", "duration_seconds")},
        **(evidence_changes or {}),
    }
    consent = TikTokPublishConsent("approval", json.dumps(evidence), json.dumps(fields or args))
    receipt = RuntimeToolAuthorizationReceipt(
        tool_name="mcp__tiktok__post_video",
        **{key: context[key] for key in ("entity_id", "user_id", "conversation_id")},
        workspace_id=context.get("workspace_id"),
        task_id=None,
        action_key="social_post.publish",
        capability_id="external.social",
        access=RuntimeAuthorizationAccess.ACTION,
        arguments_hash=runtime_authorization_arguments_hash(args, public_argument_keys=frozenset(args)),
        resource_kind="integration",
        file_resources=(),
        public_argument_keys=frozenset(args),
        tiktok_publish_consent=consent,
    )
    scope = runtime_begin_tool_authorization_scope(RuntimeToolAuthorizationLease(receipt), arguments=args)
    assert scope.valid
    token = tiktok._call_ctx_var.set({**context, "tiktok_publish_consent": consent})
    try:
        yield consent
    finally:
        tiktok._call_ctx_var.reset(token)
        runtime_end_tool_authorization_scope(scope)


@contextmanager
def approved_photo_call_context(args, *, sources, context=None):
    """Exercise a distinct photo receipt; video receipts cannot authorize it."""
    context = {"entity_id": "entity", "user_id": "actor", "conversation_id": "conversation", **(context or {})}
    context.setdefault("integration_account_id", "account")
    evidence = {
        "version": 2, "review_id": "photo-review", "tool_name": "mcp__tiktok__post_photo",
        "workspace_id": None, "media_type": "photo", "media_sources": sources,
        **context, "creator_username": CREATOR["creator_username"],
    }
    consent = TikTokPublishConsent("photo-approval", json.dumps(evidence), json.dumps(args))
    receipt = RuntimeToolAuthorizationReceipt(
        tool_name="mcp__tiktok__post_photo",
        **{key: context[key] for key in ("entity_id", "user_id", "conversation_id")},
        workspace_id=context.get("workspace_id"), task_id=None,
        action_key="social_post.publish", capability_id="external.social",
        access=RuntimeAuthorizationAccess.ACTION,
        arguments_hash=runtime_authorization_arguments_hash(args, public_argument_keys=frozenset(args)),
        resource_kind="integration", file_resources=(), public_argument_keys=frozenset(args),
        tiktok_publish_consent=consent,
    )
    scope = runtime_begin_tool_authorization_scope(RuntimeToolAuthorizationLease(receipt), arguments=args)
    assert scope.valid
    token = tiktok._call_ctx_var.set({**context, "tiktok_publish_consent": consent})
    try:
        yield consent
    finally:
        tiktok._call_ctx_var.reset(token)
        runtime_end_tool_authorization_scope(scope)


@pytest.fixture
def publish_mocks(monkeypatch):
    calls = []

    async def api(_token, _method, endpoint, params=None, body=None):
        calls.append((endpoint, body))
        if endpoint == "post/publish/creator_info/query/":
            return json.dumps({"data": dict(CREATOR)})
        return '{"data":{"publish_id":"pending"}}'

    monkeypatch.setattr(tiktok, "_api", api)
    inspect = AsyncMock(return_value=dict(MEDIA))
    signer = AsyncMock(return_value="https://app.example.com/api/v1/fs/public/capability/video.mp4")
    monkeypatch.setattr(reviews, "inspect_chat_video", inspect)
    monkeypatch.setattr(tiktok, "_signed_chat_video_url", signer)
    return calls, inspect, signer


@pytest.mark.asyncio
async def test_model_confirmation_cannot_publish_without_server_consent(monkeypatch):
    calls = []

    async def api(*args, **kwargs):
        calls.append((args, kwargs))
        return '{"data":{"publish_id":"pending"}}'

    monkeypatch.setattr(tiktok, "_api", api)
    result = await tiktok.call_tool(
        "post_video",
        {
            "video_url": "https://example.com/video.mp4",
            "privacy_level": "PUBLIC_TO_EVERYONE",
            "music_usage_confirmed": True,
            "consent_confirmed": True,
        },
        "test-token",
    )
    assert result["isError"]
    assert not calls


@pytest.mark.asyncio
async def test_reviewed_post_checks_fresh_creator_and_exact_media_then_returns_pending(publish_mocks):
    calls, inspect, signer = publish_mocks
    with approved_call_context(ARGS, fields={**ARGS, "integration_account_id": "account"}):
        result = await tiktok.call_tool("post_video", ARGS, "test-token")
    assert not result["isError"]
    assert json.loads(result["content"][0]["text"]) == {"data": {"publish_id": "pending"}}
    assert [path for path, _ in calls] == ["post/publish/creator_info/query/", "post/publish/video/init/"]
    assert calls[-1][1]["post_info"] == {
        "title": "Reviewed caption",
        "privacy_level": "PUBLIC_TO_EVERYONE",
        "disable_comment": True,
        "disable_duet": True,
        "disable_stitch": True,
        "brand_organic_toggle": False,
        "brand_content_toggle": False,
    }
    assert inspect.await_args.kwargs["expected_source_sha256"] == MEDIA["source_sha256"]
    signer.assert_awaited_once_with(ARGS["video_url"], expected_source_sha256=MEDIA["source_sha256"])
    assert "capability" not in json.dumps(result)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changed",
    [
        {"entity_id": "different"},
        {"user_id": "different"},
        {"conversation_id": "different"},
        {"workspace_id": "different"},
        {"integration_account_id": "different"},
    ],
)
async def test_changed_context_stops_before_creator_or_signing(publish_mocks, changed):
    calls, inspect, signer = publish_mocks
    with approved_call_context(ARGS):
        tiktok.set_call_context({**tiktok._call_ctx_var.get(), **changed})
        result = await tiktok.call_tool("post_video", ARGS, "test-token")
    assert result["isError"]
    assert not calls
    inspect.assert_not_awaited()
    signer.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changed",
    [
        {"title": "Different caption"},
        {"privacy_level": "SELF_ONLY"},
        {"video_url": "uploads/chat/1111111111111111.mp4"},
        {"disable_comment": False},
    ],
)
async def test_changed_args_stops_before_creator_or_signing(publish_mocks, changed):
    calls, _, signer = publish_mocks
    with approved_call_context(ARGS):
        result = await tiktok.call_tool("post_video", {**ARGS, **changed}, "test-token")
    assert result["isError"]
    assert not calls
    signer.assert_not_awaited()


@pytest.mark.asyncio
async def test_plain_dictionary_consent_is_not_server_consent(publish_mocks):
    calls, _, _ = publish_mocks
    with approved_call_context(ARGS) as consent:
        tiktok.set_call_context(
            {
                **tiktok._call_ctx_var.get(),
                "tiktok_publish_consent": {"evidence": consent.evidence, "fields": consent.fields},
            }
        )
        result = await tiktok.call_tool("post_video", ARGS, "test-token")
    assert result["isError"]
    assert not calls


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "setting",
    [
        {},
        {"privacy_level": "unknown"},
        {"disable_comment": "false", "privacy_level": "PUBLIC_TO_EVERYONE"},
        {"commercial_content": True, "privacy_level": "PUBLIC_TO_EVERYONE"},
        {"commercial_content": True, "brand_content_toggle": True, "privacy_level": "SELF_ONLY"},
    ],
)
async def test_invalid_reviewed_settings_never_initialize(publish_mocks, setting):
    calls, _, signer = publish_mocks
    args = {"video_url": MEDIA["rel_path"], **setting}
    with approved_call_context(args):
        result = await tiktok.call_tool("post_video", args, "test-token")
    assert result["isError"]
    assert all(endpoint != "post/publish/video/init/" for endpoint, _ in calls)
    signer.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changed",
    [
        {"creator_username": "other_creator"},
        {"privacy_level_options": ["SELF_ONLY"]},
        {"max_video_post_duration_sec": 10},
        {"comment_disabled": True},
    ],
)
async def test_fresh_creator_changes_block_stale_review(publish_mocks, monkeypatch, changed):
    calls, _, signer = publish_mocks
    monkeypatch.setattr(
        tiktok, "_get_creator_info", AsyncMock(return_value=json.dumps({"data": {**CREATOR, **changed}}))
    )
    args = {**ARGS, "disable_comment": False}
    with approved_call_context(args):
        result = await tiktok.call_tool("post_video", args, "test-token")
    assert result["isError"]
    assert not calls
    signer.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", [{"source_sha256": "b" * 64}, {"size_bytes": 999}, {"duration_seconds": 99.0}])
async def test_media_evidence_must_still_match_review(publish_mocks, changed):
    calls, inspect, signer = publish_mocks
    inspect.return_value = {**MEDIA, **changed}
    with approved_call_context(ARGS):
        result = await tiktok.call_tool("post_video", ARGS, "test-token")
    assert result["isError"]
    assert len(calls) == 1
    signer.assert_not_awaited()


@pytest.mark.asyncio
async def test_one_consent_cannot_initialize_twice_even_after_provider_failure(publish_mocks, monkeypatch):
    calls, _, signer = publish_mocks

    async def api(_token, _method, endpoint, params=None, body=None):
        calls.append((endpoint, body))
        if "creator_info" in endpoint:
            return json.dumps({"data": CREATOR})
        raise tiktok._TikTokError("Ambiguous provider response")

    monkeypatch.setattr(tiktok, "_api", api)
    with approved_call_context(ARGS):
        first = await tiktok.call_tool("post_video", ARGS, "test-token")
        second = await tiktok.call_tool("post_video", ARGS, "test-token")
    assert first["isError"] and second["isError"]
    assert sum(endpoint == "post/publish/video/init/" for endpoint, _ in calls) == 1
    signer.assert_awaited_once()


@pytest.mark.asyncio
async def test_photo_cannot_bypass_video_review(publish_mocks):
    calls, _, signer = publish_mocks
    with approved_call_context(ARGS):
        result = await tiktok.call_tool("post_photo", {"photo_urls": ["https://example.com/photo.jpg"]}, "test-token")
    assert result["isError"]
    assert "fresh review" in result["content"][0]["text"]
    assert not calls
    signer.assert_not_awaited()


@pytest.mark.asyncio
async def test_reviewed_photo_post_uses_ordered_signed_sources_and_exact_one_time_consent(monkeypatch):
    photos = [
        {"source_kind": "chat_attachment", "rel_path": "uploads/chat/1111111111111111.jpg", "source_sha256": "1" * 64, "size_bytes": 101},
        {"source_kind": "workspace_document", "document_id": "doc-2", "rel_path": "Knowledge/second.webp", "source_sha256": "2" * 64, "size_bytes": 102},
    ]
    args = {
        "photo_urls": [photos[0]["rel_path"], {"source_kind": "workspace_document", "document_id": "doc-2"}],
        "title": "Reviewed photos", "privacy_level": "PUBLIC_TO_EVERYONE", "photo_cover_index": 1,
    }
    calls = []

    async def api(_token, _method, endpoint, _params=None, body=None):
        calls.append((endpoint, body))
        if "creator_info" in endpoint:
            return json.dumps({"data": CREATOR})
        return json.dumps({"data": {"publish_id": "photos-pending"}})

    signed = AsyncMock(return_value=["https://app.example/one.jpg", "https://app.example/two.webp"])
    monkeypatch.setattr(tiktok, "_api", api)
    monkeypatch.setattr(tiktok, "_signed_tiktok_photo_urls", signed)
    with approved_photo_call_context(args, sources=photos):
        result = await tiktok.call_tool("post_photo", args, "test-token")
        replay = await tiktok.call_tool("post_photo", args, "test-token")
    assert not result["isError"] and replay["isError"]
    assert [endpoint for endpoint, _body in calls] == ["post/publish/creator_info/query/", "post/publish/content/init/", "post/publish/creator_info/query/"]
    assert calls[1][1]["source_info"] == {
        "source": "PULL_FROM_URL", "photo_images": ["https://app.example/one.jpg", "https://app.example/two.webp"],
        "photo_cover_index": 1,
    }
    assert calls[1][1]["post_info"]["description"] == "Reviewed photos"
    assert calls[1][1]["post_mode"] == "DIRECT_POST"
    assert calls[1][1]["media_type"] == "PHOTO"
    assert "disable_duet" not in calls[1][1]["post_info"]
    assert "disable_stitch" not in calls[1][1]["post_info"]
    signed.assert_awaited_once_with(args["photo_urls"], expected_sources=photos)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [200, 403])
async def test_unaudited_error_explains_app_audit_not_oauth_or_private_retry(monkeypatch, status):
    from test_youtube_tiktok_mcp import _FakeClient, _FakeResp

    response = _FakeResp(
        status, {"error": {"code": "unaudited_client_can_only_post_to_private_accounts", "message": "Vendor detail"}}
    )
    monkeypatch.setattr(_FakeClient, "response", response)
    monkeypatch.setattr(_FakeClient, "route", None)
    monkeypatch.setattr(tiktok.httpx, "AsyncClient", _FakeClient)
    with pytest.raises(tiktok._TikTokError) as exc:
        await tiktok._api("test-token", "POST", "post/publish/video/init/")
    message = str(exc.value)
    assert "audit required" in message
    assert "Content Posting API audit" in message
    assert "Reconnect" not in message and "retry" not in message.lower()


@pytest.mark.asyncio
async def test_status_rate_limit_does_not_claim_the_publication_stopped(monkeypatch):
    from test_youtube_tiktok_mcp import _FakeClient, _FakeResp

    monkeypatch.setattr(_FakeClient, "response", _FakeResp(429, {"error": {"code": "rate_limit_exceeded"}}))
    monkeypatch.setattr(_FakeClient, "route", None)
    monkeypatch.setattr(tiktok.httpx, "AsyncClient", _FakeClient)
    result = await tiktok.call_tool("get_publish_status", {"publish_id": "existing-post"}, "test-token")
    assert result["isError"] is True
    assert "rate_limit_exceeded" in result["content"][0]["text"]
    assert "publishing attempt has stopped" not in result["content"][0]["text"]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [200, 429])
@pytest.mark.parametrize("code", ["spam_risk_too_many_posts", "reached_active_user_cap", "rate_limit_exceeded"])
async def test_creator_limit_stops_publish_before_signing_and_explains_retry(monkeypatch, status, code):
    from test_youtube_tiktok_mcp import _FakeClient, _FakeResp

    monkeypatch.setattr(_FakeClient, "response", _FakeResp(status, {"error": {"code": code, "message": "Limit reached"}}))
    monkeypatch.setattr(_FakeClient, "route", None)
    monkeypatch.setattr(_FakeClient, "calls", [])
    monkeypatch.setattr(tiktok.httpx, "AsyncClient", _FakeClient)
    signer = AsyncMock()
    monkeypatch.setattr(tiktok, "_signed_chat_video_url", signer)
    with approved_call_context(ARGS):
        result = await tiktok.call_tool("post_video", ARGS, "test-token")
    assert result["isError"] is True
    assert "try again later" in result["content"][0]["text"]
    assert "Reconnect" not in result["content"][0]["text"]
    assert len(_FakeClient.calls) == 1
    assert _FakeClient.calls[0]["url"].endswith("/post/publish/creator_info/query/")
    signer.assert_not_awaited()
