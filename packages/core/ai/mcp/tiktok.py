"""
TikTok MCP server — in-process MCP implementation for the TikTok API v2.

Auth: Bearer token = TikTok OAuth access_token (from the entity integration
config). Tools follow ``mcp__tiktok__{tool_name}`` naming via the MCP tool pool.

Surfaces:
  - Display API        : user info, list own videos, query videos by id
  - Content Posting API: publish reviewed video / photo attachments or Workspace
                         Knowledge media via PULL_FROM_URL, check publish status

Scopes used:
  - user.info.basic / user.info.profile / user.info.stats : user reads
  - video.list                                            : list own videos
  - video.publish                                         : direct post + status

Publishing requires fresh creator permissions and a server-owned human review
of the exact current-conversation attachment or active Workspace Knowledge media,
account, privacy, and disclosures. The verified source is delivered using a short-lived PULL_FROM_URL capability
on the domain verified for the TikTok app. Public posting requires TikTok audit.
"""

from __future__ import annotations

import contextvars
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote, urlencode, urlsplit

import httpx

logger = logging.getLogger(__name__)

_API = "https://open.tiktokapis.com/v2"
_MAX_CHARS = 12_000
_TIMEOUT = 30.0
_MAX_CHAT_VIDEO_BYTES = 512 * 1024 * 1024  # Signed file response snapshot limit.
_call_ctx_var: contextvars.ContextVar[Dict[str, Any]] = contextvars.ContextVar(
    "tiktok_mcp_call_ctx",
    default={},
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
    if path.lstrip("/") in {
        "post/publish/creator_info/query/", "post/publish/video/init/", "post/publish/content/init/",
    } and isinstance(vendor_error, dict) and vendor_error.get("code") in {
        "spam_risk_too_many_posts", "reached_active_user_cap", "rate_limit_exceeded",
    }:
        raise _TikTokError(
            f"TikTok publishing is temporarily unavailable ({vendor_error['code']}). "
            "This publishing attempt has stopped. Please try again later."
        )
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


def _redact_signed_video_url(text: str, video_url: str | list[str]) -> str:
    from packages.core.services.file_refs import signed_file_url_capability

    urls = [video_url] if isinstance(video_url, str) else video_url
    for url in urls:
        capability = signed_file_url_capability(url)
        if capability:
            text = text.replace(url, "[signed media URL]").replace(capability, "[file token]")
    return text


async def _signed_chat_video_url(reference: Any, *, expected_source_sha256: str | None = None) -> str:
    """Issue one brief capability for the approved original source bytes."""
    from packages.core.config import get_settings
    from packages.core.services.file_access_tokens import create_file_access_token
    from packages.core.services.tiktok_publish_review import inspect_tiktok_video_source

    ctx = _call_ctx_var.get()
    entity_id = ctx.get("entity_id", "")
    if not entity_id or not ctx.get("user_id") or not ctx.get("conversation_id"):
        raise _TikTokError("Video publishing requires the current user and conversation context.")
    public_base = str(get_settings().PUBLIC_BASE_URL or "").rstrip("/")
    parsed = urlsplit(public_base)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise _TikTokError("Video publishing requires a public HTTPS base URL verified for the TikTok app.")
    try:
        media = await inspect_tiktok_video_source(
            reference,
            entity_id=entity_id,
            user_id=ctx["user_id"],
            conversation_id=ctx["conversation_id"],
            workspace_id=ctx.get("workspace_id"),
            expected_source_sha256=expected_source_sha256,
        )
    except Exception as exc:
        raise _TikTokError(str(exc)) from None
    capability = create_file_access_token(
        entity_id=entity_id,
        rel_path=media["rel_path"],
        user_id=ctx["user_id"],
        conversation_id=ctx["conversation_id"],
        workspace_id=ctx.get("workspace_id"),
        source_sha256=media["source_sha256"],
        source_kind=("workspace_document" if media["source_kind"] == "workspace_document" else None),
        document_id=media.get("document_id"),
        expires_in_seconds=900,
    )
    return f"{public_base}/api/v1/fs/public/{capability}/{quote(Path(media['rel_path']).name)}"


async def _signed_tiktok_photo_urls(references: Any, *, expected_sources: list[dict[str, Any]]) -> list[str]:
    """Issue one short-lived exact-byte capability per reviewed ordered photo."""
    from packages.core.config import get_settings
    from packages.core.services.file_access_tokens import create_file_access_token
    from packages.core.services.tiktok_publish_review import inspect_tiktok_photo_sources

    ctx = _call_ctx_var.get()
    entity_id = ctx.get("entity_id", "")
    if not entity_id or not ctx.get("user_id") or not ctx.get("conversation_id"):
        raise _TikTokError("Photo publishing requires the current user and conversation context.")
    public_base = str(get_settings().PUBLIC_BASE_URL or "").rstrip("/")
    parsed = urlsplit(public_base)
    if (
        parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
        or parsed.query or parsed.fragment
    ):
        raise _TikTokError("Photo publishing requires a public HTTPS base URL verified for the TikTok app.")
    try:
        sources = await inspect_tiktok_photo_sources(
            references, entity_id=entity_id, user_id=ctx["user_id"],
            conversation_id=ctx["conversation_id"], workspace_id=ctx.get("workspace_id"),
        )
    except Exception as exc:
        raise _TikTokError(str(exc)) from None
    keys = ("source_kind", "rel_path", "source_sha256", "size_bytes", "document_id")
    if len(sources) != len(expected_sources) or any(
        any(source.get(key) != expected.get(key) for key in keys)
        for source, expected in zip(sources, expected_sources, strict=True)
    ):
        raise _TikTokError("The reviewed photos changed. Open a fresh TikTok review.")
    return [
        f"{public_base}/api/v1/fs/public/{create_file_access_token(entity_id=entity_id, rel_path=source['rel_path'], user_id=ctx['user_id'], conversation_id=ctx['conversation_id'], workspace_id=ctx.get('workspace_id'), source_sha256=source['source_sha256'], source_kind=('workspace_document' if source['source_kind'] == 'workspace_document' else None), document_id=source.get('document_id'), expires_in_seconds=900)}/{quote(Path(source['rel_path']).name)}"
        for source in sources
    ]


async def _verified_video_consent(token: str, args: Dict):
    from packages.core.ai.runtime.authorization_receipts import (
        TikTokPublishConsent,
        runtime_current_tool_authorization_receipt,
    )
    from packages.core.ai.runtime.dynamic_mcp import runtime_mcp_runtime_only_argument_keys
    from packages.core.services.tiktok_publish_review import (
        _arguments_hash,
        inspect_tiktok_video_source,
        validate_creator_info,
        validate_post_settings,
    )

    ctx = _call_ctx_var.get()
    consent = ctx.get("tiktok_publish_consent")
    receipt = runtime_current_tool_authorization_receipt()
    if (
        not isinstance(consent, TikTokPublishConsent)
        or receipt is None
        or receipt.tiktok_publish_consent is not consent
    ):
        raise _TikTokError(
            "Open the TikTok review and confirm the video, account, privacy, disclosures, and Music Usage Confirmation before publishing."
        )
    evidence = consent.evidence
    runtime_only_keys = runtime_mcp_runtime_only_argument_keys()
    approved_args = {key: value for key, value in consent.fields.items() if key not in runtime_only_keys}
    if (
        evidence.get("version") != 1
        or not evidence.get("review_id")
        or evidence.get("tool_name") != "mcp__tiktok__post_video"
        or receipt.tool_name != "mcp__tiktok__post_video"
        or any(
            not ctx.get(key) or ctx.get(key) != evidence.get(key) or getattr(receipt, key) != evidence.get(key)
            for key in ("entity_id", "user_id", "conversation_id")
        )
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
    media = await inspect_tiktok_video_source(
        args.get("video_url"),
        entity_id=ctx["entity_id"],
        user_id=ctx["user_id"],
        conversation_id=ctx["conversation_id"],
        workspace_id=ctx.get("workspace_id"),
        expected_source_sha256=digest,
    )
    evidence_keys = ("rel_path", "source_sha256", "size_bytes", "duration_seconds")
    if evidence.get("source_kind") is not None:
        evidence_keys += ("source_kind",)
    if evidence.get("source_kind") == "workspace_document":
        evidence_keys += ("document_id",)
    if any(media[key] != evidence.get(key) for key in evidence_keys):
        raise _TikTokError("The reviewed video changed. Open a fresh TikTok review.")
    validate_post_settings(args, creator, media["duration_seconds"])
    return consent, digest


async def _verified_photo_consent(token: str, args: Dict):
    from packages.core.ai.runtime.authorization_receipts import (
        TikTokPublishConsent,
        runtime_current_tool_authorization_receipt,
    )
    from packages.core.ai.runtime.dynamic_mcp import runtime_mcp_runtime_only_argument_keys
    from packages.core.services.tiktok_publish_review import _arguments_hash, validate_creator_info, validate_post_settings

    ctx = _call_ctx_var.get()
    consent = ctx.get("tiktok_publish_consent")
    receipt = runtime_current_tool_authorization_receipt()
    if not isinstance(consent, TikTokPublishConsent) or receipt is None or receipt.tiktok_publish_consent is not consent:
        raise _TikTokError("Open the TikTok review and confirm the photos, account, privacy, disclosures, and Music Usage Confirmation before publishing.")
    evidence = consent.evidence
    runtime_only_keys = runtime_mcp_runtime_only_argument_keys()
    approved_args = {key: value for key, value in consent.fields.items() if key not in runtime_only_keys}
    if (
        evidence.get("version") != 2 or not evidence.get("review_id")
        or evidence.get("tool_name") != "mcp__tiktok__post_photo" or receipt.tool_name != "mcp__tiktok__post_photo"
        or any(not ctx.get(key) or ctx.get(key) != evidence.get(key) or getattr(receipt, key) != evidence.get(key)
               for key in ("entity_id", "user_id", "conversation_id"))
        or (ctx.get("workspace_id") or None) != evidence.get("workspace_id")
        or (receipt.workspace_id or None) != evidence.get("workspace_id")
        or not evidence.get("integration_account_id")
        or ctx.get("integration_account_id") != evidence["integration_account_id"]
        or _arguments_hash(args) != _arguments_hash(approved_args)
    ):
        raise _TikTokError("The TikTok account, photos, or publication choices changed. Open a fresh review.")
    sources = evidence.get("media_sources")
    if not isinstance(sources, list) or not 1 <= len(sources) <= 35 or any(
        not isinstance(source, dict) or not isinstance(source.get("source_sha256"), str)
        for source in sources
    ):
        raise _TikTokError("The TikTok review has no verified ordered photos. Open a fresh review.")
    creator = validate_creator_info(json.loads(await _get_creator_info(token, {})))
    if creator["creator_username"] != evidence.get("creator_username"):
        raise _TikTokError("The connected TikTok creator changed. Open a fresh review.")
    validate_post_settings(args, creator, 1.0, interactions=("comment",))
    return consent, sources


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
    from packages.core.ai.runtime.authorization_receipts import runtime_claim_tiktok_publish_consent

    consent, sources = await _verified_photo_consent(token, args)
    post_info: Dict[str, Any] = {
        # Photo titles allow only 90 UTF-16 units; the reviewed caption is a
        # post description (4,000 units) so it keeps the review contract used
        # by video posts instead of silently truncating it.
        "description": args.get("title", ""), "privacy_level": args["privacy_level"],
        "disable_comment": args.get("disable_comment", True),
        "brand_organic_toggle": args.get("brand_organic_toggle", False),
        "brand_content_toggle": args.get("brand_content_toggle", False),
    }
    cover_index = args.get("photo_cover_index")
    if type(cover_index) is not int or not 0 <= cover_index < len(sources):
        raise _TikTokError("Choose a reviewed cover photo before publishing.")
    if not runtime_claim_tiktok_publish_consent(consent):
        raise _TikTokError("This TikTok publication review has already been used. Check the existing publish status before starting a new review.")
    photo_urls: list[str] = []
    try:
        photo_urls = await _signed_tiktok_photo_urls(args["photo_urls"], expected_sources=sources)
        return await _api(token, "POST", "post/publish/content/init/", None, {
            "post_info": post_info,
            "source_info": {"source": "PULL_FROM_URL", "photo_images": photo_urls, "photo_cover_index": cover_index},
            "post_mode": "DIRECT_POST",
            "media_type": "PHOTO",
        })
    except Exception as exc:
        raise _TikTokError(_redact_signed_video_url(str(exc), photo_urls)) from None


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
        "description": "Request a human review to publish an exact current-conversation chat video attachment or active Workspace Knowledge video. The user must preview the video and confirm current creator options, privacy, disclosures, and music consent. Returns a publish_id after submission; check get_publish_status for completion.",
        "properties": {
            "video_url": {
                "description": "Exact uploads/chat/hash.mp4 reference (also /api/v1/fs/{entity}/uploads/chat/hash.mp4) from this conversation, or {source_kind: workspace_document, document_id: ...} for a readable video in the active Workspace. Remote URLs and arbitrary paths are not supported. Maximum 512 MiB.",
                "oneOf": [
                    {"type": "string", "minLength": 1},
                    {
                        "type": "object",
                        "required": ["source_kind", "document_id"],
                        "additionalProperties": False,
                        "properties": {
                            "source_kind": {"const": "workspace_document"},
                            "document_id": {"type": "string", "minLength": 1},
                        },
                    },
                ],
            },
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
            "brand_content_toggle": _prop(
                "Third-party branded content disclosure, selected by the user; not allowed with SELF_ONLY", "boolean"
            ),
            "cover_timestamp_ms": _prop("Cover frame timestamp (ms)", "integer"),
        },
        "required": ["video_url"],
    },
    "post_photo": {
        "description": "Request a human review to publish 1 to 35 ordered exact JPEG/WebP chat attachments or active Workspace Knowledge images. The user previews every photo, chooses its cover, and confirms current creator options, privacy, disclosures, and music consent. Returns a publish_id after submission; check get_publish_status for completion.",
        "properties": {
            "photo_urls": {
                "description": "Ordered exact uploads/chat/*.jpg|jpeg|webp references, or active Workspace Knowledge document references. Remote URLs and arbitrary paths are not supported.",
                "type": "array", "minItems": 1, "maxItems": 35,
                "items": {"oneOf": [
                    {"type": "string", "minLength": 1},
                    {"type": "object", "required": ["source_kind", "document_id"], "additionalProperties": False,
                     "properties": {"source_kind": {"const": "workspace_document"}, "document_id": {"type": "string", "minLength": 1}}},
                ]},
            },
            "title": _prop("Caption / title"),
            "photo_cover_index": _prop("Reviewed cover photo index; supplied only by the TikTok review", "integer"),
            "privacy_level": _prop("Explicit user-selected privacy level; no default"),
            "disable_comment": _prop("Disable comments", "boolean"),
            "disable_duet": _prop("Disable duet", "boolean"),
            "disable_stitch": _prop("Disable stitch", "boolean"),
            "commercial_content": _prop("Whether the user declares commercial content in the review", "boolean"),
            "brand_organic_toggle": _prop("Your own brand disclosure, selected by the user", "boolean"),
            "brand_content_toggle": _prop("Third-party branded content disclosure, selected by the user; not allowed with SELF_ONLY", "boolean"),
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
