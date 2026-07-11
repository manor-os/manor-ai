"""Product Hunt MCP server — GraphQL wrapper around the v2 API.

API docs: https://api.producthunt.com/v2/docs

Auth: bearer_token = the user's Product Hunt OAuth access token.
Manor's standard OAuth flow (resolve_oauth_config + the
``/integrations/oauth/producthunt/callback`` route) handles the
authorize/exchange. Agents call typed tools below; this module
translates each into a GraphQL query.

What's covered:
  * ``search_posts``    — find posts by name / topic / launch date
  * ``get_post``        — fetch a single post by slug or id
  * ``daily_posts``     — top posts launched today (or another date)
  * ``list_comments``   — comments on a post (most recent first)
  * ``post_comment``    — leave a comment as the authenticated user
                          (requires ``private`` scope)
  * ``me``              — current authenticated user (sanity check)

Posting a NEW launch (``createPost``) is intentionally not exposed:
PH gates that behind app approval + maker verification, and the
demo flow assumes the operator submits the launch via the PH UI
once the assets are ready. Once Manor's PH app is approved we can
add it.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List

import httpx

logger = logging.getLogger(__name__)


_GRAPHQL = "https://api.producthunt.com/v2/api/graphql"
_TIMEOUT = 30.0
_MAX_PAYLOAD_CHARS = 12_000


# ── MCP protocol ────────────────────────────────────────────────────────────

def list_tools() -> List[Dict[str, Any]]:
    return [
        {
            "name": "search_posts",
            "description": (
                "Search Product Hunt posts by topic, free-text, or "
                "launch date. Useful for competitor research before a "
                "launch (e.g. 'AI agent platforms 2026')."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "topic": {
                        "type": "string",
                        "description": "Topic slug filter (e.g. 'artificial-intelligence', 'developer-tools').",
                    },
                    "url": {
                        "type": "string",
                        "description": "Find a specific post by its public URL.",
                    },
                    "posted_after": {
                        "type": "string",
                        "description": "ISO date — only posts launched on/after this day.",
                    },
                    "posted_before": {
                        "type": "string",
                        "description": "ISO date — only posts launched on/before this day.",
                    },
                    "first": {
                        "type": "integer",
                        "description": "Page size, 1-50. Default 10.",
                    },
                    "order": {
                        "type": "string",
                        "description": "'RANKING' (default) | 'NEWEST' | 'VOTES' | 'FEATURED_AT'.",
                    },
                },
            },
        },
        {
            "name": "get_post",
            "description": (
                "Fetch one Product Hunt post in detail by slug. Returns "
                "name, tagline, description, votes, comment count, "
                "thumbnail, makers."
            ),
            "parameters": {
                "type": "object",
                "required": ["slug"],
                "properties": {
                    "slug": {"type": "string", "description": "URL slug, e.g. 'manor-os' for producthunt.com/posts/manor-os."},
                },
            },
        },
        {
            "name": "daily_posts",
            "description": (
                "Top posts launched on a specific day. Defaults to today. "
                "Useful for monitoring competition on launch day."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "day": {
                        "type": "string",
                        "description": "ISO date (YYYY-MM-DD). Defaults to today UTC.",
                    },
                    "first": {
                        "type": "integer",
                        "description": "How many to return (default 10).",
                    },
                },
            },
        },
        {
            "name": "list_comments",
            "description": (
                "Get comments on a Product Hunt post (most recent first). "
                "Use slug from ``get_post`` or ``search_posts``."
            ),
            "parameters": {
                "type": "object",
                "required": ["slug"],
                "properties": {
                    "slug": {"type": "string"},
                    "first": {
                        "type": "integer",
                        "description": "Page size, 1-50. Default 20.",
                    },
                },
            },
        },
        {
            "name": "post_comment",
            "description": (
                "Leave a comment on a post as the authenticated user. "
                "Requires the OAuth token to have the ``private`` scope. "
                "Use this to thank-you commenters during launch day."
            ),
            "parameters": {
                "type": "object",
                "required": ["post_id", "body"],
                "properties": {
                    "post_id": {
                        "type": "string",
                        "description": "PH numeric/slug post id (from get_post.id).",
                    },
                    "body": {"type": "string", "description": "Comment text (markdown supported)."},
                    "parent_comment_id": {
                        "type": "string",
                        "description": "Optional parent comment id for threaded replies.",
                    },
                },
            },
        },
        {
            "name": "me",
            "description": "Return the authenticated PH user — sanity check that the OAuth token works.",
            "parameters": {"type": "object", "properties": {}},
        },
    ]


async def call_tool(
    name: str,
    arguments: Dict[str, Any],
    bearer_token: str,
) -> Dict[str, Any]:
    if not bearer_token:
        return _error(
            "Product Hunt is not connected. Visit /integrations → "
            "Product Hunt → Connect to authorize Manor."
        )
    handler = _HANDLERS.get(name)
    if handler is None:
        return _error(f"Unknown product_hunt tool: {name}")
    try:
        return _content(await handler(arguments, bearer_token))
    except httpx.HTTPStatusError as exc:
        body = exc.response.text[:500] if exc.response is not None else ""
        return _error(f"Product Hunt HTTP {exc.response.status_code}: {body}")
    except Exception as exc:  # noqa: BLE001
        logger.exception("Product Hunt tool %s crashed", name)
        return _error(f"Product Hunt call failed: {exc}")


# ── GraphQL transport ──────────────────────────────────────────────────────

async def _gql(token: str, query: str, variables: Dict[str, Any]) -> Dict[str, Any]:
    async with httpx.AsyncClient(timeout=_TIMEOUT) as cx:
        r = await cx.post(
            _GRAPHQL,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "manor-os/1.0",
            },
            json={"query": query, "variables": variables},
        )
        r.raise_for_status()
        body = r.json()
    if body.get("errors"):
        raise RuntimeError(f"PH GraphQL errors: {body['errors']}")
    return body.get("data") or {}


# ── Handlers ────────────────────────────────────────────────────────────────

async def _search_posts(args: Dict[str, Any], token: str) -> str:
    variables: Dict[str, Any] = {"first": int(args.get("first") or 10)}
    if args.get("topic"):
        variables["topic"] = args["topic"]
    if args.get("url"):
        variables["url"] = args["url"]
    if args.get("posted_after"):
        variables["postedAfter"] = args["posted_after"]
    if args.get("posted_before"):
        variables["postedBefore"] = args["posted_before"]
    if args.get("order"):
        variables["order"] = args["order"]

    query = """
    query Search(
        $first: Int, $topic: String, $url: String,
        $postedAfter: DateTime, $postedBefore: DateTime, $order: PostsOrder
    ) {
      posts(
        first: $first, topic: $topic, url: $url,
        postedAfter: $postedAfter, postedBefore: $postedBefore, order: $order
      ) {
        edges { node {
          id slug name tagline description votesCount commentsCount
          createdAt featuredAt website url thumbnail { url }
        }}
      }
    }
    """
    data = await _gql(token, query, variables)
    posts = [edge["node"] for edge in (data.get("posts") or {}).get("edges", [])]
    return _truncate(json.dumps({"count": len(posts), "posts": posts}, ensure_ascii=False, indent=2))


async def _get_post(args: Dict[str, Any], token: str) -> str:
    slug = (args.get("slug") or "").strip()
    if not slug:
        raise ValueError("slug required")
    query = """
    query GetPost($slug: String!) {
      post(slug: $slug) {
        id slug name tagline description votesCount commentsCount
        createdAt featuredAt website url
        thumbnail { url type }
        media { url type }
        topics { edges { node { slug name } } }
        makers { id name username profileImage }
      }
    }
    """
    data = await _gql(token, query, {"slug": slug})
    return _truncate(json.dumps(data.get("post") or {}, ensure_ascii=False, indent=2))


async def _daily_posts(args: Dict[str, Any], token: str) -> str:
    from datetime import datetime, timezone, timedelta
    day = args.get("day") or datetime.now(timezone.utc).date().isoformat()
    # PH coerces a bare date to midnight, so postedAfter==postedBefore collapses
    # to an empty range. Use a full-day [day, day+1) window instead.
    try:
        d = datetime.fromisoformat(day).date()
    except ValueError:
        d = datetime.now(timezone.utc).date()
    day_end = (d + timedelta(days=1)).isoformat()
    variables = {"first": int(args.get("first") or 10), "day": d.isoformat(), "dayEnd": day_end}
    query = """
    query DailyPosts($first: Int, $day: DateTime, $dayEnd: DateTime) {
      posts(first: $first, postedAfter: $day, postedBefore: $dayEnd, order: VOTES) {
        edges { node {
          id slug name tagline votesCount commentsCount featuredAt url
        }}
      }
    }
    """
    data = await _gql(token, query, variables)
    posts = [edge["node"] for edge in (data.get("posts") or {}).get("edges", [])]
    return _truncate(json.dumps({"day": day, "count": len(posts), "posts": posts}, ensure_ascii=False, indent=2))


async def _list_comments(args: Dict[str, Any], token: str) -> str:
    slug = (args.get("slug") or "").strip()
    if not slug:
        raise ValueError("slug required")
    first = int(args.get("first") or 20)
    query = """
    query Comments($slug: String!, $first: Int) {
      post(slug: $slug) {
        id slug name commentsCount
        comments(first: $first, order: NEWEST) {
          edges { node {
            id body createdAt votesCount
            user { id username name }
            replies { edges { node { id body createdAt user { username } } } }
          }}
        }
      }
    }
    """
    data = await _gql(token, query, {"slug": slug, "first": first})
    return _truncate(json.dumps(data.get("post") or {}, ensure_ascii=False, indent=2))


async def _post_comment(args: Dict[str, Any], token: str) -> str:
    post_id = (args.get("post_id") or "").strip()
    body = (args.get("body") or "").strip()
    if not post_id or not body:
        raise ValueError("post_id and body required")
    parent = args.get("parent_comment_id")

    mutation = """
    mutation Comment($input: CommentCreateInput!) {
      commentCreate(input: $input) {
        comment { id body createdAt user { username } }
        errors { field message }
      }
    }
    """
    input_payload: Dict[str, Any] = {"subjectId": post_id, "body": body}
    if parent:
        input_payload["parentId"] = parent
    data = await _gql(token, mutation, {"input": input_payload})
    result = data.get("commentCreate") or {}
    if result.get("errors"):
        raise RuntimeError(f"commentCreate failed: {json.dumps(result['errors'])}")
    return _truncate(json.dumps(result, ensure_ascii=False, indent=2))


async def _me(args: Dict[str, Any], token: str) -> str:
    query = "{ viewer { user { id username name profileImage } } }"
    data = await _gql(token, query, {})
    return json.dumps(data.get("viewer") or {}, ensure_ascii=False, indent=2)


# ── Helpers ─────────────────────────────────────────────────────────────────

def _truncate(s: str) -> str:
    return s if len(s) <= _MAX_PAYLOAD_CHARS else s[:_MAX_PAYLOAD_CHARS] + "\n… (truncated)"


def _content(text: str) -> Dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": False}


from packages.core.ai.mcp._http import mcp_err as _error  # noqa: E402, F401


_HANDLERS = {
    "search_posts": _search_posts,
    "get_post": _get_post,
    "daily_posts": _daily_posts,
    "list_comments": _list_comments,
    "post_comment": _post_comment,
    "me": _me,
}
