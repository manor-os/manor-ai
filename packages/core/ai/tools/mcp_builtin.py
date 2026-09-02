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

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any, Callable, Optional
from urllib.parse import urlparse

from packages.core.ai.runtime.tool_context import (
    RUNTIME_TOOL_CONTEXT_KEYS,
    runtime_active_user_message_from_context,
    runtime_tool_call_context_from_kwargs,
)
from packages.core.ai.runtime.tool_results import RuntimeStructuredToolResult
from packages.core.constants.integrations import (
    INTEGRATION_ACCOUNT_CONTINUATION_ARGUMENT,
    INTEGRATION_ACCOUNT_CONTINUATION_MAX_CHARS,
    INTEGRATION_ACCOUNT_SELECTION_ARGUMENT,
    RUNTIME_MCP_ACCOUNT_REGISTRY_SNAPSHOT_ARGUMENT,
    RUNTIME_MCP_ALL_ACCOUNTS_CURSOR_ARGUMENT,
    RUNTIME_MCP_INTEGRATION_REGISTRY_ARGUMENT,
)
from packages.core.services.integration_account_service import (
    IntegrationRegistryLoadStatus,
    RuntimeIntegrationAccount,
)
from packages.core.services.official_remote_mcp import (
    MCPActionEffect,
    OfficialRemoteMCPToolNameFactory,
)

logger = logging.getLogger(__name__)

INTEGRATION_ACCOUNT_ARGUMENT = "integration_account_id"
_INTEGRATION_ACCOUNT_PARAMETER = {
    "type": "string",
    "description": (
        "Connected account ID to use for this operation. Choose an ID returned "
        "by search_tools. Every returned account is callable; omit this argument "
        "only to use the default."
    ),
}
_INTEGRATION_ACCOUNT_CONTINUATION_PARAMETER = {
    "type": "string",
    "minLength": 1,
    "maxLength": INTEGRATION_ACCOUNT_CONTINUATION_MAX_CHARS,
    "description": (
        "Opaque continuation token returned by a previous bounded all-account "
        "result. Send it only with integration_account_selection='all' to "
        "resume the same query from the next unattempted account."
    ),
}
_MCP_DISPATCH_CONTEXT_ARGUMENT = "_runtime_mcp_dispatch_context"
_MCP_ALLOWED_ACCOUNT_IDS_ARGUMENT = "_runtime_mcp_allowed_account_ids"
_MCP_ALL_ACCOUNTS_MAX_CALLS = 20
_MCP_ALL_ACCOUNTS_CHILD_TIMEOUT_SECONDS = 20.0
_MCP_ALL_ACCOUNTS_TOTAL_TIMEOUT_SECONDS = 60.0
_MCP_ALL_ACCOUNTS_MAX_RESULT_CHARS = 20_000
_MCP_ACTION_EFFECT_RANK = {
    MCPActionEffect.READ: 0,
    MCPActionEffect.WRITE: 1,
    MCPActionEffect.DESTRUCTIVE: 2,
}


@dataclass(frozen=True, slots=True)
class _MCPResolvedDispatchContext:
    """Immutable server and account snapshot reused by ALL-mode fan-out."""

    server_row: Any
    transport: str
    module: Any
    integration_registry: Any = None


@dataclass(frozen=True, slots=True)
class DiscoveredOfficialRemoteMCPTool:
    name: str
    schema: dict[str, Any]
    provider: str
    action: str
    account_ids: tuple[str, ...]
    effect: MCPActionEffect = MCPActionEffect.WRITE
    requires_explicit_account: bool = False
    supports_all_accounts: bool = True
    incomplete_account_ids: tuple[str, ...] = ()
    registry_account_ids: tuple[str, ...] | None = None
    registry_status: IntegrationRegistryLoadStatus | None = None
    endpoint: str | None = None
    token_in: str | None = None


_INTEGRATION_ACCOUNT_SELECTION_PARAMETER = {
    "type": "string",
    "enum": ["default", "all"],
    "default": "default",
    "description": (
        "Account selection mode. 'default' uses integration_account_id when one "
        "is supplied, otherwise the prioritized default. 'all' deterministically "
        "runs read-only queries against every callable connected account and "
        "combines the results; mutating tools reject 'all'."
    ),
}
_RUNTIME_WORKFLOW_CALL_CONTEXT_KEYS = (
    "workflow_run_id",
    "workflow_lineage_root_run_id",
    "workflow_project_id",
    "workflow_project_root",
    "workflow_action_grant_id",
    "workflow_step_id",
    "workflow_scene_id",
    "workflow_batch_capture",
    "approved_plan_version",
)


def _runtime_workflow_call_context(runtime_context: Any) -> dict[str, Any]:
    return {key: value for key in _RUNTIME_WORKFLOW_CALL_CONTEXT_KEYS if (value := getattr(runtime_context, key, None))}


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
        {
            "name": "create_post",
            "description": "Create a LinkedIn post on the authenticated user's profile.",
            "parameters": {
                "type": "object",
                "required": ["text"],
                "properties": {
                    "text": {"type": "string"},
                    "visibility": {"type": "string", "enum": ["PUBLIC", "CONNECTIONS"]},
                },
            },
        },
        {
            "name": "get_profile",
            "description": "Get the authenticated user's LinkedIn profile.",
            "parameters": {"type": "object", "properties": {}},
        },
        {
            "name": "comment_on_post",
            "description": "Comment on a LinkedIn post.",
            "parameters": {
                "type": "object",
                "required": ["post_urn", "text"],
                "properties": {"post_urn": {"type": "string"}, "text": {"type": "string"}},
            },
        },
    ],
    # ``github`` is populated below from packages.core.ai.mcp.github.list_tools()
    # — single source of truth for the ~50 tools in that module. Done after
    # _SERVER_TOOL_SCHEMAS is defined to keep the dict literal scannable.
    "twitter_x": [
        {
            "name": "post_tweet",
            "description": "Post a tweet.",
            "parameters": {
                "type": "object",
                "required": ["text"],
                "properties": {"text": {"type": "string"}, "reply_to_tweet_id": {"type": "string"}},
            },
        },
        {
            "name": "delete_tweet",
            "description": "Delete a tweet by ID.",
            "parameters": {"type": "object", "required": ["tweet_id"], "properties": {"tweet_id": {"type": "string"}}},
        },
        {
            "name": "search_tweets",
            "description": "Search recent tweets.",
            "parameters": {
                "type": "object",
                "required": ["query"],
                "properties": {"query": {"type": "string"}, "max_results": {"type": "integer", "default": 10}},
            },
        },
        {
            "name": "get_user",
            "description": "Get a user profile by handle.",
            "parameters": {"type": "object", "required": ["username"], "properties": {"username": {"type": "string"}}},
        },
    ],
    "quickbooks": [
        {
            "name": "list_customers",
            "description": "List QuickBooks customers.",
            "parameters": {"type": "object", "properties": {"max_results": {"type": "integer", "default": 50}}},
        },
        {
            "name": "get_customer",
            "description": "Get a customer by ID.",
            "parameters": {
                "type": "object",
                "required": ["customer_id"],
                "properties": {"customer_id": {"type": "string"}},
            },
        },
        {
            "name": "list_invoices",
            "description": "List invoices.",
            "parameters": {
                "type": "object",
                "properties": {"status": {"type": "string"}, "max_results": {"type": "integer", "default": 50}},
            },
        },
        {
            "name": "create_invoice",
            "description": "Create an invoice for a customer.",
            "parameters": {
                "type": "object",
                "required": ["customer_id", "line_items"],
                "properties": {
                    "customer_id": {"type": "string"},
                    "line_items": {"type": "array", "items": {"type": "object"}},
                },
            },
        },
    ],
    # Official remote Stripe and PayPal tools are populated below from
    # OfficialRemoteMCPFactory. The legacy in-process Stripe module remains
    # on disk for backward import compatibility but is no longer dispatched.
    # ``discord`` has NO in-process module yet (packages/core/ai/mcp/discord.py
    # does not exist) — advertising schemas for it sent agents into a
    # guaranteed "No in-process MCP module" dead end. Do not add schemas
    # here until the module ships; the catalog row stays visible in the
    # UI via the coming_soon gate in integration_service.py.
    "telegram": [
        {
            "name": "send_message",
            "description": "Send a text message to a Telegram chat.",
            "parameters": {
                "type": "object",
                "required": ["chat_id", "text"],
                "properties": {
                    "chat_id": {"type": "string", "description": "Numeric chat id or @username"},
                    "text": {"type": "string"},
                    "parse_mode": {"type": "string", "enum": ["Markdown", "MarkdownV2", "HTML"]},
                },
            },
        },
        {
            "name": "send_photo",
            "description": "Send a photo by URL or file_id.",
            "parameters": {
                "type": "object",
                "required": ["chat_id", "photo"],
                "properties": {
                    "chat_id": {"type": "string"},
                    "photo": {"type": "string", "description": "Public URL or previously-uploaded file_id."},
                    "caption": {"type": "string"},
                },
            },
        },
        {
            "name": "send_document",
            "description": "Send a document/file.",
            "parameters": {
                "type": "object",
                "required": ["chat_id", "document"],
                "properties": {
                    "chat_id": {"type": "string"},
                    "document": {"type": "string"},
                    "caption": {"type": "string"},
                },
            },
        },
        {
            "name": "get_updates",
            "description": "Pull recent bot updates (polling; use webhooks for prod).",
            "parameters": {
                "type": "object",
                "properties": {"offset": {"type": "integer"}, "limit": {"type": "integer", "default": 100}},
            },
        },
    ],
    # Personal WeChat bot — via QR-login bot runner (no AppID/Secret).
    "wechat_personal": [
        {
            "name": "list_groups",
            "description": "List recently-seen WeChat group peers the bot can reply to.",
            "parameters": {"type": "object", "properties": {}},
        },
        {
            "name": "list_contacts",
            "description": "List recently-seen 1:1 WeChat peers the bot can reply to.",
            "parameters": {"type": "object", "properties": {}},
        },
        {
            "name": "send_group_message",
            "description": "Reply with text to a WeChat group that messaged the bot recently.",
            "parameters": {
                "type": "object",
                "required": ["group_id", "content"],
                "properties": {"group_id": {"type": "string"}, "content": {"type": "string"}},
            },
        },
        {
            "name": "send_direct_message",
            "description": "Reply with text to a WeChat contact that messaged the bot recently.",
            "parameters": {
                "type": "object",
                "required": ["contact_id", "content"],
                "properties": {"contact_id": {"type": "string"}, "content": {"type": "string"}},
            },
        },
        {
            "name": "get_bot_status",
            "description": "Get this WeChat runner session status (online / QR pending / offline).",
            "parameters": {
                "type": "object",
                "properties": {
                    "group_id": {"type": "string", "description": "Optional — filter to a specific bot instance"}
                },
            },
        },
        {
            "name": "get_qr_code",
            "description": "Get a fresh QR code URL from the runner so the user can (re)scan to log in.",
            "parameters": {"type": "object", "properties": {}},
        },
    ],
    # WeChat Official Account (公众号) — Tencent cgi-bin API.
    "wechat_official": [
        {
            "name": "send_text_message",
            "description": "Send a text customer-service message to a follower (within 48h of their last message).",
            "parameters": {
                "type": "object",
                "required": ["to_user", "content"],
                "properties": {
                    "to_user": {"type": "string", "description": "Follower OpenID."},
                    "content": {"type": "string"},
                },
            },
        },
        {
            "name": "send_image_message",
            "description": "Send an image customer-service message.",
            "parameters": {
                "type": "object",
                "required": ["to_user", "media_id"],
                "properties": {
                    "to_user": {"type": "string"},
                    "media_id": {"type": "string", "description": "ID from upload_media."},
                },
            },
        },
        {
            "name": "send_template_message",
            "description": "Send a pre-approved template message.",
            "parameters": {
                "type": "object",
                "required": ["to_user", "template_id", "data"],
                "properties": {
                    "to_user": {"type": "string"},
                    "template_id": {"type": "string"},
                    "url": {"type": "string"},
                    "data": {"type": "object"},
                },
            },
        },
        {
            "name": "upload_media",
            "description": "Upload a temporary media file (image/voice/video/thumb). Returns media_id.",
            "parameters": {
                "type": "object",
                "required": ["media_type", "file_url"],
                "properties": {
                    "media_type": {"type": "string", "enum": ["image", "voice", "video", "thumb"]},
                    "file_url": {"type": "string"},
                },
            },
        },
        {
            "name": "get_follower_info",
            "description": "Fetch profile for a follower OpenID.",
            "parameters": {
                "type": "object",
                "required": ["open_id"],
                "properties": {"open_id": {"type": "string"}, "lang": {"type": "string"}},
            },
        },
        {
            "name": "list_followers",
            "description": "List follower OpenIDs (paginated).",
            "parameters": {"type": "object", "properties": {"next_open_id": {"type": "string"}}},
        },
    ],
    "email": [
        {
            "name": "send_email",
            "description": "Send an email through the configured SMTP relay.",
            "parameters": {
                "type": "object",
                "required": ["to", "subject", "body"],
                "properties": {
                    "to": {"type": "string"},
                    "subject": {"type": "string"},
                    "body": {"type": "string"},
                    "html": {"type": "string"},
                    "cc": {"type": "string"},
                    "bcc": {"type": "string"},
                    "from_address": {"type": "string"},
                    "reply_to": {"type": "string"},
                },
            },
        },
        {
            "name": "list_messages",
            "description": "List messages in an IMAP folder with optional filters.",
            "parameters": {
                "type": "object",
                "properties": {
                    "folder": {"type": "string", "default": "INBOX"},
                    "unseen_only": {"type": "boolean"},
                    "from_address": {"type": "string"},
                    "subject_contains": {"type": "string"},
                    "body_contains": {"type": "string"},
                    "since": {"type": "string"},
                    "before": {"type": "string"},
                    "max_results": {"type": "integer", "default": 20},
                },
            },
        },
        {
            "name": "get_message",
            "description": "Fetch one message by UID with headers + body.",
            "parameters": {
                "type": "object",
                "required": ["uid"],
                "properties": {
                    "uid": {"type": "string"},
                    "folder": {"type": "string", "default": "INBOX"},
                    "format": {"type": "string", "enum": ["full", "text", "headers"]},
                },
            },
        },
        {
            "name": "mark_read",
            "description": "Mark a message as read.",
            "parameters": {
                "type": "object",
                "required": ["uid"],
                "properties": {"uid": {"type": "string"}, "folder": {"type": "string"}},
            },
        },
        {
            "name": "mark_unread",
            "description": "Mark a message as unread.",
            "parameters": {
                "type": "object",
                "required": ["uid"],
                "properties": {"uid": {"type": "string"}, "folder": {"type": "string"}},
            },
        },
        {
            "name": "move_message",
            "description": "Move a message to another folder.",
            "parameters": {
                "type": "object",
                "required": ["uid", "to_folder"],
                "properties": {
                    "uid": {"type": "string"},
                    "from_folder": {"type": "string"},
                    "to_folder": {"type": "string"},
                },
            },
        },
        {
            "name": "delete_message",
            "description": "Delete a message (flag Deleted + expunge).",
            "parameters": {
                "type": "object",
                "required": ["uid"],
                "properties": {"uid": {"type": "string"}, "folder": {"type": "string"}},
            },
        },
        {
            "name": "list_folders",
            "description": "List all IMAP folders.",
            "parameters": {"type": "object", "properties": {}},
        },
    ],
    # ``nango`` (open-source self-hosted OAuth aggregator) is populated
    # below via ``_adapt_module_tools`` from the module's ``list_tools()``.
    "producthunt": [
        {
            "name": "search_posts",
            "description": "Search Product Hunt posts by topic, free-text, or launch date. Use for competitor research before a launch.",
            "parameters": {
                "type": "object",
                "properties": {
                    "topic": {"type": "string"},
                    "url": {"type": "string"},
                    "posted_after": {"type": "string"},
                    "posted_before": {"type": "string"},
                    "first": {"type": "integer"},
                    "order": {"type": "string"},
                },
            },
        },
        {
            "name": "get_post",
            "description": "Fetch one Product Hunt post in detail by slug.",
            "parameters": {"type": "object", "required": ["slug"], "properties": {"slug": {"type": "string"}}},
        },
        {
            "name": "daily_posts",
            "description": "Top posts launched on a specific day. Defaults to today UTC.",
            "parameters": {"type": "object", "properties": {"day": {"type": "string"}, "first": {"type": "integer"}}},
        },
        {
            "name": "list_comments",
            "description": "Get comments on a Product Hunt post (most recent first).",
            "parameters": {
                "type": "object",
                "required": ["slug"],
                "properties": {"slug": {"type": "string"}, "first": {"type": "integer"}},
            },
        },
        {
            "name": "post_comment",
            "description": "Leave a comment on a post as the authenticated user. Requires the OAuth token to have the 'private' scope.",
            "parameters": {
                "type": "object",
                "required": ["post_id", "body"],
                "properties": {
                    "post_id": {"type": "string"},
                    "body": {"type": "string"},
                    "parent_comment_id": {"type": "string"},
                },
            },
        },
        {
            "name": "me",
            "description": "Return the authenticated PH user — sanity check that the OAuth token works.",
            "parameters": {"type": "object", "properties": {}},
        },
    ],
    # ``facebook`` is populated below from packages.core.ai.mcp.facebook.list_tools()
    # — single source of truth for the ~30 Pages + Messenger + Instagram
    # Business tools in that module. Same dynamic-adapter pattern as
    # ``github``.
    "replicate": [
        {
            "name": "generate_image",
            "description": "Generate an image from a text prompt via a Replicate model (default: Flux Schnell, ~$0.003/image).",
            "parameters": {
                "type": "object",
                "required": ["prompt"],
                "properties": {
                    "prompt": {"type": "string"},
                    "model": {"type": "string"},
                    "aspect_ratio": {"type": "string"},
                    "num_outputs": {"type": "integer"},
                    "seed": {"type": "integer"},
                },
            },
        },
        {
            "name": "generate_video",
            "description": "Generate a short video via a Replicate video model (default: Luma Ray Flash 540p, 5s).",
            "parameters": {
                "type": "object",
                "required": ["prompt"],
                "properties": {
                    "prompt": {"type": "string"},
                    "model": {"type": "string"},
                    "duration": {"type": "integer"},
                    "aspect_ratio": {"type": "string"},
                },
            },
        },
        {
            "name": "run_model",
            "description": "Run any Replicate model not covered by the typed tools above. Pass owner/name + model-specific input.",
            "parameters": {
                "type": "object",
                "required": ["model", "input"],
                "properties": {"model": {"type": "string"}, "input": {"type": "object"}},
            },
        },
    ],
    "elevenlabs": [
        {
            "name": "text_to_speech",
            "description": "Convert text into an MP3 voiceover via ElevenLabs. Saves to Manor's filesystem; returns path + filename.",
            "parameters": {
                "type": "object",
                "required": ["text"],
                "properties": {
                    "text": {"type": "string"},
                    "voice_id": {"type": "string"},
                    "model_id": {"type": "string"},
                    "stability": {"type": "number"},
                    "similarity_boost": {"type": "number"},
                    "filename_hint": {"type": "string"},
                },
            },
        },
        {
            "name": "text_to_dialogue",
            "description": "Convert speaker turns into one multi-speaker dialogue audio file via ElevenLabs Text to Dialogue.",
            "parameters": {
                "type": "object",
                "required": ["inputs"],
                "properties": {
                    "inputs": {"type": "array", "items": {"type": "object"}},
                    "model_id": {"type": "string"},
                    "language_code": {"type": "string"},
                    "filename_hint": {"type": "string"},
                },
            },
        },
        {
            "name": "generate_sound_effect",
            "description": "Generate a sound-effect, Foley, or ambience audio file from text via ElevenLabs Sound Effects.",
            "parameters": {
                "type": "object",
                "required": ["text"],
                "properties": {
                    "text": {"type": "string"},
                    "duration_seconds": {"type": "number"},
                    "loop": {"type": "boolean"},
                    "prompt_influence": {"type": "number"},
                    "model_id": {"type": "string"},
                    "filename_hint": {"type": "string"},
                },
            },
        },
        {
            "name": "compose_music",
            "description": "Generate music or score audio via ElevenLabs Music. Use for BGM, themes, and stingers.",
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string"},
                    "composition_plan": {"type": "object"},
                    "music_length_ms": {"type": "integer"},
                    "force_instrumental": {"type": "boolean"},
                    "model_id": {"type": "string"},
                    "filename_hint": {"type": "string"},
                },
            },
        },
        {
            "name": "list_voices",
            "description": "List the user's available ElevenLabs voices (prebuilt + cloned) with their labels.",
            "parameters": {"type": "object", "properties": {}},
        },
    ],
    "tavily": [
        {
            "name": "search",
            "description": "Run a web search optimized for AI agents. Returns synthesized snippets, an optional 1-sentence answer, and (if requested) inline article body.",
            "parameters": {
                "type": "object",
                "required": ["query"],
                "properties": {
                    "query": {"type": "string"},
                    "max_results": {"type": "integer"},
                    "search_depth": {"type": "string"},
                    "topic": {"type": "string"},
                    "include_answer": {"type": "boolean"},
                    "include_raw_content": {"type": "boolean"},
                    "include_domains": {"type": "array", "items": {"type": "string"}},
                    "exclude_domains": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
        {
            "name": "extract",
            "description": "Pull clean article text from one or more URLs (use after search() when you need the full body).",
            "parameters": {
                "type": "object",
                "required": ["urls"],
                "properties": {
                    "urls": {"type": "array", "items": {"type": "string"}},
                    "include_images": {"type": "boolean"},
                },
            },
        },
    ],
    "jimeng": [
        {
            "name": "generate_image",
            "description": "Generate one or more images from a text prompt via Jimeng (即梦). Chinese prompts work best.",
            "parameters": {
                "type": "object",
                "required": ["prompt"],
                "properties": {
                    "prompt": {"type": "string"},
                    "model": {"type": "string"},
                    "ratio": {"type": "string"},
                    "resolution": {"type": "string"},
                    "n": {"type": "integer"},
                    "intelligent_ratio": {"type": "boolean"},
                },
            },
        },
        {
            "name": "edit_image",
            "description": "Image-to-image edit via Jimeng. Pass source image URL + transform instruction.",
            "parameters": {
                "type": "object",
                "required": ["prompt", "image_url"],
                "properties": {
                    "prompt": {"type": "string"},
                    "image_url": {"type": "string"},
                    "model": {"type": "string"},
                    "ratio": {"type": "string"},
                    "resolution": {"type": "string"},
                },
            },
        },
        {
            "name": "generate_video",
            "description": (
                "Generate a short video from a text prompt via Jimeng. Slow (1–4 minutes). "
                "Use only when the user explicitly asks for Jimeng/即梦 and the Jimeng integration is connected. "
                "For Manor's Account-selected video model, Seedance BYOK, Kling BYOK, uploaded media references, "
                "or generic video generation, use the first-party generate_file tool with kind='video' instead."
            ),
            "parameters": {
                "type": "object",
                "required": ["prompt"],
                "properties": {
                    "prompt": {"type": "string"},
                    "model": {"type": "string"},
                    "ratio": {"type": "string"},
                    "resolution": {"type": "string"},
                    "duration_seconds": {"type": "integer"},
                },
            },
        },
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
        out.append(
            {
                "name": t["name"],
                "description": t.get("description", ""),
                "parameters": (t.get("parameters") or t.get("inputSchema") or {"type": "object", "properties": {}}),
            }
        )
    return out


def _mcp_tool_result_to_text(result: dict) -> str:
    structured = result.get("structuredContent")
    if structured is None:
        structured = result.get("structured_content")
    if structured is not None:
        content = result.get("content") or []
        display_text = (
            str(content[0].get("text", ""))
            if content and isinstance(content[0], dict) and content[0].get("type") == "text"
            else json.dumps(structured, ensure_ascii=False)
        )
        return RuntimeStructuredToolResult(
            display_text,
            structured_content=structured,
        )
    content = result.get("content") or []
    if content and isinstance(content[0], dict) and content[0].get("type") == "text":
        return str(content[0].get("text", ""))
    return json.dumps(result, ensure_ascii=False)


def _bounded_all_account_result(result: Any) -> tuple[Any, bool]:
    """Keep one fan-out child from making the aggregate result unbounded."""
    try:
        serialized = json.dumps(result, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        serialized = str(result)
    limit = max(1, int(_MCP_ALL_ACCOUNTS_MAX_RESULT_CHARS))
    if len(serialized) <= limit:
        return result, False
    return {
        "truncated": True,
        "original_type": type(result).__name__,
        "preview": serialized[:limit],
    }, True


def _trusted_knowledge_artifacts(
    result: Any,
    *,
    transport: str,
    integration_scope: str | None,
) -> list[dict[str, str]]:
    if transport != "builtin" or str(integration_scope or "").lower() == "cli_worker":
        return []
    from packages.core.contracts.artifacts import extract_mcp_knowledge_artifacts

    return extract_mcp_knowledge_artifacts(result)


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
from packages.core.ai.mcp import whatsapp as _whatsapp_module  # noqa: E402
from packages.core.ai.mcp import gmail as _gmail_module  # noqa: E402
from packages.core.ai.mcp import email as _email_module  # noqa: E402
from packages.core.ai.mcp import google_calendar as _gcal_module  # noqa: E402
from packages.core.ai.mcp import manor_mcp_calendar as _manor_calendar_module  # noqa: E402
from packages.core.ai.mcp import manor_mcp_minutes as _manor_minutes_module  # noqa: E402
from packages.core.ai.mcp import google_drive as _gdrive_module  # noqa: E402
from packages.core.ai.mcp import notion as _notion_module  # noqa: E402
from packages.core.ai.mcp import outlook as _outlook_module  # noqa: E402
from packages.core.ai.mcp import onedrive as _onedrive_module  # noqa: E402
from packages.core.ai.mcp import ms_calendar as _mscal_module  # noqa: E402
from packages.core.ai.mcp import ms_teams as _msteams_module  # noqa: E402
from packages.core.ai.mcp import ms_excel as _msexcel_module  # noqa: E402
from packages.core.ai.mcp import linkedin as _linkedin_module  # noqa: E402
from packages.core.ai.mcp import twitter_x as _twitter_x_module  # noqa: E402
from packages.core.ai.mcp import alpaca_market_data as _alpaca_market_data_module  # noqa: E402
from packages.core.ai.mcp import alpha_vantage as _alpha_vantage_module  # noqa: E402
from packages.core.ai.mcp import twelve_data as _twelve_data_module  # noqa: E402

from packages.core.ai.mcp import youtube as _youtube_module  # noqa: E402
from packages.core.ai.mcp import tiktok as _tiktok_module  # noqa: E402
from packages.core.ai.mcp import shopify as _shopify_module  # noqa: E402
from packages.core.ai.mcp import woocommerce as _woocommerce_module  # noqa: E402
from packages.core.ai.mcp import square as _square_module  # noqa: E402
from packages.core.ai.mcp import tiktok_shop as _tiktok_shop_module  # noqa: E402
from packages.core.ai.mcp import amazon as _amazon_module  # noqa: E402
from packages.core.ai.mcp import twilio as _twilio_module  # noqa: E402
from packages.core.ai.mcp import webhook as _webhook_module  # noqa: E402
from packages.core.ai.mcp import quickbooks as _qb_module  # noqa: E402
from packages.core.ai.mcp import telegram as _telegram_module  # noqa: E402
from packages.core.ai.mcp import discord as _discord_module  # noqa: E402
from packages.core.ai.mcp import nango as _nango_module  # noqa: E402
from packages.core.services.official_remote_mcp import (  # noqa: E402
    OfficialRemoteMCPFactory,
    OfficialRemoteMCPProvider,
)

_SERVER_TOOL_SCHEMAS["github"] = _adapt_module_tools(_gh_module)
# quickbooks/telegram had hardcoded catalog entries above whose tool names
# and params had drifted from the real handlers (e.g. list_customers vs
# query_customers; telegram send_message's `text` vs the module's `content`).
# Auto-adapt so the agent sees exactly what dispatch accepts.
_SERVER_TOOL_SCHEMAS["quickbooks"] = _adapt_module_tools(_qb_module)
_SERVER_TOOL_SCHEMAS["telegram"] = _adapt_module_tools(_telegram_module)
_SERVER_TOOL_SCHEMAS["discord"] = _adapt_module_tools(_discord_module)
_SERVER_TOOL_SCHEMAS["facebook"] = _adapt_module_tools(_fb_module)
_SERVER_TOOL_SCHEMAS["whatsapp"] = _adapt_module_tools(_whatsapp_module)
_SERVER_TOOL_SCHEMAS["gmail"] = _adapt_module_tools(_gmail_module)
# Generic IMAP/SMTP email exposes a broader surface than the original
# hand-written catalog (threads, attachments, and draft operations).  Keep
# runtime registration sourced from the same live module catalog used by the
# capability Factory so selected action IDs are always executable.
_SERVER_TOOL_SCHEMAS["email"] = _adapt_module_tools(_email_module)
_SERVER_TOOL_SCHEMAS["google_calendar"] = _adapt_module_tools(_gcal_module)
_SERVER_TOOL_SCHEMAS["manor_mcp_calendar"] = _adapt_module_tools(_manor_calendar_module)
_SERVER_TOOL_SCHEMAS["manor_mcp_minutes"] = _adapt_module_tools(_manor_minutes_module)
_SERVER_TOOL_SCHEMAS["google_drive"] = _adapt_module_tools(_gdrive_module)
_SERVER_TOOL_SCHEMAS["notion"] = _adapt_module_tools(_notion_module)
_SERVER_TOOL_SCHEMAS["outlook"] = _adapt_module_tools(_outlook_module)
_SERVER_TOOL_SCHEMAS["onedrive"] = _adapt_module_tools(_onedrive_module)
_SERVER_TOOL_SCHEMAS["ms_calendar"] = _adapt_module_tools(_mscal_module)
_SERVER_TOOL_SCHEMAS["ms_teams"] = _adapt_module_tools(_msteams_module)
_SERVER_TOOL_SCHEMAS["ms_excel"] = _adapt_module_tools(_msexcel_module)
_SERVER_TOOL_SCHEMAS["nango"] = _adapt_module_tools(_nango_module)
# linkedin had 3 hardcoded entries (create_post, get_profile,
# comment_on_post) but the module exposes 17 tools (media uploads,
# org pages, post stats, etc.). Auto-adapt so the agent sees the
# full surface — the hardcoded subset above is overwritten here.
_SERVER_TOOL_SCHEMAS["linkedin"] = _adapt_module_tools(_linkedin_module)
# twitter_x also has a real in-process module; keep the deferred catalog in
# lockstep so workspace allowlists can use the same tool names that dispatch
# will actually accept (create_tweet, search_recent, get_mentions, etc.).
_SERVER_TOOL_SCHEMAS["twitter_x"] = _adapt_module_tools(_twitter_x_module)
_SERVER_TOOL_SCHEMAS["alpaca_market_data"] = _adapt_module_tools(
    _alpaca_market_data_module
)
_SERVER_TOOL_SCHEMAS["alpha_vantage"] = _adapt_module_tools(_alpha_vantage_module)
_SERVER_TOOL_SCHEMAS["twelve_data"] = _adapt_module_tools(_twelve_data_module)
_SERVER_TOOL_SCHEMAS["youtube"] = _adapt_module_tools(_youtube_module)
_SERVER_TOOL_SCHEMAS["tiktok"] = _adapt_module_tools(_tiktok_module)
_SERVER_TOOL_SCHEMAS["shopify"] = _adapt_module_tools(_shopify_module)
_SERVER_TOOL_SCHEMAS["woocommerce"] = _adapt_module_tools(_woocommerce_module)
_SERVER_TOOL_SCHEMAS["square"] = _adapt_module_tools(_square_module)
_SERVER_TOOL_SCHEMAS["tiktok_shop"] = _adapt_module_tools(_tiktok_shop_module)
_SERVER_TOOL_SCHEMAS["amazon"] = _adapt_module_tools(_amazon_module)
_SERVER_TOOL_SCHEMAS["twilio"] = _adapt_module_tools(_twilio_module)
_SERVER_TOOL_SCHEMAS["webhook"] = _adapt_module_tools(_webhook_module)
for _remote_provider in OfficialRemoteMCPProvider:
    _SERVER_TOOL_SCHEMAS[_remote_provider.value] = OfficialRemoteMCPFactory.tool_schemas(_remote_provider)


# ---------------------------------------------------------------------------
# Handler factory
# ---------------------------------------------------------------------------


def _build_handler(
    server_key: str,
    tool_name: str,
    *,
    effect: MCPActionEffect | None = None,
) -> Callable:
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

        resolved_context = kwargs.pop(_MCP_DISPATCH_CONTEXT_ARGUMENT, None)
        account_registry_snapshot = kwargs.pop(
            RUNTIME_MCP_ACCOUNT_REGISTRY_SNAPSHOT_ARGUMENT,
            None,
        )
        integration_registry = kwargs.pop(
            RUNTIME_MCP_INTEGRATION_REGISTRY_ARGUMENT,
            None,
        )
        allowed_account_ids = frozenset(
            str(account_id).strip()
            for account_id in (kwargs.pop(_MCP_ALLOWED_ACCOUNT_IDS_ARGUMENT, ()) or ())
            if str(account_id or "").strip()
        )
        accountless_platform = _is_accountless_platform_server(server_key)
        if accountless_platform:
            # Manor-funded providers have one platform credential and no tenant
            # account fan-out. Ignore account controls from stale schemas or a
            # model-generated optional argument instead of rejecting the call.
            kwargs.pop(INTEGRATION_ACCOUNT_ARGUMENT, None)
            kwargs.pop(INTEGRATION_ACCOUNT_SELECTION_ARGUMENT, None)
            kwargs.pop(INTEGRATION_ACCOUNT_CONTINUATION_ARGUMENT, None)
            kwargs.pop(RUNTIME_MCP_ALL_ACCOUNTS_CURSOR_ARGUMENT, None)
            public_fanout_token = ""
            runtime_fanout_cursor = ""
        else:
            public_fanout_token = str(
                kwargs.pop(INTEGRATION_ACCOUNT_CONTINUATION_ARGUMENT, None) or ""
            ).strip()
            runtime_fanout_cursor = str(
                kwargs.pop(RUNTIME_MCP_ALL_ACCOUNTS_CURSOR_ARGUMENT, None) or ""
            ).strip()
        if len(public_fanout_token) > INTEGRATION_ACCOUNT_CONTINUATION_MAX_CHARS:
            return json.dumps(
                {
                    "server": server_key,
                    "tool": tool_name,
                    "error": "invalid_all_account_continuation",
                    "reason": "The continuation token exceeds the supported size.",
                }
            )
        if isinstance(resolved_context, _MCPResolvedDispatchContext):
            server_row = resolved_context.server_row
            transport = resolved_context.transport
            module = resolved_context.module
            integration_registry = resolved_context.integration_registry
        else:
            # Resolve and validate the server once. ALL-mode child calls reuse
            # this immutable dispatch context and the same account snapshot.
            async with async_session() as db:
                server_row = (
                    await db.execute(select(MCPServer).where(MCPServer.server_key == server_key))
                ).scalar_one_or_none()

            if server_row is None:
                return json.dumps(
                    {
                        "error": f"Unknown MCP server '{server_key}' (no catalog row).",
                    }
                )

            if account_registry_snapshot is not None and not account_registry_snapshot.matches_server(server_row):
                from packages.core.ai.runtime.dynamic_mcp import (
                    RuntimeDynamicMCPFailureResultFactory,
                )

                return RuntimeDynamicMCPFailureResultFactory.stale_binding(
                    provider=server_key,
                    tool_name=tool_name,
                )

            transport = (server_row.transport or "builtin").lower()

            # Builtin path: validate the in-process module exists upfront.
            # Remote path: no module required.
            module = None
            if transport == "builtin":
                module = get_module(server_key)
                if module is None:
                    return json.dumps(
                        {
                            "error": f"No in-process MCP module for server '{server_key}'.",
                        }
                    )
            elif transport == "http":
                if not server_row.endpoint:
                    return json.dumps(
                        {
                            "error": f"Remote MCP server '{server_key}' has no endpoint URL.",
                        }
                    )
            else:
                return json.dumps(
                    {
                        "error": f"Unsupported MCP transport '{transport}' for '{server_key}'.",
                    }
                )

        from packages.core.ai.runtime.tool_discovery import (
            runtime_mcp_tool_supports_all_accounts,
        )
        from packages.core.services.integration_account_service import (
            IntegrationAccountFanoutResultFactory,
            IntegrationAccountFanoutStatus,
            IntegrationAccountSelectionMode,
            IntegrationRegistryLoadStatus,
            RuntimeIntegrationAccountCallPlanFactory,
            RuntimeIntegrationRegistryLoadResult,
            try_load_runtime_integration_registry,
        )

        try:
            selection_mode = IntegrationAccountSelectionMode.resolve(
                selector=kwargs.get(INTEGRATION_ACCOUNT_ARGUMENT),
                requested=kwargs.get(INTEGRATION_ACCOUNT_SELECTION_ARGUMENT),
            )
        except ValueError as exc:
            return json.dumps(
                {
                    "server": server_key,
                    "tool": tool_name,
                    "error": "invalid_integration_account_selection",
                    "reason": str(exc),
                }
            )

        if (public_fanout_token or runtime_fanout_cursor) and selection_mode is not IntegrationAccountSelectionMode.ALL:
            return json.dumps(
                {
                    "server": server_key,
                    "tool": tool_name,
                    "error": "invalid_integration_account_continuation",
                    "reason": (
                        f"{INTEGRATION_ACCOUNT_CONTINUATION_ARGUMENT} requires integration_account_selection='all'."
                    ),
                }
            )

        if selection_mode is IntegrationAccountSelectionMode.ALL:
            full_tool_name = f"mcp__{server_key}__{tool_name}"
            supports_all_accounts = (
                effect is MCPActionEffect.READ
                if effect is not None
                else runtime_mcp_tool_supports_all_accounts(full_tool_name)
            )
            if not supports_all_accounts:
                return json.dumps(
                    {
                        "server": server_key,
                        "tool": tool_name,
                        "error": "all_accounts_requires_read_only_tool",
                        "reason": (
                            "All-account execution is restricted to tools explicitly "
                            "classified as read-only. Select one account for this action."
                        ),
                    }
                )

            fanout_token_arguments = {
                key: value
                for key, value in kwargs.items()
                if key not in _MCP_RUNTIME_ONLY_ARGUMENT_KEYS
            }

            if integration_registry is None:
                async with async_session() as db:
                    registry_result = await try_load_runtime_integration_registry(
                        db,
                        user_id=user_id,
                        entity_id=entity_id,
                        provider_keys=[server_key],
                    )
            else:
                registry_result = RuntimeIntegrationRegistryLoadResult(
                    status=(
                        IntegrationRegistryLoadStatus.PARTIAL
                        if integration_registry.load_errors
                        else IntegrationRegistryLoadStatus.READY
                    ),
                    registry=integration_registry,
                    errors=integration_registry.load_errors,
                )
            if registry_result.registry is None:
                return json.dumps(
                    {
                        "server": server_key,
                        "tool": tool_name,
                        "error": "integration_account_registry_unavailable",
                        "reason": (
                            "The connected-account registry could not be loaded; all-account execution failed closed."
                        ),
                        "registry_status": registry_result.status.value,
                    }
                )
            if registry_result.status is IntegrationRegistryLoadStatus.PARTIAL:
                return json.dumps(
                    {
                        "server": server_key,
                        "tool": tool_name,
                        "error": "integration_account_registry_incomplete",
                        "reason": (
                            "The connected-account registry was only partially "
                            "loaded; all-account execution failed closed."
                        ),
                        "registry_status": registry_result.status.value,
                        "registry_errors": list(registry_result.errors),
                    }
                )

            plan = RuntimeIntegrationAccountCallPlanFactory.create(
                registry_result.registry.accounts_for(server_key),
                selection=selection_mode.value,
                allowed_account_ids=(allowed_account_ids if allowed_account_ids else None),
            )
            if not plan.accounts:
                return json.dumps(
                    {
                        "server": server_key,
                        "tool": tool_name,
                        "error": "credentials_unavailable",
                        "reason": f"No connected {server_key} accounts are callable.",
                    }
                )

            fanout_accounts = plan.accounts
            fanout_snapshot_account_ids = tuple(
                account.id for account in plan.accounts
            )
            fanout_total_account_count = len(fanout_snapshot_account_ids)
            fanout_offset = 0
            fanout_cursor_account_id = runtime_fanout_cursor
            if public_fanout_token:
                continuation_snapshot = IntegrationAccountFanoutResultFactory.continuation_snapshot(
                    public_fanout_token,
                    server=server_key,
                    tool=tool_name,
                    entity_id=entity_id,
                    user_id=user_id,
                    current_account_ids=fanout_snapshot_account_ids,
                    arguments=fanout_token_arguments,
                )
                if continuation_snapshot is None:
                    return json.dumps(
                        {
                            "server": server_key,
                            "tool": tool_name,
                            "error": "invalid_all_account_continuation",
                            "reason": (
                                "The continuation does not match this actor, query, "
                                "or connected-account snapshot. Restart the all-account call."
                            ),
                        }
                    )
                accounts_by_id = {account.id: account for account in plan.accounts}
                missing_account_ids = [
                    account_id
                    for account_id in continuation_snapshot.remaining_account_ids
                    if account_id not in accounts_by_id
                ]
                if missing_account_ids:
                    return json.dumps(
                        {
                            "server": server_key,
                            "tool": tool_name,
                            "error": "invalid_all_account_continuation",
                            "reason": (
                                "One or more remaining accounts from the original "
                                "snapshot are no longer callable. Restart the all-account call."
                            ),
                        }
                    )
                public_cursor_account_id = (
                    continuation_snapshot.remaining_account_ids[0]
                )
                if runtime_fanout_cursor and runtime_fanout_cursor != public_cursor_account_id:
                    return json.dumps(
                        {
                            "server": server_key,
                            "tool": tool_name,
                            "error": "invalid_integration_account_continuation",
                            "reason": "Conflicting public and runtime continuation tokens.",
                        }
                    )
                fanout_snapshot_account_ids = continuation_snapshot.account_ids
                fanout_total_account_count = len(fanout_snapshot_account_ids)
                fanout_offset = continuation_snapshot.next_offset
                fanout_cursor_account_id = public_cursor_account_id
                fanout_accounts = tuple(
                    accounts_by_id[account_id]
                    for account_id in continuation_snapshot.remaining_account_ids
                )
            elif fanout_cursor_account_id:
                fanout_offset = next(
                    (index for index, account in enumerate(plan.accounts) if account.id == fanout_cursor_account_id),
                    -1,
                )
                if fanout_offset < 0:
                    return json.dumps(
                        {
                            "server": server_key,
                            "tool": tool_name,
                            "error": "invalid_all_account_continuation",
                            "reason": ("The saved all-account cursor is no longer callable."),
                        }
                    )
                fanout_accounts = plan.accounts[fanout_offset:]

            if len(fanout_accounts) > 1:
                try:
                    IntegrationAccountFanoutResultFactory.continuation_token(
                        server=server_key,
                        tool=tool_name,
                        entity_id=entity_id,
                        user_id=user_id,
                        account_ids=fanout_snapshot_account_ids,
                        next_offset=fanout_offset + 1,
                        arguments=fanout_token_arguments,
                    )
                except ValueError:
                    return json.dumps(
                        {
                            "server": server_key,
                            "tool": tool_name,
                            "error": "all_account_continuation_snapshot_too_large",
                            "reason": (
                                "The connected-account snapshot is too large for "
                                "bounded all-account continuation. Select fewer accounts."
                            ),
                        }
                    )

            fanout_results: list[dict[str, Any]] = []
            failed_count = 0
            result_truncated_count = 0
            deadline_exhausted = False
            scheduled_accounts = fanout_accounts[
                : max(
                    1,
                    int(_MCP_ALL_ACCOUNTS_MAX_CALLS),
                )
            ]
            event_loop = asyncio.get_running_loop()
            deadline = event_loop.time() + max(0.001, float(_MCP_ALL_ACCOUNTS_TOTAL_TIMEOUT_SECONDS))
            for account in scheduled_accounts:
                remaining_seconds = deadline - event_loop.time()
                if remaining_seconds <= 0:
                    deadline_exhausted = True
                    break
                account_kwargs = dict(kwargs)
                account_kwargs.pop(INTEGRATION_ACCOUNT_SELECTION_ARGUMENT, None)
                account_kwargs[INTEGRATION_ACCOUNT_ARGUMENT] = account.id
                account_kwargs[_MCP_DISPATCH_CONTEXT_ARGUMENT] = _MCPResolvedDispatchContext(
                    server_row=server_row,
                    transport=transport,
                    module=module,
                    integration_registry=registry_result.registry,
                )
                try:
                    raw_result = await asyncio.wait_for(
                        _handler(
                            entity_id=entity_id,
                            user_id=user_id,
                            **account_kwargs,
                        ),
                        timeout=min(
                            max(
                                0.001,
                                float(_MCP_ALL_ACCOUNTS_CHILD_TIMEOUT_SECONDS),
                            ),
                            remaining_seconds,
                        ),
                    )
                except TimeoutError:
                    parsed_result = {
                        "server": server_key,
                        "tool": tool_name,
                        "error": "account_call_timeout",
                        "reason": ("This account call exceeded the bounded wait time."),
                    }
                    if event_loop.time() >= deadline:
                        deadline_exhausted = True
                except Exception:
                    logger.exception(
                        "MCP all-account child call failed: %s/%s account=%s",
                        server_key,
                        tool_name,
                        account.id,
                    )
                    parsed_result: Any = {
                        "server": server_key,
                        "tool": tool_name,
                        "error": "account_call_failed",
                        "reason": ("This account call failed before returning a provider result."),
                    }
                else:
                    structured_result = getattr(
                        raw_result,
                        "structured_content",
                        None,
                    )
                    if structured_result is not None:
                        parsed_result = structured_result
                    else:
                        try:
                            parsed_result = json.loads(raw_result)
                        except (TypeError, json.JSONDecodeError):
                            parsed_result = raw_result
                failed = isinstance(parsed_result, dict) and bool(parsed_result.get("error"))
                if failed:
                    failed_count += 1
                bounded_result, result_was_truncated = _bounded_all_account_result(parsed_result)
                if result_was_truncated:
                    result_truncated_count += 1
                fanout_results.append(
                    {
                        "integration_account_id": account.id,
                        "display_name": account.display_name,
                        "scope": account.scope.value,
                        "is_default": account.is_default,
                        "ok": not failed,
                        "result_truncated": result_was_truncated,
                        "result": bounded_result,
                    }
                )

            unattempted_accounts = fanout_accounts[len(fanout_results) :]
            status = (
                IntegrationAccountFanoutStatus.PARTIAL
                if (unattempted_accounts or result_truncated_count)
                else IntegrationAccountFanoutStatus.COMPLETE
            )
            if fanout_results and failed_count == len(fanout_results) and not unattempted_accounts:
                status = IntegrationAccountFanoutStatus.FAILED
            elif failed_count:
                status = IntegrationAccountFanoutStatus.PARTIAL
            return json.dumps(
                {
                    "server": server_key,
                    "tool": tool_name,
                    "integration_account_selection": plan.mode.value,
                    "status": status,
                    "registry_status": registry_result.status.value,
                    "registry_errors": list(registry_result.errors),
                    "total_account_count": fanout_total_account_count,
                    "account_offset": fanout_offset,
                    "account_count": len(fanout_results),
                    "failed_count": failed_count,
                    "result_truncated_count": result_truncated_count,
                    "omitted_account_count": len(unattempted_accounts),
                    "truncated": bool(unattempted_accounts or result_truncated_count),
                    "deadline_exhausted": deadline_exhausted,
                    "limits": {
                        "max_accounts": int(_MCP_ALL_ACCOUNTS_MAX_CALLS),
                        "per_account_timeout_seconds": float(_MCP_ALL_ACCOUNTS_CHILD_TIMEOUT_SECONDS),
                        "total_timeout_seconds": float(_MCP_ALL_ACCOUNTS_TOTAL_TIMEOUT_SECONDS),
                        "max_result_chars_per_account": int(_MCP_ALL_ACCOUNTS_MAX_RESULT_CHARS),
                    },
                    "continuation": (
                        IntegrationAccountFanoutResultFactory.continuation(
                            account_id=unattempted_accounts[0].id,
                            remaining_account_count=len(unattempted_accounts),
                            reason=("total_timeout" if deadline_exhausted else "account_limit"),
                            token=IntegrationAccountFanoutResultFactory.continuation_token(
                                server=server_key,
                                tool=tool_name,
                                entity_id=entity_id,
                                user_id=user_id,
                                account_ids=fanout_snapshot_account_ids,
                                next_offset=fanout_offset + len(fanout_results),
                                arguments=fanout_token_arguments,
                            ),
                        )
                        if unattempted_accounts
                        else None
                    ),
                    "results": fanout_results,
                },
                ensure_ascii=False,
            )

        async with async_session() as db:
            decision = await can_use_integration(
                db,
                user_id=user_id,
                entity_id=entity_id,
                provider=server_key,
                integration_account_id=kwargs.get(INTEGRATION_ACCOUNT_ARGUMENT),
                integration_registry=integration_registry,
                allow_env_fallback=False,
            )

        if not decision.allowed:
            # Cross-provider hint: when the primary provider is missing,
            # check if a capability-equivalent fallback IS connected and
            # tell the LLM to try that one instead.
            alt_hint = await _suggest_alternative_provider(
                server_key=server_key,
                tool_name=tool_name,
                user_id=user_id,
                entity_id=entity_id,
            )
            first_party_fallback = _FIRST_PARTY_TOOL_FALLBACKS.get((server_key, tool_name))
            first_party_hint = (
                f" If the user meant Manor's Account-selected model or BYOK, call `{first_party_fallback}` "
                "with the matching kind instead."
                if first_party_fallback
                else ""
            )
            reason = decision.reason + (f" {alt_hint}" if alt_hint else "") + first_party_hint
            return json.dumps(
                {
                    "server": server_key,
                    "tool": tool_name,
                    "error": "credentials_unavailable",
                    "reason": reason,
                    "scope": decision.scope,
                    "suggested_tool": (
                        first_party_fallback or (_ALT_TOOL_MAP.get((server_key, tool_name), None) if alt_hint else None)
                    ),
                }
            )

        # Resolve the bearer token from the Decision context (re-read because
        # can_use_integration returns the decision, not the token itself).
        # Local worker providers authenticate on the user's machine, so there
        # is no Manor-side bearer token to fetch.
        bearer_token = None
        should_resolve_bearer_token = True
        if should_resolve_bearer_token:
            bearer_token = await _resolve_bearer_token(
                server_key=server_key,
                user_id=user_id,
                entity_id=entity_id,
                scope=decision.scope,
                integration_account_id=decision.account_id,
                allow_env_fallback=False,
            )
        # Some context-only servers do not use bearer tokens; the module's
        # own dispatch logic decides whether the user can proceed.
        if not bearer_token and not _is_context_only_server(server_key):
            token_reason = (
                "The Manor platform credential is unavailable. Try again later."
                if decision.scope == "platform"
                else (
                    f"Permission check passed for '{server_key}' but no "
                    f"bearer token was available. Reconnect the integration."
                )
            )
            return json.dumps(
                {
                    "server": server_key,
                    "tool": tool_name,
                    "error": "token_resolution_failed",
                    "reason": token_reason,
                }
            )

        tool_kwargs = {key: value for key, value in kwargs.items() if key not in _MCP_RUNTIME_ONLY_ARGUMENT_KEYS}


        # Builtin: in-process module dispatch. Some private providers need
        # the calling user/entity context, so set it via hook when present.
        if transport == "builtin":
            set_ctx = getattr(module, "set_call_context", None)
            clear_ctx = getattr(module, "clear_call_context", None)
            if set_ctx:
                call_ctx = {"user_id": user_id, "entity_id": entity_id}
                if server_key == "manor_mcp_admin":
                    from packages.core.services.auth_context import current_mfa_verified

                    call_ctx["mfa_verified"] = "true" if current_mfa_verified() else "false"
                runtime_context = runtime_tool_call_context_from_kwargs(kwargs)
                if decision.account_id:
                    call_ctx[INTEGRATION_ACCOUNT_ARGUMENT] = decision.account_id
                for key in ("conversation_id", "workspace_id", "task_id"):
                    if kwargs.get(key):
                        call_ctx[key] = str(kwargs[key])
                active_message = runtime_active_user_message_from_context(kwargs)
                if active_message:
                    call_ctx["active_user_message"] = str(active_message)
                call_ctx.update(_runtime_workflow_call_context(runtime_context))
                set_ctx(call_ctx)
            try:
                result = await module.call_tool(tool_name, tool_kwargs, bearer_token or "")
            except Exception as e:
                logger.exception("MCP call failed: %s/%s", server_key, tool_name)
                return json.dumps(
                    {
                        "server": server_key,
                        "tool": tool_name,
                        "error": "call_failed",
                        "detail": str(e),
                    }
                )
            finally:
                if clear_ctx:
                    clear_ctx()
        else:
            # Remote MCP — forward to the vendor over JSON-RPC.
            from packages.core.ai.mcp._remote import (
                RemoteMCPClient,
                RemoteMCPError,
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
                    return json.dumps(
                        {
                            "server": server_key,
                            "tool": tool_name,
                            "error": "credentials_unavailable",
                            "reason": e.message,
                        }
                    )
                logger.warning("Remote MCP error %s/%s: %s", server_key, tool_name, e)
                return json.dumps(
                    {
                        "server": server_key,
                        "tool": tool_name,
                        "error": "remote_mcp_error",
                        "detail": str(e),
                    }
                )
            except Exception as e:
                logger.exception("Remote MCP call crashed %s/%s", server_key, tool_name)
                return json.dumps(
                    {
                        "server": server_key,
                        "tool": tool_name,
                        "error": "call_failed",
                        "detail": str(e),
                    }
                )

        # MCP tools/call response → string
        knowledge_artifacts = _trusted_knowledge_artifacts(
            result,
            transport=transport,
            integration_scope=decision.scope,
        )
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
            if decision.account_id:
                origin["integration_account_id"] = decision.account_id
                origin["integration_account_scope"] = decision.scope
            registered_count = await register_generated_files(
                output_text,
                entity_id=entity_id,
                user_id=user_id,
                source=server_key,
                tool_args=tool_kwargs,
                origin=origin,
                knowledge_artifacts=knowledge_artifacts,
            )
            logger.debug(
                "MCP artifact registration server=%s tool=%s contract=trusted_sidecar registered=%d",
                server_key,
                tool_name,
                registered_count,
            )
        except Exception:
            logger.debug("file_registrar failed for %s/%s", server_key, tool_name, exc_info=True)

        return output_text

    return _handler


_JSON_BLOB_PROVIDERS: set[str] = {
    "email",
    "twilio",
    "whatsapp",
    "webhook",
    "telegram",
    "discord",
    "wechat_personal",
    "wechat_official",
    # E-commerce: multi-field store credentials (domain + token, or
    # consumer key/secret) passed to the module as a JSON blob.
    "shopify",
    "woocommerce",
    "square",
    "quickbooks",
    # Marketplace sellers: app key/secret + token (TikTok Shop signs each
    # request) / LWA refresh-token bundle (Amazon SP-API).
    "tiktok_shop",
    "amazon",
    # Alpaca Market Data authenticates with an API key ID + secret pair.
    "alpaca_market_data",
}

# MCP modules that don't need a bearer_token because their auth lives
# elsewhere. Private cloud builds add paired local worker providers here.
_CONTEXT_ONLY_SERVERS: set[str] = {
    # Future cli_worker context-only servers go here as we add them.
}


def _is_context_only_server(server_key: str) -> bool:
    key = str(server_key or "").strip().lower()
    return key in _CONTEXT_ONLY_SERVERS or key.startswith("manor_mcp_")


def _is_accountless_platform_server(server_key: str) -> bool:
    """Return whether Cloud funds this provider without tenant accounts."""
    try:
        from packages.core.services.platform_managed_mcp import (
            is_platform_managed_mcp_server,
        )

        return is_platform_managed_mcp_server(server_key)
    except Exception:  # noqa: BLE001
        return False


_MCP_RUNTIME_ONLY_ARGUMENT_KEYS = frozenset(
    {
        *RUNTIME_TOOL_CONTEXT_KEYS,
        INTEGRATION_ACCOUNT_ARGUMENT,
        INTEGRATION_ACCOUNT_SELECTION_ARGUMENT,
        _MCP_DISPATCH_CONTEXT_ARGUMENT,
        _MCP_ALLOWED_ACCOUNT_IDS_ARGUMENT,
    }
)


# Capability-equivalent providers. When the primary is missing creds, we
# offer the alt as a suggestion so the LLM can switch tools instead of
# giving up. (provider, tool_name) → (alt_provider, alt_tool_name).
_ALT_TOOL_MAP: dict[tuple[str, str], tuple[str, str]] = {
    # Gmail ↔ Email (IMAP+SMTP) — any tool on one side maps to the same
    # tool on the other, since we kept the tool names aligned.
    ("gmail", "list_messages"): ("email", "list_messages"),
    ("gmail", "get_message"): ("email", "get_message"),
    ("gmail", "send_message"): ("email", "send_email"),
    ("email", "list_messages"): ("gmail", "list_messages"),
    ("email", "get_message"): ("gmail", "get_message"),
    ("email", "send_email"): ("gmail", "send_message"),
    # WhatsApp → Twilio — both can deliver text to a phone number, though
    # WhatsApp's customer-service window and template rules still apply.
    ("whatsapp", "send_text"): ("twilio", "send_sms"),
}


_FIRST_PARTY_TOOL_FALLBACKS: dict[tuple[str, str], str] = {
    ("jimeng", "generate_video"): "generate_file",
    ("replicate", "generate_video"): "generate_file",
}


async def _suggest_alternative_provider(
    *,
    server_key: str,
    tool_name: str,
    user_id: str,
    entity_id: str,
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
    return f"Alternative available: '{alt_provider}' is connected — try mcp__{alt_provider}__{alt_tool} instead."


async def _resolve_bearer_token(
    *,
    server_key: str,
    user_id: str,
    entity_id: str,
    scope: str,
    integration_account_id: str | None = None,
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


    nango_token_request: tuple[str, str, str] | None = None
    entity_fallback_token: str | None = None
    async with async_session() as db:
        provider_aliases = provider_key_aliases(server_key)
        if scope == "user":
            query = select(OAuthAccount).where(
                OAuthAccount.provider.in_(provider_aliases),
            )
            if integration_account_id:
                query = query.where(OAuthAccount.id == integration_account_id)
            else:
                query = query.where(OAuthAccount.user_id == user_id)
            rows = (await db.execute(query.order_by(OAuthAccount.created_at.desc()))).scalars().all()
            if rows:
                default = next(
                    (r for r in rows if isinstance(r.profile, dict) and r.profile.get("is_default")),
                    None,
                )
                chosen = default or rows[0]
                from packages.core.services.oauth_account_credentials import lease_oauth_account_tokens

                token = lease_oauth_account_tokens(
                    chosen,
                    requester_id=user_id,
                    reason=f"oauth.mcp.{server_key}",
                    requester_kind="agent",
                ).get("access_token")
                if token:
                    if server_key == "discord":
                        from packages.core.services.discord_app_config import (
                            resolve_discord_app_config,
                        )

                        profile = chosen.profile if isinstance(chosen.profile, dict) else {}
                        guild_id = str(profile.get("guild_id") or "").strip()
                        profile_application_id = str(profile.get("application_id") or "").strip()
                        app = await resolve_discord_app_config(db)
                        if app is None or not guild_id or profile_application_id != app.application_id:
                            return None
                        return json.dumps(
                            {
                                "bot_token": app.bot_token,
                                "application_id": app.application_id,
                                "guild_id": guild_id,
                            }
                        )
                    if server_key == "quickbooks":
                        profile = chosen.profile if isinstance(chosen.profile, dict) else {}
                        return json.dumps(
                            {
                                "access_token": token,
                                "realm_id": str(profile.get("realm_id") or "").strip(),
                            }
                        )
                    return token

        if scope == "entity":
            # Multi-account: prefer config.is_default first, fall back
            # to most-recent. Keeps send_email() deterministic when an
            # entity has several inboxes / bots / senders.
            query = select(Integration).where(
                Integration.entity_id == entity_id,
                Integration.provider.in_(provider_aliases),
                Integration.status == "active",
            )
            if integration_account_id:
                query = query.where(Integration.id == integration_account_id)
            rows = (await db.execute(query.order_by(Integration.created_at.desc()))).scalars().all()
            if rows:
                default = next(
                    (r for r in rows if isinstance(r.config, dict) and r.config.get("is_default")),
                    None,
                )
                row = default or rows[0]

                # Nango-backed Integration: the row only stores a
                # pointer (connection_id) -- the actual access token
                # lives inside Nango and refreshes on its own. Fetch
                # a fresh one per-call so we never persist stale
                # tokens locally.
                row_config = row.config if isinstance(row.config, dict) else {}
                nango_meta = row_config.get("nango")
                if not isinstance(nango_meta, dict):
                    nango_meta = {}
                connection_id = str(nango_meta.get("connection_id") or "").strip()
                if connection_id:
                    from packages.core.services.integration_account_service import (
                        nango_connection_matches_runtime_scope,
                    )

                    provider_config_key = str(nango_meta.get("provider_config_key") or server_key).strip()
                    if not nango_connection_matches_runtime_scope(
                        connection_id=connection_id,
                        entity_id=entity_id,
                        owner_user_id=row.owner_user_id,
                        provider=server_key,
                        provider_config_key=provider_config_key,
                    ):
                        logger.warning(
                            "Rejected out-of-scope Nango pointer for integration %s",
                            row.id,
                        )
                        return None
                    from packages.core.ai.mcp.nango import get_nango_secret

                    secret = await get_nango_secret(db, entity_id)
                    if server_key == "linkedin":
                        nango_ref = {
                            "via": "nango",
                            "provider_config_key": provider_config_key,
                            "connection_id": connection_id,
                        }
                        # Keep the resolved admin secret in the in-process
                        # pointer so LinkedIn can use entity-level fallback
                        # credentials without persisting or exposing tokens.
                        if secret:
                            nango_ref["nango_secret"] = secret
                        return json.dumps(nango_ref)
                    if secret:
                        nango_token_request = (
                            secret,
                            provider_config_key,
                            connection_id,
                        )
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
                    get_credential_service,
                    Requester,
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
                        server_key,
                        row.id,
                    )
                    creds = None

                if not creds and isinstance(row.credentials, dict):
                    # Dev/test and old plaintext rows may not have Vault
                    # configured. Prefer CredentialService when available,
                    # but keep these legacy rows callable instead of failing
                    # after permission has already passed.
                    legacy_creds = {k: v for k, v in row.credentials.items() if v is not None}
                    if legacy_creds:
                        creds = legacy_creds

                if creds:
                    # Multi-field credentials (IMAP+SMTP bundle, Twilio
                    # SID+token+number, webhook url+secret, …) — pass the
                    # whole dict as a JSON blob so the MCP module can
                    # decode it. Single-token providers keep the flat
                    # shape.
                    if server_key in _JSON_BLOB_PROVIDERS:
                        entity_fallback_token = json.dumps(creds)
                    else:
                        entity_fallback_token = (
                            creds.get("access_token")
                            or creds.get("secret_key")
                            or creds.get("api_key")
                        )

    if nango_token_request is not None:
        secret, provider_config_key, connection_id = nango_token_request
        token = await _fetch_token_via_nango_secret(
            secret=secret,
            provider_config_key=provider_config_key,
            connection_id=connection_id,
        )
        if token:
            return token
    return entity_fallback_token


async def _fetch_token_via_nango_secret(
    *,
    secret: str,
    provider_config_key: str,
    connection_id: str,
) -> str | None:
    """Fetch a Nango connection token without touching the database."""
    from packages.core.services.nango_bridge import fetch_nango_access_token

    return await fetch_nango_access_token(
        secret=secret,
        provider_config_key=provider_config_key,
        connection_id=connection_id,
    )


# ---------------------------------------------------------------------------
# Registration entrypoint
# ---------------------------------------------------------------------------


def _build_tool_name(server_key: str, tool_name: str) -> str:
    safe_server = server_key.replace("-", "_").replace(".", "_")
    safe_tool = tool_name.replace("-", "_").replace(".", "_")
    return f"mcp__{safe_server}__{safe_tool}"


class MCPToolRegistrationFactory:
    """Build one ToolPool schema/handler pair from a normalized MCP tool."""

    @classmethod
    def create(
        cls,
        server_key: str,
        tool_def: dict[str, Any],
        *,
        registered_name: str | None = None,
        effect: MCPActionEffect | None = None,
        supports_all_accounts: bool | None = None,
    ) -> tuple[str, dict[str, Any], Callable]:
        from packages.core.ai.runtime.tool_discovery import (
            runtime_mcp_tool_supports_all_accounts,
        )

        tool_name = str(tool_def["name"])
        mcp_name = registered_name or _build_tool_name(server_key, tool_name)
        resolved_effect = MCPActionEffect(effect) if effect is not None else None
        parameters = json.loads(json.dumps(tool_def.get("parameters", {"type": "object", "properties": {}})))
        properties = parameters.setdefault("properties", {})
        if not _is_context_only_server(server_key) and not _is_accountless_platform_server(server_key):
            properties.setdefault(
                INTEGRATION_ACCOUNT_ARGUMENT,
                dict(_INTEGRATION_ACCOUNT_PARAMETER),
            )
            resolved_all_account_support = (
                supports_all_accounts
                if supports_all_accounts is not None
                else (
                    resolved_effect is MCPActionEffect.READ
                    if resolved_effect is not None
                    else runtime_mcp_tool_supports_all_accounts(mcp_name)
                )
            )
            if resolved_all_account_support:
                properties.setdefault(
                    INTEGRATION_ACCOUNT_SELECTION_ARGUMENT,
                    dict(_INTEGRATION_ACCOUNT_SELECTION_PARAMETER),
                )
                properties.setdefault(
                    INTEGRATION_ACCOUNT_CONTINUATION_ARGUMENT,
                    dict(_INTEGRATION_ACCOUNT_CONTINUATION_PARAMETER),
                )
            else:
                properties.pop(INTEGRATION_ACCOUNT_SELECTION_ARGUMENT, None)
                properties.pop(INTEGRATION_ACCOUNT_CONTINUATION_ARGUMENT, None)
        schema = {
            "type": "function",
            "function": {
                "name": mcp_name,
                "description": f"[MCP:{server_key}] {tool_def['description']}",
                "parameters": parameters,
            },
        }
        return (
            mcp_name,
            schema,
            _build_handler(
                server_key,
                tool_name,
                effect=resolved_effect,
            ),
        )


def build_official_remote_dynamic_handler(
    provider: str,
    action: str,
    *,
    account_ids: tuple[str, ...] = (),
    effect: MCPActionEffect = MCPActionEffect.WRITE,
    requires_explicit_account: bool = False,
    supports_all_accounts: bool = True,
    incomplete_account_ids: tuple[str, ...] = (),
) -> Callable | None:
    """Build a handler from the lossless vendor identity found at runtime."""

    from packages.core.services.integration_account_service import (
        IntegrationAccountSelectionMode,
    )

    try:
        resolved_provider = OfficialRemoteMCPProvider(provider)
    except ValueError:
        return None
    resolved_action = str(action or "").strip()
    if not resolved_action:
        return None
    try:
        resolved_effect = MCPActionEffect(effect)
    except (TypeError, ValueError):
        resolved_effect = MCPActionEffect.WRITE
    base_handler = _build_handler(
        resolved_provider.value,
        resolved_action,
        effect=resolved_effect,
    )
    allowed_account_ids = frozenset(account_ids)

    async def _handler(
        entity_id: str = "",
        user_id: str = "",
        **kwargs: Any,
    ) -> str:
        selected = str(kwargs.get(INTEGRATION_ACCOUNT_ARGUMENT) or "").strip()
        try:
            selection_mode = IntegrationAccountSelectionMode.resolve(
                selector=selected,
                requested=kwargs.get(INTEGRATION_ACCOUNT_SELECTION_ARGUMENT),
            )
        except ValueError as exc:
            return json.dumps(
                {
                    "server": resolved_provider.value,
                    "tool": resolved_action,
                    "error": "invalid_integration_account_selection",
                    "reason": str(exc),
                }
            )
        if selection_mode is IntegrationAccountSelectionMode.ALL and not supports_all_accounts:
            return json.dumps(
                {
                    "server": resolved_provider.value,
                    "tool": resolved_action,
                    "error": "incompatible_account_contracts",
                    "reason": (
                        "This action exposes different contracts across connected "
                        "accounts. Select one exact account before executing it."
                    ),
                    "available_account_ids": list(account_ids),
                }
            )
        if (requires_explicit_account or incomplete_account_ids) and not selected:
            return json.dumps(
                {
                    "server": resolved_provider.value,
                    "tool": resolved_action,
                    "error": "integration_account_registry_incomplete",
                    "reason": (
                        "The connected-account registry is incomplete. Select an "
                        "exact known account or retry after the integration service recovers."
                    ),
                    "available_account_ids": list(account_ids),
                }
            )
        if selected and allowed_account_ids and selected not in allowed_account_ids:
            return json.dumps(
                {
                    "server": resolved_provider.value,
                    "tool": resolved_action,
                    "error": "tool_unavailable_for_account",
                    "reason": "This live MCP action is not exposed by the selected account.",
                    "available_account_ids": list(account_ids),
                }
            )
        if selection_mode is IntegrationAccountSelectionMode.DEFAULT and account_ids:
            kwargs[INTEGRATION_ACCOUNT_ARGUMENT] = account_ids[0]
        if selection_mode is IntegrationAccountSelectionMode.ALL and allowed_account_ids:
            kwargs[_MCP_ALLOWED_ACCOUNT_IDS_ARGUMENT] = account_ids
        return await base_handler(entity_id=entity_id, user_id=user_id, **kwargs)

    return _handler


_OFFICIAL_REMOTE_MCP_DISCOVERY_MAX_CONCURRENCY = 4
_OFFICIAL_REMOTE_MCP_DISCOVERY_TIMEOUT_SECONDS = 20.0
_OFFICIAL_REMOTE_MCP_DISCOVERY_TOTAL_TIMEOUT_SECONDS = 60.0


@dataclass(frozen=True, slots=True)
class _OfficialRemoteMCPAccountDiscoveryPlan:
    account: RuntimeIntegrationAccount
    credential_scope: str
    credential_account_id: str | None


@dataclass(frozen=True, slots=True)
class _OfficialRemoteMCPProviderDiscoveryPlan:
    provider_key: str
    provider: OfficialRemoteMCPProvider
    endpoint: str
    token_in: str
    accounts: tuple[_OfficialRemoteMCPAccountDiscoveryPlan, ...]
    requires_explicit_account: bool
    registry_account_ids: tuple[str, ...]
    registry_status: IntegrationRegistryLoadStatus


@dataclass(frozen=True, slots=True)
class _OfficialRemoteMCPAccountDiscoveryResult:
    provider: _OfficialRemoteMCPProviderDiscoveryPlan
    account: _OfficialRemoteMCPAccountDiscoveryPlan
    live_tools: list[dict[str, Any]] | None


async def _discover_official_remote_mcp_account_tools(
    provider_plan: _OfficialRemoteMCPProviderDiscoveryPlan,
    account_plan: _OfficialRemoteMCPAccountDiscoveryPlan,
    *,
    entity_id: str,
    user_id: str,
    semaphore: asyncio.Semaphore,
) -> _OfficialRemoteMCPAccountDiscoveryResult:
    """Perform credential lookup and vendor I/O without an owning DB session."""

    from packages.core.ai.mcp._remote import list_tools_cached

    async with semaphore:
        token = await _resolve_bearer_token(
            server_key=provider_plan.provider_key,
            user_id=user_id,
            entity_id=entity_id,
            scope=account_plan.credential_scope,
            integration_account_id=account_plan.credential_account_id,
            allow_env_fallback=False,
        )
        if not token:
            return _OfficialRemoteMCPAccountDiscoveryResult(
                provider=provider_plan,
                account=account_plan,
                live_tools=None,
            )
        try:
            live_tools = await asyncio.wait_for(
                list_tools_cached(
                    provider_plan.endpoint,
                    token,
                    token_in=provider_plan.token_in,
                ),
                timeout=_OFFICIAL_REMOTE_MCP_DISCOVERY_TIMEOUT_SECONDS,
            )
        except Exception:
            logger.warning(
                "Official remote MCP discovery failed for %s account=%s",
                provider_plan.provider_key,
                account_plan.account.id,
                exc_info=True,
            )
            live_tools = None
        return _OfficialRemoteMCPAccountDiscoveryResult(
            provider=provider_plan,
            account=account_plan,
            live_tools=live_tools,
        )


async def discover_official_remote_tool_schemas(
    *,
    provider_keys: frozenset[str],
    entity_id: str,
    user_id: str,
) -> dict[str, list[DiscoveredOfficialRemoteMCPTool]]:
    """Discover actor-scoped vendor schemas without mutating the global pool."""

    if not entity_id or not user_id or not provider_keys:
        return {}

    from sqlalchemy import select

    from packages.core.database import async_session
    from packages.core.models.mcp import MCPServer
    from packages.core.services.agent_permission_service import can_use_integration
    from packages.core.services.integration_account_service import (
        load_runtime_integration_registry,
    )
    from packages.core.services.mcp_account_tool_catalog import (
        MCPAccountToolCatalogFactory,
        invalidate_mcp_account_tool_catalog,
        persist_mcp_account_tool_catalog,
    )

    providers = {provider.value: provider for provider in OfficialRemoteMCPProvider if provider.value in provider_keys}
    if not providers:
        return {}

    discovery_plans: list[_OfficialRemoteMCPProviderDiscoveryPlan] = []
    async with async_session() as db:
        rows = list((await db.execute(select(MCPServer).where(MCPServer.server_key.in_(providers)))).scalars().all())
        by_provider = {row.server_key: row for row in rows}
        registry = await load_runtime_integration_registry(
            db,
            user_id=user_id,
            entity_id=entity_id,
            provider_keys=providers,
        )
        for provider_key, provider in providers.items():
            registry_binding = registry.integration(provider_key)
            requires_explicit_account = bool(registry_binding and registry_binding.requires_explicit_account)
            registry_account_ids = tuple(account.id for account in registry.accounts_for(provider_key))
            registry_status = (
                IntegrationRegistryLoadStatus.PARTIAL
                if requires_explicit_account
                else IntegrationRegistryLoadStatus.READY
            )
            server_row = by_provider.get(provider_key)
            if server_row is None or server_row.transport != "http" or not server_row.endpoint:
                continue
            account_plans: list[_OfficialRemoteMCPAccountDiscoveryPlan] = []
            for account in registry.accounts_for(provider_key):
                decision = await can_use_integration(
                    db,
                    user_id=user_id,
                    entity_id=entity_id,
                    provider=provider_key,
                    integration_account_id=account.id,
                    integration_registry=registry,
                    allow_env_fallback=False,
                )
                if decision.allowed:
                    account_plans.append(
                        _OfficialRemoteMCPAccountDiscoveryPlan(
                            account=account,
                            credential_scope=decision.scope,
                            credential_account_id=decision.account_id,
                        )
                    )
            discovery_plans.append(
                _OfficialRemoteMCPProviderDiscoveryPlan(
                    provider_key=provider_key,
                    provider=provider,
                    endpoint=str(server_row.endpoint),
                    token_in=str(
                        (server_row.default_config or {}).get(
                            "mcp_token_in",
                            "header",
                        )
                    ),
                    accounts=tuple(account_plans),
                    requires_explicit_account=requires_explicit_account,
                    registry_account_ids=registry_account_ids,
                    registry_status=registry_status,
                )
            )

    semaphore = asyncio.Semaphore(
        _OFFICIAL_REMOTE_MCP_DISCOVERY_MAX_CONCURRENCY,
    )
    completed_results: dict[
        tuple[str, str],
        _OfficialRemoteMCPAccountDiscoveryResult,
    ] = {}

    async def discover_account(
        provider_plan: _OfficialRemoteMCPProviderDiscoveryPlan,
        account_plan: _OfficialRemoteMCPAccountDiscoveryPlan,
    ) -> None:
        result = await _discover_official_remote_mcp_account_tools(
            provider_plan,
            account_plan,
            entity_id=entity_id,
            user_id=user_id,
            semaphore=semaphore,
        )
        completed_results[(provider_plan.provider_key, account_plan.account.id)] = result

    discovery_tasks = [
        asyncio.create_task(discover_account(provider_plan, account_plan))
        for provider_plan in discovery_plans
        for account_plan in provider_plan.accounts
    ]
    done_tasks: set[asyncio.Task[None]] = set()
    pending_tasks: set[asyncio.Task[None]] = set()
    if discovery_tasks:
        try:
            done_tasks, pending_tasks = await asyncio.wait(
                discovery_tasks,
                timeout=_OFFICIAL_REMOTE_MCP_DISCOVERY_TOTAL_TIMEOUT_SECONDS,
                return_when=asyncio.FIRST_EXCEPTION,
            )
        except BaseException:
            for task in discovery_tasks:
                task.cancel()
            await asyncio.gather(*discovery_tasks, return_exceptions=True)
            raise

    failed_task = next(
        (
            task
            for task in done_tasks
            if not task.cancelled() and task.exception() is not None
        ),
        None,
    )
    if failed_task is not None:
        for task in pending_tasks:
            task.cancel()
        await asyncio.gather(*discovery_tasks, return_exceptions=True)
        failed_task.result()
    elif pending_tasks:
        for task in pending_tasks:
            task.cancel()
        await asyncio.gather(*pending_tasks, return_exceptions=True)
        logger.warning(
            "Official remote MCP discovery exceeded the %.1fs total deadline",
            _OFFICIAL_REMOTE_MCP_DISCOVERY_TOTAL_TIMEOUT_SECONDS,
        )

    discovery_results = [
        completed_results.get(
            (provider_plan.provider_key, account_plan.account.id),
            _OfficialRemoteMCPAccountDiscoveryResult(
                provider_plan,
                account_plan,
                None,
            ),
        )
        for provider_plan in discovery_plans
        for account_plan in provider_plan.accounts
    ]

    if discovery_results:
        async with async_session() as db:
            catalog_changed = False
            for result in discovery_results:
                if result.live_tools is None:
                    await invalidate_mcp_account_tool_catalog(
                        db,
                        provider=result.provider.provider_key,
                        account=result.account.account,
                    )
                    catalog_changed = True
                    continue
                try:
                    async with db.begin_nested():
                        await persist_mcp_account_tool_catalog(
                            db,
                            provider=result.provider.provider_key,
                            account=result.account.account,
                            endpoint=result.provider.endpoint,
                            raw_tools=result.live_tools,
                        )
                    catalog_changed = True
                except Exception:
                    logger.warning(
                        "Could not persist MCP account tool catalog for %s account=%s",
                        result.provider.provider_key,
                        result.account.account.id,
                        exc_info=True,
                    )
            if catalog_changed:
                await db.commit()

    results_by_provider_account = {
        (result.provider.provider_key, result.account.account.id): result for result in discovery_results
    }
    discovered: dict[str, list[DiscoveredOfficialRemoteMCPTool]] = {}
    for provider_plan in discovery_plans:
        provider_key = provider_plan.provider_key
        provider = provider_plan.provider
        tools_by_action: dict[str, dict[str, Any]] = {}
        contract_by_action: dict[str, str] = {}
        account_ids_by_action: dict[str, list[str]] = {}
        account_input_schemas_by_action: dict[str, dict[str, dict[str, Any]]] = {}
        effects_by_action: dict[str, MCPActionEffect] = {}
        conflicting_actions: set[str] = set()
        incomplete_account_ids: list[str] = []
        for account_plan in provider_plan.accounts:
            account = account_plan.account
            result = results_by_provider_account.get((provider_key, account.id))
            if result is None or result.live_tools is None:
                incomplete_account_ids.append(account.id)
                continue
            live_tools = result.live_tools
            normalized = OfficialRemoteMCPFactory.tool_schemas(
                provider,
                discovered_tools=live_tools,
            )
            for tool_def in normalized:
                action = str(tool_def.get("name") or "").strip()
                if not action:
                    continue
                contract = MCPAccountToolCatalogFactory.contract_signature(tool_def)
                if action in contract_by_action and contract_by_action[action] != contract:
                    conflicting_actions.add(action)
                tools_by_action.setdefault(action, tool_def)
                contract_by_action.setdefault(action, contract)
                account_ids = account_ids_by_action.setdefault(action, [])
                if account.id not in account_ids:
                    account_ids.append(account.id)
                    account_input_schemas_by_action.setdefault(action, {})[account.id] = (
                        MCPAccountToolCatalogFactory.input_schema(tool_def)
                    )
                annotations = tool_def.get("annotations")
                account_effect = OfficialRemoteMCPFactory.resolve_action_effect(
                    provider,
                    action,
                    annotations=(annotations if isinstance(annotations, dict) else None),
                )
                current_effect = effects_by_action.get(action)
                if (
                    current_effect is None
                    or _MCP_ACTION_EFFECT_RANK[account_effect] > _MCP_ACTION_EFFECT_RANK[current_effect]
                ):
                    effects_by_action[action] = account_effect

        if not tools_by_action:
            continue
        provider_tools: list[DiscoveredOfficialRemoteMCPTool] = []
        for action, base_tool_def in tools_by_action.items():
            tool_def = dict(base_tool_def)
            contracts_compatible = action not in conflicting_actions
            if not contracts_compatible:
                tool_def["parameters"] = MCPAccountToolCatalogFactory.runtime_input_schema(
                    account_input_schemas_by_action[action],
                    account_argument=INTEGRATION_ACCOUNT_ARGUMENT,
                )
            effect = effects_by_action[action]
            registered_name = OfficialRemoteMCPToolNameFactory.create(
                provider,
                action,
            )
            name, schema, _handler = MCPToolRegistrationFactory.create(
                provider_key,
                tool_def,
                registered_name=registered_name,
                effect=effect,
                supports_all_accounts=contracts_compatible,
            )
            provider_tools.append(
                DiscoveredOfficialRemoteMCPTool(
                    name=name,
                    schema=schema,
                    provider=provider_key,
                    action=action,
                    account_ids=tuple(account_ids_by_action[action]),
                    effect=effect,
                    requires_explicit_account=(
                        provider_plan.requires_explicit_account
                        or bool(incomplete_account_ids)
                        or action in conflicting_actions
                    ),
                    supports_all_accounts=contracts_compatible,
                    incomplete_account_ids=tuple(incomplete_account_ids),
                    registry_account_ids=provider_plan.registry_account_ids,
                    registry_status=provider_plan.registry_status,
                    endpoint=provider_plan.endpoint,
                    token_in=provider_plan.token_in,
                )
            )
        discovered[provider_key] = provider_tools
    return discovered


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
            _name, schema, handler = MCPToolRegistrationFactory.create(
                server_key,
                tool_def,
            )
            out.append((schema, handler))
            total += 1
    logger.info("Built-in MCP catalog: %d tools across %d servers", total, len(_SERVER_TOOL_SCHEMAS))
    return out
