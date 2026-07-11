"""
LinkedIn MCP server — in-process MCP for LinkedIn REST API.

Scopes used:
  - w_member_social:        create/delete posts, comments, reactions, media
  - openid + profile:       get person URN (required for authoring posts)
  - email:                  get user email
  - r_organization_admin:   list orgs the user can administer
  - r_organization_social:  read posts/insights for those orgs
  - w_organization_social:  publish on behalf of an org page

The org-* scopes require LinkedIn's "Community Management API" partner
program approval. They will be ignored at OAuth-consent time until the
LinkedIn app has been allow-listed.

Auth: Bearer token = LinkedIn OAuth access_token (from entity integration config).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)

from packages.core.external_api_versions import LINKEDIN as _LINKEDIN_PIN

_API = "https://api.linkedin.com"
_MAX_CHARS = 12_000
# LinkedIn-Version follows YYYYMM; centralized so CI can flag it when
# stale. Bump in packages/core/external_api_versions.py.
_VERSION = _LINKEDIN_PIN.value


# ── MCP Protocol ─────────────────────────────────────────────────────────────

def list_tools() -> List[Dict[str, Any]]:
    return [_tool_def(name, spec) for name, spec in _TOOLS.items()]


async def call_tool(
    name: str,
    arguments: Dict[str, Any],
    bearer_token: str,
) -> Dict[str, Any]:
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
        logger.exception("LinkedIn MCP tool %s failed", name)
        return _error(str(e))


from packages.core.ai.mcp._http import mcp_err as _error  # noqa: E402, F401


# ── LinkedIn API client ───────────────────────────────────────────────────────

async def _api(
    token: str,
    method: str,
    path: str,
    body: Optional[Dict] = None,
    use_rest: bool = True,
) -> str:
    """Call LinkedIn API. use_rest=True for /rest/ endpoints, False for /v2/."""
    nango_ref = _parse_nango_ref(token)
    prefix = f"{_API}/rest" if use_rest else f"{_API}/v2"
    url = f"{prefix}/{path.lstrip('/')}" if not path.startswith("http") else path

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }
    if use_rest:
        headers["LinkedIn-Version"] = _VERSION
        headers["X-Restli-Protocol-Version"] = "2.0.0"
    if method in ("POST", "PATCH"):
        headers["Content-Type"] = "application/json"

    async with httpx.AsyncClient(timeout=20.0) as client:
        if nango_ref:
            resp = await _request_via_nango(
                client,
                nango_ref=nango_ref,
                method=method,
                path=path,
                body=body,
                use_rest=use_rest,
            )
        elif method == "GET":
            resp = await client.get(url, headers=headers)
        elif method == "POST":
            resp = await client.post(url, headers=headers, json=body or {})
        elif method == "DELETE":
            resp = await client.delete(url, headers=headers)
        else:
            resp = await client.request(method, url, headers=headers, json=body)

    if resp.status_code == 401:
        raise RuntimeError("LinkedIn authentication failed. Reconnect LinkedIn on the Integration page.")
    if resp.status_code == 403:
        raise RuntimeError(f"LinkedIn forbidden (scope or permissions): {resp.text[:300]}")
    if resp.status_code == 404:
        raise RuntimeError("Not found.")
    if resp.status_code == 201:
        # Created — extract URN from header
        urn = resp.headers.get("x-restli-id", "")
        return json.dumps({"created": True, "urn": urn}, indent=2)
    if resp.status_code == 204:
        return json.dumps({"success": True})
    if not resp.is_success:
        raise RuntimeError(f"LinkedIn API error ({resp.status_code}): {resp.text[:300]}")

    try:
        data = resp.json()
    except Exception:
        return resp.text[:_MAX_CHARS]

    out = json.dumps(data, ensure_ascii=False, indent=2, default=str)
    if len(out) > _MAX_CHARS:
        return out[:_MAX_CHARS] + "\n… (truncated)"
    return out


def _parse_nango_ref(token: str) -> Optional[Dict[str, str]]:
    """Return a Nango connection reference encoded by mcp_builtin, if present."""
    if not token or not token.lstrip().startswith("{"):
        return None
    try:
        data = json.loads(token)
    except Exception:
        return None
    if data.get("via") != "nango":
        return None
    provider_config_key = (data.get("provider_config_key") or "").strip()
    connection_id = (data.get("connection_id") or "").strip()
    if not provider_config_key or not connection_id:
        return None
    return {
        "provider_config_key": provider_config_key,
        "connection_id": connection_id,
    }


async def _request_via_nango(
    client: httpx.AsyncClient,
    *,
    nango_ref: Dict[str, str],
    method: str,
    path: str,
    body: Optional[Dict],
    use_rest: bool,
) -> httpx.Response:
    """Call LinkedIn through Nango proxy so Manor never handles user tokens."""
    secret = (os.environ.get("NANGO_SECRET_KEY") or "").strip()
    if not secret:
        raise RuntimeError("NANGO_SECRET_KEY is not configured for LinkedIn proxy calls")

    from packages.core.ai.mcp.nango import _NANGO_BASE

    if path.startswith("http"):
        endpoint = path
    else:
        endpoint = f"{'rest' if use_rest else 'v2'}/{path.lstrip('/')}"

    headers = {
        "Authorization": f"Bearer {secret}",
        "Provider-Config-Key": nango_ref["provider_config_key"],
        "Connection-Id": nango_ref["connection_id"],
        "Nango-Proxy-Accept": "application/json",
    }
    if use_rest:
        headers["Nango-Proxy-LinkedIn-Version"] = _VERSION
        headers["Nango-Proxy-X-Restli-Protocol-Version"] = "2.0.0"
    if method in ("POST", "PATCH"):
        headers["Nango-Proxy-Content-Type"] = "application/json"

    request_body = (body or {}) if method in ("POST", "PUT", "PATCH") else None
    return await client.request(
        method,
        f"{_NANGO_BASE}/proxy/{endpoint.lstrip('/')}",
        headers=headers,
        json=request_body,
    )


# ── Tool handlers ─────────────────────────────────────────────────────────────

async def _get_profile(token: str, args: Dict) -> str:
    """Get authenticated user's profile (OpenID userinfo)."""
    return await _api(token, "GET", "userinfo", use_rest=False)


async def _get_person_id(token: str, args: Dict) -> str:
    """Get the person URN needed for authoring posts."""
    return await _api(token, "GET", "userinfo", use_rest=False)


async def _create_post(token: str, args: Dict) -> str:
    """Create a post. Supports text, article link, single image/video/document,
    or multi-image. Mutually-exclusive content options — first match wins."""
    author = args["author_urn"]
    body: Dict[str, Any] = {
        "author": author,
        "commentary": args["text"],
        "visibility": args.get("visibility", "PUBLIC"),
        "distribution": {
            "feedDistribution": "MAIN_FEED",
            "targetEntities": [],
            "thirdPartyDistributionChannels": [],
        },
        "lifecycleState": "PUBLISHED",
    }
    if args.get("article_url"):
        body["content"] = {
            "article": {
                "source": args["article_url"],
                "title": args.get("article_title", ""),
                "description": args.get("article_description", ""),
            }
        }
    elif args.get("image_urn"):
        body["content"] = {
            "media": {
                "id": args["image_urn"],
                "altText": args.get("alt_text", "") or args["text"][:300],
            }
        }
    elif args.get("video_urn"):
        body["content"] = {
            "media": {
                "id": args["video_urn"],
                "altText": args.get("alt_text", "") or args["text"][:300],
            }
        }
    elif args.get("document_urn"):
        body["content"] = {
            "media": {
                "id": args["document_urn"],
                "title": args.get("document_title", "Document"),
                "altText": args.get("alt_text", "") or args["text"][:300],
            }
        }
    elif args.get("image_urns"):
        urns = args["image_urns"] or []
        if not (2 <= len(urns) <= 20):
            raise RuntimeError("image_urns must contain 2-20 image URNs")
        alt = args.get("alt_text", "") or args["text"][:300]
        body["content"] = {
            "multiImage": {
                "images": [{"id": u, "altText": alt} for u in urns],
            }
        }
    return await _api(token, "POST", "posts", body)


# ── Media upload (image / video / document) ──────────────────────────────

def _format_err(msg: str) -> str:
    return json.dumps({"error": msg}, indent=2)


async def _read_bytes(src: str) -> bytes:
    """Fetch bytes from an HTTPS URL or read from a local path.
    Local paths are typically /mnt/manor/... but any abs path works."""
    src = (src or "").strip()
    if src.startswith("http://") or src.startswith("https://"):
        async with httpx.AsyncClient(timeout=120.0) as client:
            r = await client.get(src)
            r.raise_for_status()
            return r.content
    p = Path(src)
    if not (p.exists() and p.is_file()):
        raise RuntimeError(f"Local file not found or not a file: {src}")
    return p.read_bytes()


async def _put_binary(url: str, content: bytes, *, content_type: Optional[str] = None) -> Tuple[int, str, Dict[str, str]]:
    """PUT bytes to a LinkedIn-issued signed upload URL. The signed URL
    embeds its own auth — do NOT add Authorization headers here."""
    headers: Dict[str, str] = {}
    if content_type:
        headers["Content-Type"] = content_type
    async with httpx.AsyncClient(timeout=300.0) as client:
        r = await client.put(url, content=content, headers=headers)
    return r.status_code, r.text[:300], dict(r.headers)


async def _upload_image(token: str, args: Dict) -> str:
    return await _upload_media(token, args, kind="image")


async def _upload_video(token: str, args: Dict) -> str:
    return await _upload_media(token, args, kind="video")


async def _upload_document(token: str, args: Dict) -> str:
    return await _upload_media(token, args, kind="document")


async def _upload_media(token: str, args: Dict, *, kind: str) -> str:
    """Two-step upload: initializeUpload → PUT bytes (single or multipart) → return URN.

    LinkedIn returns ``urn:li:image:...`` / ``urn:li:video:...`` /
    ``urn:li:document:...`` which can then be passed to ``create_post``
    via ``image_urn`` / ``video_urn`` / ``document_urn``.
    """
    owner = args["owner_urn"]
    src = args["src"]
    try:
        data = await _read_bytes(src)
    except Exception as exc:
        raise RuntimeError(f"failed to read source: {exc}")
    size = len(data)
    if size == 0:
        raise RuntimeError("source is empty")

    init_req: Dict[str, Any] = {"owner": owner}
    if kind == "video":
        init_req["fileSizeBytes"] = size
        init_req["uploadCaptions"] = False
        init_req["uploadThumbnail"] = False

    init_path = f"{kind}s?action=initializeUpload"
    init_resp = await _api(token, "POST", init_path, {"initializeUploadRequest": init_req})

    parsed = _parse_json_or_none(init_resp)
    if not parsed:
        raise RuntimeError(f"initializeUpload failed: {init_resp[:300]}")
    value = parsed.get("value") or {}
    media_urn = value.get(kind)
    if not media_urn:
        raise RuntimeError(f"initializeUpload returned no urn: {init_resp[:300]}")

    upload_url = value.get("uploadUrl")
    if upload_url:
        # Single-part upload (image / document / small video).
        status, text, _hdrs = await _put_binary(upload_url, data)
        if status >= 400:
            raise RuntimeError(f"binary upload failed ({status}): {text}")
        return json.dumps({"urn": media_urn, "size": size}, indent=2)

    # Multipart video upload.
    instructions = value.get("uploadInstructions") or []
    upload_token = value.get("uploadToken", "")
    if not instructions:
        raise RuntimeError(f"unexpected initializeUpload response: {init_resp[:300]}")

    etags: List[str] = []
    for ins in instructions:
        first = int(ins.get("firstByte", 0))
        last = int(ins.get("lastByte", size - 1))
        chunk = data[first : last + 1]
        status, text, hdrs = await _put_binary(ins["uploadUrl"], chunk)
        if status >= 400:
            raise RuntimeError(f"chunk upload failed ({status}): {text}")
        etag = hdrs.get("ETag") or hdrs.get("etag") or ""
        etags.append(etag.strip('"'))

    if kind == "video":
        finalize_body = {
            "finalizeUploadRequest": {
                "video": media_urn,
                "uploadToken": upload_token,
                "uploadedPartIds": etags,
            }
        }
        fin_resp = await _api(token, "POST", "videos?action=finalizeUpload", finalize_body)
        if "error" in fin_resp.lower() and "finaliz" in fin_resp.lower():
            raise RuntimeError(f"finalizeUpload failed: {fin_resp[:300]}")

    return json.dumps({"urn": media_urn, "size": size, "parts": len(etags)}, indent=2)


def _parse_json_or_none(raw: str) -> Optional[Dict[str, Any]]:
    s = (raw or "").lstrip()
    if not s.startswith("{"):
        return None
    try:
        return json.loads(s)
    except Exception:
        return None


# ── Organizations (Company Pages) ────────────────────────────────────────

async def _list_organizations(token: str, args: Dict) -> str:
    """List orgs where the user is an ADMINISTRATOR (or other role).
    Use the returned urn:li:organization:{id} as ``author_urn`` in
    ``create_post`` to publish on the company page (needs
    w_organization_social)."""
    role = (args.get("role") or "ADMINISTRATOR").upper()
    return await _api(
        token, "GET",
        f"organizationAcls?q=roleAssignee&role={quote(role, safe='')}&state=APPROVED",
    )


async def _get_organization(token: str, args: Dict) -> str:
    """Fetch a company page's profile (name, vanity url, logo, etc.)."""
    org_id = str(args["org_id"])
    if org_id.startswith("urn:li:organization:"):
        org_id = org_id.rsplit(":", 1)[-1]
    return await _api(token, "GET", f"organizations/{org_id}")


async def _list_org_posts(token: str, args: Dict) -> str:
    """List recent posts published by an organization page."""
    org_urn = args["org_urn"]
    if not org_urn.startswith("urn:li:organization:"):
        org_urn = f"urn:li:organization:{org_urn}"
    count = int(args.get("count", 10))
    return await _api(
        token, "GET",
        f"posts?author={quote(org_urn, safe='')}&q=author&count={count}&start=0",
    )


# ── Content analysis — aggregate engagement on user's recent posts ───────

async def _get_post_stats(token: str, args: Dict) -> str:
    """Pull the user's last N posts and aggregate per-post like + comment
    totals. Useful for surfacing top-performing content for an agent that
    decides what to post next.

    Cost: 1 + 2N API calls (posts list + per-post comments + likes).
    Default N=10 keeps it under 25 calls."""
    author = args["author_urn"]
    count = max(1, min(25, int(args.get("count", 10))))

    posts_raw = await _api(
        token, "GET",
        f"posts?author={quote(author, safe='')}&q=author&count={count}&start=0",
    )
    parsed = _parse_json_or_none(posts_raw)
    if not parsed:
        return posts_raw
    elements = parsed.get("elements") or []

    async def _stats_for(urn: str) -> Tuple[Optional[int], Optional[int]]:
        urn_q = quote(urn, safe="")
        c_raw, l_raw = await asyncio.gather(
            _api(token, "GET", f"socialActions/{urn_q}/comments?count=1&start=0"),
            _api(token, "GET", f"socialActions/{urn_q}/likes?count=1&start=0"),
        )
        c_data = _parse_json_or_none(c_raw) or {}
        l_data = _parse_json_or_none(l_raw) or {}
        return (
            (c_data.get("paging") or {}).get("total"),
            (l_data.get("paging") or {}).get("total"),
        )

    rows: List[Dict[str, Any]] = []
    for p in elements:
        urn = p.get("id") or p.get("urn")
        if not urn:
            continue
        comments_total, likes_total = await _stats_for(urn)
        rows.append({
            "urn": urn,
            "commentary_excerpt": (p.get("commentary") or "")[:240],
            "created_at": p.get("createdAt"),
            "comment_count": comments_total,
            "like_count": likes_total,
            "engagement_total": (comments_total or 0) + (likes_total or 0),
        })
    rows.sort(key=lambda r: r.get("engagement_total") or 0, reverse=True)
    return json.dumps(
        {"author": author, "count": len(rows), "posts": rows},
        ensure_ascii=False,
        indent=2,
    )


async def _delete_post(token: str, args: Dict) -> str:
    """Delete a post by URN."""
    urn = quote(args["post_urn"], safe="")
    return await _api(token, "DELETE", f"posts/{urn}")


async def _get_my_posts(token: str, args: Dict) -> str:
    """Get the authenticated user's own posts."""
    author = quote(args["author_urn"], safe="")
    count = int(args.get("count", 10))
    return await _api(token, "GET", f"posts?author={author}&q=author&count={count}&start=0")


async def _create_comment(token: str, args: Dict) -> str:
    """Add a comment to a post."""
    post_urn = quote(args["post_urn"], safe="")
    body = {
        "actor": args["actor_urn"],
        "message": {"text": args["text"]},
    }
    return await _api(token, "POST", f"socialActions/{post_urn}/comments", body)


async def _delete_comment(token: str, args: Dict) -> str:
    """Delete a comment."""
    post_urn = quote(args["post_urn"], safe="")
    comment_id = args["comment_id"]
    return await _api(token, "DELETE", f"socialActions/{post_urn}/comments/{comment_id}")


async def _react_to_post(token: str, args: Dict) -> str:
    """React to a post (LIKE, PRAISE, APPRECIATION, EMPATHY, INTEREST, ENTERTAINMENT)."""
    body = {
        "root": args["post_urn"],
        "reactionType": args.get("reaction_type", "LIKE").upper(),
        "actor": args["actor_urn"],
    }
    return await _api(token, "POST", "reactions", body)


async def _remove_reaction(token: str, args: Dict) -> str:
    """Remove a reaction from a post."""
    actor = quote(args["actor_urn"], safe="")
    entity = quote(args["post_urn"], safe="")
    return await _api(token, "DELETE", f"reactions/(actor:{actor},entity:{entity})")


async def _get_post_comments(token: str, args: Dict) -> str:
    """Get comments on a post."""
    post_urn = quote(args["post_urn"], safe="")
    count = int(args.get("count", 20))
    return await _api(token, "GET", f"socialActions/{post_urn}/comments?start=0&count={count}")


async def _get_post_reactions(token: str, args: Dict) -> str:
    """Get reactions on a post."""
    post_urn = quote(args["post_urn"], safe="")
    count = int(args.get("count", 20))
    return await _api(token, "GET", f"socialActions/{post_urn}/likes?start=0&count={count}")


# ── Tool definitions ──────────────────────────────────────────────────────────

def _prop(desc: str, type_: str = "string") -> Dict[str, str]:
    return {"type": type_, "description": desc}


_TOOLS: Dict[str, Dict[str, Any]] = {
    "get_profile": {
        "description": "Get the authenticated LinkedIn user's profile (name, picture, email, sub/person ID)",
        "properties": {},
        "required": [],
    },
    "create_post": {
        "description": (
            "Create a LinkedIn post — text-only, or with one of: article "
            "link, single image (image_urn), single video (video_urn), "
            "single document (document_urn), or 2-20 images "
            "(image_urns). Media URNs come from upload_image / "
            "upload_video / upload_document. To post on a company page, "
            "set author_urn to urn:li:organization:{id} (requires "
            "w_organization_social)."
        ),
        "properties": {
            "author_urn": _prop("urn:li:person:{id} (member) or urn:li:organization:{id} (page)"),
            "text": _prop("Post text / commentary"),
            "visibility": _prop("PUBLIC or CONNECTIONS (default: PUBLIC)"),
            "article_url": _prop("Optional article/link URL to share"),
            "article_title": _prop("Optional article title"),
            "article_description": _prop("Optional article description"),
            "image_urn": _prop("Optional urn:li:image:... from upload_image"),
            "video_urn": _prop("Optional urn:li:video:... from upload_video"),
            "document_urn": _prop("Optional urn:li:document:... from upload_document"),
            "document_title": _prop("Document title (used when document_urn is set)"),
            "image_urns": {
                "type": "array",
                "description": "2-20 image URNs for a multi-image post",
                "items": {"type": "string"},
            },
            "alt_text": _prop("Accessibility alt text for media (recommended)"),
        },
        "required": ["author_urn", "text"],
    },
    "upload_image": {
        "description": (
            "Upload an image to LinkedIn and return its URN. Pass that "
            "URN to create_post via image_urn (or image_urns for "
            "multi-image). Source can be an HTTPS URL or a local path "
            "(typically /mnt/manor/...)."
        ),
        "properties": {
            "owner_urn": _prop("urn:li:person:{id} or urn:li:organization:{id}"),
            "src": _prop("HTTPS URL or local file path"),
        },
        "required": ["owner_urn", "src"],
    },
    "upload_video": {
        "description": (
            "Upload a video to LinkedIn (handles single-part and "
            "multipart automatically) and return its URN. Pass to "
            "create_post via video_urn. Source can be HTTPS URL or "
            "local path."
        ),
        "properties": {
            "owner_urn": _prop("urn:li:person:{id} or urn:li:organization:{id}"),
            "src": _prop("HTTPS URL or local file path (e.g. /mnt/manor/.../clip.mp4)"),
        },
        "required": ["owner_urn", "src"],
    },
    "upload_document": {
        "description": (
            "Upload a document (PDF, PPTX, DOCX) to LinkedIn and return "
            "its URN. Pass to create_post via document_urn (set "
            "document_title for the slide-deck card title)."
        ),
        "properties": {
            "owner_urn": _prop("urn:li:person:{id} or urn:li:organization:{id}"),
            "src": _prop("HTTPS URL or local file path"),
        },
        "required": ["owner_urn", "src"],
    },
    "list_organizations": {
        "description": (
            "List company pages where the connected user holds the "
            "given role (default ADMINISTRATOR). Use the returned "
            "urn:li:organization:{id} as author_urn in create_post to "
            "publish on that page. Requires r_organization_admin."
        ),
        "properties": {
            "role": _prop("ADMINISTRATOR (default), DIRECT_SPONSORED_CONTENT_POSTER, RECRUITING_POSTER, …"),
        },
        "required": [],
    },
    "get_organization": {
        "description": "Fetch a company page's profile (name, vanity url, logo, follower count).",
        "properties": {
            "org_id": _prop("Numeric org id, or urn:li:organization:{id}"),
        },
        "required": ["org_id"],
    },
    "list_org_posts": {
        "description": "List recent posts published by an organization (company page).",
        "properties": {
            "org_urn": _prop("urn:li:organization:{id} or just the numeric id"),
            "count": _prop("Max results (default: 10)", "integer"),
        },
        "required": ["org_urn"],
    },
    "get_post_stats": {
        "description": (
            "Pull the user's most recent N posts and aggregate per-post "
            "engagement (likes + comments). Returns rows sorted by "
            "engagement_total desc — useful for an agent deciding what "
            "topic / format performed best before drafting a new post. "
            "Costs 1 + 2N API calls; default N=10."
        ),
        "properties": {
            "author_urn": _prop("urn:li:person:{id} or urn:li:organization:{id}"),
            "count": _prop("Posts to analyze (1-25, default 10)", "integer"),
        },
        "required": ["author_urn"],
    },
    "delete_post": {
        "description": "Delete a LinkedIn post",
        "properties": {"post_urn": _prop("Post URN (urn:li:share:{id})")},
        "required": ["post_urn"],
    },
    "get_my_posts": {
        "description": "Get the authenticated user's own LinkedIn posts",
        "properties": {
            "author_urn": _prop("Author URN (urn:li:person:{id})"),
            "count": _prop("Max results (default: 10)", "integer"),
        },
        "required": ["author_urn"],
    },
    "create_comment": {
        "description": "Add a comment to a LinkedIn post",
        "properties": {
            "post_urn": _prop("Post URN to comment on"),
            "actor_urn": _prop("Your person URN (urn:li:person:{id})"),
            "text": _prop("Comment text"),
        },
        "required": ["post_urn", "actor_urn", "text"],
    },
    "delete_comment": {
        "description": "Delete a comment from a LinkedIn post",
        "properties": {
            "post_urn": _prop("Post URN"),
            "comment_id": _prop("Comment ID to delete"),
        },
        "required": ["post_urn", "comment_id"],
    },
    "react_to_post": {
        "description": "React to a LinkedIn post (like, praise, etc.)",
        "properties": {
            "post_urn": _prop("Post URN to react to"),
            "actor_urn": _prop("Your person URN"),
            "reaction_type": _prop("LIKE, PRAISE, APPRECIATION, EMPATHY, INTEREST, or ENTERTAINMENT (default: LIKE)"),
        },
        "required": ["post_urn", "actor_urn"],
    },
    "remove_reaction": {
        "description": "Remove your reaction from a LinkedIn post",
        "properties": {
            "actor_urn": _prop("Your person URN"),
            "post_urn": _prop("Post URN"),
        },
        "required": ["actor_urn", "post_urn"],
    },
    "get_post_comments": {
        "description": "Get comments on a LinkedIn post",
        "properties": {
            "post_urn": _prop("Post URN"),
            "count": _prop("Max results (default: 20)", "integer"),
        },
        "required": ["post_urn"],
    },
    "get_post_reactions": {
        "description": "Get reactions/likes on a LinkedIn post",
        "properties": {
            "post_urn": _prop("Post URN"),
            "count": _prop("Max results (default: 20)", "integer"),
        },
        "required": ["post_urn"],
    },
}

_HANDLERS = {
    "get_profile": _get_profile,
    "create_post": _create_post,
    "delete_post": _delete_post,
    "get_my_posts": _get_my_posts,
    "create_comment": _create_comment,
    "delete_comment": _delete_comment,
    "react_to_post": _react_to_post,
    "remove_reaction": _remove_reaction,
    "get_post_comments": _get_post_comments,
    "get_post_reactions": _get_post_reactions,
    # Media uploads (PR1)
    "upload_image": _upload_image,
    "upload_video": _upload_video,
    "upload_document": _upload_document,
    # Organization (Company Page) ops (PR1)
    "list_organizations": _list_organizations,
    "get_organization": _get_organization,
    "list_org_posts": _list_org_posts,
    # Content analysis (PR1)
    "get_post_stats": _get_post_stats,
}


def _tool_def(name: str, spec: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "name": name,
        "description": spec["description"],
        "inputSchema": {
            "type": "object",
            "properties": spec.get("properties", {}),
            "required": spec.get("required", []),
        },
    }
