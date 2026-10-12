"""TikTok pulls exact authorized chat video bytes through a scoped capability."""
from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit
from unittest.mock import AsyncMock

import pytest

from packages.core.ai.mcp import tiktok
from packages.core.config import get_settings
from packages.core.models.base import generate_ulid
from packages.core.models.document import Document
from packages.core.models.task import Conversation
from packages.core.services import file_access_tokens
from packages.core.services import tiktok_publish_review as reviews
from test_chat_attachment_read_access import attachment_fs as attachment_fs, seed_attachment
from test_tiktok_mcp import CREATOR, MEDIA, approved_call_context
from test_youtube_tiktok_mcp import _FakeResp, tk_http as tk_http


@pytest.fixture(autouse=True)
def video_probe_and_creator(monkeypatch):
    monkeypatch.setattr(reviews, "_probe_video", AsyncMock(return_value={
        "duration_seconds": 45.25, "width": 1080, "height": 1920,
    }))
    monkeypatch.setattr(tiktok, "_get_creator_info", AsyncMock(return_value=json.dumps({"data": CREATOR})))


def _media(path, source):
    return {**MEDIA, "rel_path": path, "size_bytes": source.stat().st_size,
            "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest()}


async def _video(db, root, *, workspace=False):
    user, conv, msg, ws, old_source, _old_path = await seed_attachment(db, root, workspace=workspace)
    data = b"synthetic video bytes for the approved attachment"
    path = f"uploads/chat/{hashlib.sha256(data).hexdigest()[:16]}.mp4"
    source = old_source.parent / path.rsplit("/", 1)[-1]
    source.write_bytes(data)
    msg.attachments = [{"kind": "chat_upload", "path": path}]
    await db.commit()
    context = dict(entity_id=user.entity_id, user_id=user.id, conversation_id=conv.id)
    if ws:
        context["workspace_id"] = ws.id
    return user, conv, msg, ws, source, path, context


def _mint(path, source, context, **kwargs):
    return file_access_tokens.create_file_access_token(
        rel_path=path, source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
        **context, **kwargs,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("url_reference,workspace", [(False, False), (True, True)])
async def test_post_attachment_pulls_signed_exact_bytes(
    db_session, client, attachment_fs, monkeypatch, url_reference, workspace,
):
    _user, _conv, _msg, _ws, source, path, context = await _video(db_session, attachment_fs, workspace=workspace)
    monkeypatch.setattr(get_settings(), "PUBLIC_BASE_URL", "https://app.example.com")
    calls = []

    async def api(token, method, endpoint, params, body):
        calls.append((token, method, endpoint, body))
        return json.dumps({"data": {"publish_id": "pending-example"}})

    monkeypatch.setattr(tiktok, "_api", api)
    reference = f"/api/v1/fs/{context['entity_id']}/{path}" if url_reference else path
    arguments = {"video_url": reference, "title": "Approved caption", "privacy_level": "PUBLIC_TO_EVERYONE"}
    with approved_call_context(arguments, context=context, media=_media(path, source)):
        result = await tiktok.call_tool("post_video", arguments, "oauth-example")
    assert not result["isError"]
    assert json.loads(result["content"][0]["text"]) == {"data": {"publish_id": "pending-example"}}
    assert len(calls) == 1
    token, method, endpoint, body = calls[0]
    assert (token, method, endpoint) == ("oauth-example", "POST", "post/publish/video/init/")
    assert body["post_info"]["privacy_level"] == "PUBLIC_TO_EVERYONE"
    assert body["source_info"]["source"] == "PULL_FROM_URL"
    signed_url = body["source_info"]["video_url"]
    assert signed_url.startswith("https://app.example.com/api/v1/fs/public/")
    signed_path = urlsplit(signed_url).path
    capability = signed_path.split("/public/", 1)[1].split("/", 1)[0]
    claims = file_access_tokens.verify_file_access_token(capability)
    assert claims == {**context, "path": path, "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest()}
    assert capability not in json.dumps(result)
    head = await client.head(signed_path)
    assert head.status_code == 200
    assert head.headers["content-type"] == "video/mp4"
    assert head.headers["content-length"] == str(source.stat().st_size)
    assert head.headers["cache-control"] == "private, no-store"
    get = await client.get(signed_path)
    assert get.status_code == 200
    assert get.content == source.read_bytes()
    assert get.headers["cache-control"] == "private, no-store"
    partial = await client.get(signed_path, headers={"Range": "bytes=0-8"})
    assert partial.status_code == 206
    assert partial.content == source.read_bytes()[:9]


@pytest.mark.asyncio
@pytest.mark.parametrize("restriction", [
    "expired", "deleted_file", "changed_bytes", "symlink", "inactive_user",
    "deleted_conversation", "removed_reference", "other_conversation", "other_user",
    "wrong_workspace", "deleted_workspace", "trashed_document", "restricted_document",
])
async def test_signed_chat_reads_revalidate_scope_and_exact_content(
    db_session, client, attachment_fs, monkeypatch, restriction,
):
    user, conv, msg, ws, source, path, context = await _video(db_session, attachment_fs, workspace=True)
    if restriction == "other_conversation":
        other = Conversation(entity_id=user.entity_id, user_id=user.id, status="active", scope="channel")
        db_session.add(other)
        await db_session.flush()
        context["conversation_id"] = other.id
    elif restriction == "other_user":
        context["user_id"] = generate_ulid()
    elif restriction == "wrong_workspace":
        context["workspace_id"] = generate_ulid()
    token = _mint(path, source, context)
    if restriction == "expired":
        now = time.time()
        monkeypatch.setattr(file_access_tokens.time, "time", lambda: now + 901)
    elif restriction == "deleted_file":
        source.unlink()
    elif restriction == "changed_bytes":
        source.write_bytes(b"different video after approval")
    elif restriction == "symlink":
        other_source = source.with_name("private.mp4")
        source.rename(other_source)
        source.symlink_to(other_source)
    elif restriction == "inactive_user":
        user.status = "inactive"
    elif restriction == "deleted_conversation":
        await db_session.delete(conv)
    elif restriction == "removed_reference":
        msg.attachments = None
    elif restriction == "deleted_workspace":
        ws.deleted_at = datetime.now(timezone.utc)
    elif restriction in {"trashed_document", "restricted_document"}:
        doc = Document(entity_id=user.entity_id, name="video.mp4", fs_path=path, file_type="mp4", owner_id=user.id, visibility="private")
        if restriction == "trashed_document":
            doc.is_trashed = True
        else:
            doc.classification = "restricted"
        db_session.add(doc)
    await db_session.commit()
    for method in (client.head, client.get):
        response = await method(f"/api/v1/fs/public/{token}/video.mp4")
        assert response.status_code in {403, 404}
        assert b"synthetic video" not in response.content


@pytest.mark.asyncio
@pytest.mark.parametrize("reference,context_override", [
    ("uploads/chat/../private.mp4", {}),
    ("uploads/chat/1234567890abcdef.mp4", {}),
    ("/api/v1/fs/other/uploads/chat/1234567890abcdef.mp4", {}),
    (None, {"conversation_id": ""}),
    (None, {"user_id": ""}),
])
async def test_attachment_denial_happens_before_tiktok_init(
    db_session, attachment_fs, monkeypatch, reference, context_override,
):
    _user, _conv, _msg, _ws, source, path, context = await _video(db_session, attachment_fs)
    monkeypatch.setattr(get_settings(), "PUBLIC_BASE_URL", "https://app.example.com")

    async def api(*_args, **_kwargs):
        pytest.fail("Unauthorized attachment reached TikTok init")

    monkeypatch.setattr(tiktok, "_api", api)
    arguments = {"video_url": reference or path, "privacy_level": "PUBLIC_TO_EVERYONE"}
    with approved_call_context(arguments, context=context, media=_media(path, source)):
        tiktok.set_call_context({**tiktok._call_ctx_var.get(), **context_override})
        result = await tiktok.call_tool("post_video", arguments, "oauth-example")
    assert result["isError"]


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [0, 512 * 1024 * 1024 + 1])
async def test_video_outside_signed_download_limit_fails_before_init(db_session, attachment_fs, monkeypatch, size):
    _user, _conv, _msg, _ws, source, path, context = await _video(db_session, attachment_fs)
    with source.open("wb") as output:
        output.truncate(size)
    monkeypatch.setattr(get_settings(), "PUBLIC_BASE_URL", "https://app.example.com")

    async def api(*_args, **_kwargs):
        pytest.fail("An unreadable-size video reached TikTok init")

    monkeypatch.setattr(tiktok, "_api", api)
    with pytest.raises(reviews.TikTokPublishReviewError, match="512 MiB"):
        await reviews.prepare_tiktok_publish_review("mcp__tiktok__post_video", {"video_url": path}, **context)


@pytest.mark.asyncio
async def test_provider_errors_cannot_echo_the_signed_capability(db_session, attachment_fs, monkeypatch):
    _user, _conv, _msg, _ws, source, path, context = await _video(db_session, attachment_fs)
    monkeypatch.setattr(get_settings(), "PUBLIC_BASE_URL", "https://app.example.com")
    calls = []

    async def api(_token, _method, _endpoint, _params, body):
        calls.append(body)
        raise tiktok._TikTokError(f"url_ownership_unverified: {body['source_info']['video_url']}")

    monkeypatch.setattr(tiktok, "_api", api)
    arguments = {"video_url": path, "privacy_level": "PUBLIC_TO_EVERYONE"}
    with approved_call_context(arguments, context=context, media=_media(path, source)):
        result = await tiktok.call_tool("post_video", arguments, "oauth-example")
    assert result["isError"]
    assert "url_ownership_unverified" in result["content"][0]["text"]
    assert "/public/" not in result["content"][0]["text"]
    assert len(calls) == 1  # An init can create a post; it must not be retried.


def test_chat_capability_claims_are_bounded_and_not_silently_downgraded(monkeypatch):
    monkeypatch.setattr(file_access_tokens.time, "time", lambda: 1000)
    kwargs = dict(entity_id="entity", rel_path="uploads/chat/1234567890abcdef.mp4", user_id="actor", conversation_id="conversation", source_sha256="a" * 64)
    token = file_access_tokens.create_file_access_token(**kwargs, expires_in_seconds=99999)
    monkeypatch.setattr(file_access_tokens.time, "time", lambda: 1901)
    assert file_access_tokens.verify_file_access_token(token) is None
    for missing in ("user_id", "conversation_id", "source_sha256"):
        with pytest.raises(ValueError):
            file_access_tokens.create_file_access_token(**{**kwargs, missing: None})


@pytest.mark.asyncio
async def test_legacy_media_generation_chat_urls_remain_compatible(db_session, client, attachment_fs, monkeypatch):
    from packages.core.tasks import media_tasks

    _user, _conv, _msg, _ws, source, path, context = await _video(db_session, attachment_fs)

    async def preflight(url):
        response = await client.head(urlsplit(url).path)
        return response.status_code == 200, ""

    monkeypatch.setattr(media_tasks, "_preflight_public_media_url", preflight)
    signed_url = await media_tasks._ensure_public_url(
        f"/api/v1/fs/{context['entity_id']}/{path}", context["entity_id"],
        allow_data_uri=False, public_base_url="https://app.example.com",
    )
    signed_path = urlsplit(signed_url).path
    token = signed_path.split("/public/", 1)[1].split("/", 1)[0]
    assert file_access_tokens.verify_file_access_token(token) == {"entity_id": context["entity_id"], "path": path}
    assert (await client.head(signed_path)).status_code == 200
    assert (await client.get(signed_path)).content == source.read_bytes()


@pytest.mark.parametrize("missing", ["user_id", "conversation_id", "source_sha256"])
def test_partially_scoped_signed_tokens_fail_verification(missing):
    payload = {"entity_id": "entity", "path": "uploads/chat/1234567890abcdef.mp4", "user_id": "actor", "conversation_id": "conversation", "source_sha256": "a" * 64, "exp": int(time.time()) + 900}
    payload.pop(missing)
    encoded = file_access_tokens._b64_encode(json.dumps(payload).encode())
    token = f"{encoded}.{file_access_tokens._sign(encoded)}"
    assert file_access_tokens.verify_file_access_token(token) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [200, 400])
async def test_api_redacts_provider_echo_before_truncating(tk_http, status):
    capability = "a" * 600 + ".signature"
    signed_url = f"https://app.example.com/api/v1/fs/public/{capability}/video.mp4"
    tk_http.response = _FakeResp(status, {"error": {"code": "url_ownership_unverified", "message": signed_url}})
    with pytest.raises(tiktok._TikTokError) as raised:
        await tiktok._api("oauth-example", "POST", "post/publish/video/init/", None, {"source_info": {"video_url": signed_url}})
    assert "url_ownership_unverified" in str(raised.value)
    assert "aaaa" not in str(raised.value)
    assert "/public/" not in str(raised.value)


@pytest.mark.asyncio
async def test_scoped_response_cannot_follow_replacement_after_authorization(db_session, client, attachment_fs, monkeypatch):
    from pathlib import Path

    from apps.api import file_responses

    _user, _conv, _msg, _ws, source, path, context = await _video(db_session, attachment_fs)
    approved_bytes = source.read_bytes()
    token = _mint(path, source, context)
    original_copy = file_responses._copy_file_snapshot
    snapshots = []

    def replace_before_copy(descriptor_path):
        private = source.with_name("private.mp4")
        private.write_bytes(b"unauthorized replacement")
        source.unlink()
        source.symlink_to(private)
        copied_path = original_copy(descriptor_path)
        snapshots.append(Path(copied_path))
        assert Path(copied_path).read_bytes() == approved_bytes
        return copied_path

    monkeypatch.setattr(file_responses, "_copy_file_snapshot", replace_before_copy)
    result = await client.get(f"/api/v1/fs/public/{token}/video.mp4")
    assert result.status_code == 404
    assert b"unauthorized replacement" not in result.content
    assert snapshots and all(not snapshot.exists() for snapshot in snapshots)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["changed_bytes", "symlink", "removed_reference", "other_conversation"])
async def test_reviewed_attachment_rechecks_guard_and_pinned_content(
    db_session, attachment_fs, monkeypatch, change,
):
    _user, _conv, msg, _ws, source, path, context = await _video(db_session, attachment_fs)
    media = await reviews.inspect_chat_video(path, **context)
    assert media["source_sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert media["size_bytes"] == source.stat().st_size
    if change == "changed_bytes":
        source.write_bytes(b"replaced after review")
    elif change == "symlink":
        private = source.with_name("private.mp4")
        source.rename(private)
        source.symlink_to(private)
    elif change == "removed_reference":
        msg.attachments = None
        await db_session.commit()
    elif change == "other_conversation":
        context["conversation_id"] = generate_ulid()
    with pytest.raises(reviews.TikTokPublishReviewError):
        await reviews.inspect_chat_video(path, **context, expected_source_sha256=media["source_sha256"])


@pytest.mark.asyncio
async def test_changed_source_between_preflight_and_signing_cannot_mint_or_initialize(
    db_session, attachment_fs, monkeypatch,
):
    _user, _conv, _msg, _ws, source, path, context = await _video(db_session, attachment_fs)
    monkeypatch.setattr(get_settings(), "PUBLIC_BASE_URL", "https://app.example.com")
    original_signer = tiktok._signed_chat_video_url

    async def replace_before_signing(reference, *, expected_source_sha256):
        source.write_bytes(b"a different video after the final media preflight")
        return await original_signer(reference, expected_source_sha256=expected_source_sha256)

    monkeypatch.setattr(tiktok, "_signed_chat_video_url", replace_before_signing)
    api = AsyncMock()
    mint = AsyncMock()
    monkeypatch.setattr(tiktok, "_api", api)
    monkeypatch.setattr(file_access_tokens, "create_file_access_token", mint)
    args = {"video_url": path, "privacy_level": "PUBLIC_TO_EVERYONE"}
    with approved_call_context(args, context=context, media=_media(path, source)):
        result = await tiktok.call_tool("post_video", args, "oauth-example")
    assert result["isError"]
    mint.assert_not_called()
    api.assert_not_called()
