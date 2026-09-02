"""
YouTube MCP server — in-process MCP implementation for the YouTube Data API v3.

Auth: Bearer token = Google OAuth access_token (from the entity integration
config), same auth model as the gmail / google_drive modules. Tools follow
``mcp__youtube__{tool_name}`` naming via the MCP tool pool.

Scopes used:
  - youtube.force-ssl  : authenticated reads plus comments, ratings, playlists,
                         video edits, and resumable video uploads

``youtube.force-ssl`` is deliberately the only YouTube scope. It authorizes all
of the API operations exposed here, including ``videos.insert``; requesting
``youtube.readonly`` or ``youtube.upload`` alongside it would be redundant.
"""
from __future__ import annotations

import json
import logging
import mimetypes
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode, urlsplit

import httpx

from packages.core.services.dashboard_http import (
    create_public_https_transport,
    resolve_public_https_target,
)

logger = logging.getLogger(__name__)

_API = "https://www.googleapis.com/youtube/v3"
_UPLOAD_API = "https://www.googleapis.com/upload/youtube/v3/videos"
_MAX_CHARS = 12_000
_TIMEOUT = 30.0
_UPLOAD_TIMEOUT = httpx.Timeout(600.0, connect=15.0)
_UPLOAD_CHUNK_BYTES = 8 * 1024 * 1024  # YouTube chunks must be a multiple of 256 KiB.
_MAX_VIDEO_BYTES = 256 * 1024 * 1024 * 1024


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
        return _error("YouTube access token is missing. Connect Google/YouTube first.")

    try:
        text = await handler(token, arguments)
        return {"content": [{"type": "text", "text": text}], "isError": False}
    except _YouTubeError as e:
        return _error(str(e))
    except Exception as e:
        logger.exception("YouTube MCP tool %s failed", name)
        return _error(str(e))


def _error(msg: str) -> Dict[str, Any]:
    return {"content": [{"type": "text", "text": msg}], "isError": True}


def _is_blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


class _YouTubeError(RuntimeError):
    pass


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
        raise _YouTubeError("YouTube authentication failed. Reconnect Google/YouTube on the Integration page.")
    if resp.status_code == 403:
        raise _YouTubeError(f"YouTube forbidden (quota, scope, or permissions): {resp.text[:300]}")
    if resp.status_code == 404:
        raise _YouTubeError("Not found.")
    if not resp.is_success:
        raise _YouTubeError(f"YouTube API error ({resp.status_code}): {resp.text[:300]}")

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
        raise _YouTubeError("Provide channel_id, handle, or mine=true.")
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
        raise _YouTubeError("rating must be one of: like, dislike, none.")
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
        raise _YouTubeError("YouTube API returned an invalid response.") from None
    if not items:
        raise _YouTubeError(f"Video {args['video_id']} not found.")
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


async def _stage_video_source(source_url: str) -> tuple[Path, int, str]:
    """Download one public HTTPS video to a temporary file without buffering
    it in process memory.

    The public-host checks and no-redirect policy keep the server-side fetch
    from becoming an internal-network proxy. The temporary file lets the
    subsequent YouTube upload retry by chunk while keeping memory bounded.
    """
    try:
        target = await resolve_public_https_target(source_url)
    except Exception as exc:
        raise RuntimeError(f"video_url must be a public standard-port HTTPS URL: {exc}") from exc

    suffix = Path(urlsplit(target.url).path).suffix[:16]
    fd, raw_path = tempfile.mkstemp(prefix="manor-youtube-", suffix=suffix)
    os.close(fd)
    path = Path(raw_path)
    total = 0
    media_type = ""
    try:
        async with httpx.AsyncClient(
            timeout=_UPLOAD_TIMEOUT,
            follow_redirects=False,
            trust_env=False,
            transport=create_public_https_transport(target),
        ) as client:
            async with client.stream(
                "GET",
                target.url,
                headers={"Accept": "video/*,application/octet-stream"},
            ) as response:
                if response.is_redirect:
                    raise RuntimeError("video_url redirects are not followed")
                response.raise_for_status()
                media_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                if media_type and not (
                    media_type.startswith("video/")
                    or media_type == "application/octet-stream"
                ):
                    raise RuntimeError(f"video_url returned unsupported content type: {media_type}")
                declared = response.headers.get("content-length", "").strip()
                if declared:
                    try:
                        declared_size = int(declared)
                    except ValueError:
                        declared_size = 0
                    if declared_size > _MAX_VIDEO_BYTES:
                        raise RuntimeError("video exceeds YouTube's 256 GB upload limit")

                with path.open("wb") as output:
                    async for chunk in response.aiter_bytes():
                        total += len(chunk)
                        if total > _MAX_VIDEO_BYTES:
                            raise RuntimeError("video exceeds YouTube's 256 GB upload limit")
                        output.write(chunk)
        if total == 0:
            raise RuntimeError("video_url returned an empty file")
        if not media_type:
            media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        return path, total, media_type
    except Exception:
        path.unlink(missing_ok=True)
        raise


def _upload_error(response: httpx.Response) -> str:
    if response.status_code == 401:
        return "YouTube authentication failed. Reconnect Google/YouTube on the Integration page."
    if response.status_code == 403:
        return f"YouTube forbidden (quota, scope, or permissions): {response.text[:300]}"
    return f"YouTube upload error ({response.status_code}): {response.text[:300]}"


async def _upload_video(token: str, args: Dict) -> str:
    """Upload a public HTTPS video with YouTube's resumable upload protocol."""
    privacy = str(args.get("privacy", "private")).lower()
    if privacy not in {"private", "unlisted", "public"}:
        raise _YouTubeError("privacy must be one of: private, unlisted, public.")

    tags = args.get("tags")
    if tags is not None and not isinstance(tags, list):
        tags = [part.strip() for part in str(tags).split(",") if part.strip()]

    snippet: Dict[str, Any] = {
        "title": args["title"],
        "description": args.get("description", ""),
        "categoryId": str(args.get("category_id", "22")),
    }
    if tags:
        snippet["tags"] = tags

    status: Dict[str, Any] = {"privacyStatus": privacy}
    if args.get("made_for_kids") is not None:
        status["selfDeclaredMadeForKids"] = bool(args["made_for_kids"])
    if args.get("publish_at"):
        if privacy != "private":
            raise _YouTubeError(
                "publish_at requires privacy=private per the YouTube API."
            )
        status["publishAt"] = args["publish_at"]

    path: Path | None = None
    try:
        path, size, media_type = await _stage_video_source(args["video_url"])
        params = {
            "uploadType": "resumable",
            "part": "snippet,status",
            "notifySubscribers": str(bool(args.get("notify_subscribers", False))).lower(),
        }
        init_url = f"{_UPLOAD_API}?{urlencode(params)}"
        auth_headers = {"Authorization": f"Bearer {token}"}

        async with httpx.AsyncClient(timeout=_UPLOAD_TIMEOUT) as client:
            init_response = await client.request(
                "POST",
                init_url,
                headers={
                    **auth_headers,
                    "Accept": "application/json",
                    "Content-Type": "application/json; charset=UTF-8",
                    "X-Upload-Content-Length": str(size),
                    "X-Upload-Content-Type": media_type,
                },
                json={"snippet": snippet, "status": status},
            )
            if not init_response.is_success:
                raise _YouTubeError(_upload_error(init_response))
            session_url = init_response.headers.get("location")
            if not session_url:
                raise _YouTubeError(
                    "YouTube upload error: resumable session URL was not returned."
                )

            uploaded = 0
            final_response: httpx.Response | None = None
            with path.open("rb") as source:
                while uploaded < size:
                    chunk = source.read(min(_UPLOAD_CHUNK_BYTES, size - uploaded))
                    if not chunk:
                        raise _YouTubeError(
                            "YouTube upload error: source ended before the declared size."
                        )
                    end = uploaded + len(chunk) - 1
                    response = await client.request(
                        "PUT",
                        session_url,
                        headers={
                            **auth_headers,
                            "Accept": "application/json",
                            "Content-Type": media_type,
                            "Content-Length": str(len(chunk)),
                            "Content-Range": f"bytes {uploaded}-{end}/{size}",
                        },
                        content=chunk,
                    )
                    if response.status_code == 308:
                        uploaded = end + 1
                        continue
                    if not response.is_success:
                        raise _YouTubeError(_upload_error(response))
                    uploaded = end + 1
                    final_response = response

        if final_response is None:
            raise _YouTubeError(
                "YouTube upload error: upload completed without a final response."
            )
        try:
            result = final_response.json()
        except Exception:
            return final_response.text[:_MAX_CHARS]
        return json.dumps(result, ensure_ascii=False, indent=2, default=str)[:_MAX_CHARS]
    finally:
        if path is not None:
            path.unlink(missing_ok=True)


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
    "upload_video": {
        "description": "Upload a new YouTube video from a public HTTPS URL using resumable upload",
        "properties": {
            "video_url": _prop("Public standard-port HTTPS URL of the video file"),
            "title": _prop("Video title"),
            "description": _prop("Video description (optional)"),
            "tags": _prop("Tags (comma-separated or array)"),
            "category_id": _prop("YouTube category id (default: 22)"),
            "privacy": _prop("private, unlisted, or public (default: private)"),
            "made_for_kids": _prop("User-attested Made for Kids setting", "boolean"),
            "publish_at": _prop("Optional RFC 3339 scheduled publish time; requires privacy=private"),
            "notify_subscribers": _prop("Notify subscribers (default: false)", "boolean"),
        },
        "required": ["video_url", "title"],
    },
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
    "upload_video": _upload_video,
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
