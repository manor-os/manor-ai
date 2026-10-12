"""Creator/media evidence and explicit TikTok review fields are fail-closed."""

import asyncio
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from packages.core.services import tiktok_publish_review as reviews
from test_tiktok_mcp import ARGS, CREATOR, MEDIA


@pytest.fixture
def review():
    return {
        "kind": "tiktok_publish",
        "version": 1,
        "review_id": "review-1",
        "creator_info": copy.deepcopy(CREATOR),
        "media": {"type": "video", "duration_sec": MEDIA["duration_seconds"]},
        "initial_caption": "Original caption",
    }


@pytest.fixture
def submitted(review):
    return {
        "kind": "tiktok_publish",
        "version": 1,
        "review_id": review["review_id"],
        "caption": "Human edited caption",
        "privacy_level": "PUBLIC_TO_EVERYONE",
        "allow_comment": False,
        "allow_duet": False,
        "allow_stitch": False,
        "commercial_content": False,
        "brand_organic_toggle": False,
        "brand_content_toggle": False,
        "music_usage_confirmed": True,
        "branded_content_policy_confirmed": False,
        "consent_confirmed": True,
    }


@pytest.mark.asyncio
async def test_prepare_keeps_exact_account_media_evidence_private_and_refreshes_review_id(monkeypatch):
    inspect = AsyncMock(return_value=dict(MEDIA))
    creator = AsyncMock(return_value=("account", dict(CREATOR)))
    monkeypatch.setattr(reviews, "inspect_chat_video", inspect)
    monkeypatch.setattr(reviews, "_creator_for_actor", creator)
    scope = dict(entity_id="entity", user_id="actor", conversation_id="conversation", workspace_id="workspace")
    first = await reviews.prepare_tiktok_publish_review("mcp__tiktok__post_video", ARGS, **scope)
    second = await reviews.prepare_tiktok_publish_review("mcp__tiktok__post_video", ARGS, **scope)
    public = first["review"]
    evidence = first["tiktok_publish_evidence"]
    assert public["review_id"] != second["review"]["review_id"]
    assert evidence["review_id"] == public["review_id"]
    assert public["media"] == {
        "url": f"/api/v1/fs/entity/{MEDIA['rel_path']}",
        "name": "0123456789abcdef.mp4",
        "type": "video",
        "duration_sec": 45.25,
        "width": 1080,
        "height": 1920,
        "size_bytes": 1024,
    }
    assert public["creator_info"] == CREATOR
    assert "source_sha256" not in json.dumps(public)
    assert "integration_account_id" not in json.dumps(public)
    assert evidence["integration_account_id"] == "account"
    assert evidence["original_args_sha256"] == reviews._arguments_hash(ARGS)
    assert evidence["source_sha256"] == MEDIA["source_sha256"]
    assert {key: evidence[key] for key in scope} == scope
    assert inspect.await_count == creator.await_count == 2


@pytest.mark.parametrize(
    "reference",
    [
        "https://example.com/video.mp4",
        "file:///private/video.mp4",
        "/tmp/video.mp4",
        "uploads/chat/../secret.mp4",
        "uploads/chat/0123456789abcdef.mp4?x=1",
        "/api/v1/fs/other/uploads/chat/0123456789abcdef.mp4",
        "uploads/chat/0123456789abcdef.m3u8",
    ],
)
def test_only_exact_current_entity_chat_video_references_are_accepted(reference):
    with pytest.raises(reviews.TikTokPublishReviewError):
        reviews.chat_video_path(reference, "entity")


@pytest.mark.asyncio
async def test_remote_url_fails_before_probe_or_creator_query(monkeypatch):
    probe = AsyncMock()
    creator = AsyncMock()
    monkeypatch.setattr(reviews, "_probe_video", probe)
    monkeypatch.setattr(reviews, "_creator_for_actor", creator)
    with pytest.raises(reviews.TikTokPublishReviewError, match="Upload.*Manor"):
        await reviews.prepare_tiktok_publish_review(
            "mcp__tiktok__post_video",
            {"video_url": "https://example.com/video.mp4"},
            "entity",
            "actor",
            "conversation",
        )
    probe.assert_not_awaited()
    creator.assert_not_awaited()


@pytest.mark.asyncio
async def test_creator_read_uses_same_actor_exact_selected_account_and_public_fields(monkeypatch):
    from packages.core.ai.tools import mcp_builtin
    from packages.core.services import agent_permission_service

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    permission = AsyncMock(return_value=SimpleNamespace(allowed=True, account_id="selected-account"))
    handler = AsyncMock(return_value=json.dumps({"data": {**CREATOR, "open_id": "private-account-id"}}))
    monkeypatch.setattr("packages.core.database.async_session", Session)
    monkeypatch.setattr(agent_permission_service, "can_use_integration", permission)
    monkeypatch.setattr(
        mcp_builtin,
        "_build_handler",
        lambda server, tool: handler if (server, tool) == ("tiktok", "get_creator_info") else None,
    )
    scope = dict(entity_id="entity", user_id="actor", conversation_id="conversation", workspace_id="workspace")
    account_id, creator = await reviews._creator_for_actor({"integration_account_id": "selected-account"}, **scope)
    assert account_id == "selected-account"
    assert creator == CREATOR
    handler.assert_awaited_once_with(**scope, integration_account_id="selected-account")
    assert permission.await_args.kwargs == {
        "user_id": "actor",
        "entity_id": "entity",
        "provider": "tiktok",
        "integration_account_id": "selected-account",
        "allow_env_fallback": False,
    }


@pytest.mark.parametrize(
    "payload",
    [
        {"data": CREATOR, "error": {"code": "scope_not_authorized"}},
        {"data": {**CREATOR, "max_video_post_duration_sec": True}},
        {"data": {**CREATOR, "privacy_level_options": []}},
        {"data": {**CREATOR, "duet_disabled": "false"}},
    ],
)
def test_incomplete_or_error_creator_result_cannot_be_reviewed(payload):
    with pytest.raises(reviews.TikTokPublishReviewError):
        reviews.validate_creator_info(payload)


def test_apply_preserves_media_account_and_only_uses_explicit_human_choices(review, submitted):
    original = {**ARGS, "integration_account_id": "account", "cover_timestamp_ms": 100}
    final = reviews.apply_tiktok_publish_review(review, submitted, original)
    assert final == {
        **original,
        "title": "Human edited caption",
        "disable_comment": True,
        "disable_duet": True,
        "disable_stitch": True,
        "commercial_content": False,
        "brand_organic_toggle": False,
        "brand_content_toggle": False,
    }
    assert original["title"] == "Reviewed caption"


@pytest.mark.parametrize(
    "changed",
    [
        {"review_id": "stale"},
        {"privacy_level": ""},
        {"privacy_level": "MUTUAL_FOLLOW_FRIENDS"},
        {"music_usage_confirmed": False},
        {"music_usage_confirmed": "true"},
        {"consent_confirmed": False},
        {"consent_confirmed": 1},
        {"allow_comment": "false"},
        {"allow_duet": 0},
        {"brand_organic_toggle": "false"},
        {"commercial_content": True},
        {"brand_organic_toggle": True},
        {"video_url": "uploads/chat/1111111111111111.mp4"},
        {"integration_account_id": "other"},
        {"auto_approve": True},
        {"version": True},
        {"branded_content_policy_confirmed": {"hidden": "payload"}},
        {"caption": "😀" * 1101},
        {"caption": 123},
    ],
)
def test_invalid_or_forged_submission_never_becomes_final_args(review, submitted, changed):
    with pytest.raises(reviews.TikTokPublishReviewError):
        reviews.apply_tiktok_publish_review(review, {**submitted, **changed}, ARGS)


@pytest.mark.parametrize(
    "missing",
    [
        "privacy_level",
        "allow_comment",
        "allow_duet",
        "allow_stitch",
        "commercial_content",
        "brand_organic_toggle",
        "brand_content_toggle",
        "music_usage_confirmed",
        "consent_confirmed",
    ],
)
def test_required_choices_have_no_implicit_defaults(review, submitted, missing):
    del submitted[missing]
    with pytest.raises(reviews.TikTokPublishReviewError):
        reviews.apply_tiktok_publish_review(review, submitted, ARGS)


def test_branded_content_requires_policy_and_cannot_use_only_me(review, submitted):
    submitted.update(commercial_content=True, brand_content_toggle=True)
    with pytest.raises(reviews.TikTokPublishReviewError, match="Branded Content Policy"):
        reviews.apply_tiktok_publish_review(review, submitted, ARGS)
    submitted.update(branded_content_policy_confirmed=True, privacy_level="SELF_ONLY")
    with pytest.raises(reviews.TikTokPublishReviewError, match="Only me"):
        reviews.apply_tiktok_publish_review(review, submitted, ARGS)
    submitted.update(privacy_level="PUBLIC_TO_EVERYONE", brand_organic_toggle=True)
    final = reviews.apply_tiktok_publish_review(review, submitted, ARGS)
    assert final["brand_organic_toggle"] is True and final["brand_content_toggle"] is True


@pytest.mark.parametrize("interaction", ["comment", "duet", "stitch"])
def test_creator_disabled_interactions_cannot_be_enabled(review, submitted, interaction):
    review["creator_info"][f"{interaction}_disabled"] = True
    submitted[f"allow_{interaction}"] = True
    with pytest.raises(reviews.TikTokPublishReviewError, match=f"disables {interaction}"):
        reviews.apply_tiktok_publish_review(review, submitted, ARGS)


@pytest.mark.parametrize("duration", [301, 0, float("nan"), float("inf")])
def test_verified_duration_must_fit_creator_limit(review, submitted, duration):
    review["media"]["duration_sec"] = duration
    with pytest.raises(reviews.TikTokPublishReviewError, match="duration"):
        reviews.apply_tiktok_publish_review(review, submitted, ARGS)


@pytest.mark.asyncio
async def test_probe_uses_only_pinned_descriptor_and_bounded_container_protocols(monkeypatch):
    stdout = SimpleNamespace(
        read=AsyncMock(side_effect=[b'{"format":{"duration":"45.25"},"streams":[{"width":1080,"height":1920}]}', b""])
    )
    process = SimpleNamespace(stdout=stdout, returncode=0, wait=AsyncMock())
    spawn = AsyncMock(return_value=process)
    monkeypatch.setattr(reviews.shutil, "which", lambda _name: "/usr/bin/ffprobe")
    monkeypatch.setattr(reviews.asyncio, "create_subprocess_exec", spawn)
    assert await reviews._probe_video("/dev/fd/8") == {"duration_seconds": 45.25, "width": 1080, "height": 1920}
    args = spawn.await_args.args
    assert args[-1] == "/dev/fd/8"
    assert args[args.index("-protocol_whitelist") + 1] == "file,pipe"
    assert args[args.index("-format_whitelist") + 1] == "mov,matroska,webm"
    assert spawn.await_args.kwargs["pass_fds"] == (8,)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["excessive", "cancelled", "timeout"])
async def test_probe_failure_kills_child_without_unbounded_output(monkeypatch, failure):
    class Process:
        returncode = None
        killed = False

        def __init__(self):
            effect = {"excessive": None, "cancelled": asyncio.CancelledError(), "timeout": TimeoutError()}[failure]
            self.stdout = SimpleNamespace(read=AsyncMock(return_value=b"x" * 4096, side_effect=effect))

        def kill(self):
            self.killed = True

        async def wait(self):
            self.returncode = -9

    process = Process()
    monkeypatch.setattr(reviews.shutil, "which", lambda _name: "/usr/bin/ffprobe")
    monkeypatch.setattr(reviews.asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    with pytest.raises(asyncio.CancelledError if failure == "cancelled" else reviews.TikTokPublishReviewError):
        await reviews._probe_video("/dev/fd/8")
    assert process.killed
    assert process.stdout.read.await_count <= 17
