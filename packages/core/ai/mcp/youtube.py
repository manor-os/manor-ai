"""
YouTube MCP server — in-process MCP implementation for the YouTube Data API v3.

Auth: Bearer token = Google OAuth access_token (from the entity integration
config), same auth model as the gmail / google_drive modules. Tools follow
``mcp__youtube__{tool_name}`` naming via the MCP tool pool.

Scopes used:
  - youtube.readonly   : search, video/channel/playlist reads, comments, captions
  - youtube.force-ssl  : post/reply/delete comments, rate, playlists, video edits

Note on uploads: publishing a *new* video file is a resumable multipart upload
(megabytes of media), which is out of scope for this lightweight JSON wrapper.
The publish surface here is engagement + metadata (comments, ratings,
playlists, video title/description/tags) — the operations an agent realistically
drives. Use Instagram Reels (facebook module) / TikTok (tiktok module) for
video *file* publishing from a hosted URL.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

import httpx

logger = logging.getLogger(__name__)

_API = "https://www.googleapis.com/youtube/v3"
_MAX_CHARS = 12_000
_TIMEOUT = 30.0


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
        logger.exception("YouTube MCP tool %s failed", name)
        return _error(str(e))


def _error(msg: str) -> Dict[str, Any]:
    return {"content": [{"type": "text", "text": msg}], "isError": True}


# ── YouTube API client ────────────────────────────────────────────────────────

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
        "Accept": "application/json",
    }
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.request(method, url, headers=headers, json=body)

    if resp.status_code == 401:
        return "YouTube authentication failed. Reconnect Google/YouTube on the Integration page."
    if resp.status_code == 403:
        return f"YouTube forbidden (quota, scope, or permissions): {resp.text[:300]}"
    if resp.status_code == 404:
        return "Not found."
    if not resp.is_success:
        return f"YouTube API error ({resp.status_code}): {resp.text[:300]}"

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


def _ok_or(api_result: str, message: str) -> str:
    """Write endpoints with an empty 2xx body surface through ``_api`` as
    ``{"ok": true}``; swap for a readable confirmation, pass errors through."""
    if api_result.strip() in ('{"ok": true}', '{"ok":true}', "{}", ""):
        return json.dumps({"ok": True, "message": message})
    return api_result


# ── Read ────────────────────────────────────────────────────────────────────

async def _search(token: str, args: Dict) -> str:
    params = {
        "part": "snippet",
        "q": args.get("query"),
        "type": args.get("type", "video"),
        "maxResults": int(args.get("max_results", 10)),
        "order": args.get("order"),
        "channelId": args.get("channel_id"),
    }
    return await _api(token, "GET", "search", params)


async def _get_video(token: str, args: Dict) -> str:
    return await _api(token, "GET", "videos", {
        "part": "snippet,statistics,contentDetails,status",
        "id": args["video_id"],
    })


async def _get_channel(token: str, args: Dict) -> str:
    params: Dict[str, Any] = {"part": "snippet,statistics,contentDetails"}
    if args.get("mine"):
        params["mine"] = "true"
    elif args.get("handle"):
        params["forHandle"] = args["handle"]
    elif args.get("channel_id"):
        params["id"] = args["channel_id"]
    else:
        return "Provide channel_id, handle, or mine=true."
    return await _api(token, "GET", "channels", params)


async def _list_comments(token: str, args: Dict) -> str:
    return await _api(token, "GET", "commentThreads", {
        "part": "snippet,replies",
        "videoId": args["video_id"],
        "maxResults": int(args.get("max_results", 20)),
        "order": args.get("order", "relevance"),
    })


async def _list_captions(token: str, args: Dict) -> str:
    return await _api(token, "GET", "captions", {
        "part": "snippet",
        "videoId": args["video_id"],
    })


async def _list_my_videos(token: str, args: Dict) -> str:
    return await _api(token, "GET", "search", {
        "part": "snippet",
        "forMine": "true",
        "type": "video",
        "maxResults": int(args.get("max_results", 10)),
        "q": args.get("query"),
        "order": args.get("order", "date"),
    })


# ── Publish / engagement ──────────────────────────────────────────────────────

async def _post_comment(token: str, args: Dict) -> str:
    return await _api(token, "POST", "commentThreads", {"part": "snippet"}, {
        "snippet": {
            "videoId": args["video_id"],
            "topLevelComment": {"snippet": {"textOriginal": args["text"]}},
        }
    })


async def _reply_comment(token: str, args: Dict) -> str:
    return await _api(token, "POST", "comments", {"part": "snippet"}, {
        "snippet": {"parentId": args["parent_id"], "textOriginal": args["text"]}
    })


async def _delete_comment(token: str, args: Dict) -> str:
    res = await _api(token, "DELETE", "comments", {"id": args["comment_id"]})
    return _ok_or(res, f"Comment {args['comment_id']} deleted.")


async def _rate_video(token: str, args: Dict) -> str:
    rating = args.get("rating", "like")
    if rating not in ("like", "dislike", "none"):
        return "rating must be one of: like, dislike, none."
    res = await _api(token, "POST", "videos/rate", {"id": args["video_id"], "rating": rating})
    return _ok_or(res, f"Video {args['video_id']} rated '{rating}'.")


async def _update_video(token: str, args: Dict) -> str:
    # videos.update with part=snippet REPLACES the snippet, and the API
    # requires both snippet.title and snippet.categoryId. So a title/
    # description-only edit must merge onto the *current* snippet — otherwise
    # we'd 400 (missing title) or silently reset the category to "22". Read
    # the current writable fields first, then apply the requested changes.
    current = await _api(token, "GET", "videos", {
        "part": "snippet", "id": args["video_id"],
    })
    try:
        items = json.loads(current).get("items", [])
    except (json.JSONDecodeError, AttributeError):
        return current  # propagate the _api error string (auth/quota/etc.)
    if not items:
        return f"Video {args['video_id']} not found."
    cur = items[0].get("snippet", {}) or {}

    # Seed from current writable fields only (drop read-only ones like
    # channelId/publishedAt/thumbnails that videos.update rejects).
    snippet: Dict[str, Any] = {
        "title": cur.get("title", ""),
        "categoryId": cur.get("categoryId", "22"),  # 22 = People & Blogs
    }
    if cur.get("description") is not None:
        snippet["description"] = cur["description"]
    if cur.get("tags"):
        snippet["tags"] = cur["tags"]
    if cur.get("defaultLanguage"):
        snippet["defaultLanguage"] = cur["defaultLanguage"]

    # Apply requested overrides.
    if args.get("title") is not None:
        snippet["title"] = args["title"]
    if args.get("description") is not None:
        snippet["description"] = args["description"]
    if args.get("category_id") is not None:
        snippet["categoryId"] = str(args["category_id"])
    if args.get("tags") is not None:
        tags = args["tags"]
        snippet["tags"] = tags if isinstance(tags, list) else [
            t.strip() for t in str(tags).split(",") if t.strip()
        ]
    return await _api(token, "PUT", "videos", {"part": "snippet"}, {
        "id": args["video_id"], "snippet": snippet,
    })


async def _create_playlist(token: str, args: Dict) -> str:
    return await _api(token, "POST", "playlists", {"part": "snippet,status"}, {
        "snippet": {
            "title": args["title"],
            "description": args.get("description", ""),
        },
        "status": {"privacyStatus": args.get("privacy", "private")},
    })


async def _add_to_playlist(token: str, args: Dict) -> str:
    return await _api(token, "POST", "playlistItems", {"part": "snippet"}, {
        "snippet": {
            "playlistId": args["playlist_id"],
            "resourceId": {"kind": "youtube#video", "videoId": args["video_id"]},
        }
    })


# ── Tool definitions ──────────────────────────────────────────────────────────

def _prop(desc: str, type_: str = "string", **extra) -> Dict[str, Any]:
    out: Dict[str, Any] = {"type": type_, "description": desc}
    out.update(extra)
    return out


_TOOLS: Dict[str, Dict[str, Any]] = {
    # ── Read ──
    "search": {
        "description": "Search YouTube for videos, channels, or playlists",
        "properties": {
            "query": _prop("Search query"),
            "type": _prop("video, channel, or playlist (default: video)"),
            "max_results": _prop("Max results (default: 10)", "integer"),
            "order": _prop("date, rating, relevance, title, viewCount"),
            "channel_id": _prop("Restrict to a channel id (optional)"),
        },
        "required": ["query"],
    },
    "get_video": {
        "description": "Get a video's snippet, statistics and details",
        "properties": {"video_id": _prop("YouTube video id")},
        "required": ["video_id"],
    },
    "get_channel": {
        "description": "Get a channel by id, @handle, or the authenticated user (mine)",
        "properties": {
            "channel_id": _prop("Channel id (optional)"),
            "handle": _prop("Channel @handle without the @ (optional)"),
            "mine": _prop("Set true for the authenticated user's channel", "boolean"),
        },
        "required": [],
    },
    "list_comments": {
        "description": "List comment threads on a video",
        "properties": {
            "video_id": _prop("YouTube video id"),
            "max_results": _prop("Max results (default: 20)", "integer"),
            "order": _prop("relevance or time (default: relevance)"),
        },
        "required": ["video_id"],
    },
    "list_captions": {
        "description": "List caption tracks available for a video",
        "properties": {"video_id": _prop("YouTube video id")},
        "required": ["video_id"],
    },
    "list_my_videos": {
        "description": "List the authenticated user's own videos",
        "properties": {
            "query": _prop("Optional filter query"),
            "max_results": _prop("Max results (default: 10)", "integer"),
            "order": _prop("date, rating, title, viewCount (default: date)"),
        },
        "required": [],
    },
    # ── Publish / engagement ──
    "post_comment": {
        "description": "Post a top-level comment on a video",
        "properties": {
            "video_id": _prop("YouTube video id"),
            "text": _prop("Comment text"),
        },
        "required": ["video_id", "text"],
    },
    "reply_comment": {
        "description": "Reply to an existing comment",
        "properties": {
            "parent_id": _prop("Parent comment id"),
            "text": _prop("Reply text"),
        },
        "required": ["parent_id", "text"],
    },
    "delete_comment": {
        "description": "Delete a comment by id (must be yours / on your video)",
        "properties": {"comment_id": _prop("Comment id")},
        "required": ["comment_id"],
    },
    "rate_video": {
        "description": "Like, dislike, or clear your rating on a video",
        "properties": {
            "video_id": _prop("YouTube video id"),
            "rating": _prop("like, dislike, or none (default: like)"),
        },
        "required": ["video_id"],
    },
    "update_video": {
        "description": "Update your video's title, description or tags",
        "properties": {
            "video_id": _prop("YouTube video id (must be yours)"),
            "title": _prop("New title (optional)"),
            "description": _prop("New description (optional)"),
            "tags": _prop("Tags (comma-separated or array)"),
            "category_id": _prop("Category id (default: 22)"),
        },
        "required": ["video_id"],
    },
    "create_playlist": {
        "description": "Create a playlist on the authenticated user's channel",
        "properties": {
            "title": _prop("Playlist title"),
            "description": _prop("Playlist description (optional)"),
            "privacy": _prop("private, public, or unlisted (default: private)"),
        },
        "required": ["title"],
    },
    "add_to_playlist": {
        "description": "Add a video to a playlist",
        "properties": {
            "playlist_id": _prop("Playlist id"),
            "video_id": _prop("YouTube video id"),
        },
        "required": ["playlist_id", "video_id"],
    },
}


_HANDLERS = {
    # Read
    "search": _search,
    "get_video": _get_video,
    "get_channel": _get_channel,
    "list_comments": _list_comments,
    "list_captions": _list_captions,
    "list_my_videos": _list_my_videos,
    # Publish / engagement
    "post_comment": _post_comment,
    "reply_comment": _reply_comment,
    "delete_comment": _delete_comment,
    "rate_video": _rate_video,
    "update_video": _update_video,
    "create_playlist": _create_playlist,
    "add_to_playlist": _add_to_playlist,
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
