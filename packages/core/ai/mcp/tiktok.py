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

Publishing notes: TikTok requires a ``creator_info`` pre-flight (``get_creator_info``)
to learn the allowed privacy levels before a post. Apps in sandbox / not yet
audited may only post privately, so ``post_video`` defaults privacy_level to
``SELF_ONLY``. Source media must be a publicly reachable URL on a domain that
has been verified for your TikTok app.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

import httpx

logger = logging.getLogger(__name__)

_API = "https://open.tiktokapis.com/v2"
_MAX_CHARS = 12_000
_TIMEOUT = 30.0

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

    spec = _TOOLS.get(name, {})
    missing = [p for p in spec.get("required", []) if arguments.get(p) in (None, "")]
    if missing:
        return _error(f"Missing required params: {', '.join(missing)}")

    try:
        text = await handler(bearer_token, arguments)
        return {"content": [{"type": "text", "text": text}], "isError": False}
    except Exception as e:
        logger.exception("TikTok MCP tool %s failed", name)
        return _error(str(e))


def _error(msg: str) -> Dict[str, Any]:
    return {"content": [{"type": "text", "text": msg}], "isError": True}


# ── TikTok API client ──────────────────────────────────────────────────────────

async def _api(
    token: str,
    method: str,
    path: str,
    params: Optional[Dict[str, Any]] = None,
    body: Optional[Dict] = None,
) -> str:
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

    if resp.status_code == 401:
        return "TikTok authentication failed. Reconnect TikTok on the Integration page."
    if resp.status_code == 403:
        return f"TikTok forbidden (scope or permissions): {resp.text[:300]}"
    if not resp.is_success:
        return f"TikTok API error ({resp.status_code}): {resp.text[:300]}"

    if not resp.text:
        return json.dumps({"ok": True})
    try:
        data = resp.json()
    except Exception:
        return resp.text[:_MAX_CHARS]
    out = json.dumps(data, ensure_ascii=False, indent=2, default=str)
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


async def _post_video(token: str, args: Dict) -> str:
    post_info: Dict[str, Any] = {
        "title": args.get("title", ""),
        "privacy_level": args.get("privacy_level", "SELF_ONLY"),
        "disable_duet": bool(args.get("disable_duet", False)),
        "disable_comment": bool(args.get("disable_comment", False)),
        "disable_stitch": bool(args.get("disable_stitch", False)),
    }
    if args.get("cover_timestamp_ms") is not None:
        post_info["video_cover_timestamp_ms"] = int(args["cover_timestamp_ms"])
    return await _api(token, "POST", "post/publish/video/init/", None, {
        "post_info": post_info,
        "source_info": {"source": "PULL_FROM_URL", "video_url": args["video_url"]},
    })


async def _post_photo(token: str, args: Dict) -> str:
    photos = args["photo_urls"]
    if not isinstance(photos, list):
        photos = [s.strip() for s in str(photos).split(",") if s.strip()]
    post_info: Dict[str, Any] = {
        "title": args.get("title", ""),
        "description": args.get("description", ""),
        "privacy_level": args.get("privacy_level", "SELF_ONLY"),
        "disable_comment": bool(args.get("disable_comment", False)),
    }
    return await _api(token, "POST", "post/publish/content/init/", None, {
        "post_info": post_info,
        "source_info": {
            "source": "PULL_FROM_URL",
            "photo_cover_index": int(args.get("cover_index", 0)),
            "photo_images": photos,
        },
        "post_mode": "DIRECT_POST",
        "media_type": "PHOTO",
    })


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
        "description": "Publish a video from a public URL (PULL_FROM_URL). Call get_creator_info first.",
        "properties": {
            "video_url": _prop("Public URL of the video file (verified domain)"),
            "title": _prop("Caption / title"),
            "privacy_level": _prop(
                "PUBLIC_TO_EVERYONE, MUTUAL_FOLLOW_FRIENDS, FOLLOWER_OF_CREATOR, "
                "or SELF_ONLY (default: SELF_ONLY)"
            ),
            "disable_comment": _prop("Disable comments", "boolean"),
            "disable_duet": _prop("Disable duet", "boolean"),
            "disable_stitch": _prop("Disable stitch", "boolean"),
            "cover_timestamp_ms": _prop("Cover frame timestamp (ms)", "integer"),
        },
        "required": ["video_url"],
    },
    "post_photo": {
        "description": "Publish a photo post from public image URLs (PULL_FROM_URL)",
        "properties": {
            "photo_urls": _prop("Public image URLs (comma-separated or array)"),
            "title": _prop("Title"),
            "description": _prop("Description"),
            "privacy_level": _prop("Privacy level (default: SELF_ONLY)"),
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
