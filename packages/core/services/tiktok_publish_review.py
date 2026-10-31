"""Exact media and creator evidence for the human TikTok publication review."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
from pathlib import Path
import shutil
from typing import Any
from uuid import uuid4

from packages.core.services.file_refs import parse_file_reference
from packages.core.services.generated_file_refs import entity_fs_open_url


_MAX_VIDEO_BYTES = 512 * 1024 * 1024
_MAX_PHOTO_BYTES = 20 * 1024 * 1024
_MAX_PHOTO_COUNT = 35
_PHOTO_SUFFIXES = {".jpg", ".jpeg", ".webp"}
_PRIVACY_OPTIONS = frozenset(
    {
        "PUBLIC_TO_EVERYONE",
        "MUTUAL_FOLLOW_FRIENDS",
        "FOLLOWER_OF_CREATOR",
        "SELF_ONLY",
    }
)
_SUBMITTED_FIELDS = frozenset(
    {
        "kind",
        "version",
        "review_id",
        "caption",
        "privacy_level",
        "allow_comment",
        "allow_duet",
        "allow_stitch",
        "commercial_content",
        "brand_organic_toggle",
        "brand_content_toggle",
        "music_usage_confirmed",
        "branded_content_policy_confirmed",
        "consent_confirmed",
        "photo_cover_index",
    }
)


class TikTokPublishReviewError(ValueError):
    """A safe user-facing reason the exact publication cannot proceed."""


def _inspect_tiktok_photo_snapshot(path: str) -> dict[str, int]:
    """Verify the pinned bytes are a provider-supported image."""
    try:
        from PIL import Image, UnidentifiedImageError

        with Image.open(path) as image:
            image.verify()
        with Image.open(path) as image:
            if image.format not in {"JPEG", "WEBP"}:
                raise TikTokPublishReviewError("TikTok photos must contain JPEG or WebP image bytes.")
            width, height = image.size
    except TikTokPublishReviewError:
        raise
    except (OSError, UnidentifiedImageError) as exc:
        raise TikTokPublishReviewError("TikTok photo bytes are invalid.") from exc
    if width <= 0 or height <= 0:
        raise TikTokPublishReviewError("TikTok photo dimensions are invalid.")
    return {"width": width, "height": height}


def _arguments_hash(arguments: dict[str, Any]) -> str:
    try:
        encoded = json.dumps(arguments, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise TikTokPublishReviewError("TikTok publication arguments are invalid.") from exc
    return hashlib.sha256(encoded.encode()).hexdigest()


def chat_video_path(reference: Any, entity_id: str) -> str:
    from packages.core.services.ai_file_permissions import is_chat_upload_path

    if not isinstance(reference, str):
        raise TikTokPublishReviewError("Upload the video to this Manor conversation before reviewing publication.")
    path = reference
    file_ref = parse_file_reference(reference)
    if file_ref is not None and file_ref.entity_id:
        if file_ref.entity_id != entity_id:
            raise TikTokPublishReviewError("The video reference belongs to another Entity.")
        path = file_ref.fs_path
    if not is_chat_upload_path(path) or Path(path).suffix.lower() not in {".mp4", ".mov", ".webm"}:
        raise TikTokPublishReviewError(
            "Upload an MP4, MOV, or WebM video to this Manor conversation so you can preview and approve the exact media. Remote video URLs are not supported for this review."
        )
    return path


def chat_photo_path(reference: Any, entity_id: str) -> str:
    """Resolve one uploaded provider-supported image without granting access."""
    from packages.core.services.ai_file_permissions import is_chat_upload_path

    if not isinstance(reference, str):
        raise TikTokPublishReviewError("Upload each photo to this Manor conversation before reviewing publication.")
    path = reference
    file_ref = parse_file_reference(reference)
    if file_ref is not None and file_ref.entity_id:
        if file_ref.entity_id != entity_id:
            raise TikTokPublishReviewError("The photo reference belongs to another Entity.")
        path = file_ref.fs_path
    if not is_chat_upload_path(path) or Path(path).suffix.lower() not in _PHOTO_SUFFIXES:
        raise TikTokPublishReviewError(
            "Upload JPEG or WebP photos to this Manor conversation so you can preview and approve the exact media. Remote image URLs are not supported for this review."
        )
    return path


async def _probe_video(descriptor_path: str) -> dict[str, Any]:
    """Probe only a pinned local descriptor, with bounded time and output."""
    executable = shutil.which("ffprobe")
    if not executable:
        raise TikTokPublishReviewError(
            "Video inspection is unavailable; publication cannot proceed without a verified duration."
        )
    descriptor = int(Path(descriptor_path).name)
    process = await asyncio.create_subprocess_exec(
        executable,
        "-v",
        "error",
        "-protocol_whitelist",
        "file,pipe",
        "-format_whitelist",
        "mov,matroska,webm",
        "-select_streams",
        "v:0",
        "-show_entries",
        "format=duration:stream=width,height",
        "-of",
        "json",
        descriptor_path,
        pass_fds=(descriptor,),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    output = bytearray()
    try:
        async with asyncio.timeout(20):
            while True:
                chunk = await process.stdout.read(4096)
                if not chunk:
                    break
                output.extend(chunk)
                if len(output) > 65536:
                    raise TikTokPublishReviewError("Video inspection returned excessive metadata.")
            await process.wait()
        if process.returncode != 0:
            raise TikTokPublishReviewError("The attachment could not be inspected as a valid video.")
        parsed = json.loads(output)
        duration = float(parsed["format"]["duration"])
        stream = parsed["streams"][0]
        width, height = int(stream["width"]), int(stream["height"])
        if not math.isfinite(duration) or duration <= 0 or width <= 0 or height <= 0:
            raise ValueError("invalid video metadata")
        return {"duration_seconds": duration, "width": width, "height": height}
    except (KeyError, IndexError, TypeError, ValueError, TimeoutError) as exc:
        if isinstance(exc, TikTokPublishReviewError):
            raise
        raise TikTokPublishReviewError("A valid video duration and dimensions could not be verified.") from exc
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


async def inspect_chat_video(
    reference: Any,
    *,
    entity_id: str,
    user_id: str,
    conversation_id: str,
    workspace_id: str | None = None,
    expected_source_sha256: str | None = None,
) -> dict[str, Any]:
    from packages.core.services.ai_file_permissions import guard_ai_file_read_access
    from packages.core.services.entity_fs import (
        EntityFilesystemError,
        canonical_entity_root,
        entity_filesystem_read_lock,
        is_fs_enabled,
        open_entity_file_snapshot,
    )

    if not entity_id or not user_id or not conversation_id:
        raise TikTokPublishReviewError("TikTok publication requires the current user and conversation.")
    path = chat_video_path(reference, entity_id)
    if not is_fs_enabled():
        raise TikTokPublishReviewError("The attachment filesystem is unavailable.")

    def digest_file(descriptor_path: str) -> str:
        with open(descriptor_path, "rb") as source:
            return hashlib.file_digest(source, "sha256").hexdigest()

    try:
        async with entity_filesystem_read_lock(canonical_entity_root(entity_id)):
            denied = await guard_ai_file_read_access(
                entity_id=entity_id,
                user_id=user_id,
                conversation_id=conversation_id,
                workspace_id=workspace_id,
                tool_name="mcp__tiktok__post_video",
                paths=[path],
            )
            if denied:
                raise TikTokPublishReviewError("The attachment is no longer readable in this conversation.")
            with open_entity_file_snapshot(entity_id, path) as snapshot:
                if not 0 < snapshot.stat.st_size <= _MAX_VIDEO_BYTES:
                    raise TikTokPublishReviewError("The video must be nonempty and at most 512 MiB.")
                digest = snapshot.content_sha256 or await asyncio.to_thread(digest_file, snapshot.descriptor_path)
                if expected_source_sha256 is not None and digest != expected_source_sha256:
                    raise TikTokPublishReviewError("The approved video has changed. Refresh the TikTok review.")
                metadata = await _probe_video(snapshot.descriptor_path)
                return {"rel_path": path, "source_sha256": digest, "size_bytes": snapshot.stat.st_size, **metadata}
    except (EntityFilesystemError, OSError) as exc:
        raise TikTokPublishReviewError(
            "The approved video is unavailable or has changed. Refresh the TikTok review."
        ) from exc


async def inspect_tiktok_video_source(
    reference: Any,
    *,
    entity_id: str,
    user_id: str,
    conversation_id: str,
    workspace_id: str | None = None,
    expected_source_sha256: str | None = None,
) -> dict[str, Any]:
    """Resolve an approved chat attachment or explicit Workspace Knowledge video."""
    if not isinstance(reference, dict):
        media = await inspect_chat_video(
            reference,
            entity_id=entity_id,
            user_id=user_id,
            conversation_id=conversation_id,
            workspace_id=workspace_id,
            expected_source_sha256=expected_source_sha256,
        )
        return {"source_kind": "chat_attachment", **media}
    if set(reference) != {"source_kind", "document_id"} or reference.get("source_kind") != "workspace_document":
        raise TikTokPublishReviewError("Use an exact chat attachment or a Workspace Knowledge document reference.")
    try:
        from packages.core.services.workspace_document_media import (
            WorkspaceDocumentMediaError,
            inspect_workspace_document_media,
        )

        return await inspect_workspace_document_media(
            reference,
            entity_id=entity_id,
            user_id=user_id,
            conversation_id=conversation_id,
            workspace_id=workspace_id or "",
            tool_name="mcp__tiktok__post_video",
            allowed_suffixes={".mp4", ".mov", ".webm"},
            max_bytes=_MAX_VIDEO_BYTES,
            expected_source_sha256=expected_source_sha256,
            inspect_snapshot=_probe_video,
        )
    except WorkspaceDocumentMediaError as exc:
        raise TikTokPublishReviewError(str(exc)) from None


async def inspect_tiktok_photo_source(
    reference: Any,
    *,
    entity_id: str,
    user_id: str,
    conversation_id: str,
    workspace_id: str | None = None,
    expected_source_sha256: str | None = None,
) -> dict[str, Any]:
    """Resolve one approved image from chat or active Workspace Knowledge."""
    from packages.core.services.ai_file_permissions import guard_ai_file_read_access
    from packages.core.services.entity_fs import (
        EntityFilesystemError,
        canonical_entity_root,
        entity_filesystem_read_lock,
        is_fs_enabled,
        open_entity_file_snapshot,
    )

    if not isinstance(reference, dict):
        if not entity_id or not user_id or not conversation_id:
            raise TikTokPublishReviewError("TikTok publication requires the current user and conversation.")
        path = chat_photo_path(reference, entity_id)
        if not is_fs_enabled():
            raise TikTokPublishReviewError("The attachment filesystem is unavailable.")
        try:
            async with entity_filesystem_read_lock(canonical_entity_root(entity_id)):
                denied = await guard_ai_file_read_access(
                    entity_id=entity_id, user_id=user_id, conversation_id=conversation_id,
                    workspace_id=workspace_id, tool_name="mcp__tiktok__post_photo", paths=[path],
                )
                if denied:
                    raise TikTokPublishReviewError("The photo is no longer readable in this conversation.")
                with open_entity_file_snapshot(entity_id, path) as snapshot:
                    if not 0 < snapshot.stat.st_size <= _MAX_PHOTO_BYTES:
                        raise TikTokPublishReviewError("Each photo must be nonempty and at most 20 MiB.")
                    details = _inspect_tiktok_photo_snapshot(snapshot.descriptor_path)
                    with open(snapshot.descriptor_path, "rb") as source:
                        digest = snapshot.content_sha256 or hashlib.file_digest(source, "sha256").hexdigest()
                    if expected_source_sha256 is not None and digest != expected_source_sha256:
                        raise TikTokPublishReviewError("An approved photo has changed. Refresh the TikTok review.")
                    return {
                        "source_kind": "chat_attachment", "rel_path": path,
                        "source_sha256": digest, "size_bytes": snapshot.stat.st_size, **details,
                    }
        except (EntityFilesystemError, OSError) as exc:
            raise TikTokPublishReviewError("An approved photo is unavailable or has changed. Refresh the TikTok review.") from exc
    if set(reference) != {"source_kind", "document_id"} or reference.get("source_kind") != "workspace_document":
        raise TikTokPublishReviewError("Use exact chat attachments or Workspace Knowledge document references for photos.")
    try:
        from packages.core.services.workspace_document_media import (
            WorkspaceDocumentMediaError,
            inspect_workspace_document_media,
        )

        return await inspect_workspace_document_media(
            reference, entity_id=entity_id, user_id=user_id, conversation_id=conversation_id,
            workspace_id=workspace_id or "", tool_name="mcp__tiktok__post_photo",
            allowed_suffixes=_PHOTO_SUFFIXES, max_bytes=_MAX_PHOTO_BYTES,
            expected_source_sha256=expected_source_sha256,
            inspect_snapshot=_inspect_tiktok_photo_snapshot,
        )
    except WorkspaceDocumentMediaError as exc:
        raise TikTokPublishReviewError(str(exc)) from None


async def inspect_tiktok_photo_sources(
    references: Any, **scope: Any,
) -> list[dict[str, Any]]:
    if not isinstance(references, list) or not 1 <= len(references) <= _MAX_PHOTO_COUNT:
        raise TikTokPublishReviewError("TikTok photo posts require 1 to 35 ordered JPEG or WebP photos.")
    sources = [await inspect_tiktok_photo_source(reference, **scope) for reference in references]
    if len({source["rel_path"] for source in sources}) != len(sources):
        raise TikTokPublishReviewError("A TikTok photo post cannot contain the same source more than once.")
    return sources


def validate_creator_info(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise TikTokPublishReviewError("TikTok creator information is unavailable.")
    data = payload.get("data")
    if payload.get("error") or not isinstance(data, dict):
        raise TikTokPublishReviewError("TikTok could not provide current creator information. Publication stopped.")
    options = data.get("privacy_level_options")
    if (
        not isinstance(data.get("creator_username"), str)
        or not data["creator_username"]
        or not isinstance(options, list)
        or not options
        or any(not isinstance(item, str) or item not in _PRIVACY_OPTIONS for item in options)
        or any(type(data.get(key)) is not bool for key in ("comment_disabled", "duet_disabled", "stitch_disabled"))
        or type(data.get("max_video_post_duration_sec")) is not int
        or data["max_video_post_duration_sec"] <= 0
    ):
        raise TikTokPublishReviewError(
            "TikTok returned incomplete creator permissions. Refresh the review before publishing."
        )
    return {
        "creator_username": data["creator_username"],
        "creator_nickname": str(data.get("creator_nickname") or data["creator_username"]),
        "creator_avatar_url": str(data.get("creator_avatar_url") or ""),
        "privacy_level_options": list(dict.fromkeys(options)),
        **{
            key: data[key]
            for key in ("comment_disabled", "duet_disabled", "stitch_disabled", "max_video_post_duration_sec")
        },
    }


def validate_post_settings(
    arguments: dict[str, Any], creator: dict[str, Any], duration_seconds: float,
    *, interactions: tuple[str, ...] = ("comment", "duet", "stitch"),
) -> None:
    privacy = arguments.get("privacy_level")
    if not isinstance(privacy, str) or privacy not in creator["privacy_level_options"]:
        raise TikTokPublishReviewError("Choose a privacy level from the current TikTok account options.")
    if not math.isfinite(duration_seconds) or not 0 < duration_seconds <= creator["max_video_post_duration_sec"]:
        raise TikTokPublishReviewError("The video exceeds this creator's current TikTok duration limit.")
    for interaction in interactions:
        disabled = arguments.get(f"disable_{interaction}", True)
        if type(disabled) is not bool:
            raise TikTokPublishReviewError("TikTok interaction choices must be explicit booleans.")
        if not disabled and creator[f"{interaction}_disabled"]:
            raise TikTokPublishReviewError(f"This TikTok account currently disables {interaction}. Refresh the review.")
    for key in ("commercial_content", "brand_organic_toggle", "brand_content_toggle"):
        if type(arguments.get(key, False)) is not bool:
            raise TikTokPublishReviewError("Commercial disclosure choices must be explicit booleans.")
    commercial = arguments.get("commercial_content", False)
    organic = arguments.get("brand_organic_toggle", False)
    branded = arguments.get("brand_content_toggle", False)
    if commercial != (organic or branded):
        raise TikTokPublishReviewError(
            "Commercial disclosure must select Your brand or Branded content, and both must be off when disclosure is off."
        )
    if branded and privacy == "SELF_ONLY":
        raise TikTokPublishReviewError(
            "Branded content cannot use Only me visibility. Choose an allowed public or friends option."
        )


async def _creator_for_actor(
    arguments: dict[str, Any], *, entity_id: str, user_id: str, conversation_id: str, workspace_id: str | None
) -> tuple[str, dict[str, Any]]:
    from packages.core.ai.runtime.dynamic_mcp import runtime_builtin_mcp_tool_handler
    from packages.core.database import async_session
    from packages.core.services.agent_permission_service import can_use_integration

    if arguments.get("integration_account_selection") not in (None, "", "default"):
        raise TikTokPublishReviewError("Review one TikTok account at a time.")
    async with async_session() as db:
        decision = await can_use_integration(
            db,
            user_id=user_id,
            entity_id=entity_id,
            provider="tiktok",
            integration_account_id=arguments.get("integration_account_id"),
            allow_env_fallback=False,
        )
    if not decision.allowed or not decision.account_id:
        raise TikTokPublishReviewError("The selected TikTok account is unavailable for this user.")
    raw = await runtime_builtin_mcp_tool_handler("tiktok", "get_creator_info")(
        entity_id=entity_id,
        user_id=user_id,
        conversation_id=conversation_id,
        workspace_id=workspace_id,
        integration_account_id=decision.account_id,
    )
    try:
        creator = validate_creator_info(json.loads(raw))
    except (TypeError, json.JSONDecodeError) as exc:
        raise TikTokPublishReviewError("TikTok creator information could not be read.") from exc
    return str(decision.account_id), creator


async def prepare_tiktok_publish_review(
    tool_name: str,
    arguments: dict[str, Any],
    entity_id: str,
    user_id: str,
    conversation_id: str,
    workspace_id: str | None = None,
) -> dict[str, Any]:
    if tool_name not in {"post_video", "mcp__tiktok__post_video", "post_photo", "mcp__tiktok__post_photo"}:
        raise TikTokPublishReviewError("TikTok publication requires a matching human media review.")
    if not isinstance(arguments, dict):
        raise TikTokPublishReviewError("TikTok publication arguments must be an object.")
    photo = tool_name in {"post_photo", "mcp__tiktok__post_photo"}
    media = (
        await inspect_tiktok_photo_sources(
            arguments.get("photo_urls"), entity_id=entity_id, user_id=user_id,
            conversation_id=conversation_id, workspace_id=workspace_id,
        )
        if photo else await inspect_tiktok_video_source(
            arguments.get("video_url"), entity_id=entity_id, user_id=user_id,
            conversation_id=conversation_id, workspace_id=workspace_id,
        )
    )
    account_id, creator = await _creator_for_actor(
        arguments,
        entity_id=entity_id,
        user_id=user_id,
        conversation_id=conversation_id,
        workspace_id=workspace_id,
    )
    if not photo and media["duration_seconds"] > creator["max_video_post_duration_sec"]:
        raise TikTokPublishReviewError("The video exceeds this creator's current TikTok duration limit.")
    review_id = str(uuid4())
    review = {
        "kind": "tiktok_publish",
        "version": 2 if photo else 1,
        "review_id": review_id,
        "creator_info": creator,
        "media": (
            {"type": "photo", "items": [
                {"url": entity_fs_open_url(entity_id, item["rel_path"]), "name": Path(item["rel_path"]).name,
                 "type": "image", "size_bytes": item["size_bytes"]}
                for item in media
            ]}
            if photo else {
                "url": entity_fs_open_url(entity_id, media["rel_path"]), "name": Path(media["rel_path"]).name,
                "type": "video", "duration_sec": media["duration_seconds"], "width": media["width"],
                "height": media["height"], "size_bytes": media["size_bytes"],
            }
        ),
        "initial_caption": str(arguments.get("title") or ""),
    }
    evidence = {
        "version": 2 if photo else 1,
        "review_id": review_id,
        "tool_name": "mcp__tiktok__post_photo" if photo else "mcp__tiktok__post_video",
        "entity_id": entity_id,
        "user_id": user_id,
        "conversation_id": conversation_id,
        "workspace_id": workspace_id or None,
        "integration_account_id": account_id,
        "original_args_sha256": _arguments_hash(arguments),
        "creator_username": creator["creator_username"],
        **({"media_type": "photo", "media_sources": [
            {key: item[key] for key in ("source_kind", "rel_path", "source_sha256", "size_bytes")}
            | ({"document_id": item["document_id"]} if item["source_kind"] == "workspace_document" else {})
            for item in media
        ]} if photo else {key: media[key] for key in ("source_kind", "rel_path", "source_sha256", "size_bytes", "duration_seconds")}),
    }
    if not photo and media["source_kind"] == "workspace_document":
        evidence["document_id"] = media["document_id"]
    return {"review": review, "tiktok_publish_evidence": evidence}


def apply_tiktok_publish_review(
    review: dict[str, Any], submitted: dict[str, Any], original_args: dict[str, Any]
) -> dict[str, Any]:
    if (
        not isinstance(review, dict)
        or not isinstance(submitted, dict)
        or not isinstance(original_args, dict)
        or review.get("kind") != "tiktok_publish"
        or type(review.get("version")) is not int
        or review["version"] not in {1, 2}
        or submitted.get("kind") != "tiktok_publish"
        or type(submitted.get("version")) is not int
        or submitted["version"] != review["version"]
        or not review.get("review_id")
        or submitted.get("review_id") != review["review_id"]
    ):
        raise TikTokPublishReviewError("This TikTok review has changed. Refresh it and review the current video.")
    if set(submitted) - _SUBMITTED_FIELDS:
        raise TikTokPublishReviewError("TikTok review contains unsupported fields. Refresh the review.")
    if submitted.get("consent_confirmed") is not True or submitted.get("music_usage_confirmed") is not True:
        raise TikTokPublishReviewError(
            "Confirm the exact TikTok upload and Music Usage Confirmation before publishing."
        )
    caption = submitted.get("caption")
    if not isinstance(caption, str) or len(caption.encode("utf-16-le")) // 2 > 2200:
        raise TikTokPublishReviewError("TikTok captions must be text of at most 2200 UTF-16 characters.")
    for key in (
        "allow_comment",
        "allow_duet",
        "allow_stitch",
        "commercial_content",
        "brand_organic_toggle",
        "brand_content_toggle",
        "branded_content_policy_confirmed",
    ):
        if type(submitted.get(key)) is not bool:
            raise TikTokPublishReviewError("Choose all interaction and commercial disclosure options explicitly.")
    if submitted["brand_content_toggle"] and submitted.get("branded_content_policy_confirmed") is not True:
        raise TikTokPublishReviewError("Branded content requires your confirmation of TikTok's Branded Content Policy.")
    creator = validate_creator_info({"data": review.get("creator_info")})
    media = review.get("media")
    if (
        not isinstance(media, dict)
        or media.get("type") not in {"video", "photo"}
        or (media.get("type") == "video" and type(media.get("duration_sec")) not in (int, float))
        or (media.get("type") == "photo" and (not isinstance(media.get("items"), list) or not 1 <= len(media["items"]) <= _MAX_PHOTO_COUNT))
    ):
        raise TikTokPublishReviewError("The current TikTok media preview is incomplete.")
    final = {
        **original_args,
        "title": caption,
        "privacy_level": submitted.get("privacy_level"),
        **{f"disable_{name}": not submitted[f"allow_{name}"] for name in ("comment", "duet", "stitch")},
        **{key: submitted[key] for key in ("commercial_content", "brand_organic_toggle", "brand_content_toggle")},
    }
    if media["type"] == "photo":
        cover_index = submitted.get("photo_cover_index")
        if type(cover_index) is not int or not 0 <= cover_index < len(media["items"]):
            raise TikTokPublishReviewError("Choose a cover photo from the reviewed ordered photos.")
        final["photo_cover_index"] = cover_index
    if media["type"] == "video":
        validate_post_settings(final, creator, float(media["duration_sec"]))
    else:
        validate_post_settings(final, creator, 1.0, interactions=("comment",))
    return final
