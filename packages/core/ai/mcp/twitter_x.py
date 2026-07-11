"""
Twitter/X MCP server — in-process MCP implementation for X API v2.

Auth: Bearer token = X OAuth2 access_token (from entity integration config).

Tools follow mcp__twitter_x__{tool_name} naming via the MCP tool pool.

Scopes used:
  - tweet.read: read tweets, timeline, search
  - tweet.write: create and delete tweets
  - users.read: read user profiles, followers, following
  - like.read/like.write: inspect and mutate likes
  - follows.read/follows.write: inspect and mutate follows
  - offline.access: refresh token support
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import logging
import time
from typing import Any, Dict, List, Optional
from urllib.parse import quote, urlencode

import httpx

logger = logging.getLogger(__name__)

_API = "https://api.x.com/2"
_MAX_CHARS = 12_000

# Default tweet fields requested on most endpoints
_TWEET_FIELDS = "created_at,public_metrics,lang,author_id,conversation_id,in_reply_to_user_id"
_USER_FIELDS = "created_at,description,location,public_metrics,profile_image_url,verified"
_DEFAULT_ME_USER_FIELDS = "public_metrics,profile_image_url,description"

# ── Per-call context (set by mcp_builtin dispatcher) ─────────────────────────
#
# Module-level dict matches the convention used by the other in-process MCP
# modules (elevenlabs.py, _cli_runner.py). The dispatcher set/clears it
# around each call_tool invocation. Used to scope an inline 401 refresh
# back to the right OAuthAccount row.

_call_context: Dict[str, str] = {}


def set_call_context(ctx: Dict[str, str]) -> None:
    global _call_context
    _call_context = dict(ctx or {})


def clear_call_context() -> None:
    global _call_context
    _call_context = {}


# ── Process-local /users/me cache ────────────────────────────────────────────
#
# The X v2 `users/me` call is needed before every like/retweet/unlike/etc.
# (those endpoints are user-scoped: `users/{me_id}/likes`). Without a cache
# every write doubles its API spend and adds a second failure surface.
#
# Cached by token-prefix (full token would be a bigger secret to hold);
# 32 chars is plenty to distinguish real X access tokens. TTL is short
# enough that a token rotation invalidates the cache reasonably quickly,
# and any miss triggers a fresh /users/me lookup so correctness is never
# stale. Bounded to avoid leaking memory on long-lived workers.

_USER_ID_CACHE_TTL = 600.0
_USER_ID_CACHE_MAX = 256
_user_id_cache: Dict[str, tuple[str, float]] = {}


def _user_id_cache_key(token: str) -> str:
    return token[:32] if token else ""


def _user_id_cache_get(token: str) -> Optional[str]:
    key = _user_id_cache_key(token)
    if not key:
        return None
    entry = _user_id_cache.get(key)
    if not entry:
        return None
    user_id, expires_at = entry
    if expires_at <= time.monotonic():
        _user_id_cache.pop(key, None)
        return None
    return user_id


def _user_id_cache_put(token: str, user_id: str) -> None:
    key = _user_id_cache_key(token)
    if not key or not user_id:
        return
    if len(_user_id_cache) >= _USER_ID_CACHE_MAX:
        # Drop the oldest entry — simple LRU-ish without importing OrderedDict.
        oldest = min(_user_id_cache.items(), key=lambda kv: kv[1][1])[0]
        _user_id_cache.pop(oldest, None)
    _user_id_cache[key] = (user_id, time.monotonic() + _USER_ID_CACHE_TTL)


# ── Typed error for retry/dispatch decisions ─────────────────────────────────


class _XApiError(Exception):
    """Structured error raised by the HTTP layer.

    ``retryable`` flags transient failures (429, 5xx, network) that the
    caller should backoff and retry. ``status == 401`` is handled
    specially by ``_request`` — one inline token refresh, then retry.
    """

    def __init__(
        self,
        message: str,
        *,
        status: int = 0,
        retryable: bool = False,
        retry_after: float = 0.0,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.retryable = retryable
        self.retry_after = retry_after


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
    except _XApiError as e:
        # Surface as a real MCP error so the dispatcher's isError branch
        # fires (mcp_builtin returns ``{"error": "tool_error", …}``);
        # before this fix every API failure leaked through as
        # ``isError: false`` plain text and the agent treated it as success.
        return _error(e.message)
    except Exception as e:
        logger.exception("Twitter/X MCP tool %s failed", name)
        return _error(str(e))


def _error(msg: str) -> Dict[str, Any]:
    return {"content": [{"type": "text", "text": msg}], "isError": True}


# ── Simulation (dry_run / sandbox plans) ────────────────────────────────────

async def simulate_tool(
    name: str,
    arguments: Dict[str, Any],
) -> Dict[str, Any]:
    """Return a schema-realistic fake response without calling the X API.

    PlanExecutor calls this when the plan's execution_mode is dry_run
    or sandbox so we can demo the entire pipeline (Strategist →
    Planner → Executor → workspace_chat receipt) without spending
    real API quota or posting real tweets.

    Shape mirrors what the live X v2 API would return — same envelope
    keys (``data``, ``meta``) — so consumers don't branch on simulated
    vs live results."""
    handler = _SIMULATORS.get(name)
    if not handler:
        # Generic fallback so unknown tool names still produce a
        # syntactically valid response and dry_run plans don't crash
        # on tools we haven't bothered to fake yet.
        text = json.dumps({
            "_simulated": True,
            "tool": name,
            "input": arguments,
        }, ensure_ascii=False)
        return {"content": [{"type": "text", "text": text}], "isError": False}

    try:
        text = handler(arguments)
        return {"content": [{"type": "text", "text": text}], "isError": False}
    except Exception as e:
        logger.exception("Twitter/X simulator for %s failed", name)
        return _error(str(e))


def _sim_get_me(args: Dict) -> str:
    return json.dumps({"data": _sim_user()})


def _sim_get_user(args: Dict) -> str:
    username = str(args.get("username") or "sandbox_user").lstrip("@")
    return json.dumps({"data": _sim_user(username=username)})


def _sim_search_users(args: Dict) -> str:
    query = str(args.get("query") or "sandbox").strip() or "sandbox"
    n = min(int(args.get("max_results") or 5), 5)
    users = [
        _sim_user(
            user_id=f"22345678901234567{i}",
            username=f"{query.lower().replace(' ', '_')}_{i + 1}"[:15],
            name=f"{query.title()} User {i + 1}",
        )
        for i in range(n)
    ]
    return json.dumps({"data": users, "meta": {"result_count": n}}, ensure_ascii=False)


def _sim_get_user_by_id(args: Dict) -> str:
    user_id = str(args.get("user_id") or "1234567890123456789")
    return json.dumps({"data": _sim_user(user_id=user_id)})


def _sim_create_tweet(args: Dict) -> str:
    text = (args.get("text") or "")[:280]
    payload = {
        "data": {
            "id": _fake_tweet_id(),
            "text": text,
            "created_at": _utc_now_iso(),
            "edit_history_tweet_ids": [_fake_tweet_id()],
        },
        "_simulated": True,
    }
    return json.dumps(_normalize_publish_payload(payload, fallback_text=text), ensure_ascii=False)


def _sim_comment_tweet(args: Dict) -> str:
    payload = json.loads(_sim_create_tweet({"text": args.get("text") or ""}))
    reply_to = str(args.get("tweet_id") or "")
    if reply_to:
        payload["reply_to"] = reply_to
        if isinstance(payload.get("data"), dict):
            payload["data"]["in_reply_to_tweet_id"] = reply_to
    return json.dumps(payload, ensure_ascii=False)


def _sim_create_thread(args: Dict) -> str:
    texts = _thread_texts(args)
    tweets: list[dict[str, Any]] = []
    previous_id = str(args.get("reply_to") or "")
    for index, text in enumerate(texts):
        payload = json.loads(_sim_create_tweet({"text": text}))
        tweet_id = _fake_tweet_id(suffix=str(index))
        payload["tweet_id"] = tweet_id
        payload["tweet_url"] = f"https://x.com/i/web/status/{tweet_id}"
        if isinstance(payload.get("data"), dict):
            payload["data"]["id"] = tweet_id
            payload["data"]["edit_history_tweet_ids"] = [tweet_id]
        payload["thread_index"] = index
        if previous_id:
            payload["reply_to"] = previous_id
            if isinstance(payload.get("data"), dict):
                payload["data"]["in_reply_to_tweet_id"] = previous_id
        previous_id = tweet_id
        tweets.append(payload)

    ids = [str(item.get("tweet_id") or item.get("data", {}).get("id") or "") for item in tweets]
    return json.dumps({
        "data": {
            "tweets": tweets,
            "root_tweet_id": ids[0] if ids else None,
            "last_tweet_id": ids[-1] if ids else None,
            "count": len(tweets),
        },
        "thread_ids": ids,
        "status": "published",
        "_simulated": True,
    }, ensure_ascii=False)


def _sim_delete_tweet(args: Dict) -> str:
    return json.dumps({
        "data": {
            "deleted": True,
            "id": args.get("tweet_id") or _fake_tweet_id(),
        },
        "_simulated": True,
    })


def _sim_get_tweet(args: Dict) -> str:
    return json.dumps({
        "data": {
            "id": args.get("tweet_id") or _fake_tweet_id(),
            "text": "[simulated tweet content]",
            "created_at": "2026-04-24T12:00:00.000Z",
            "lang": "en",
            "author_id": "1234567890123456789",
            "public_metrics": {
                "retweet_count": 3,
                "reply_count": 1,
                "like_count": 18,
                "quote_count": 0,
                "impression_count": 540,
            },
        }
    })


def _sim_get_tweet_metrics(args: Dict) -> str:
    return json.dumps({
        "data": {
            "id": args.get("tweet_id") or _fake_tweet_id(),
            "text": "[simulated tweet content]",
            "public_metrics": {
                "retweet_count": 3,
                "reply_count": 1,
                "like_count": 18,
                "quote_count": 0,
                "impression_count": 540,
            },
            "non_public_metrics": {
                "url_link_clicks": 12,
                "user_profile_clicks": 7,
            },
        },
        "metrics": {
            "impressions": 540,
            "likes": 18,
            "retweets": 3,
            "replies": 1,
            "quotes": 0,
            "url_link_clicks": 12,
            "user_profile_clicks": 7,
        },
    })


def _sim_get_user_timeline(args: Dict) -> str:
    n = min(int(args.get("max_results") or 5), 5)
    tweets = [
        {
            "id": _fake_tweet_id(suffix=str(i)),
            "text": f"[simulated tweet #{i + 1}]",
            "created_at": "2026-04-24T12:00:00.000Z",
            "lang": "en",
            "public_metrics": {
                "retweet_count": i, "reply_count": 0,
                "like_count": 5 + i * 2, "quote_count": 0,
                "impression_count": 100 + i * 50,
            },
        }
        for i in range(n)
    ]
    return json.dumps({"data": tweets, "meta": {"result_count": n}})


def _sim_search_recent(args: Dict) -> str:
    return _sim_get_user_timeline({"max_results": args.get("max_results", 3)})


def _sim_user_collection(args: Dict, *, source: str) -> str:
    n = min(int(args.get("max_results") or 5), 5)
    users = [
        _sim_user(
            user_id=f"12345678901234567{i}",
            username=f"{source}_user_{i + 1}",
            name=f"{source.title()} User {i + 1}",
        )
        for i in range(n)
    ]
    return json.dumps({"data": users, "meta": {"result_count": n}})


def _sim_get_mentions(args: Dict) -> str:
    n = min(int(args.get("max_results") or 5), 5)
    tweets = [
        {
            "id": _fake_tweet_id(suffix=str(i)),
            "text": f"@sandbox_user simulated mention #{i + 1}",
            "created_at": "2026-04-24T12:00:00.000Z",
            "lang": "en",
            "author_id": f"12345678901234567{i}",
            "conversation_id": _fake_tweet_id(suffix=str(i)),
            "public_metrics": {
                "retweet_count": i,
                "reply_count": 1,
                "like_count": 8 + i,
                "quote_count": 0,
                "impression_count": 200 + i * 40,
            },
        }
        for i in range(n)
    ]
    return json.dumps({"data": tweets, "meta": {"result_count": n}})


def _sim_like_tweet(args: Dict) -> str:
    return json.dumps({"data": {"liked": True, "_simulated": True}})


def _sim_unlike_tweet(args: Dict) -> str:
    return json.dumps({"data": {"liked": False, "_simulated": True}})


def _sim_retweet(args: Dict) -> str:
    return json.dumps({"data": {"retweeted": True, "_simulated": True}})


def _sim_unretweet(args: Dict) -> str:
    return json.dumps({"data": {"retweeted": False, "_simulated": True}})


def _sim_follow_user(args: Dict) -> str:
    return json.dumps({
        "data": {
            "following": True,
            "pending_follow": False,
            "target_user_id": args.get("target_user_id") or args.get("user_id") or "1234567890123456789",
            "_simulated": True,
        },
    })


def _sim_unfollow_user(args: Dict) -> str:
    return json.dumps({
        "data": {
            "following": False,
            "target_user_id": args.get("target_user_id") or args.get("user_id") or "1234567890123456789",
            "_simulated": True,
        },
    })


def _sim_user(
    *,
    user_id: str = "1234567890123456789",
    username: str = "sandbox_user",
    name: str = "Sandbox User",
) -> Dict[str, Any]:
    return {
        "id": user_id,
        "name": name,
        "username": username,
        "description": "Simulated profile for Manor sandbox.",
        "verified": False,
        "created_at": "2026-04-24T12:00:00.000Z",
        "public_metrics": {
            "followers_count": 2_847,
            "following_count": 312,
            "tweet_count": 1_205,
            "listed_count": 14,
        },
    }


def _fake_tweet_id(suffix: str = "") -> str:
    # 19-digit IDs match the real X tweet id shape.
    base = "199900000000000000"
    return (base + (suffix or "0"))[:19]


_SIMULATORS = {
    "get_me": _sim_get_me,
    "get_user": _sim_get_user,
    "search_users": _sim_search_users,
    "get_user_by_id": _sim_get_user_by_id,
    "create_tweet": _sim_create_tweet,
    "comment_tweet": _sim_comment_tweet,
    "create_thread": _sim_create_thread,
    "delete_tweet": _sim_delete_tweet,
    "get_tweet": _sim_get_tweet,
    "get_tweet_metrics": _sim_get_tweet_metrics,
    "get_user_timeline": _sim_get_user_timeline,
    "get_my_timeline": _sim_get_user_timeline,
    "search_recent": _sim_search_recent,
    "get_followers": lambda args: _sim_user_collection(args, source="follower"),
    "get_following": lambda args: _sim_user_collection(args, source="following"),
    "like_tweet": _sim_like_tweet,
    "unlike_tweet": _sim_unlike_tweet,
    "retweet": _sim_retweet,
    "unretweet": _sim_unretweet,
    "follow_user": _sim_follow_user,
    "unfollow_user": _sim_unfollow_user,
    "get_liking_users": lambda args: _sim_user_collection(args, source="liking"),
    "get_mentions": _sim_get_mentions,
}


# ── X API client ─────────────────────────────────────────────────────────────

async def _api(
    token: str,
    method: str,
    path: str,
    body: Optional[Dict] = None,
    params: Optional[Dict] = None,
) -> str:
    """Single HTTP attempt against the X v2 API.

    Raises ``_XApiError`` for any non-2xx response or network failure;
    returns a JSON-pretty string on success. Callers go through
    ``_request`` to get retry + inline token refresh.
    """
    url = f"{_API}/{path.lstrip('/')}" if not path.startswith("http") else path
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }
    if method in ("POST", "PUT", "DELETE") and body is not None:
        headers["Content-Type"] = "application/json"

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            if method == "GET":
                resp = await client.get(url, headers=headers, params=params)
            elif method == "POST":
                resp = await client.post(url, headers=headers, json=body or {})
            elif method == "DELETE":
                resp = await client.delete(url, headers=headers)
            else:
                resp = await client.request(method, url, headers=headers, json=body)
    except httpx.TimeoutException as e:
        raise _XApiError(
            f"X API timeout after 20s: {e}", retryable=True,
        ) from e
    except httpx.HTTPError as e:
        raise _XApiError(
            f"X API network error: {e}", retryable=True,
        ) from e

    status = resp.status_code
    if status == 401:
        # Caller (_request) tries one inline token refresh before giving up.
        raise _XApiError(
            "X access token rejected (401). Reconnect X/Twitter on the "
            "Integration page if this persists.",
            status=401,
        )
    if status == 403:
        # X uses 429 for rate limits; 403 is genuine permission/scope rejection.
        raise _XApiError(
            f"X forbidden (403, missing scope or app permission): "
            f"{resp.text[:300]}",
            status=403,
        )
    if status == 404:
        raise _XApiError("Not found.", status=404)
    if status == 429:
        retry_after = _parse_retry_after(resp)
        raise _XApiError(
            f"X rate limit exceeded (retry in {int(retry_after)}s).",
            status=429,
            retryable=True,
            retry_after=retry_after,
        )
    if status >= 500:
        raise _XApiError(
            f"X server error ({status}): {resp.text[:200]}",
            status=status,
            retryable=True,
        )
    if not resp.is_success:
        raise _XApiError(
            f"X API error ({status}): {resp.text[:300]}",
            status=status,
        )

    try:
        data = resp.json()
    except Exception:
        return resp.text[:_MAX_CHARS]

    out = json.dumps(data, ensure_ascii=False, indent=2, default=str)
    if len(out) > _MAX_CHARS:
        return out[:_MAX_CHARS] + "\n… (truncated)"
    return out


def _parse_retry_after(resp: httpx.Response) -> float:
    """Read 429 backoff from headers. Caps at 60s so we don't stall a tool
    call indefinitely — agent can decide to give up after that."""
    raw = resp.headers.get("retry-after")
    if raw:
        try:
            return min(60.0, max(0.0, float(raw)))
        except ValueError:
            pass
    reset = resp.headers.get("x-rate-limit-reset")
    if reset:
        try:
            wait = float(reset) - time.time()
            return min(60.0, max(0.0, wait))
        except ValueError:
            pass
    return 0.0


async def _request(
    token: str,
    method: str,
    path: str,
    body: Optional[Dict] = None,
    params: Optional[Dict] = None,
) -> str:
    """High-level call: retry transient failures with backoff, attempt
    one inline token refresh on 401.

    Reads (GET): up to 3 retries on 429/5xx/network.
    Writes (POST/DELETE): one retry only — write idempotency isn't
    guaranteed by X, so we minimise the chance of a double-post.
    """
    is_write = method in ("POST", "PUT", "DELETE")
    max_retries = 1 if is_write else 3
    refreshed_once = False
    backoff = 1.0
    current_token = token

    for attempt in range(max_retries + 1):
        try:
            return await _api(current_token, method, path, body, params)
        except _XApiError as e:
            # 401 → try a single inline refresh, then continue (doesn't
            # consume a retry budget; rare and worth the extra attempt).
            if e.status == 401 and not refreshed_once:
                refreshed_once = True
                new_token = await _try_inline_refresh(current_token)
                if new_token and new_token != current_token:
                    current_token = new_token
                    continue
                raise

            if not e.retryable or attempt >= max_retries:
                raise

            wait = e.retry_after if e.retry_after > 0 else min(
                backoff * (2 ** attempt), 30.0,
            )
            logger.info(
                "twitter_x %s %s: transient %s, retrying in %.1fs (attempt %d/%d)",
                method, path, e.status or "network", wait, attempt + 1, max_retries,
            )
            await asyncio.sleep(wait)

    # Loop exits only via return or raise — this line is unreachable but
    # keeps the type checker happy.
    raise _XApiError("X API: exhausted retries", retryable=False)


async def _try_inline_refresh(old_token: str) -> Optional[str]:
    """Refresh the user's twitter_x OAuth token and persist it.

    Returns the new access_token on success, ``None`` on any failure
    (no context, no row, no refresh_token, refresh endpoint failed). On
    None the caller surfaces the original 401 so the user is prompted
    to reconnect.

    Scoped to user-OAuthAccount only — entity Integration tokens are
    refreshed by the Celery beat (see oauth_refresh.py); supporting
    them inline would require routing through CredentialService for
    vault-backed reads which we keep out of the MCP module.
    """
    user_id = _call_context.get("user_id")
    if not user_id:
        return None

    try:
        from datetime import datetime, timedelta, timezone
        from sqlalchemy import select

        from packages.core.database import async_session
        from packages.core.models.user import OAuthAccount
        from packages.core.services.provider_keys import provider_key_aliases
        from packages.core.tasks.oauth_refresh import refresh_token_via_provider

        async with async_session() as db:
            row = (await db.execute(
                select(OAuthAccount).where(
                    OAuthAccount.user_id == user_id,
                    OAuthAccount.provider.in_(provider_key_aliases("twitter_x")),
                ).order_by(OAuthAccount.updated_at.desc()).limit(1)
            )).scalar_one_or_none()
            if not row or not row.refresh_token or row.access_token != old_token:
                return None

            data = await refresh_token_via_provider(
                "twitter_x", row.refresh_token, db=db,
            )
            if not data or not data.get("access_token"):
                return None

            new_token = data["access_token"]
            row.access_token = new_token
            if data.get("refresh_token"):
                row.refresh_token = data["refresh_token"]  # X rotates these
            if data.get("expires_in"):
                row.token_expires_at = datetime.now(timezone.utc) + timedelta(
                    seconds=int(data["expires_in"]),
                )
            await db.commit()
            return new_token
    except Exception as e:  # noqa: BLE001 — refresh is best-effort
        logger.warning("twitter_x inline token refresh failed: %s", e)
        return None


async def _resolve_user_id(token: str) -> Optional[str]:
    """Get the authenticated X user's id, hitting /users/me at most once
    per ``_USER_ID_CACHE_TTL`` per token. Returns None if the call fails
    in a way the caller should surface as an error."""
    cached = _user_id_cache_get(token)
    if cached:
        return cached

    me_resp = await _request(token, "GET", "users/me")
    try:
        me = json.loads(me_resp)
        user_id = me.get("data", {}).get("id")
    except (json.JSONDecodeError, KeyError, TypeError):
        return None

    if user_id:
        _user_id_cache_put(token, user_id)
    return user_id


# ── Tool handlers ─────────────────────────────────────────────────────────────

async def _get_me(token: str, args: Dict) -> str:
    """Get the authenticated user's profile."""
    user_fields = str(args.get("user_fields") or _DEFAULT_ME_USER_FIELDS).strip()
    return await _request(token, "GET", "users/me", params={"user.fields": user_fields})


async def _get_user(token: str, args: Dict) -> str:
    """Get a user's profile by username."""
    username = args["username"].lstrip("@")
    return await _request(token, "GET", f"users/by/username/{username}", params={"user.fields": _USER_FIELDS})


async def _search_users(token: str, args: Dict) -> str:
    """Search X/Twitter users by keyword."""
    params = {
        "query": args["query"],
        "user.fields": _USER_FIELDS,
        "max_results": _clamp(args.get("max_results", 20), 1, 1000),
    }
    next_token = args.get("next_token") or args.get("pagination_token")
    if next_token:
        params["next_token"] = next_token
    return await _request(token, "GET", "users/search", params=params)


async def _get_user_by_id(token: str, args: Dict) -> str:
    """Get a user's profile by ID."""
    return await _request(token, "GET", f"users/{args['user_id']}", params={"user.fields": _USER_FIELDS})


async def _get_user_timeline(token: str, args: Dict) -> str:
    """Get a user's recent tweets."""
    user_id = args["user_id"]
    params = {
        "tweet.fields": _TWEET_FIELDS,
        "max_results": _clamp(args.get("max_results", 10), 5, 100),
    }
    if args.get("pagination_token"):
        params["pagination_token"] = args["pagination_token"]
    return await _request(token, "GET", f"users/{user_id}/tweets", params=params)


async def _get_my_timeline(token: str, args: Dict) -> str:
    """Get the authenticated user's own recent tweets."""
    user_id = await _resolve_user_id(token)
    if not user_id:
        raise _XApiError("Could not resolve authenticated X user id.")

    params = {
        "tweet.fields": _TWEET_FIELDS,
        "max_results": _clamp(args.get("max_results", 10), 5, 100),
    }
    return await _request(token, "GET", f"users/{user_id}/tweets", params=params)


async def _search_recent(token: str, args: Dict) -> str:
    """Search recent tweets (last 7 days)."""
    params = {
        "query": args["query"],
        "tweet.fields": _TWEET_FIELDS,
        "max_results": _clamp(args.get("max_results", 10), 10, 100),
    }
    if args.get("pagination_token"):
        params["next_token"] = args["pagination_token"]
    return await _request(token, "GET", "tweets/search/recent", params=params)


async def _get_tweet(token: str, args: Dict) -> str:
    """Get a single tweet by ID."""
    return await _request(token, "GET", f"tweets/{args['tweet_id']}", params={
        "tweet.fields": _TWEET_FIELDS,
        "expansions": "author_id",
        "user.fields": "username,name,profile_image_url",
    })


async def _get_tweet_metrics(token: str, args: Dict) -> str:
    """Get public and non-public metrics for a tweet."""
    raw = await _request(token, "GET", f"tweets/{args['tweet_id']}", params={
        "tweet.fields": "public_metrics,non_public_metrics",
    })
    try:
        payload = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return raw

    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    public = data.get("public_metrics") if isinstance(data.get("public_metrics"), dict) else {}
    non_public = data.get("non_public_metrics") if isinstance(data.get("non_public_metrics"), dict) else {}
    payload["metrics"] = {
        "impressions": public.get("impression_count"),
        "likes": public.get("like_count"),
        "retweets": public.get("retweet_count"),
        "replies": public.get("reply_count"),
        "quotes": public.get("quote_count"),
        "url_link_clicks": non_public.get("url_link_clicks"),
        "user_profile_clicks": non_public.get("user_profile_clicks"),
    }
    return json.dumps(payload, ensure_ascii=False, indent=2, default=str)


async def _create_tweet(token: str, args: Dict) -> str:
    """Create a new tweet."""
    body: Dict[str, Any] = {"text": args["text"]}
    media_ids = args.get("media_ids")
    poll_options = args.get("poll_options")
    poll_duration_minutes = args.get("poll_duration_minutes")
    reply_to_tweet_id = (
        args.get("reply_to_tweet_id")
        or args.get("reply_tweet_id")
        or args.get("reply_to")
    )

    if media_ids is not None:
        body["media"] = {"media_ids": media_ids}
    if poll_options is not None:
        if not isinstance(poll_options, list) or not 2 <= len(poll_options) <= 4:
            raise ValueError("poll_options must be an array with 2 to 4 items.")
        body["poll"] = {"options": poll_options}
    if poll_duration_minutes is not None:
        try:
            body.setdefault("poll", {})["duration_minutes"] = int(poll_duration_minutes)
        except (TypeError, ValueError) as e:
            raise ValueError("poll_duration_minutes must be an integer.") from e
    if args.get("quote_tweet_id") is not None:
        body["quote_tweet_id"] = args["quote_tweet_id"]
    if reply_to_tweet_id is not None:
        body["reply"] = {"in_reply_to_tweet_id": reply_to_tweet_id}
    created_text = await _request(token, "POST", "tweets", body)
    try:
        payload = json.loads(created_text)
    except (TypeError, json.JSONDecodeError):
        return created_text

    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    tweet_id = data.get("id")
    if tweet_id:
        try:
            fetched_text = await _request(token, "GET", f"tweets/{tweet_id}", params={
                "tweet.fields": _TWEET_FIELDS,
            })
            fetched = json.loads(fetched_text)
            fetched_data = fetched.get("data") if isinstance(fetched.get("data"), dict) else {}
            if fetched_data:
                payload["data"] = {**data, **fetched_data}
        except Exception:
            logger.warning("Twitter/X create_tweet publish metadata lookup failed", exc_info=True)
    return json.dumps(_normalize_publish_payload(payload, fallback_text=args.get("text")), ensure_ascii=False)


async def _comment_tweet(token: str, args: Dict) -> str:
    """Reply/comment on a tweet."""
    text = await _create_tweet(token, {
        "text": args["text"],
        "reply_to": args["tweet_id"],
    })
    try:
        payload = json.loads(text)
    except (TypeError, json.JSONDecodeError):
        return text
    payload["reply_to"] = str(args["tweet_id"])
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    data["in_reply_to_tweet_id"] = str(args["tweet_id"])
    payload["data"] = data
    return json.dumps(payload, ensure_ascii=False)


async def _create_thread(token: str, args: Dict) -> str:
    """Create a tweet thread by posting each item as a reply to the previous one."""
    texts = _thread_texts(args)
    if len(texts) > 25:
        raise ValueError("create_thread supports at most 25 tweets per call.")

    tweets: list[dict[str, Any]] = []
    previous_id = str(args.get("reply_to") or "").strip()
    for index, text in enumerate(texts):
        posted = await _create_tweet(token, {
            "text": text,
            **({"reply_to": previous_id} if previous_id else {}),
        })
        payload = json.loads(posted)
        tweet_id = str(payload.get("tweet_id") or payload.get("data", {}).get("id") or "").strip()
        if not tweet_id:
            raise _XApiError(f"Thread publish failed at item {index + 1}: missing tweet id.")
        payload["thread_index"] = index
        if previous_id:
            payload["reply_to"] = previous_id
            data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
            data["in_reply_to_tweet_id"] = previous_id
            payload["data"] = data
        tweets.append(payload)
        previous_id = tweet_id

    ids = [str(item.get("tweet_id") or item.get("data", {}).get("id") or "") for item in tweets]
    return json.dumps({
        "data": {
            "tweets": tweets,
            "root_tweet_id": ids[0] if ids else None,
            "last_tweet_id": ids[-1] if ids else None,
            "count": len(tweets),
        },
        "thread_ids": ids,
        "status": "published",
    }, ensure_ascii=False)


async def _delete_tweet(token: str, args: Dict) -> str:
    """Delete a tweet by ID."""
    return await _request(token, "DELETE", f"tweets/{args['tweet_id']}")


async def _get_followers(token: str, args: Dict) -> str:
    """Get a user's followers."""
    params = {
        "user.fields": _USER_FIELDS,
        "max_results": _clamp(args.get("max_results", 20), 1, 1000),
    }
    if args.get("pagination_token"):
        params["pagination_token"] = args["pagination_token"]
    return await _request(token, "GET", f"users/{args['user_id']}/followers", params=params)


async def _get_following(token: str, args: Dict) -> str:
    """Get users that a user is following."""
    params = {
        "user.fields": _USER_FIELDS,
        "max_results": _clamp(args.get("max_results", 20), 1, 1000),
    }
    if args.get("pagination_token"):
        params["pagination_token"] = args["pagination_token"]
    return await _request(token, "GET", f"users/{args['user_id']}/following", params=params)


async def _like_tweet(token: str, args: Dict) -> str:
    """Like a tweet."""
    user_id = await _resolve_user_id(token)
    if not user_id:
        raise _XApiError("Could not resolve authenticated X user id.")
    return await _request(token, "POST", f"users/{user_id}/likes", {"tweet_id": args["tweet_id"]})


async def _unlike_tweet(token: str, args: Dict) -> str:
    """Unlike a tweet."""
    user_id = await _resolve_user_id(token)
    if not user_id:
        raise _XApiError("Could not resolve authenticated X user id.")
    return await _request(token, "DELETE", f"users/{user_id}/likes/{args['tweet_id']}")


async def _retweet(token: str, args: Dict) -> str:
    """Retweet a tweet."""
    user_id = await _resolve_user_id(token)
    if not user_id:
        raise _XApiError("Could not resolve authenticated X user id.")
    return await _request(token, "POST", f"users/{user_id}/retweets", {"tweet_id": args["tweet_id"]})


async def _unretweet(token: str, args: Dict) -> str:
    """Undo a retweet."""
    user_id = await _resolve_user_id(token)
    if not user_id:
        raise _XApiError("Could not resolve authenticated X user id.")
    return await _request(token, "DELETE", f"users/{user_id}/retweets/{args['tweet_id']}")


async def _follow_user(token: str, args: Dict) -> str:
    """Follow a user as the authenticated account."""
    source_user_id = await _resolve_user_id(token)
    if not source_user_id:
        raise _XApiError("Could not resolve authenticated X user id.")
    target_user_id = await _resolve_target_user_id(token, args)
    return await _request(token, "POST", f"users/{source_user_id}/following", {
        "target_user_id": target_user_id,
    })


async def _unfollow_user(token: str, args: Dict) -> str:
    """Unfollow a user as the authenticated account."""
    source_user_id = await _resolve_user_id(token)
    if not source_user_id:
        raise _XApiError("Could not resolve authenticated X user id.")
    target_user_id = await _resolve_target_user_id(token, args)
    return await _request(token, "DELETE", f"users/{source_user_id}/following/{target_user_id}")


async def _get_liking_users(token: str, args: Dict) -> str:
    """Get users who liked a tweet."""
    params = {"user.fields": _USER_FIELDS}
    return await _request(token, "GET", f"tweets/{args['tweet_id']}/liking_users", params=params)


async def _get_mentions(token: str, args: Dict) -> str:
    """Get recent mentions of a user."""
    params = {
        "tweet.fields": _TWEET_FIELDS,
        "max_results": _clamp(args.get("max_results", 10), 5, 100),
    }
    return await _request(token, "GET", f"users/{args['user_id']}/mentions", params=params)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _clamp(value, lo, hi):
    try:
        return max(lo, min(hi, int(value)))
    except (TypeError, ValueError):
        return lo


async def _resolve_target_user_id(token: str, args: Dict) -> str:
    user_id = str(args.get("target_user_id") or args.get("user_id") or "").strip()
    if user_id:
        return user_id
    username = str(args.get("username") or "").strip().lstrip("@")
    if username:
        payload = json.loads(await _get_user(token, {"username": username}))
        user_id = str((payload.get("data") or {}).get("id") or "").strip()
        if user_id:
            return user_id
    raise _XApiError("Target X user is required. Provide target_user_id/user_id or username.")


def _normalize_publish_payload(payload: Dict[str, Any], *, fallback_text: Any = None) -> Dict[str, Any]:
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    tweet_id = payload.get("tweet_id") or data.get("id")
    if tweet_id:
        payload["tweet_id"] = str(tweet_id)
        payload.setdefault("status", "published")
        payload.setdefault("tweet_url", f"https://x.com/i/web/status/{tweet_id}")

    text = data.get("text") or fallback_text
    if text and not payload.get("post_text"):
        payload["post_text"] = str(text)[:280]

    published_at = payload.get("published_at") or data.get("created_at")
    if not published_at and tweet_id:
        published_at = _utc_now_iso()
    if published_at:
        payload["published_at"] = str(published_at)
    return payload


def _thread_texts(args: Dict) -> list[str]:
    raw = args.get("texts") or args.get("tweets") or []
    if not isinstance(raw, list):
        raise ValueError("create_thread requires texts to be an array of tweet strings.")
    texts = [str(item).strip() for item in raw if str(item).strip()]
    if not texts:
        raise ValueError("create_thread requires at least one non-empty tweet.")
    too_long = [idx + 1 for idx, text in enumerate(texts) if len(text) > 280]
    if too_long:
        raise ValueError(f"create_thread tweet(s) exceed 280 characters: {too_long}")
    return texts


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


# ── Tool definitions ──────────────────────────────────────────────────────────

def _prop(desc: str, type_: str = "string") -> Dict[str, str]:
    return {"type": type_, "description": desc}


_TOOLS: Dict[str, Dict[str, Any]] = {
    "get_me": {
        "description": "Get the authenticated user's basic profile information.",
        "properties": {},
        "required": [],
    },
    "get_user": {
        "description": "Look up an X/Twitter user by username",
        "properties": {
            "username": _prop("X username (with or without @)"),
        },
        "required": ["username"],
    },
    "search_users": {
        "description": "Search X/Twitter users by keyword across name, username, and bio",
        "properties": {
            "query": _prop("User search query (name, username, or bio keyword)"),
            "max_results": _prop("Max users to return (1-1000, default: 20)", "integer"),
            "next_token": _prop("Pagination token for next page"),
        },
        "required": ["query"],
    },
    "get_user_by_id": {
        "description": "Look up an X/Twitter user by numeric ID",
        "properties": {
            "user_id": _prop("X user ID"),
        },
        "required": ["user_id"],
    },
    "get_user_timeline": {
        "description": "Get a user's recent tweets by user ID",
        "properties": {
            "user_id": _prop("X user ID"),
            "max_results": _prop("Max tweets to return (5-100, default: 10)", "integer"),
            "pagination_token": _prop("Pagination token for next page"),
        },
        "required": ["user_id"],
    },
    "get_my_timeline": {
        "description": "Get the authenticated user's own recent tweets",
        "properties": {
            "max_results": _prop("Max tweets to return (5-100, default: 10)", "integer"),
        },
        "required": [],
    },
    "search_recent": {
        "description": "Search recent tweets from the last 7 days",
        "properties": {
            "query": _prop("Search query (X search syntax — e.g. 'from:elonmusk', '#AI', 'real estate lang:en')"),
            "max_results": _prop("Max tweets to return (10-100, default: 10)", "integer"),
            "pagination_token": _prop("Pagination token for next page"),
        },
        "required": ["query"],
    },
    "get_tweet": {
        "description": "Get a single tweet by its ID",
        "properties": {
            "tweet_id": _prop("Tweet ID"),
        },
        "required": ["tweet_id"],
    },
    "get_tweet_metrics": {
        "description": "Get public and non-public metrics for a tweet.",
        "properties": {
            "tweet_id": _prop("Tweet ID"),
        },
        "required": ["tweet_id"],
    },
    "create_tweet": {
        "description": "Create a new tweet.",
        "properties": {
            "text": _prop("The text content of the tweet."),
            "media_ids": {
                "type": "array",
                "description": "Media IDs to attach.",
                "items": {"type": "string"},
            },
            "poll_options": {
                "type": "array",
                "description": "Poll options (2-4 items).",
                "items": {"type": "string"},
                "minItems": 2,
                "maxItems": 4,
            },
            "poll_duration_minutes": _prop("Poll duration in minutes.", "integer"),
            "quote_tweet_id": _prop("Tweet ID to quote."),
        },
        "required": ["text"],
    },
    "comment_tweet": {
        "description": "Reply/comment on an existing tweet",
        "properties": {
            "tweet_id": _prop("Tweet ID to reply to"),
            "text": _prop("Reply/comment text (max 280 characters)"),
        },
        "required": ["tweet_id", "text"],
    },
    "create_thread": {
        "description": "Create a tweet thread by posting each text item as a reply to the previous one",
        "properties": {
            "texts": {
                "type": "array",
                "description": "Ordered tweet texts for the thread. Each item should be 280 characters or less.",
                "items": {"type": "string"},
                "minItems": 1,
                "maxItems": 25,
            },
            "reply_to": _prop("Optional tweet ID to attach the thread under an existing tweet"),
        },
        "required": ["texts"],
    },
    "delete_tweet": {
        "description": "Delete a tweet by ID (must be the authenticated user's tweet)",
        "properties": {
            "tweet_id": _prop("Tweet ID to delete"),
        },
        "required": ["tweet_id"],
    },
    "get_followers": {
        "description": "Get a user's followers",
        "properties": {
            "user_id": _prop("X user ID"),
            "max_results": _prop("Max results (1-1000, default: 20)", "integer"),
            "pagination_token": _prop("Pagination token for next page"),
        },
        "required": ["user_id"],
    },
    "get_following": {
        "description": "Get users that a user follows",
        "properties": {
            "user_id": _prop("X user ID"),
            "max_results": _prop("Max results (1-1000, default: 20)", "integer"),
            "pagination_token": _prop("Pagination token for next page"),
        },
        "required": ["user_id"],
    },
    "like_tweet": {
        "description": "Like a tweet",
        "properties": {
            "tweet_id": _prop("Tweet ID to like"),
        },
        "required": ["tweet_id"],
    },
    "unlike_tweet": {
        "description": "Unlike a previously liked tweet",
        "properties": {
            "tweet_id": _prop("Tweet ID to unlike"),
        },
        "required": ["tweet_id"],
    },
    "retweet": {
        "description": "Retweet a tweet",
        "properties": {
            "tweet_id": _prop("Tweet ID to retweet"),
        },
        "required": ["tweet_id"],
    },
    "unretweet": {
        "description": "Undo a retweet",
        "properties": {
            "tweet_id": _prop("Tweet ID to unretweet"),
        },
        "required": ["tweet_id"],
    },
    "follow_user": {
        "description": "Follow an X/Twitter user as the authenticated account. Provide target_user_id/user_id or username.",
        "properties": {
            "target_user_id": _prop("X user ID to follow"),
            "user_id": _prop("Alias for target_user_id"),
            "username": _prop("X username to resolve and follow (with or without @)"),
        },
        "required": [],
    },
    "unfollow_user": {
        "description": "Unfollow an X/Twitter user as the authenticated account. Provide target_user_id/user_id or username.",
        "properties": {
            "target_user_id": _prop("X user ID to unfollow"),
            "user_id": _prop("Alias for target_user_id"),
            "username": _prop("X username to resolve and unfollow (with or without @)"),
        },
        "required": [],
    },
    "get_liking_users": {
        "description": "Get users who liked a specific tweet",
        "properties": {
            "tweet_id": _prop("Tweet ID"),
        },
        "required": ["tweet_id"],
    },
    "get_mentions": {
        "description": "Get recent tweets that mention a user",
        "properties": {
            "user_id": _prop("X user ID"),
            "max_results": _prop("Max tweets (5-100, default: 10)", "integer"),
        },
        "required": ["user_id"],
    },
}

_HANDLERS = {
    "get_me": _get_me,
    "get_user": _get_user,
    "search_users": _search_users,
    "get_user_by_id": _get_user_by_id,
    "get_user_timeline": _get_user_timeline,
    "get_my_timeline": _get_my_timeline,
    "search_recent": _search_recent,
    "get_tweet": _get_tweet,
    "get_tweet_metrics": _get_tweet_metrics,
    "create_tweet": _create_tweet,
    "comment_tweet": _comment_tweet,
    "create_thread": _create_thread,
    "delete_tweet": _delete_tweet,
    "get_followers": _get_followers,
    "get_following": _get_following,
    "like_tweet": _like_tweet,
    "unlike_tweet": _unlike_tweet,
    "retweet": _retweet,
    "unretweet": _unretweet,
    "follow_user": _follow_user,
    "unfollow_user": _unfollow_user,
    "get_liking_users": _get_liking_users,
    "get_mentions": _get_mentions,
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
