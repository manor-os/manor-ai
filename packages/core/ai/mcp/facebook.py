"""Facebook + Instagram Business MCP server — Meta Graph API wrapper.

Lets agents:
  * list / read / edit / delete Pages, posts, comments, albums
  * publish text / link / image / video / multi-photo posts (with scheduling)
  * read / reply / hide / delete / like comments
  * Messenger: send text + image DMs, typing indicators, mark seen,
    list conversations + thread history (subject to Meta's 24-hour rule)
  * Instagram Business: list IG accounts linked to managed Pages, list
    media, create + publish image / video / Reel, manage comments,
    pull account insights
  * pull Page-level + per-post insights

Auth model
──────────
``bearer_token`` here is the user's Facebook **User Access Token**
(returned by Manor's Nango OAuth flow). Page-level operations need a
**Page Access Token**, fetched on demand via ``GET /me/accounts`` (or
``GET /{page_id}?fields=access_token``) and cached in-process by
``(user_token, page_id)``. Instagram Business calls reuse the Page
token of the linked Page (Meta's design).

Scopes (Meta App Review required for non-Test users)
───────────────────────────────────────────────────
Currently seeded scopes (see ``mcp_seed.py``):

  ``email, public_profile, pages_show_list, pages_read_engagement,
   pages_manage_posts, pages_manage_engagement, pages_messaging``

To unlock the new surfaces, request these *additionally* during App
Review (no code change needed — the same OAuth flow grants them):

  * ``pages_manage_metadata``      — ``update_page``
  * ``read_insights``              — page / post insights when
                                      ``pages_read_engagement`` falls short
  * ``instagram_basic``            — list / read IG accounts + media
  * ``instagram_content_publish``  — ``create_instagram_media`` +
                                      ``publish_instagram_media``
  * ``instagram_manage_comments``  — IG comment reply / delete
  * ``instagram_manage_insights``  — IG account insights

Live Video (Page broadcasts):
  ``create_live_video`` / ``list_live_videos`` / ``end_live_video`` /
  ``get_live_video`` ride the existing ``pages_manage_posts`` grant —
  no extra scope to request for Page targets. Personal-Timeline or
  Group destinations would need ``publish_video``; not exposed here
  because the FB MCP is Page-centric.

Production prerequisites
────────────────────────
All ``page_*`` and ``instagram_*`` scopes require **App Review** by
Meta before they work for non-Test users:
https://developers.facebook.com/docs/app-review.
"""
from __future__ import annotations

import json  # noqa: F401
import logging
import time
from typing import Any, Dict, List, Optional  # noqa: F401

import httpx  # noqa: F401

logger = logging.getLogger(__name__)


from packages.core.external_api_versions import META_GRAPH as _META_PIN
from packages.core.services.meta_graph import (
    MetaGraphClient, MetaGraphError, graph as _graph,
)

# Kept for back-compat with anything that still reads the literal.
_API_VERSION = _META_PIN.value
_PAGE_TOKEN_TTL_SEC = 30 * 60   # cache page tokens for 30 min
_IG_ACCOUNT_TTL_SEC = 30 * 60   # cache page→IG mapping similarly


# Cache: (user_token_prefix, page_id) → (page_token, expires_at)
_page_token_cache: Dict[tuple[str, str], tuple[str, float]] = {}
# Cache: (user_token_prefix, page_id) → (ig_user_id, expires_at)
_ig_account_cache: Dict[tuple[str, str], tuple[str, float]] = {}


# ── Tool definitions ───────────────────────────────────────────────────────

def _prop(desc: str, type_: str = "string", **extra) -> Dict[str, Any]:
    out: Dict[str, Any] = {"type": type_, "description": desc}
    out.update(extra)
    return out


_TOOLS: Dict[str, Dict[str, Any]] = {
    # ── Pages — discovery + metadata ──
    "list_pages": {
        "description": (
            "List the Facebook Pages the connected user manages. Returns "
            "id, name, category, tasks (perms granted). Run this once "
            "before posting to find the right page_id."
        ),
        "properties": {},
        "required": [],
    },
    "get_page": {
        "description": (
            "Fetch a single Page's profile (about, category, fan count, "
            "website, location, picture URL)."
        ),
        "properties": {"page_id": _prop("Page id from list_pages")},
        "required": ["page_id"],
    },
    "update_page": {
        "description": (
            "Update Page metadata (about / website / phone). Requires "
            "pages_manage_metadata."
        ),
        "properties": {
            "page_id": _prop("Page id"),
            "about": _prop("About text (≤155 chars)"),
            "website": _prop("Website URL"),
            "phone": _prop("Phone number"),
        },
        "required": ["page_id"],
    },
    "list_page_albums": {
        "description": "List photo albums on a Page.",
        "properties": {
            "page_id": _prop("Page id"),
            "limit": _prop("1-100, default 25", "integer"),
        },
        "required": ["page_id"],
    },

    # ── Posts ──
    "create_post": {
        "description": (
            "Publish a post to a Facebook Page wall. Supports plain text, "
            "an external link (Meta auto-renders the OG card), or an "
            "image URL. Requires pages_manage_posts."
        ),
        "properties": {
            "page_id": _prop("Page id"),
            "message": _prop("Post body (max 63206 chars)"),
            "link": _prop("Optional URL — Meta fetches OG metadata"),
            "image_url": _prop(
                "Optional public image URL. If set, posts as a photo "
                "with `message` as caption."
            ),
            "scheduled_publish_time": _prop(
                "Optional Unix timestamp; Meta schedules the post "
                "(10 min – 75 days out).",
                "integer",
            ),
        },
        "required": ["page_id", "message"],
    },
    "create_multi_photo_post": {
        "description": (
            "Publish a multi-photo post. Internally uploads each image as "
            "an unpublished photo, then attaches them to a single feed "
            "post with the given message."
        ),
        "properties": {
            "page_id": _prop("Page id"),
            "message": _prop("Post body / caption"),
            "image_urls": _prop(
                "Public image URLs (2-10)", "array",
                items={"type": "string"},
            ),
            "scheduled_publish_time": _prop(
                "Optional Unix timestamp", "integer",
            ),
        },
        "required": ["page_id", "message", "image_urls"],
    },
    "create_video_post": {
        "description": (
            "Publish a video post to a Page using a hosted video URL "
            "(Meta downloads it)."
        ),
        "properties": {
            "page_id": _prop("Page id"),
            "file_url": _prop("Public video URL"),
            "description": _prop("Caption / body"),
            "title": _prop("Video title"),
        },
        "required": ["page_id", "file_url"],
    },
    "list_posts": {
        "description": (
            "List recent posts on a Page. Useful for finding a post_id to "
            "reply to its comments or read its insights."
        ),
        "properties": {
            "page_id": _prop("Page id"),
            "limit": _prop("1-50, default 10", "integer"),
        },
        "required": ["page_id"],
    },
    "get_post": {
        "description": "Fetch full details on a single Page post.",
        "properties": {
            "page_id": _prop("Page id (for token resolution)"),
            "post_id": _prop("Post id (looks like {page_id}_{numeric})"),
        },
        "required": ["page_id", "post_id"],
    },
    "update_post": {
        "description": "Edit a Page post's body text.",
        "properties": {
            "page_id": _prop("Page id"),
            "post_id": _prop("Post id"),
            "message": _prop("New body text"),
        },
        "required": ["page_id", "post_id", "message"],
    },
    "delete_post": {
        "description": "Permanently delete a Page post. Cannot be undone.",
        "properties": {
            "page_id": _prop("Page id"),
            "post_id": _prop("Post id"),
        },
        "required": ["page_id", "post_id"],
    },

    # ── Comments ──
    "list_comments": {
        "description": (
            "Fetch comments on a Page post. Returns comment id, "
            "from {id, name}, message, created_time, like_count."
        ),
        "properties": {
            "post_id": _prop("Post id"),
            "page_id": _prop("Page that owns the post"),
            "limit": _prop("1-100, default 25", "integer"),
            "filter": _prop("'toplevel' (default) | 'stream' (include replies)"),
        },
        "required": ["post_id"],
    },
    "reply_comment": {
        "description": (
            "Reply to a Page comment. Posts as a child comment under the "
            "original. Requires pages_manage_engagement."
        ),
        "properties": {
            "page_id": _prop("Page id"),
            "comment_id": _prop("Comment id"),
            "message": _prop("Reply body"),
        },
        "required": ["page_id", "comment_id", "message"],
    },
    "hide_comment": {
        "description": (
            "Hide a comment from public view (only the commenter and "
            "their friends can see it). Reversible via unhide."
        ),
        "properties": {
            "page_id": _prop("Page id"),
            "comment_id": _prop("Comment id"),
            "hide": _prop("True to hide (default), false to unhide", "boolean"),
        },
        "required": ["page_id", "comment_id"],
    },
    "delete_comment": {
        "description": "Permanently delete a comment. Not reversible.",
        "properties": {
            "page_id": _prop("Page id (for token resolution)"),
            "comment_id": _prop("Comment id"),
        },
        "required": ["page_id", "comment_id"],
    },
    "like_comment": {
        "description": "Like a comment as the Page.",
        "properties": {
            "page_id": _prop("Page id"),
            "comment_id": _prop("Comment id"),
        },
        "required": ["page_id", "comment_id"],
    },

    # ── Messenger ──
    "send_messenger": {
        "description": (
            "Send a Messenger DM to a user who has messaged the Page in "
            "the last 24h. Outside that window Meta requires a "
            "message_tag (CONFIRMED_EVENT_UPDATE / POST_PURCHASE_UPDATE / "
            "ACCOUNT_UPDATE) — pass it via `messaging_tag` for those."
        ),
        "properties": {
            "page_id": _prop("Page id"),
            "recipient_id": _prop("PSID — Page-Scoped User ID"),
            "message": _prop("Text body"),
            "messaging_tag": _prop("Required outside the 24-hour window"),
        },
        "required": ["page_id", "recipient_id", "message"],
    },
    "send_messenger_image": {
        "description": (
            "Send an image attachment via Messenger. Image must be at a "
            "public HTTPS URL — Meta fetches it."
        ),
        "properties": {
            "page_id": _prop("Page id"),
            "recipient_id": _prop("PSID"),
            "image_url": _prop("Public image URL"),
            "messaging_tag": _prop("Required outside the 24-hour window"),
        },
        "required": ["page_id", "recipient_id", "image_url"],
    },
    "send_typing_indicator": {
        "description": (
            "Show / hide the typing indicator in a Messenger thread."
        ),
        "properties": {
            "page_id": _prop("Page id"),
            "recipient_id": _prop("PSID"),
            "on": _prop("True = typing_on (default), false = typing_off", "boolean"),
        },
        "required": ["page_id", "recipient_id"],
    },
    "mark_seen": {
        "description": "Mark the latest message in a Messenger thread as seen.",
        "properties": {
            "page_id": _prop("Page id"),
            "recipient_id": _prop("PSID"),
        },
        "required": ["page_id", "recipient_id"],
    },
    "list_conversations": {
        "description": (
            "List recent Messenger conversations for a Page (id, "
            "participants, updated_time, unread_count)."
        ),
        "properties": {
            "page_id": _prop("Page id"),
            "limit": _prop("1-100, default 25", "integer"),
        },
        "required": ["page_id"],
    },
    "list_conversation_messages": {
        "description": (
            "Fetch message history for a conversation thread. Returns "
            "id, from, to, message, created_time."
        ),
        "properties": {
            "page_id": _prop("Page id (for token resolution)"),
            "conversation_id": _prop("Conversation id from list_conversations"),
            "limit": _prop("1-100, default 25", "integer"),
        },
        "required": ["page_id", "conversation_id"],
    },

    # ── Live Video (RTMP broadcast destinations) ──
    # The four tools below cover the full broadcast lifecycle the
    # Meta App Review "Live Video API" submission demos:
    #   1. ``create_live_video``     — provisions an RTMP destination
    #                                   on a Page and returns the
    #                                   stream_url + stream_secret_key
    #                                   the encoder needs
    #   2. ``list_live_videos``      — discover existing/scheduled
    #                                   broadcasts on a Page
    #   3. ``end_live_video``        — close a LIVE broadcast cleanly
    #                                   (transitions it to VOD)
    #   4. ``get_live_video``        — post-broadcast metadata + live
    #                                   viewer count + VOD permalink
    # All four use ``pages_manage_posts`` on a Page Access Token
    # (already in the seeded scope set). For Group / personal Timeline
    # targets Meta wants ``publish_video`` — not exposed here yet
    # because the FB MCP is Page-centric.
    "create_live_video": {
        "description": (
            "Provision an RTMP broadcast destination on a Facebook Page. "
            "Returns the live video id plus the secure_stream_url and "
            "stream_secret_key the encoder (OBS, ffmpeg, hardware) needs "
            "to push the feed. The broadcast goes live as soon as the "
            "encoder connects. Requires pages_manage_posts."
        ),
        "properties": {
            "page_id": _prop("Page id that owns the broadcast"),
            "title": _prop("Headline shown in the FB UI"),
            "description": _prop("Longer description / body text"),
            "privacy": _prop(
                "EVERYONE (default) | ALL_FRIENDS | FRIENDS_OF_FRIENDS | "
                "SELF — only meaningful for Timeline live videos; Pages "
                "ignore privacy."
            ),
            "status": _prop(
                "LIVE_NOW (default — open the RTMP destination now) | "
                "UNPUBLISHED (create destination but don't go live until "
                "the encoder pushes) | SCHEDULED_UNPUBLISHED (paired with "
                "planned_start_time for scheduled broadcasts)."
            ),
            "planned_start_time": _prop(
                "Unix epoch seconds. Only used when status="
                "SCHEDULED_UNPUBLISHED.",
                "integer",
            ),
        },
        "required": ["page_id", "title"],
    },
    "list_live_videos": {
        "description": (
            "List a Page's live videos (LIVE, VOD, SCHEDULED_LIVE, "
            "SCHEDULED_UNPUBLISHED). Use this to find a broadcast id "
            "for end_live_video / get_live_video."
        ),
        "properties": {
            "page_id": _prop("Page id"),
            "broadcast_status": _prop(
                "Filter: LIVE | LIVE_STOPPED | VOD | "
                "SCHEDULED_LIVE | SCHEDULED_UNPUBLISHED | SCHEDULED_CANCELED. "
                "Omit for all."
            ),
            "limit": _prop("1-100, default 25", "integer"),
        },
        "required": ["page_id"],
    },
    "end_live_video": {
        "description": (
            "End a currently-live broadcast on a Page. Transitions the "
            "live video to VOD so the recording stays on the timeline. "
            "Requires pages_manage_posts."
        ),
        "properties": {
            "page_id": _prop("Page id (for token resolution)"),
            "live_video_id": _prop("Live video id from create_live_video"),
        },
        "required": ["page_id", "live_video_id"],
    },
    "get_live_video": {
        "description": (
            "Fetch broadcast state — status (LIVE/VOD/etc), current "
            "live_views, the RTMP urls if the broadcast hasn't ended, "
            "and the permalink + VOD video id once it has. Use this for "
            "the post-broadcast review the App Review screencast asks "
            "for (reach + engagement)."
        ),
        "properties": {
            "page_id": _prop("Page id (for token resolution)"),
            "live_video_id": _prop("Live video id"),
        },
        "required": ["page_id", "live_video_id"],
    },

    # ── Insights ──
    "get_page_insights": {
        "description": (
            "Pull aggregate Page metrics for the last N days. Returns "
            "impressions, reach, engagements, fan adds/removes (where "
            "available; some metrics deprecated by Meta over time)."
        ),
        "properties": {
            "page_id": _prop("Page id"),
            "days": _prop("1-90, default 7", "integer"),
        },
        "required": ["page_id"],
    },
    "get_post_insights": {
        "description": (
            "Per-post insights — impressions, reach, reactions, clicks, "
            "video views (for videos)."
        ),
        "properties": {
            "page_id": _prop("Page id (for token resolution)"),
            "post_id": _prop("Post id"),
        },
        "required": ["page_id", "post_id"],
    },

    # ── Instagram Business ──
    "list_instagram_accounts": {
        "description": (
            "List Instagram Business accounts linked to the user's "
            "managed Pages. Returns ig_user_id, username, follower count."
        ),
        "properties": {},
        "required": [],
    },
    "get_instagram_account": {
        "description": "Profile of an Instagram Business account.",
        "properties": {
            "page_id": _prop("Page id linked to the IG account (for token)"),
            "ig_user_id": _prop("Instagram user id"),
        },
        "required": ["page_id", "ig_user_id"],
    },
    "list_instagram_media": {
        "description": "List recent media (posts + Reels) on an IG account.",
        "properties": {
            "page_id": _prop("Page id (for token)"),
            "ig_user_id": _prop("Instagram user id"),
            "limit": _prop("1-100, default 25", "integer"),
        },
        "required": ["page_id", "ig_user_id"],
    },
    "get_instagram_media": {
        "description": "Fetch a single IG media object's full fields.",
        "properties": {
            "page_id": _prop("Page id (for token)"),
            "media_id": _prop("IG media id"),
        },
        "required": ["page_id", "media_id"],
    },
    "create_instagram_media": {
        "description": (
            "Step 1 of IG publishing — create a media container from a "
            "public image_url or video_url. Returns ``creation_id`` to "
            "hand to publish_instagram_media. Requires "
            "instagram_content_publish."
        ),
        "properties": {
            "page_id": _prop("Page id (for token)"),
            "ig_user_id": _prop("Instagram user id"),
            "image_url": _prop("Public image URL (mutually exclusive with video_url)"),
            "video_url": _prop("Public video URL (for REELS / VIDEO)"),
            "caption": _prop("Caption (≤2200 chars)"),
            "media_type": _prop("REELS, STORIES, IMAGE (default: image if image_url, REELS if video_url)"),
            "thumb_offset_ms": _prop("Reel thumbnail offset (ms)", "integer"),
            "share_to_feed": _prop("Reels only — also share to feed", "boolean"),
        },
        "required": ["page_id", "ig_user_id"],
    },
    "publish_instagram_media": {
        "description": (
            "Step 2 of IG publishing — publish a previously created "
            "media container. Container must be in FINISHED status "
            "(allow ~30s for video / Reel processing)."
        ),
        "properties": {
            "page_id": _prop("Page id (for token)"),
            "ig_user_id": _prop("Instagram user id"),
            "creation_id": _prop("From create_instagram_media"),
        },
        "required": ["page_id", "ig_user_id", "creation_id"],
    },
    "list_instagram_comments": {
        "description": "List comments on an IG media.",
        "properties": {
            "page_id": _prop("Page id (for token)"),
            "media_id": _prop("IG media id"),
            "limit": _prop("1-100, default 25", "integer"),
        },
        "required": ["page_id", "media_id"],
    },
    "reply_instagram_comment": {
        "description": "Reply to an IG comment. Requires instagram_manage_comments.",
        "properties": {
            "page_id": _prop("Page id (for token)"),
            "comment_id": _prop("IG comment id"),
            "message": _prop("Reply body"),
        },
        "required": ["page_id", "comment_id", "message"],
    },
    "delete_instagram_comment": {
        "description": "Delete an IG comment. Requires instagram_manage_comments.",
        "properties": {
            "page_id": _prop("Page id (for token)"),
            "comment_id": _prop("IG comment id"),
        },
        "required": ["page_id", "comment_id"],
    },
    "get_instagram_insights": {
        "description": (
            "Aggregate IG account metrics over the last N days. Requires "
            "instagram_manage_insights."
        ),
        "properties": {
            "page_id": _prop("Page id (for token)"),
            "ig_user_id": _prop("Instagram user id"),
            "days": _prop("1-90, default 7", "integer"),
        },
        "required": ["page_id", "ig_user_id"],
    },
}


# ── Tool implementations ───────────────────────────────────────────────────

# ── Pages — discovery + metadata ──

async def _list_pages(user_token: str, _args: Dict[str, Any]) -> Dict[str, Any]:
    data = await _graph_get(
        "/me/accounts",
        params={"fields": "id,name,category,tasks,access_token"},
        token=user_token,
    )
    pages = []
    for p in data.get("data", []):
        pages.append({
            "id": p.get("id"),
            "name": p.get("name"),
            "category": p.get("category"),
            "tasks": p.get("tasks", []),
            "_has_token": bool(p.get("access_token")),
        })
        if p.get("id") and p.get("access_token"):
            _cache_page_token(user_token, p["id"], p["access_token"])
    return {"count": len(pages), "pages": pages}


async def _get_page(user_token: str, args: Dict[str, Any]) -> Dict[str, Any]:
    page_id = args["page_id"]
    page_token = await _resolve_page_token(user_token, page_id)
    return await _graph_get(
        f"/{page_id}",
        params={
            "fields": (
                "id,name,about,category,fan_count,followers_count,"
                "website,phone,emails,location,picture{url},link"
            ),
        },
        token=page_token,
    )


async def _update_page(user_token: str, args: Dict[str, Any]) -> Dict[str, Any]:
    page_id = args["page_id"]
    page_token = await _resolve_page_token(user_token, page_id)
    body: Dict[str, Any] = {}
    for k in ("about", "website", "phone"):
        if args.get(k) is not None:
            body[k] = args[k]
    if not body:
        return {"error": "no fields to update — pass about / website / phone"}
    return await _graph_post(f"/{page_id}", body, token=page_token)


async def _list_page_albums(user_token: str, args: Dict[str, Any]) -> Dict[str, Any]:
    page_id = args["page_id"]
    page_token = await _resolve_page_token(user_token, page_id)
    limit = max(1, min(100, int(args.get("limit") or 25)))
    return await _graph_get(
        f"/{page_id}/albums",
        params={
            "fields": "id,name,description,count,cover_photo{id},created_time",
            "limit": limit,
        },
        token=page_token,
    )


# ── Posts ──

async def _create_post(user_token: str, args: Dict[str, Any]) -> Dict[str, Any]:
    page_id = args["page_id"]
    page_token = await _resolve_page_token(user_token, page_id)
    message = args["message"]
    image_url = args.get("image_url")
    body: Dict[str, Any] = {"message": message}
    if args.get("link"):
        body["link"] = args["link"]
    if args.get("scheduled_publish_time"):
        body["scheduled_publish_time"] = int(args["scheduled_publish_time"])
        body["published"] = "false"

    if image_url:
        # /photos endpoint with an external URL: Meta fetches the image.
        photo_body = {"url": image_url, "caption": message, "message": message}
        if args.get("scheduled_publish_time"):
            photo_body["scheduled_publish_time"] = body["scheduled_publish_time"]
            photo_body["published"] = "false"
        return await _graph_post(f"/{page_id}/photos", photo_body, token=page_token)
    return await _graph_post(f"/{page_id}/feed", body, token=page_token)


async def _create_multi_photo_post(user_token: str, args: Dict[str, Any]) -> Dict[str, Any]:
    page_id = args["page_id"]
    page_token = await _resolve_page_token(user_token, page_id)
    image_urls = args["image_urls"] or []
    if not (2 <= len(image_urls) <= 10):
        return {"error": "image_urls must contain 2-10 URLs"}

    # 1. Upload each photo as unpublished, collect the photo ids.
    media_ids: List[str] = []
    for url in image_urls:
        photo = await _graph_post(
            f"/{page_id}/photos",
            {"url": url, "published": "false"},
            token=page_token,
        )
        if not photo.get("id"):
            return {"error": "photo upload failed", "detail": photo}
        media_ids.append(photo["id"])

    # 2. Attach all photos to a single feed post.
    body: Dict[str, Any] = {
        "message": args["message"],
        # Meta accepts the JSON-encoded list as a string field.
        "attached_media": json.dumps(
            [{"media_fbid": mid} for mid in media_ids]
        ),
    }
    if args.get("scheduled_publish_time"):
        body["scheduled_publish_time"] = int(args["scheduled_publish_time"])
        body["published"] = "false"
    return await _graph_post(f"/{page_id}/feed", body, token=page_token)


async def _create_video_post(user_token: str, args: Dict[str, Any]) -> Dict[str, Any]:
    page_id = args["page_id"]
    page_token = await _resolve_page_token(user_token, page_id)
    body: Dict[str, Any] = {"file_url": args["file_url"]}
    if args.get("description"):
        body["description"] = args["description"]
    if args.get("title"):
        body["title"] = args["title"]
    return await _graph_post(f"/{page_id}/videos", body, token=page_token)


async def _list_posts(user_token: str, args: Dict[str, Any]) -> Dict[str, Any]:
    page_id = args["page_id"]
    page_token = await _resolve_page_token(user_token, page_id)
    limit = max(1, min(50, int(args.get("limit") or 10)))
    return await _graph_get(
        f"/{page_id}/posts",
        params={
            "fields": (
                "id,message,created_time,permalink_url,"
                "reactions.summary(true),comments.summary(true)"
            ),
            "limit": limit,
        },
        token=page_token,
    )


async def _get_post(user_token: str, args: Dict[str, Any]) -> Dict[str, Any]:
    page_token = await _resolve_page_token(user_token, args["page_id"])
    return await _graph_get(
        f"/{args['post_id']}",
        params={
            "fields": (
                "id,message,story,created_time,updated_time,permalink_url,"
                "is_published,is_hidden,attachments,reactions.summary(true),"
                "comments.summary(true),shares"
            ),
        },
        token=page_token,
    )


async def _update_post(user_token: str, args: Dict[str, Any]) -> Dict[str, Any]:
    page_token = await _resolve_page_token(user_token, args["page_id"])
    return await _graph_post(
        f"/{args['post_id']}",
        {"message": args["message"]},
        token=page_token,
    )


async def _delete_post(user_token: str, args: Dict[str, Any]) -> Dict[str, Any]:
    page_token = await _resolve_page_token(user_token, args["page_id"])
    return await _graph_delete(f"/{args['post_id']}", token=page_token)


# ── Comments ──

async def _list_comments(user_token: str, args: Dict[str, Any]) -> Dict[str, Any]:
    post_id = args["post_id"]
    page_id = args.get("page_id") or post_id.split("_")[0]
    page_token = await _resolve_page_token(user_token, page_id)
    limit = max(1, min(100, int(args.get("limit") or 25)))
    filt = args.get("filter") or "toplevel"
    return await _graph_get(
        f"/{post_id}/comments",
        params={
            "fields": "id,from,message,created_time,like_count,comment_count,parent",
            "limit": limit,
            "filter": filt,
        },
        token=page_token,
    )


async def _reply_comment(user_token: str, args: Dict[str, Any]) -> Dict[str, Any]:
    page_token = await _resolve_page_token(user_token, args["page_id"])
    return await _graph_post(
        f"/{args['comment_id']}/comments",
        {"message": args["message"]},
        token=page_token,
    )


async def _hide_comment(user_token: str, args: Dict[str, Any]) -> Dict[str, Any]:
    page_token = await _resolve_page_token(user_token, args["page_id"])
    return await _graph_post(
        f"/{args['comment_id']}",
        {"is_hidden": "true" if args.get("hide", True) else "false"},
        token=page_token,
    )


async def _delete_comment(user_token: str, args: Dict[str, Any]) -> Dict[str, Any]:
    page_token = await _resolve_page_token(user_token, args["page_id"])
    return await _graph_delete(f"/{args['comment_id']}", token=page_token)


async def _like_comment(user_token: str, args: Dict[str, Any]) -> Dict[str, Any]:
    page_token = await _resolve_page_token(user_token, args["page_id"])
    return await _graph_post(
        f"/{args['comment_id']}/likes", {}, token=page_token,
    )


# ── Messenger ──

async def _send_messenger(user_token: str, args: Dict[str, Any]) -> Dict[str, Any]:
    page_id = args["page_id"]
    page_token = await _resolve_page_token(user_token, page_id)
    body: Dict[str, Any] = {
        "recipient": {"id": args["recipient_id"]},
        "message": {"text": args["message"]},
        "messaging_type": "RESPONSE",
    }
    if args.get("messaging_tag"):
        body["messaging_type"] = "MESSAGE_TAG"
        body["tag"] = args["messaging_tag"]
    # Messenger Send API requires a real JSON body (nested recipient/message
    # objects); form-encoding stringifies the dicts and Meta rejects them.
    return await _graph_post(f"/{page_id}/messages", body, token=page_token, json_body=True)


async def _send_messenger_image(user_token: str, args: Dict[str, Any]) -> Dict[str, Any]:
    page_id = args["page_id"]
    page_token = await _resolve_page_token(user_token, page_id)
    body: Dict[str, Any] = {
        "recipient": {"id": args["recipient_id"]},
        "message": {
            "attachment": {
                "type": "image",
                "payload": {"url": args["image_url"], "is_reusable": False},
            },
        },
        "messaging_type": "RESPONSE",
    }
    if args.get("messaging_tag"):
        body["messaging_type"] = "MESSAGE_TAG"
        body["tag"] = args["messaging_tag"]
    # Messenger Send API requires a real JSON body (nested recipient/message
    # objects); form-encoding stringifies the dicts and Meta rejects them.
    return await _graph_post(f"/{page_id}/messages", body, token=page_token, json_body=True)


async def _send_typing_indicator(user_token: str, args: Dict[str, Any]) -> Dict[str, Any]:
    page_id = args["page_id"]
    page_token = await _resolve_page_token(user_token, page_id)
    on = args.get("on", True)
    return await _graph_post(
        f"/{page_id}/messages",
        {
            "recipient": {"id": args["recipient_id"]},
            "sender_action": "typing_on" if on else "typing_off",
        },
        token=page_token,
        json_body=True,
    )


async def _mark_seen(user_token: str, args: Dict[str, Any]) -> Dict[str, Any]:
    page_id = args["page_id"]
    page_token = await _resolve_page_token(user_token, page_id)
    return await _graph_post(
        f"/{page_id}/messages",
        {
            "recipient": {"id": args["recipient_id"]},
            "sender_action": "mark_seen",
        },
        token=page_token,
        json_body=True,
    )


async def _list_conversations(user_token: str, args: Dict[str, Any]) -> Dict[str, Any]:
    page_id = args["page_id"]
    page_token = await _resolve_page_token(user_token, page_id)
    limit = max(1, min(100, int(args.get("limit") or 25)))
    return await _graph_get(
        f"/{page_id}/conversations",
        params={
            "fields": (
                "id,participants,updated_time,unread_count,"
                "messages.limit(1){id,from,to,message,created_time}"
            ),
            "limit": limit,
        },
        token=page_token,
    )


async def _list_conversation_messages(user_token: str, args: Dict[str, Any]) -> Dict[str, Any]:
    page_token = await _resolve_page_token(user_token, args["page_id"])
    limit = max(1, min(100, int(args.get("limit") or 25)))
    return await _graph_get(
        f"/{args['conversation_id']}/messages",
        params={
            "fields": "id,from,to,message,created_time",
            "limit": limit,
        },
        token=page_token,
    )


# ── Live Video ──

# Fields we read back on every live-video response. ``status`` and
# ``live_views`` are the actionable bits during a broadcast; the rest
# matter for the post-broadcast review. ``stream_url`` /
# ``secure_stream_url`` / ``stream_secret_key`` are normally only
# populated until the broadcast actually goes LIVE — Meta nulls them
# out after end_live_video to discourage re-publishing.
_LIVE_VIDEO_FIELDS = (
    "id,title,description,status,broadcast_start_time,live_views,"
    "embed_html,permalink_url,secure_stream_url,stream_url,"
    "stream_secret_key,video{id,length,permalink_url}"
)


async def _create_live_video(user_token: str, args: Dict[str, Any]) -> Dict[str, Any]:
    """Provision an RTMP destination on the given Page.

    Returns the Graph response (id + stream_url + secure_stream_url +
    stream_secret_key + status) so the caller can hand the RTMP target
    to whatever encoder the user runs (OBS, ffmpeg, hardware encoder,
    cloud broadcasting service).
    """
    page_id = args["page_id"]
    page_token = await _resolve_page_token(user_token, page_id)
    body: Dict[str, Any] = {
        "title": args["title"],
        "status": args.get("status") or "LIVE_NOW",
    }
    if args.get("description"):
        body["description"] = args["description"]
    if args.get("privacy"):
        body["privacy"] = {"value": args["privacy"]}
    if args.get("planned_start_time"):
        body["planned_start_time"] = int(args["planned_start_time"])
    return await _graph_post(f"/{page_id}/live_videos", body, token=page_token)


async def _list_live_videos(user_token: str, args: Dict[str, Any]) -> Dict[str, Any]:
    page_id = args["page_id"]
    page_token = await _resolve_page_token(user_token, page_id)
    limit = max(1, min(100, int(args.get("limit") or 25)))
    params: Dict[str, Any] = {
        "fields": _LIVE_VIDEO_FIELDS,
        "limit": limit,
    }
    if args.get("broadcast_status"):
        params["broadcast_status"] = args["broadcast_status"]
    return await _graph_get(f"/{page_id}/live_videos", params=params, token=page_token)


async def _end_live_video(user_token: str, args: Dict[str, Any]) -> Dict[str, Any]:
    """Transition a LIVE broadcast to VOD. After this call the RTMP
    keys stop accepting frames and the recording shows up on the
    Page's timeline as a normal video post."""
    page_token = await _resolve_page_token(user_token, args["page_id"])
    return await _graph_post(
        f"/{args['live_video_id']}",
        {"end_live_video": "true"},
        token=page_token,
    )


async def _get_live_video(user_token: str, args: Dict[str, Any]) -> Dict[str, Any]:
    page_token = await _resolve_page_token(user_token, args["page_id"])
    return await _graph_get(
        f"/{args['live_video_id']}",
        params={"fields": _LIVE_VIDEO_FIELDS},
        token=page_token,
    )


# ── Insights ──

async def _get_page_insights(user_token: str, args: Dict[str, Any]) -> Dict[str, Any]:
    page_id = args["page_id"]
    page_token = await _resolve_page_token(user_token, page_id)
    days = max(1, min(90, int(args.get("days") or 7)))
    metrics = (
        "page_impressions,page_impressions_unique,page_post_engagements,"
        "page_fan_adds_unique,page_fan_removes_unique"
    )
    return await _graph_get(
        f"/{page_id}/insights",
        params={
            "metric": metrics,
            "period": "day",
            "since": int(time.time()) - days * 86400,
            "until": int(time.time()),
        },
        token=page_token,
    )


async def _get_post_insights(user_token: str, args: Dict[str, Any]) -> Dict[str, Any]:
    page_token = await _resolve_page_token(user_token, args["page_id"])
    metrics = (
        "post_impressions,post_impressions_unique,post_engaged_users,"
        "post_clicks,post_reactions_by_type_total,post_video_views"
    )
    return await _graph_get(
        f"/{args['post_id']}/insights",
        params={"metric": metrics},
        token=page_token,
    )


# ── Instagram Business ──

async def _list_instagram_accounts(user_token: str, _args: Dict[str, Any]) -> Dict[str, Any]:
    """Walk managed Pages and surface IG accounts linked to each."""
    pages = await _graph_get(
        "/me/accounts",
        params={
            "fields": (
                "id,name,access_token,"
                "instagram_business_account{"
                "id,username,name,profile_picture_url,followers_count,"
                "follows_count,media_count}"
            ),
        },
        token=user_token,
    )
    out = []
    for p in pages.get("data", []):
        ig = p.get("instagram_business_account")
        if not ig:
            continue
        if p.get("id") and p.get("access_token"):
            _cache_page_token(user_token, p["id"], p["access_token"])
            _cache_ig_account(user_token, p["id"], ig.get("id", ""))
        out.append({
            "page_id": p.get("id"),
            "page_name": p.get("name"),
            "ig_user_id": ig.get("id"),
            "username": ig.get("username"),
            "name": ig.get("name"),
            "profile_picture_url": ig.get("profile_picture_url"),
            "followers_count": ig.get("followers_count"),
            "follows_count": ig.get("follows_count"),
            "media_count": ig.get("media_count"),
        })
    return {"count": len(out), "accounts": out}


async def _get_instagram_account(user_token: str, args: Dict[str, Any]) -> Dict[str, Any]:
    page_token = await _resolve_page_token(user_token, args["page_id"])
    return await _graph_get(
        f"/{args['ig_user_id']}",
        params={
            "fields": (
                "id,username,name,biography,profile_picture_url,"
                "followers_count,follows_count,media_count,website"
            ),
        },
        token=page_token,
    )


async def _list_instagram_media(user_token: str, args: Dict[str, Any]) -> Dict[str, Any]:
    page_token = await _resolve_page_token(user_token, args["page_id"])
    limit = max(1, min(100, int(args.get("limit") or 25)))
    return await _graph_get(
        f"/{args['ig_user_id']}/media",
        params={
            "fields": (
                "id,caption,media_type,media_url,thumbnail_url,permalink,"
                "timestamp,like_count,comments_count"
            ),
            "limit": limit,
        },
        token=page_token,
    )


async def _get_instagram_media(user_token: str, args: Dict[str, Any]) -> Dict[str, Any]:
    page_token = await _resolve_page_token(user_token, args["page_id"])
    return await _graph_get(
        f"/{args['media_id']}",
        params={
            "fields": (
                "id,caption,media_type,media_url,thumbnail_url,permalink,"
                "timestamp,like_count,comments_count,owner,username"
            ),
        },
        token=page_token,
    )


async def _create_instagram_media(user_token: str, args: Dict[str, Any]) -> Dict[str, Any]:
    page_token = await _resolve_page_token(user_token, args["page_id"])
    image_url = args.get("image_url")
    video_url = args.get("video_url")
    if not (image_url or video_url):
        return {"error": "provide image_url or video_url"}
    if image_url and video_url:
        return {"error": "image_url and video_url are mutually exclusive"}

    body: Dict[str, Any] = {}
    media_type = args.get("media_type")
    if image_url:
        body["image_url"] = image_url
        if media_type:
            body["media_type"] = media_type   # e.g. STORIES
    else:
        body["video_url"] = video_url
        body["media_type"] = media_type or "REELS"
        if args.get("thumb_offset_ms") is not None:
            body["thumb_offset"] = int(args["thumb_offset_ms"])
        if args.get("share_to_feed") is not None:
            body["share_to_feed"] = bool(args["share_to_feed"])

    if args.get("caption"):
        body["caption"] = args["caption"]

    return await _graph_post(
        f"/{args['ig_user_id']}/media", body, token=page_token,
    )


async def _publish_instagram_media(user_token: str, args: Dict[str, Any]) -> Dict[str, Any]:
    page_token = await _resolve_page_token(user_token, args["page_id"])
    return await _graph_post(
        f"/{args['ig_user_id']}/media_publish",
        {"creation_id": args["creation_id"]},
        token=page_token,
    )


async def _list_instagram_comments(user_token: str, args: Dict[str, Any]) -> Dict[str, Any]:
    page_token = await _resolve_page_token(user_token, args["page_id"])
    limit = max(1, min(100, int(args.get("limit") or 25)))
    return await _graph_get(
        f"/{args['media_id']}/comments",
        params={
            "fields": "id,text,timestamp,username,like_count,replies",
            "limit": limit,
        },
        token=page_token,
    )


async def _reply_instagram_comment(user_token: str, args: Dict[str, Any]) -> Dict[str, Any]:
    page_token = await _resolve_page_token(user_token, args["page_id"])
    return await _graph_post(
        f"/{args['comment_id']}/replies",
        {"message": args["message"]},
        token=page_token,
    )


async def _delete_instagram_comment(user_token: str, args: Dict[str, Any]) -> Dict[str, Any]:
    page_token = await _resolve_page_token(user_token, args["page_id"])
    return await _graph_delete(
        f"/{args['comment_id']}", token=page_token,
    )


async def _get_instagram_insights(user_token: str, args: Dict[str, Any]) -> Dict[str, Any]:
    page_token = await _resolve_page_token(user_token, args["page_id"])
    days = max(1, min(90, int(args.get("days") or 7)))
    # IG account-level metrics: impressions / reach were renamed in v22+.
    # Use the current set; older fields will be ignored by Meta.
    metrics = "reach,profile_views,website_clicks,follower_count"
    return await _graph_get(
        f"/{args['ig_user_id']}/insights",
        params={
            "metric": metrics,
            "period": "day",
            "since": int(time.time()) - days * 86400,
            "until": int(time.time()),
        },
        token=page_token,
    )


# ── Handler dispatch table ──────────────────────────────────────────────────

_HANDLERS = {
    # Pages
    "list_pages": _list_pages,
    "get_page": _get_page,
    "update_page": _update_page,
    "list_page_albums": _list_page_albums,
    # Posts
    "create_post": _create_post,
    "create_multi_photo_post": _create_multi_photo_post,
    "create_video_post": _create_video_post,
    "list_posts": _list_posts,
    "get_post": _get_post,
    "update_post": _update_post,
    "delete_post": _delete_post,
    # Comments
    "list_comments": _list_comments,
    "reply_comment": _reply_comment,
    "hide_comment": _hide_comment,
    "delete_comment": _delete_comment,
    "like_comment": _like_comment,
    # Messenger
    "send_messenger": _send_messenger,
    "send_messenger_image": _send_messenger_image,
    "send_typing_indicator": _send_typing_indicator,
    "mark_seen": _mark_seen,
    "list_conversations": _list_conversations,
    "list_conversation_messages": _list_conversation_messages,
    # Live Video
    "create_live_video": _create_live_video,
    "list_live_videos": _list_live_videos,
    "end_live_video": _end_live_video,
    "get_live_video": _get_live_video,
    # Insights
    "get_page_insights": _get_page_insights,
    "get_post_insights": _get_post_insights,
    # Instagram
    "list_instagram_accounts": _list_instagram_accounts,
    "get_instagram_account": _get_instagram_account,
    "list_instagram_media": _list_instagram_media,
    "get_instagram_media": _get_instagram_media,
    "create_instagram_media": _create_instagram_media,
    "publish_instagram_media": _publish_instagram_media,
    "list_instagram_comments": _list_instagram_comments,
    "reply_instagram_comment": _reply_instagram_comment,
    "delete_instagram_comment": _delete_instagram_comment,
    "get_instagram_insights": _get_instagram_insights,
}


# ── MCP protocol entry points ──────────────────────────────────────────────

def list_tools() -> List[Dict[str, Any]]:
    return [
        {
            "name": name,
            "description": spec["description"],
            "parameters": {
                "type": "object",
                "properties": spec.get("properties", {}),
                "required": spec.get("required", []),
            },
        }
        for name, spec in _TOOLS.items()
    ]


async def call_tool(
    name: str,
    arguments: Dict[str, Any],
    bearer_token: str,
) -> Dict[str, Any]:
    if not bearer_token:
        return _err(
            "Facebook user access token missing. Connect Facebook in "
            "Integrations first.",
        )
    handler = _HANDLERS.get(name)
    if handler is None:
        return _err(f"Unknown facebook tool: {name!r}")

    spec = _TOOLS.get(name, {})
    args = arguments or {}
    missing = [p for p in spec.get("required", []) if args.get(p) in (None, "")]
    if missing:
        return _err(f"Missing required params: {', '.join(missing)}")

    try:
        return _ok(await handler(bearer_token, args))
    except _GraphError as exc:
        return _err(str(exc))
    except Exception as exc:  # noqa: BLE001
        logger.exception("facebook tool %s failed", name)
        return _err(f"{type(exc).__name__}: {exc}")


# ── Page token + IG account resolution ─────────────────────────────────────

def _cache_page_token(user_token: str, page_id: str, page_token: str) -> None:
    key = (user_token[:16], page_id)
    _page_token_cache[key] = (page_token, time.time() + _PAGE_TOKEN_TTL_SEC)


def _cache_ig_account(user_token: str, page_id: str, ig_user_id: str) -> None:
    key = (user_token[:16], page_id)
    _ig_account_cache[key] = (ig_user_id, time.time() + _IG_ACCOUNT_TTL_SEC)


async def _resolve_page_token(user_token: str, page_id: str) -> str:
    """Convert the user access token + a page_id into that Page's
    access token. Cached briefly so repeated tool calls in the same
    conversation don't hammer ``/me/accounts``.
    """
    key = (user_token[:16], page_id)
    cached = _page_token_cache.get(key)
    if cached and cached[1] > time.time():
        return cached[0]

    # Direct lookup — Meta exposes ``access_token`` on /{page_id} when
    # the caller has admin perms on it.
    try:
        data = await _graph_get(
            f"/{page_id}",
            params={"fields": "access_token"},
            token=user_token,
        )
        if data.get("access_token"):
            _cache_page_token(user_token, page_id, data["access_token"])
            return data["access_token"]
    except _GraphError:
        pass

    # Fallback: enumerate /me/accounts — slower but more reliable when
    # the user has access via Business Manager rather than direct admin.
    data = await _graph_get(
        "/me/accounts",
        params={"fields": "id,access_token", "limit": 100},
        token=user_token,
    )
    for p in data.get("data", []):
        if p.get("id") == page_id and p.get("access_token"):
            _cache_page_token(user_token, page_id, p["access_token"])
            return p["access_token"]
    # Client-side condition (not a Graph API response): the user
    # authenticated but no page admin token is reachable. Use code 0
    # since there's no Meta error code to forward. The outer call_tool
    # catch turns the message into a clean tool_error for the agent.
    raise _GraphError(
        code=0,
        message=(
            f"No page access token available for page_id={page_id!r}. "
            "User may lack admin role on this Page, or the OAuth grant "
            "is missing pages_show_list / pages_manage_posts. Try "
            "`list_pages` to see which Pages this account can manage."
        ),
    )


# ── HTTP helpers — thin aliases over the shared MetaGraphClient ────────────

_GraphError = MetaGraphError


async def _graph_get(path, *, params=None, token):
    return await _graph.get(path, params=params, token=token)


async def _graph_post(path, body, *, token, json_body=False):
    return await _graph.post(path, body, token=token, json_body=json_body)


async def _graph_delete(path, *, token):
    return await _graph.delete(path, token=token)


# ── MCP envelope helpers — re-exported from the shared module ─────────────

from packages.core.ai.mcp._http import (  # noqa: E402, F401
    mcp_ok as _ok,
    mcp_err as _err,
)
