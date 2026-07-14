"""Built-in MCP server tool catalog.

Registers the 8 seeded MCP servers (gmail, google_calendar, google_drive,
linkedin, github, twitter_x, quickbooks, stripe) into the tool pool with
curated tool schemas, so agents can discover them via ``search_tools``
without needing the actual MCP HTTP servers running.

Naming: every tool is ``mcp__<server_key>__<tool_name>`` — same convention
Claude Code uses.

Handler contract (mirrors Claude Code's approach):
  * At call time, resolve credentials via
    ``agent_permission_service.can_use_integration``.
  * If the user hasn't connected that integration (or lacks
    ``Integration.required_permission``), return a friendly
    "connect this integration" message — the LLM surfaces it to the user.
  * Otherwise, dispatch to the provider-specific client. Until per-provider
    HTTP/builtin MCP handlers are implemented, the handler returns an
    "integration wired but call path pending" placeholder so the tool is
    visibly bound.

All MCP tools are **deferred by default** — schema is loaded on demand via
``search_tools``. Claude Code follows the same rule to keep session-start
context small. Agents see the tool name in the deferred list, request the
schema when they want to use it, and then call.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Callable, Optional
from urllib.parse import urlparse

from packages.core.ai.runtime.tool_context import (
    RUNTIME_TOOL_CONTEXT_KEYS,
    runtime_active_user_message_from_context,
    runtime_tool_call_context_from_kwargs,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Curated tool schemas — one dict per MCP server. Ported from the
# manor-multi-agent in-process MCP modules' list_tools() contracts.
# Keep to the highest-value operations per provider; full lists can be
# expanded later once real MCP servers are attached.
# ---------------------------------------------------------------------------

_SERVER_TOOL_SCHEMAS: dict[str, list[dict]] = {
    # ``gmail`` / ``google_calendar`` / ``google_drive`` are populated
    # below via ``_adapt_module_tools`` from each module's
    # ``list_tools()`` — single source of truth, no drift between this
    # catalogue and the actual handlers.
    "linkedin": [
        {"name": "create_post", "description": "Create a LinkedIn post on the authenticated user's profile.",
         "parameters": {"type": "object", "required": ["text"],
                        "properties": {"text": {"type": "string"},
                                       "visibility": {"type": "string", "enum": ["PUBLIC", "CONNECTIONS"]}}}},
        {"name": "get_profile", "description": "Get the authenticated user's LinkedIn profile.",
         "parameters": {"type": "object", "properties": {}}},
        {"name": "comment_on_post", "description": "Comment on a LinkedIn post.",
         "parameters": {"type": "object", "required": ["post_urn", "text"],
                        "properties": {"post_urn": {"type": "string"}, "text": {"type": "string"}}}},
    ],
    # ``github`` is populated below from packages.core.ai.mcp.github.list_tools()
    # — single source of truth for the ~50 tools in that module. Done after
    # _SERVER_TOOL_SCHEMAS is defined to keep the dict literal scannable.
    "twitter_x": [
        {"name": "post_tweet", "description": "Post a tweet.",
         "parameters": {"type": "object", "required": ["text"],
                        "properties": {"text": {"type": "string"},
                                       "reply_to_tweet_id": {"type": "string"}}}},
        {"name": "delete_tweet", "description": "Delete a tweet by ID.",
         "parameters": {"type": "object", "required": ["tweet_id"],
                        "properties": {"tweet_id": {"type": "string"}}}},
        {"name": "search_tweets", "description": "Search recent tweets.",
         "parameters": {"type": "object", "required": ["query"],
                        "properties": {"query": {"type": "string"},
                                       "max_results": {"type": "integer", "default": 10}}}},
        {"name": "get_user", "description": "Get a user profile by handle.",
         "parameters": {"type": "object", "required": ["username"],
                        "properties": {"username": {"type": "string"}}}},
    ],
    "quickbooks": [
        {"name": "list_customers", "description": "List QuickBooks customers.",
         "parameters": {"type": "object",
                        "properties": {"max_results": {"type": "integer", "default": 50}}}},
        {"name": "get_customer", "description": "Get a customer by ID.",
         "parameters": {"type": "object", "required": ["customer_id"],
                        "properties": {"customer_id": {"type": "string"}}}},
        {"name": "list_invoices", "description": "List invoices.",
         "parameters": {"type": "object",
                        "properties": {"status": {"type": "string"},
                                       "max_results": {"type": "integer", "default": 50}}}},
        {"name": "create_invoice", "description": "Create an invoice for a customer.",
         "parameters": {"type": "object", "required": ["customer_id", "line_items"],
                        "properties": {"customer_id": {"type": "string"},
                                       "line_items": {"type": "array",
                                                       "items": {"type": "object"}}}}},
    ],
    # ``stripe`` is now a remote MCP server (transport=http, OAuth).
    # Tools come from mcp.stripe.com via tools/list at agent runtime —
    # no static schemas registered here. The legacy in-process module
    # at packages/core/ai/mcp/stripe.py is retained on disk for
    # backward import compatibility but no longer dispatched.
    "discord": [
        {"name": "send_message", "description": "Send a message to a Discord channel.",
         "parameters": {"type": "object", "required": ["channel_id", "content"],
                        "properties": {"channel_id": {"type": "string"},
                                       "content": {"type": "string"},
                                       "tts": {"type": "boolean", "default": False}}}},
        {"name": "list_channels", "description": "List channels in a Discord guild (server).",
         "parameters": {"type": "object", "required": ["guild_id"],
                        "properties": {"guild_id": {"type": "string"}}}},
        {"name": "get_channel_messages", "description": "Fetch recent messages from a channel.",
         "parameters": {"type": "object", "required": ["channel_id"],
                        "properties": {"channel_id": {"type": "string"},
                                       "limit": {"type": "integer", "default": 50}}}},
        {"name": "add_reaction", "description": "Add a reaction emoji to a message.",
         "parameters": {"type": "object", "required": ["channel_id", "message_id", "emoji"],
                        "properties": {"channel_id": {"type": "string"},
                                       "message_id": {"type": "string"},
                                       "emoji": {"type": "string",
                                                 "description": "URL-encoded unicode emoji or 'name:id' for custom"}}}},
    ],
    "telegram": [
        {"name": "send_message", "description": "Send a text message to a Telegram chat.",
         "parameters": {"type": "object", "required": ["chat_id", "text"],
                        "properties": {"chat_id": {"type": "string",
                                                   "description": "Numeric chat id or @username"},
                                       "text": {"type": "string"},
                                       "parse_mode": {"type": "string",
                                                      "enum": ["Markdown", "MarkdownV2", "HTML"]}}}},
        {"name": "send_photo", "description": "Send a photo by URL or file_id.",
         "parameters": {"type": "object", "required": ["chat_id", "photo"],
                        "properties": {"chat_id": {"type": "string"},
                                       "photo": {"type": "string",
                                                 "description": "Public URL or previously-uploaded file_id."},
                                       "caption": {"type": "string"}}}},
        {"name": "send_document", "description": "Send a document/file.",
         "parameters": {"type": "object", "required": ["chat_id", "document"],
                        "properties": {"chat_id": {"type": "string"},
                                       "document": {"type": "string"},
                                       "caption": {"type": "string"}}}},
        {"name": "get_updates", "description": "Pull recent bot updates (polling; use webhooks for prod).",
         "parameters": {"type": "object",
                        "properties": {"offset": {"type": "integer"},
                                       "limit": {"type": "integer", "default": 100}}}},
    ],
    # Personal WeChat bot — via QR-login bot runner (no AppID/Secret).
    "wechat_personal": [
        {"name": "list_groups",
         "description": "List recently-seen WeChat group peers the bot can reply to.",
         "parameters": {"type": "object", "properties": {}}},
        {"name": "list_contacts",
         "description": "List recently-seen 1:1 WeChat peers the bot can reply to.",
         "parameters": {"type": "object", "properties": {}}},
        {"name": "send_group_message",
         "description": "Reply with text to a WeChat group that messaged the bot recently.",
         "parameters": {"type": "object", "required": ["group_id", "content"],
                        "properties": {"group_id": {"type": "string"},
                                       "content": {"type": "string"}}}},
        {"name": "send_direct_message",
         "description": "Reply with text to a WeChat contact that messaged the bot recently.",
         "parameters": {"type": "object", "required": ["contact_id", "content"],
                        "properties": {"contact_id": {"type": "string"},
                                       "content": {"type": "string"}}}},
        {"name": "get_bot_status",
         "description": "Get this WeChat runner session status (online / QR pending / offline).",
         "parameters": {"type": "object",
                        "properties": {"group_id": {"type": "string",
                                                    "description": "Optional — filter to a specific bot instance"}}}},
        {"name": "get_qr_code",
         "description": "Get a fresh QR code URL from the runner so the user can (re)scan to log in.",
         "parameters": {"type": "object", "properties": {}}},
    ],
    # WeChat Official Account (公众号) — Tencent cgi-bin API.
    "wechat_official": [
        {"name": "send_text_message",
         "description": "Send a text customer-service message to a follower (within 48h of their last message).",
         "parameters": {"type": "object", "required": ["to_user", "content"],
                        "properties": {"to_user": {"type": "string",
                                                   "description": "Follower OpenID."},
                                       "content": {"type": "string"}}}},
        {"name": "send_image_message",
         "description": "Send an image customer-service message.",
         "parameters": {"type": "object", "required": ["to_user", "media_id"],
                        "properties": {"to_user": {"type": "string"},
                                       "media_id": {"type": "string",
                                                    "description": "ID from upload_media."}}}},
        {"name": "send_template_message",
         "description": "Send a pre-approved template message.",
         "parameters": {"type": "object", "required": ["to_user", "template_id", "data"],
                        "properties": {"to_user": {"type": "string"},
                                       "template_id": {"type": "string"},
                                       "url": {"type": "string"},
                                       "data": {"type": "object"}}}},
        {"name": "upload_media",
         "description": "Upload a temporary media file (image/voice/video/thumb). Returns media_id.",
         "parameters": {"type": "object", "required": ["media_type", "file_url"],
                        "properties": {"media_type": {"type": "string",
                                                      "enum": ["image", "voice", "video", "thumb"]},
                                       "file_url": {"type": "string"}}}},
        {"name": "get_follower_info",
         "description": "Fetch profile for a follower OpenID.",
         "parameters": {"type": "object", "required": ["open_id"],
                        "properties": {"open_id": {"type": "string"},
                                       "lang": {"type": "string"}}}},
        {"name": "list_followers",
         "description": "List follower OpenIDs (paginated).",
         "parameters": {"type": "object",
                        "properties": {"next_open_id": {"type": "string"}}}},
    ],
    "email": [
        {"name": "send_email", "description": "Send an email through the configured SMTP relay.",
         "parameters": {"type": "object", "required": ["to", "subject", "body"],
                        "properties": {"to": {"type": "string"},
                                       "subject": {"type": "string"},
                                       "body": {"type": "string"},
                                       "html": {"type": "string"},
                                       "cc": {"type": "string"},
                                       "bcc": {"type": "string"},
                                       "from_address": {"type": "string"},
                                       "reply_to": {"type": "string"}}}},
        {"name": "list_messages", "description": "List messages in an IMAP folder with optional filters.",
         "parameters": {"type": "object",
                        "properties": {"folder": {"type": "string", "default": "INBOX"},
                                       "unseen_only": {"type": "boolean"},
                                       "from_address": {"type": "string"},
                                       "subject_contains": {"type": "string"},
                                       "body_contains": {"type": "string"},
                                       "since": {"type": "string"},
                                       "before": {"type": "string"},
                                       "max_results": {"type": "integer", "default": 20}}}},
        {"name": "get_message", "description": "Fetch one message by UID with headers + body.",
         "parameters": {"type": "object", "required": ["uid"],
                        "properties": {"uid": {"type": "string"},
                                       "folder": {"type": "string", "default": "INBOX"},
                                       "format": {"type": "string", "enum": ["full", "text", "headers"]}}}},
        {"name": "mark_read", "description": "Mark a message as read.",
         "parameters": {"type": "object", "required": ["uid"],
                        "properties": {"uid": {"type": "string"},
                                       "folder": {"type": "string"}}}},
        {"name": "mark_unread", "description": "Mark a message as unread.",
         "parameters": {"type": "object", "required": ["uid"],
                        "properties": {"uid": {"type": "string"},
                                       "folder": {"type": "string"}}}},
        {"name": "move_message", "description": "Move a message to another folder.",
         "parameters": {"type": "object", "required": ["uid", "to_folder"],
                        "properties": {"uid": {"type": "string"},
                                       "from_folder": {"type": "string"},
                                       "to_folder": {"type": "string"}}}},
        {"name": "delete_message", "description": "Delete a message (flag Deleted + expunge).",
         "parameters": {"type": "object", "required": ["uid"],
                        "properties": {"uid": {"type": "string"},
                                       "folder": {"type": "string"}}}},
        {"name": "list_folders", "description": "List all IMAP folders.",
         "parameters": {"type": "object", "properties": {}}},
    ],

    # Nango — open-source self-hosted OAuth aggregator. Same UX as
    # Composio (one MCP server, hundreds of platforms) but free.
    "nango": [
        {"name": "nango_list_providers",
         "description": "List integrations configured on this Nango server.",
         "parameters": {"type": "object", "properties": {}}},
        {"name": "nango_list_connections",
         "description": "List active OAuth connections held for this entity.",
         "parameters": {"type": "object",
                        "properties": {"provider_config_key": {"type": "string"}}}},
        {"name": "nango_proxy",
         "description": "Make an authenticated HTTP call to a provider via Nango (handles OAuth + token refresh).",
         "parameters": {"type": "object",
                        "required": ["provider_config_key", "connection_id", "method", "endpoint"],
                        "properties": {
                            "provider_config_key": {"type": "string"},
                            "connection_id": {"type": "string"},
                            "method": {"enum": ["GET", "POST", "PUT", "PATCH", "DELETE"]},
                            "endpoint": {"type": "string"},
                            "params": {"type": "object"},
                            "headers": {"type": "object"},
                            "data": {"type": "object"}}}},
    ],


    "producthunt": [
        {"name": "search_posts",
         "description": "Search Product Hunt posts by topic, free-text, or launch date. Use for competitor research before a launch.",
         "parameters": {"type": "object",
                        "properties": {"topic": {"type": "string"},
                                       "url": {"type": "string"},
                                       "posted_after": {"type": "string"},
                                       "posted_before": {"type": "string"},
                                       "first": {"type": "integer"},
                                       "order": {"type": "string"}}}},
        {"name": "get_post",
         "description": "Fetch one Product Hunt post in detail by slug.",
         "parameters": {"type": "object", "required": ["slug"],
                        "properties": {"slug": {"type": "string"}}}},
        {"name": "daily_posts",
         "description": "Top posts launched on a specific day. Defaults to today UTC.",
         "parameters": {"type": "object",
                        "properties": {"day": {"type": "string"},
                                       "first": {"type": "integer"}}}},
        {"name": "list_comments",
         "description": "Get comments on a Product Hunt post (most recent first).",
         "parameters": {"type": "object", "required": ["slug"],
                        "properties": {"slug": {"type": "string"},
                                       "first": {"type": "integer"}}}},
        {"name": "post_comment",
         "description": "Leave a comment on a post as the authenticated user. Requires the OAuth token to have the 'private' scope.",
         "parameters": {"type": "object", "required": ["post_id", "body"],
                        "properties": {"post_id": {"type": "string"},
                                       "body": {"type": "string"},
                                       "parent_comment_id": {"type": "string"}}}},
        {"name": "me",
         "description": "Return the authenticated PH user — sanity check that the OAuth token works.",
         "parameters": {"type": "object", "properties": {}}},
    ],

    # ``facebook`` is populated below from packages.core.ai.mcp.facebook.list_tools()
    # — single source of truth for the ~30 Pages + Messenger + Instagram
    # Business tools in that module. Same dynamic-adapter pattern as
    # ``github``.

    "replicate": [
        {"name": "generate_image",
         "description": "Generate an image from a text prompt via a Replicate model (default: Flux Schnell, ~$0.003/image).",
         "parameters": {"type": "object", "required": ["prompt"],
                        "properties": {"prompt": {"type": "string"},
                                       "model": {"type": "string"},
                                       "aspect_ratio": {"type": "string"},
                                       "num_outputs": {"type": "integer"},
                                       "seed": {"type": "integer"}}}},
        {"name": "generate_video",
         "description": "Generate a short video via a Replicate video model (default: Luma Ray Flash 540p, 5s).",
         "parameters": {"type": "object", "required": ["prompt"],
                        "properties": {"prompt": {"type": "string"},
                                       "model": {"type": "string"},
                                       "duration": {"type": "integer"},
                                       "aspect_ratio": {"type": "string"}}}},
        {"name": "run_model",
         "description": "Run any Replicate model not covered by the typed tools above. Pass owner/name + model-specific input.",
         "parameters": {"type": "object", "required": ["model", "input"],
                        "properties": {"model": {"type": "string"},
                                       "input": {"type": "object"}}}},
    ],

    "elevenlabs": [
        {"name": "text_to_speech",
         "description": "Convert text into an MP3 voiceover via ElevenLabs. Saves to Manor's filesystem; returns path + filename.",
         "parameters": {"type": "object", "required": ["text"],
                        "properties": {"text": {"type": "string"},
                                       "voice_id": {"type": "string"},
                                       "model_id": {"type": "string"},
                                       "stability": {"type": "number"},
                                       "similarity_boost": {"type": "number"},
                                       "filename_hint": {"type": "string"}}}},
        {"name": "text_to_dialogue",
         "description": "Convert speaker turns into one multi-speaker dialogue audio file via ElevenLabs Text to Dialogue.",
         "parameters": {"type": "object", "required": ["inputs"],
                        "properties": {"inputs": {"type": "array",
                                                  "items": {"type": "object"}},
                                       "model_id": {"type": "string"},
                                       "language_code": {"type": "string"},
                                       "filename_hint": {"type": "string"}}}},
        {"name": "generate_sound_effect",
         "description": "Generate a sound-effect, Foley, or ambience audio file from text via ElevenLabs Sound Effects.",
         "parameters": {"type": "object", "required": ["text"],
                        "properties": {"text": {"type": "string"},
                                       "duration_seconds": {"type": "number"},
                                       "loop": {"type": "boolean"},
                                       "prompt_influence": {"type": "number"},
                                       "model_id": {"type": "string"},
                                       "filename_hint": {"type": "string"}}}},
        {"name": "compose_music",
         "description": "Generate music or score audio via ElevenLabs Music. Use for BGM, themes, and stingers.",
         "parameters": {"type": "object",
                        "properties": {"prompt": {"type": "string"},
                                       "composition_plan": {"type": "object"},
                                       "music_length_ms": {"type": "integer"},
                                       "force_instrumental": {"type": "boolean"},
                                       "model_id": {"type": "string"},
                                       "filename_hint": {"type": "string"}}}},
        {"name": "list_voices",
         "description": "List the user's available ElevenLabs voices (prebuilt + cloned) with their labels.",
         "parameters": {"type": "object", "properties": {}}},
    ],

    "tavily": [
        {"name": "search",
         "description": "Run a web search optimized for AI agents. Returns synthesized snippets, an optional 1-sentence answer, and (if requested) inline article body.",
         "parameters": {"type": "object", "required": ["query"],
                        "properties": {"query": {"type": "string"},
                                       "max_results": {"type": "integer"},
                                       "search_depth": {"type": "string"},
                                       "topic": {"type": "string"},
                                       "include_answer": {"type": "boolean"},
                                       "include_raw_content": {"type": "boolean"},
                                       "include_domains": {"type": "array", "items": {"type": "string"}},
                                       "exclude_domains": {"type": "array", "items": {"type": "string"}}}}},
        {"name": "extract",
         "description": "Pull clean article text from one or more URLs (use after search() when you need the full body).",
         "parameters": {"type": "object", "required": ["urls"],
                        "properties": {"urls": {"type": "array", "items": {"type": "string"}},
                                       "include_images": {"type": "boolean"}}}},
    ],

    "jimeng": [
        {"name": "generate_image",
         "description": "Generate one or more images from a text prompt via Jimeng (即梦). Chinese prompts work best.",
         "parameters": {"type": "object", "required": ["prompt"],
                        "properties": {"prompt": {"type": "string"},
                                       "model": {"type": "string"},
                                       "ratio": {"type": "string"},
                                       "resolution": {"type": "string"},
                                       "n": {"type": "integer"},
                                       "intelligent_ratio": {"type": "boolean"}}}},
        {"name": "edit_image",
         "description": "Image-to-image edit via Jimeng. Pass source image URL + transform instruction.",
         "parameters": {"type": "object", "required": ["prompt", "image_url"],
                        "properties": {"prompt": {"type": "string"},
                                       "image_url": {"type": "string"},
                                       "model": {"type": "string"},
                                       "ratio": {"type": "string"},
                                       "resolution": {"type": "string"}}}},
        {"name": "generate_video",
         "description": (
             "Generate a short video from a text prompt via Jimeng. Slow (1–4 minutes). "
             "Use only when the user explicitly asks for Jimeng/即梦 and the Jimeng integration is connected. "
             "For Manor's Account-selected video model, Seedance BYOK, Kling BYOK, uploaded media references, "
             "or generic video generation, use the first-party generate_file tool with kind='video' instead."
         ),
         "parameters": {"type": "object", "required": ["prompt"],
                        "properties": {"prompt": {"type": "string"},
                                       "model": {"type": "string"},
                                       "ratio": {"type": "string"},
                                       "resolution": {"type": "string"},
                                       "duration_seconds": {"type": "integer"}}}},
    ],

}


def _adapt_module_tools(module) -> list[dict]:
    """Pull tool schemas from an in-process MCP module's ``list_tools()``
    so the deferred-tool registry stays in sync with the module without
    duplicating definitions in this catalogue.

    Different modules use different keys for the JSON-Schema parameters:
    GitHub uses MCP-standard ``inputSchema``; Facebook uses ``parameters``
    directly. Accept either; fall back to an empty schema if neither is
    present."""
    out: list[dict] = []
    for t in module.list_tools():
        out.append({
            "name": t["name"],
            "description": t.get("description", ""),
            "parameters": (
                t.get("parameters")
                or t.get("inputSchema")
                or {"type": "object", "properties": {}}
            ),
        })
    return out


def _mcp_tool_result_to_text(result: dict) -> str:
    content = result.get("content") or []
    if content and isinstance(content[0], dict) and content[0].get("type") == "text":
        return str(content[0].get("text", ""))
    structured = result.get("structuredContent")
    if structured is None:
        structured = result.get("structured_content")
    if structured is not None:
        return json.dumps(structured, ensure_ascii=False)
    return json.dumps(result, ensure_ascii=False)


def _mcp_error_result_to_text(server_key: str, tool_name: str, result: dict) -> str:
    content = result.get("content") or []
    text = content[0].get("text") if content and isinstance(content[0], dict) else "Error"
    payload = {
        "server": server_key,
        "tool": tool_name,
        "error": "tool_error",
        "detail": text,
    }
    if isinstance(text, str) and text.lstrip().startswith("{"):
        try:
            parsed = json.loads(text.lstrip())
        except (TypeError, json.JSONDecodeError):
            parsed = None
        if isinstance(parsed, dict):
            for key in (
                "content",
                "message",
                "notice",
                "notice_key",
                "replace_visible_text",
                "stop_parent",
                "stop_reason",
                "terminal_failure",
                "retryable",
                "recommended_next_action",
                "reason",
            ):
                if key in parsed:
                    payload[key] = parsed[key]
    return json.dumps(payload, ensure_ascii=False)


from packages.core.ai.mcp import github as _gh_module  # noqa: E402
from packages.core.ai.mcp import facebook as _fb_module  # noqa: E402
from packages.core.ai.mcp import gmail as _gmail_module  # noqa: E402
from packages.core.ai.mcp import google_calendar as _gcal_module  # noqa: E402
from packages.core.ai.mcp import manor_mcp_calendar as _manor_calendar_module  # noqa: E402
from packages.core.ai.mcp import google_drive as _gdrive_module  # noqa: E402
from packages.core.ai.mcp import outlook as _outlook_module  # noqa: E402
from packages.core.ai.mcp import onedrive as _onedrive_module  # noqa: E402
from packages.core.ai.mcp import ms_calendar as _mscal_module  # noqa: E402
from packages.core.ai.mcp import ms_teams as _msteams_module  # noqa: E402
from packages.core.ai.mcp import ms_excel as _msexcel_module  # noqa: E402
from packages.core.ai.mcp import linkedin as _linkedin_module  # noqa: E402
from packages.core.ai.mcp import twitter_x as _twitter_x_module  # noqa: E402
from packages.core.ai.mcp import youtube as _youtube_module  # noqa: E402
from packages.core.ai.mcp import tiktok as _tiktok_module  # noqa: E402
from packages.core.ai.mcp import shopify as _shopify_module  # noqa: E402
from packages.core.ai.mcp import woocommerce as _woocommerce_module  # noqa: E402
from packages.core.ai.mcp import square as _square_module  # noqa: E402
from packages.core.ai.mcp import tiktok_shop as _tiktok_shop_module  # noqa: E402
from packages.core.ai.mcp import amazon as _amazon_module  # noqa: E402
from packages.core.ai.mcp import quickbooks as _qb_module  # noqa: E402
from packages.core.ai.mcp import telegram as _telegram_module  # noqa: E402

_SERVER_TOOL_SCHEMAS["github"] = _adapt_module_tools(_gh_module)
# quickbooks/telegram had hardcoded catalog entries above whose tool names
# and params had drifted from the real handlers (e.g. list_customers vs
# query_customers; telegram send_message's `text` vs the module's `content`).
# Auto-adapt so the agent sees exactly what dispatch accepts.
_SERVER_TOOL_SCHEMAS["quickbooks"] = _adapt_module_tools(_qb_module)
_SERVER_TOOL_SCHEMAS["telegram"] = _adapt_module_tools(_telegram_module)
_SERVER_TOOL_SCHEMAS["facebook"] = _adapt_module_tools(_fb_module)
_SERVER_TOOL_SCHEMAS["gmail"] = _adapt_module_tools(_gmail_module)
_SERVER_TOOL_SCHEMAS["google_calendar"] = _adapt_module_tools(_gcal_module)
_SERVER_TOOL_SCHEMAS["manor_mcp_calendar"] = _adapt_module_tools(_manor_calendar_module)
_SERVER_TOOL_SCHEMAS["google_drive"] = _adapt_module_tools(_gdrive_module)
_SERVER_TOOL_SCHEMAS["outlook"] = _adapt_module_tools(_outlook_module)
_SERVER_TOOL_SCHEMAS["onedrive"] = _adapt_module_tools(_onedrive_module)
_SERVER_TOOL_SCHEMAS["ms_calendar"] = _adapt_module_tools(_mscal_module)
_SERVER_TOOL_SCHEMAS["ms_teams"] = _adapt_module_tools(_msteams_module)
_SERVER_TOOL_SCHEMAS["ms_excel"] = _adapt_module_tools(_msexcel_module)
# linkedin had 3 hardcoded entries (create_post, get_profile,
# comment_on_post) but the module exposes 17 tools (media uploads,
# org pages, post stats, etc.). Auto-adapt so the agent sees the
# full surface — the hardcoded subset above is overwritten here.
_SERVER_TOOL_SCHEMAS["linkedin"] = _adapt_module_tools(_linkedin_module)
# twitter_x also has a real in-process module; keep the deferred catalog in
# lockstep so workspace allowlists can use the same tool names that dispatch
# will actually accept (create_tweet, search_recent, get_mentions, etc.).
_SERVER_TOOL_SCHEMAS["twitter_x"] = _adapt_module_tools(_twitter_x_module)
_SERVER_TOOL_SCHEMAS["youtube"] = _adapt_module_tools(_youtube_module)
_SERVER_TOOL_SCHEMAS["tiktok"] = _adapt_module_tools(_tiktok_module)
_SERVER_TOOL_SCHEMAS["shopify"] = _adapt_module_tools(_shopify_module)
_SERVER_TOOL_SCHEMAS["woocommerce"] = _adapt_module_tools(_woocommerce_module)
_SERVER_TOOL_SCHEMAS["square"] = _adapt_module_tools(_square_module)
_SERVER_TOOL_SCHEMAS["tiktok_shop"] = _adapt_module_tools(_tiktok_shop_module)
_SERVER_TOOL_SCHEMAS["amazon"] = _adapt_module_tools(_amazon_module)


# ---------------------------------------------------------------------------
# Handler factory
# ---------------------------------------------------------------------------

def _build_handler(server_key: str, tool_name: str) -> Callable:
    async def _handler(
        entity_id: str = "",
        user_id: str = "",
        **kwargs: Any,
    ) -> str:
        """Permission-aware MCP dispatcher.

        1. Resolve credentials via agent_permission_service.can_use_integration.
           - personal OAuth in oauth_accounts (user-scope)  OR
           - entity Integration (entity-scope, gated by required_permission)
        2. Branch on the MCPServer row's ``transport``:
             builtin → load packages/core/ai/mcp/<server>.py and dispatch
             http    → forward to vendor MCP via RemoteMCPClient
        3. Forward kwargs as the tool arguments + bearer_token as auth.
        """
        if not entity_id:
            return json.dumps({"error": "entity_id is required for MCP tool calls."})

        from packages.core.database import async_session
        from packages.core.ai.mcp import get_module
        from packages.core.services.agent_permission_service import (
            can_use_integration,
        )
        from sqlalchemy import select
        from packages.core.models.mcp import MCPServer

        # Look up transport + endpoint once. We need the MCPServer row
        # whether we end up dispatching builtin or http.
        async with async_session() as db:
            server_row = (await db.execute(
                select(MCPServer).where(MCPServer.server_key == server_key)
            )).scalar_one_or_none()

        if server_row is None:
            return json.dumps({
                "error": f"Unknown MCP server '{server_key}' (no catalog row).",
            })

        transport = (server_row.transport or "builtin").lower()

        # Builtin path: validate the in-process module exists upfront.
        # Remote path: no module required.
        module = None
        if transport == "builtin":
            module = get_module(server_key)
            if module is None:
                return json.dumps({
                    "error": f"No in-process MCP module for server '{server_key}'.",
                })
        elif transport == "http":
            if not server_row.endpoint:
                return json.dumps({
                    "error": f"Remote MCP server '{server_key}' has no endpoint URL.",
                })
        else:
            return json.dumps({
                "error": f"Unsupported MCP transport '{transport}' for '{server_key}'.",
            })

        async with async_session() as db:
            decision = await can_use_integration(
                db,
                user_id=user_id,
                entity_id=entity_id,
                provider=server_key,
                allow_env_fallback=False,
            )

        if not decision.allowed:
            # Cross-provider hint: when the primary provider is missing,
            # check if a capability-equivalent fallback IS connected and
            # tell the LLM to try that one instead.
            alt_hint = await _suggest_alternative_provider(
                server_key=server_key, tool_name=tool_name,
                user_id=user_id, entity_id=entity_id,
            )
            first_party_fallback = _FIRST_PARTY_TOOL_FALLBACKS.get((server_key, tool_name))
            first_party_hint = (
                f" If the user meant Manor's Account-selected model or BYOK, call `{first_party_fallback}` "
                "with the matching kind instead."
                if first_party_fallback else ""
            )
            reason = decision.reason + (f" {alt_hint}" if alt_hint else "") + first_party_hint
            return json.dumps({
                "server": server_key,
                "tool": tool_name,
                "error": "credentials_unavailable",
                "reason": reason,
                "scope": decision.scope,
                "suggested_tool": (
                    first_party_fallback
                    or (_ALT_TOOL_MAP.get((server_key, tool_name), None) if alt_hint else None)
                ),
            })

        # Resolve the bearer token from the Decision context (re-read because
        # can_use_integration returns the decision, not the token itself).
        # Local worker providers authenticate on the user's machine, so there
        # is no Manor-side bearer token to fetch.
        bearer_token = None
        should_resolve_bearer_token = True
        if should_resolve_bearer_token:
            bearer_token = await _resolve_bearer_token(
                server_key=server_key, user_id=user_id, entity_id=entity_id,
                scope=decision.scope, allow_env_fallback=False,
            )
        # Some context-only servers do not use bearer tokens; the module's
        # own dispatch logic decides whether the user can proceed.
        if not bearer_token and not _is_context_only_server(server_key):
            return json.dumps({
                "server": server_key,
                "tool": tool_name,
                "error": "token_resolution_failed",
                "reason": (
                    f"Permission check passed for '{server_key}' but no "
                    f"bearer token was available. Reconnect the integration."
                ),
            })

        tool_kwargs = {
            key: value
            for key, value in kwargs.items()
            if key not in _MCP_RUNTIME_ONLY_ARGUMENT_KEYS
        }

        # Builtin: in-process module dispatch. Some private providers need
        # the calling user/entity context, so set it via hook when present.
        if transport == "builtin":
            set_ctx = getattr(module, "set_call_context", None)
            clear_ctx = getattr(module, "clear_call_context", None)
            if set_ctx:
                call_ctx = {"user_id": user_id, "entity_id": entity_id}
                for key in ("conversation_id", "workspace_id", "task_id"):
                    if kwargs.get(key):
                        call_ctx[key] = str(kwargs[key])
                active_message = runtime_active_user_message_from_context(kwargs)
                if active_message:
                    call_ctx["active_user_message"] = str(active_message)
                set_ctx(call_ctx)
            try:
                result = await module.call_tool(tool_name, tool_kwargs, bearer_token or "")
            except Exception as e:
                logger.exception("MCP call failed: %s/%s", server_key, tool_name)
                return json.dumps({
                    "server": server_key, "tool": tool_name,
                    "error": "call_failed", "detail": str(e),
                })
            finally:
                if clear_ctx:
                    clear_ctx()
        else:
            # Remote MCP — forward to the vendor over JSON-RPC.
            from packages.core.ai.mcp._remote import (
                RemoteMCPClient, RemoteMCPError,
            )
            token_in = (server_row.default_config or {}).get("mcp_token_in", "header")
            client = RemoteMCPClient(
                endpoint=server_row.endpoint,
                access_token=bearer_token or "",
                token_in=token_in,
            )
            try:
                result = await client.call_tool(tool_name, tool_kwargs)
            except RemoteMCPError as e:
                # 401 → tell the user to reconnect; everything else is
                # a tool-level failure surfaced to the agent.
                if e.code == -32001:
                    return json.dumps({
                        "server": server_key, "tool": tool_name,
                        "error": "credentials_unavailable",
                        "reason": e.message,
                    })
                logger.warning("Remote MCP error %s/%s: %s",
                               server_key, tool_name, e)
                return json.dumps({
                    "server": server_key, "tool": tool_name,
                    "error": "remote_mcp_error",
                    "detail": str(e),
                })
            except Exception as e:
                logger.exception("Remote MCP call crashed %s/%s",
                                 server_key, tool_name)
                return json.dumps({
                    "server": server_key, "tool": tool_name,
                    "error": "call_failed", "detail": str(e),
                })

        # MCP tools/call response → string
        if isinstance(result, dict):
            if result.get("isError"):
                return _mcp_error_result_to_text(server_key, tool_name, result)
            output_text = _mcp_tool_result_to_text(result)
        else:
            output_text = str(result)

        # Auto-register any generated files in knowledge base
        try:
            from packages.core.ai.mcp.file_registrar import register_generated_files
            runtime_context = runtime_tool_call_context_from_kwargs(kwargs)
            origin = {
                "workspace_id": runtime_context.workspace_id,
                "task_id": runtime_context.task_id,
                "conversation_id": runtime_context.conversation_id,
                "agent_id": runtime_context.agent_id,
                "user_id": runtime_context.user_id or user_id,
                "tool_name": f"mcp__{server_key}__{tool_name}",
                "mcp_server": server_key,
            }
            await register_generated_files(
                output_text,
                entity_id=entity_id,
                user_id=user_id,
                source=server_key,
                tool_args=tool_kwargs,
                origin=origin,
            )
        except Exception:
            logger.debug("file_registrar failed for %s/%s", server_key, tool_name, exc_info=True)

        return output_text

    return _handler


_JSON_BLOB_PROVIDERS: set[str] = {
    "email", "twilio", "whatsapp", "webhook",
    "telegram", "discord",
    "wechat_personal", "wechat_official",
    # E-commerce: multi-field store credentials (domain + token, or
    # consumer key/secret) passed to the module as a JSON blob.
    "shopify", "woocommerce", "square",
    # Marketplace sellers: app key/secret + token (TikTok Shop signs each
    # request) / LWA refresh-token bundle (Amazon SP-API).
    "tiktok_shop", "amazon",
}

# MCP modules that don't need a bearer_token because their auth lives
# elsewhere. Private cloud builds add paired local worker providers here.
_CONTEXT_ONLY_SERVERS: set[str] = {
    # Future cli_worker context-only servers go here as we add them.
}


def _is_context_only_server(server_key: str) -> bool:
    key = str(server_key or "").strip().lower()
    return key in _CONTEXT_ONLY_SERVERS or key.startswith("manor_mcp_")


_MCP_RUNTIME_ONLY_ARGUMENT_KEYS = frozenset(RUNTIME_TOOL_CONTEXT_KEYS)


# Capability-equivalent providers. When the primary is missing creds, we
# offer the alt as a suggestion so the LLM can switch tools instead of
# giving up. (provider, tool_name) → (alt_provider, alt_tool_name).
_ALT_TOOL_MAP: dict[tuple[str, str], tuple[str, str]] = {
    # Gmail ↔ Email (IMAP+SMTP) — any tool on one side maps to the same
    # tool on the other, since we kept the tool names aligned.
    ("gmail", "list_messages"):   ("email", "list_messages"),
    ("gmail", "get_message"):     ("email", "get_message"),
    ("gmail", "send_message"):    ("email", "send_email"),
    ("email", "list_messages"):   ("gmail", "list_messages"),
    ("email", "get_message"):     ("gmail", "get_message"),
    ("email", "send_email"):      ("gmail", "send_message"),
    # WhatsApp → Twilio — both can SMS the same number, though the
    # semantics differ slightly. Still useful as a fallback hint.
    ("whatsapp", "send_message"): ("twilio", "send_sms"),
}


_FIRST_PARTY_TOOL_FALLBACKS: dict[tuple[str, str], str] = {
    ("jimeng", "generate_video"): "generate_file",
    ("replicate", "generate_video"): "generate_file",
}


async def _suggest_alternative_provider(
    *, server_key: str, tool_name: str, user_id: str, entity_id: str,
) -> Optional[str]:
    """If the primary provider is unavailable but an alternative IS
    connected, return a one-line hint the LLM can consume. Returns None
    when there's no suitable alternative.
    """
    alt = _ALT_TOOL_MAP.get((server_key, tool_name))
    if not alt:
        return None
    alt_provider, alt_tool = alt
    try:
        from packages.core.database import async_session
        from packages.core.services.agent_permission_service import (
            can_use_integration,
        )
        async with async_session() as db:
            decision = await can_use_integration(
                db,
                user_id=user_id,
                entity_id=entity_id,
                provider=alt_provider,
                allow_env_fallback=False,
            )
    except Exception:
        return None
    if not decision.allowed:
        return None
    return (
        f"Alternative available: '{alt_provider}' is connected — "
        f"try mcp__{alt_provider}__{alt_tool} instead."
    )


async def _resolve_bearer_token(
    *,
    server_key: str,
    user_id: str,
    entity_id: str,
    scope: str,
    allow_env_fallback: bool = False,
) -> str | None:
    """Load the actual token based on the scope the permission decision
    resolved to. Mirrors the logic in can_use_integration() but returns
    the token value rather than just a decision.
    """
    from sqlalchemy import select
    from packages.core.database import async_session
    from packages.core.models.document import Integration
    from packages.core.models.user import OAuthAccount
    from packages.core.services.agent_permission_service import _env_token_for
    from packages.core.services.provider_keys import provider_key_aliases

    if scope == "env":
        if not allow_env_fallback:
            return None
        # Dev / cloud-default: read from environment (gated by
        # MANOR_ALLOW_ENV_TOKENS inside _env_token_for).
        return _env_token_for(server_key)

    async with async_session() as db:
        provider_aliases = provider_key_aliases(server_key)
        if scope == "user":
            # Multi-account aware: prefer profile.is_default, then most-recent.
            rows = (
                await db.execute(
                    select(OAuthAccount).where(
                        OAuthAccount.user_id == user_id,
                        OAuthAccount.provider.in_(provider_aliases),
                    ).order_by(OAuthAccount.created_at.desc())
                )
            ).scalars().all()
            if rows:
                default = next(
                    (r for r in rows if (r.profile or {}).get("is_default")),
                    None,
                )
                chosen = default or rows[0]
                if chosen.access_token:
                    return chosen.access_token

        if scope == "entity":
            # Multi-account: prefer config.is_default first, fall back
            # to most-recent. Keeps send_email() deterministic when an
            # entity has several inboxes / bots / senders.
            rows = (
                await db.execute(
                    select(Integration).where(
                        Integration.entity_id == entity_id,
                        Integration.provider.in_(provider_aliases),
                        Integration.status == "active",
                    ).order_by(Integration.created_at.desc())
                )
            ).scalars().all()
            if rows:
                default = next(
                    (r for r in rows if (r.config or {}).get("is_default")),
                    None,
                )
                row = default or rows[0]

                # Nango-backed Integration: the row only stores a
                # pointer (connection_id) -- the actual access token
                # lives inside Nango and refreshes on its own. Fetch
                # a fresh one per-call so we never persist stale
                # tokens locally.
                nango_meta = (row.config or {}).get("nango") or {}
                connection_id = nango_meta.get("connection_id")
                if connection_id:
                    if server_key == "linkedin":
                        return json.dumps({
                            "via": "nango",
                            "provider_config_key": nango_meta.get("provider_config_key") or server_key,
                            "connection_id": connection_id,
                        })
                    token = await _fetch_token_via_nango(
                        db,
                        entity_id=entity_id,
                        provider_config_key=nango_meta.get("provider_config_key") or server_key,
                        connection_id=connection_id,
                    )
                    if token:
                        return token
                    # Falls through to legacy path below in case the
                    # entity has both a Nango connection AND a
                    # hand-rolled credential as backup.

                # Always lease via CredentialService so we get the
                # decrypted plaintext for vault_transit-stored rows.
                # Reading row.credentials directly only works for
                # legacy plaintext rows — it's empty after the Vault
                # rollout for any Integration created via the
                # ApiKeyConfigModal flow.
                from packages.core.credentials import (
                    get_credential_service, Requester,
                )
                try:
                    creds = get_credential_service().lease_integration(
                        row,
                        requester=Requester(kind="agent", id=entity_id),
                        reason=f"mcp_builtin._resolve_bearer_token:{server_key}",
                    )
                except Exception:
                    logger.exception(
                        "Failed to lease credentials for %s/%s",
                        server_key, row.id,
                    )
                    creds = None

                if not creds and isinstance(row.credentials, dict):
                    # Dev/test and old plaintext rows may not have Vault
                    # configured. Prefer CredentialService when available,
                    # but keep these legacy rows callable instead of failing
                    # after permission has already passed.
                    legacy_creds = {
                        k: v for k, v in row.credentials.items()
                        if v is not None
                    }
                    if legacy_creds:
                        creds = legacy_creds

                if creds:
                    # Multi-field credentials (IMAP+SMTP bundle, Twilio
                    # SID+token+number, webhook url+secret, …) — pass the
                    # whole dict as a JSON blob so the MCP module can
                    # decode it. Single-token providers keep the flat
                    # shape.
                    if server_key in _JSON_BLOB_PROVIDERS:
                        return json.dumps(creds)
                    return (
                        creds.get("access_token")
                        or creds.get("secret_key")
                        or creds.get("api_key")
                    )
    return None


async def _fetch_token_via_nango(
    db, *, entity_id: str, provider_config_key: str, connection_id: str,
) -> str | None:
    """Ask Nango for a fresh access_token for a given Connection.

    The Nango admin secret_key resolves env-first
    (``NANGO_SECRET_KEY``) with a per-entity Integration row as
    fallback. Nango handles refresh under the hood, so each call
    returns a non-expired token.
    """
    from packages.core.ai.mcp.nango import _NANGO_BASE, get_nango_secret
    import httpx

    secret = await get_nango_secret(db, entity_id)
    if not secret:
        logger.warning(
            "Nango bridge requested for %s but no NANGO_SECRET_KEY env "
            "var and no nango Integration on entity %s",
            provider_config_key, entity_id,
        )
        return None

    try:
        async with httpx.AsyncClient(timeout=15.0) as cx:
            r = await cx.get(
                f"{_NANGO_BASE}/connection/{connection_id}",
                params={"provider_config_key": provider_config_key},
                headers={"Authorization": f"Bearer {secret}"},
            )
            r.raise_for_status()
            body = r.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Nango bridge fetch failed for %s/%s: %s",
            provider_config_key, connection_id, exc,
        )
        return None

    creds_block = (body.get("credentials") or {})
    return (
        creds_block.get("access_token")
        or creds_block.get("api_key")
        or body.get("access_token")
    )


# ---------------------------------------------------------------------------
# Registration entrypoint
# ---------------------------------------------------------------------------

def _build_tool_name(server_key: str, tool_name: str) -> str:
    safe_server = server_key.replace("-", "_").replace(".", "_")
    safe_tool = tool_name.replace("-", "_").replace(".", "_")
    return f"mcp__{safe_server}__{safe_tool}"


def get_tools() -> list[tuple[dict, Callable]]:
    """Return schemas + handlers for every built-in MCP server tool.

    Called by tools/__init__.py::register_all_tools at pool init.
    All tools are registered as DEFERRED so agents discover them on
    demand via search_tools rather than inflating every request's
    tool-schema payload.
    """
    out: list[tuple[dict, Callable]] = []
    total = 0
    for server_key, tools in _SERVER_TOOL_SCHEMAS.items():
        for tool_def in tools:
            mcp_name = _build_tool_name(server_key, tool_def["name"])
            schema = {
                "type": "function",
                "function": {
                    "name": mcp_name,
                    "description": f"[MCP:{server_key}] {tool_def['description']}",
                    "parameters": tool_def.get(
                        "parameters", {"type": "object", "properties": {}}
                    ),
                },
            }
            out.append((schema, _build_handler(server_key, tool_def["name"])))
            total += 1
    logger.info("Built-in MCP catalog: %d tools across %d servers",
                total, len(_SERVER_TOOL_SCHEMAS))
    return out
