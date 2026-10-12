"""
TikTok MCP server — in-process MCP implementation for the TikTok API v2.

Auth: Bearer token = TikTok OAuth access_token (from the entity integration
config). Tools follow ``mcp__tiktok__{tool_name}`` naming via the MCP tool pool.

Surfaces:
  - Display API        : user info, list own videos, query videos by id
  - Content Posting API: publish a video / photo post from a hosted URL
                         (PULL_FROM_URL), check publish status

Scopes used:
  - user.info.basic / user.info.profile / user.info.stats : user reads
  - video.list                                            : list own videos
  - video.publish / video.upload                          : post content

Publishing requires fresh creator permissions and a server-owned human review
of the exact current-conversation video, account, privacy, and disclosures.
The verified attachment is delivered using a short-lived PULL_FROM_URL capability
on the domain verified for the TikTok app. Public posting requires TikTok audit.
"""
from __future__ import annotations

import asyncio
import contextvars
import hashlib
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote, urlencode, urlsplit

import httpx

from packages.core.services.file_refs import parse_file_reference

logger = logging.getLogger(__name__)

_API = "https://open.tiktokapis.com/v2"
_MAX_CHARS = 12_000
_TIMEOUT = 30.0
_MAX_CHAT_VIDEO_BYTES = 512 * 1024 * 1024  # Signed file response snapshot limit.
_call_ctx_var: contextvars.ContextVar[Dict[str, Any]] = contextvars.ContextVar(
    "tiktok_mcp_call_ctx", default={},
)


def set_call_context(ctx: Dict[str, Any]) -> None:
    _call_ctx_var.set(dict(ctx or {}))


def clear_call_context() -> None:
    _call_ctx_var.set({})

# Default Display-API field sets (TikTok requires an explicit fields list).
_USER_FIELDS = (
    "open_id,union_id,avatar_url,display_name,bio_description,"
    "profile_deep_link,follower_count,following_count,likes_count,video_count"
)
_VIDEO_FIELDS = (
    "id,title,video_description,duration,cover_image_url,share_url,"
    "embed_link,like_count,comment_count,share_count,view_count,create_time"
)


# ── MCP Protocol ─────────────────────────────────────────────────────────────

def list_tools() -> List[Dict[str, Any]]:
    """Return MCP tool definitions (tools/list format)."""
    return [_tool_def(name, spec) for name, spec in _TOOLS.items()]


async def call_tool(
    name: str,
    arguments: Dict[str, Any],
    bearer_token: str,
) -> Dict[str, Any]:
    """Execute a tool (tools/call format). Returns MCP content result."""
    handler = _HANDLERS.get(name)
    if not handler:
        return _error(f"Unknown tool: {name}")
    if not isinstance(arguments, dict):
        return _error("arguments must be an object")
    arguments = dict(arguments)

    spec = _TOOLS.get(name, {})
    missing = [p for p in spec.get("required", []) if _is_blank(arguments.get(p))]
    if missing:
        return _error(f"Missing required params: {', '.join(missing)}")
    token = bearer_token.strip() if isinstance(bearer_token, str) else ""
    if not token:
        return _error("TikTok access token is missing. Connect TikTok first.")

    try:
        text = await handler(token, arguments)
        return {"content": [{"type": "text", "text": text}], "isError": False}
    except _TikTokError as e:
        return _error(str(e))
    except Exception as e:
        logger.exception("TikTok MCP tool %s failed", name)
        return _error(str(e))


def _error(msg: str) -> Dict[str, Any]:
    return {"content": [{"type": "text", "text": msg}], "isError": True}


def _is_blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


class _TikTokError(RuntimeError):
    pass


# ── TikTok API client ──────────────────────────────────────────────────────────

async def _api(
    token: str,
    method: str,
    path: str,
    params: Optional[Dict[str, Any]] = None,
    body: Optional[Dict] = None,
) -> str:
    source_url = (body or {}).get("source_info", {}).get("video_url", "")

    def safe_text(text: str) -> str:
        return _redact_signed_video_url(text, source_url)

    qs = ""
    if params:
        clean = {k: v for k, v in params.items() if v is not None and v != ""}
        if clean:
            qs = "?" + urlencode(clean)
    url = f"{_API}/{path.lstrip('/')}{qs}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json; charset=UTF-8",
    }
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.request(method, url, headers=headers, json=body)

    try:
        response_data = resp.json() if resp.text else None
    except Exception:
        response_data = None
    vendor_error = response_data.get("error") if isinstance(response_data, dict) else None
    if isinstance(vendor_error, dict) and vendor_error.get("code") == "unaudited_client_can_only_post_to_private_accounts":
        raise _TikTokError(
            "TikTok audit required (unaudited_client_can_only_post_to_private_accounts): "
            "TikTok rejected this application's Direct Post request because the app has not "
            "completed the required Content Posting API audit. The app owner must complete "
            "TikTok's audit before this public publication can proceed."
        )
    if resp.status_code == 401:
        raise _TikTokError("TikTok authentication failed. Reconnect TikTok on the Integration page.")
    if resp.status_code == 403:
        raise _TikTokError(f"TikTok forbidden (scope or permissions): {safe_text(resp.text)[:300]}")
    if not resp.is_success:
        raise _TikTokError(f"TikTok API error ({resp.status_code}): {safe_text(resp.text)[:300]}")

    if not resp.text:
        return json.dumps({"ok": True})
    try:
        data = resp.json()
    except Exception:
        return safe_text(resp.text)[:_MAX_CHARS]
    error = data.get("error") if isinstance(data, dict) else None
    if isinstance(error, dict):
        code = error.get("code")
        if code not in (None, 0, "0", "ok", "OK"):
            message = error.get("message") or "TikTok returned a business error"
            raise _TikTokError(safe_text(f"TikTok API error ({code}): {message}"))
        # TikTok includes `error` even on success. Manor reserves a truthy
        # top-level `error` for failures, so retain these diagnostics elsewhere.
        data = dict(data)
        data["response_metadata"] = data.pop("error")
    out = safe_text(json.dumps(data, ensure_ascii=False, indent=2, default=str))
    if len(out) > _MAX_CHARS:
        return out[:_MAX_CHARS] + "\n… (truncated)"
    return out


# ── Display API (read) ────────────────────────────────────────────────────────

async def _get_user_info(token: str, args: Dict) -> str:
    fields = args.get("fields") or _USER_FIELDS
    return await _api(token, "GET", "user/info/", {"fields": fields})


async def _list_videos(token: str, args: Dict) -> str:
    # TikTok caps page size at 20 (min 1); clamp so an out-of-range value is
    # corrected locally instead of bouncing off the API.
    max_count = max(1, min(int(args.get("max_count", 10)), 20))
    body: Dict[str, Any] = {"max_count": max_count}
    if args.get("cursor"):
        body["cursor"] = args["cursor"]
    return await _api(
        token, "POST", "video/list/",
        {"fields": args.get("fields") or _VIDEO_FIELDS}, body,
    )


async def _query_videos(token: str, args: Dict) -> str:
    ids = args["video_ids"]
    if not isinstance(ids, list):
        ids = [s.strip() for s in str(ids).split(",") if s.strip()]
    return await _api(
        token, "POST", "video/query/",
        {"fields": args.get("fields") or _VIDEO_FIELDS},
        {"filters": {"video_ids": ids}},
    )


# ── Content Posting API (publish) ──────────────────────────────────────────────

async def _get_creator_info(token: str, _args: Dict) -> str:
    """Pre-flight required before posting: returns the creator's allowed
    privacy levels, interaction toggles, and posting limits."""
    return await _api(token, "POST", "post/publish/creator_info/query/")


def _redact_signed_video_url(text: str, video_url: str) -> str:
    if isinstance(video_url, str) and "/api/v1/fs/public/" in video_url:
        capability = video_url.split("/api/v1/fs/public/", 1)[1].split("/", 1)[0]
        return text.replace(video_url, "[signed video URL]").replace(capability, "[file token]")
    return text


async def _signed_chat_video_url(reference: str, *, expected_source_sha256: str | None = None) -> str:
    """Issue one brief capability for the approved original attachment bytes."""
    from packages.core.config import get_settings
    from packages.core.services.ai_file_permissions import guard_ai_file_read_access, is_chat_upload_path
    from packages.core.services.entity_fs import (
        canonical_entity_root, entity_filesystem_read_lock, is_fs_enabled,
        open_entity_file_snapshot,
    )
    from packages.core.services.file_access_tokens import create_file_access_token

    ctx = _call_ctx_var.get()
    entity_id = ctx.get("entity_id", "")
    if not entity_id or not ctx.get("user_id") or not ctx.get("conversation_id"):
        raise _TikTokError("Chat attachment upload requires the current user and conversation context.")
    rel_path = reference
    file_ref = parse_file_reference(reference)
    if file_ref is not None and file_ref.entity_id:
        if file_ref.entity_id != entity_id:
            raise _TikTokError("Chat attachment URL does not belong to the current entity.")
        rel_path = file_ref.fs_path
    if not is_chat_upload_path(rel_path):
        raise _TikTokError("Use the exact uploads/chat attachment reference from this conversation.")
    if Path(rel_path).suffix.lower() not in {".mp4", ".mov", ".webm"}:
        raise _TikTokError("TikTok requires an MP4, MOV, or WebM video attachment.")
    if not is_fs_enabled():
        raise _TikTokError("Entity filesystem is unavailable for chat attachment upload.")
    public_base = str(get_settings().PUBLIC_BASE_URL or "").rstrip("/")
    parsed = urlsplit(public_base)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise _TikTokError("Chat video publishing requires a public HTTPS base URL verified for the TikTok app.")

    def source_digest(descriptor_path: str) -> str:
        with open(descriptor_path, "rb") as source:
            return hashlib.file_digest(source, "sha256").hexdigest()

    async with entity_filesystem_read_lock(canonical_entity_root(entity_id)):
        denied = await guard_ai_file_read_access(
            entity_id=entity_id, user_id=ctx["user_id"], conversation_id=ctx["conversation_id"],
            workspace_id=ctx.get("workspace_id"), tool_name="mcp__tiktok__post_video", paths=[rel_path],
        )
        if denied:
            raise _TikTokError("The chat attachment is unavailable or is not readable in this conversation.")
        with open_entity_file_snapshot(entity_id, rel_path, expected_content_sha256=expected_source_sha256) as snapshot:
            if not 0 < snapshot.stat.st_size <= _MAX_CHAT_VIDEO_BYTES:
                raise _TikTokError("Chat video must be nonempty and at most 512 MiB for signed download.")
            digest = snapshot.content_sha256 or await asyncio.to_thread(source_digest, snapshot.descriptor_path)
            capability = create_file_access_token(
                entity_id=entity_id, rel_path=rel_path, user_id=ctx["user_id"],
                conversation_id=ctx["conversation_id"], workspace_id=ctx.get("workspace_id"),
                source_sha256=digest, expires_in_seconds=900,
            )
    return f"{public_base}/api/v1/fs/public/{capability}/{quote(Path(rel_path).name)}"


async def _verified_video_consent(token: str, args: Dict):
    from packages.core.ai.runtime.authorization_receipts import (
        TikTokPublishConsent, runtime_current_tool_authorization_receipt,
    )
    from packages.core.ai.runtime.dynamic_mcp import runtime_mcp_runtime_only_argument_keys
    from packages.core.services.tiktok_publish_review import (
        _arguments_hash, inspect_chat_video, validate_creator_info, validate_post_settings,
    )

    ctx = _call_ctx_var.get()
    consent = ctx.get("tiktok_publish_consent")
    receipt = runtime_current_tool_authorization_receipt()
    if not isinstance(consent, TikTokPublishConsent) or receipt is None or receipt.tiktok_publish_consent is not consent:
        raise _TikTokError("Open the TikTok review and confirm the video, account, privacy, disclosures, and Music Usage Confirmation before publishing.")
    evidence = consent.evidence
    runtime_only_keys = runtime_mcp_runtime_only_argument_keys()
    approved_args = {key: value for key, value in consent.fields.items() if key not in runtime_only_keys}
    if (
        evidence.get("version") != 1 or not evidence.get("review_id")
        or evidence.get("tool_name") != "mcp__tiktok__post_video"
        or receipt.tool_name != "mcp__tiktok__post_video"
        or any(not ctx.get(key) or ctx.get(key) != evidence.get(key) or getattr(receipt, key) != evidence.get(key)
               for key in ("entity_id", "user_id", "conversation_id"))
        or (ctx.get("workspace_id") or None) != evidence.get("workspace_id")
        or (receipt.workspace_id or None) != evidence.get("workspace_id")
        or not evidence.get("integration_account_id")
        or ctx.get("integration_account_id") != evidence["integration_account_id"]
        or _arguments_hash(args) != _arguments_hash(approved_args)
    ):
        raise _TikTokError("The TikTok account, video, or publication choices changed. Open a fresh review.")
    if not isinstance(args.get("privacy_level"), str) or not args["privacy_level"]:
        raise _TikTokError("Choose an explicit privacy level in the TikTok review before publishing.")
    # Recheck both the same account's current permissions and the exact bytes.
    creator = validate_creator_info(json.loads(await _get_creator_info(token, {})))
    if creator["creator_username"] != evidence.get("creator_username"):
        raise _TikTokError("The connected TikTok creator changed. Open a fresh review.")
    digest = evidence.get("source_sha256")
    if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise _TikTokError("The TikTok review has no verified video. Open a fresh review.")
    media = await inspect_chat_video(
        args.get("video_url"), entity_id=ctx["entity_id"], user_id=ctx["user_id"],
        conversation_id=ctx["conversation_id"], workspace_id=ctx.get("workspace_id"),
        expected_source_sha256=digest,
    )
    if any(media[key] != evidence.get(key) for key in ("rel_path", "source_sha256", "size_bytes", "duration_seconds")):
        raise _TikTokError("The reviewed video changed. Open a fresh TikTok review.")
    validate_post_settings(args, creator, media["duration_seconds"])
    return consent, digest


async def _post_video(token: str, args: Dict) -> str:
    from packages.core.ai.runtime.authorization_receipts import runtime_claim_tiktok_publish_consent

    consent, digest = await _verified_video_consent(token, args)
    post_info: Dict[str, Any] = {
        "title": args.get("title", ""),
        "privacy_level": args["privacy_level"],
        "disable_duet": args.get("disable_duet", True),
        "disable_comment": args.get("disable_comment", True),
        "disable_stitch": args.get("disable_stitch", True),
        "brand_organic_toggle": args.get("brand_organic_toggle", False),
        "brand_content_toggle": args.get("brand_content_toggle", False),
    }
    if args.get("cover_timestamp_ms") is not None:
        post_info["video_cover_timestamp_ms"] = int(args["cover_timestamp_ms"])
    if not runtime_claim_tiktok_publish_consent(consent):
        raise _TikTokError("This TikTok publication review has already been used. Check the existing publish status before starting a new review.")
    video_url = ""
    try:
        video_url = await _signed_chat_video_url(args["video_url"], expected_source_sha256=digest)
        return await _api(token, "POST", "post/publish/video/init/", None, {
            "post_info": post_info,
            "source_info": {"source": "PULL_FROM_URL", "video_url": video_url},
        })
    except Exception as exc:
        raise _TikTokError(_redact_signed_video_url(str(exc), video_url)) from None


async def _post_photo(token: str, args: Dict) -> str:
    raise _TikTokError("Photo publishing requires a matching human photo preview, current creator permissions, and consent review. This video review flow does not authorize photo publication.")


async def _get_publish_status(token: str, args: Dict) -> str:
    return await _api(token, "POST", "post/publish/status/fetch/", None, {
        "publish_id": args["publish_id"],
    })


# ── Tool definitions ──────────────────────────────────────────────────────────

def _prop(desc: str, type_: str = "string", **extra) -> Dict[str, Any]:
    out: Dict[str, Any] = {"type": type_, "description": desc}
    out.update(extra)
    return out


_TOOLS: Dict[str, Dict[str, Any]] = {
    # ── Display API (read) ──
    "get_user_info": {
        "description": "Get the authenticated TikTok user's profile and stats",
        "properties": {
            "fields": _prop("Comma-separated user fields (optional; sensible default applied)"),
        },
        "required": [],
    },
    "list_videos": {
        "description": "List the authenticated user's own videos",
        "properties": {
            "max_count": _prop("Number of videos (1-20, default: 10)", "integer"),
            "cursor": _prop("Pagination cursor from a previous response"),
            "fields": _prop("Comma-separated video fields (optional)"),
        },
        "required": [],
    },
    "query_videos": {
        "description": "Fetch specific videos by id",
        "properties": {
            "video_ids": _prop("Video ids (comma-separated or array)"),
            "fields": _prop("Comma-separated video fields (optional)"),
        },
        "required": ["video_ids"],
    },
    # ── Content Posting API (publish) ──
    "get_creator_info": {
        "description": "Pre-flight before posting: allowed privacy levels, toggles, limits",
        "properties": {},
        "required": [],
    },
    "post_video": {
        "description": "Request a human review to publish an exact current-conversation chat video attachment. The user must preview the video and confirm current creator options, privacy, disclosures, and music consent. Returns a publish_id after submission; check get_publish_status for completion.",
        "properties": {
            "video_url": _prop("Exact uploads/chat/hash.mp4 reference (also /api/v1/fs/{entity}/uploads/chat/hash.mp4) from this conversation. Upload remote videos to Manor first for human preview. Maximum 512 MiB."),
            "title": _prop("Caption / title"),
            "privacy_level": _prop(
                "PUBLIC_TO_EVERYONE, MUTUAL_FOLLOW_FRIENDS, FOLLOWER_OF_CREATOR, "
                "or SELF_ONLY. The user must explicitly choose an available privacy level in the review; no default."
            ),
            "disable_comment": _prop("Disable comments", "boolean"),
            "disable_duet": _prop("Disable duet", "boolean"),
            "disable_stitch": _prop("Disable stitch", "boolean"),
            "commercial_content": _prop("Whether the user declares commercial content in the review", "boolean"),
            "brand_organic_toggle": _prop("Your own brand disclosure, selected by the user", "boolean"),
            "brand_content_toggle": _prop("Third-party branded content disclosure, selected by the user; not allowed with SELF_ONLY", "boolean"),
            "cover_timestamp_ms": _prop("Cover frame timestamp (ms)", "integer"),
        },
        "required": ["video_url"],
    },
    "post_photo": {
        "description": "Photo publication requires a matching human photo review. Currently unavailable in the video review flow.",
        "properties": {
            "photo_urls": _prop("Public image URLs (comma-separated or array)"),
            "title": _prop("Title"),
            "description": _prop("Description"),
            "privacy_level": _prop("Explicit user-selected privacy level; no default"),
            "cover_index": _prop("Index of the cover image (default: 0)", "integer"),
            "disable_comment": _prop("Disable comments", "boolean"),
        },
        "required": ["photo_urls"],
    },
    "get_publish_status": {
        "description": "Check the status of a post by its publish_id",
        "properties": {"publish_id": _prop("publish_id from a post_* call")},
        "required": ["publish_id"],
    },
}


_HANDLERS = {
    # Display API
    "get_user_info": _get_user_info,
    "list_videos": _list_videos,
    "query_videos": _query_videos,
    # Content Posting API
    "get_creator_info": _get_creator_info,
    "post_video": _post_video,
    "post_photo": _post_photo,
    "get_publish_status": _get_publish_status,
}


def _tool_def(name: str, spec: Dict[str, Any]) -> Dict[str, Any]:
    """Build MCP tool definition."""
    return {
        "name": name,
        "description": spec["description"],
        "inputSchema": {
            "type": "object",
            "properties": spec.get("properties", {}),
            "required": spec.get("required", []),
        },
    }
