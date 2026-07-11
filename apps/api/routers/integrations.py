"""Integration & Channel endpoints — CRUD for external integrations and channels."""
from __future__ import annotations

import logging
import os
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlsplit, urlunsplit

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.deps import get_current_user
from packages.core.constants.plans import is_dev
from packages.core.database import get_db
from packages.core.models.user import User
from packages.core.services.integration_service import (
    list_integrations, get_integration, create_integration, update_integration,
    list_channels, get_channel, create_channel, update_channel, delete_channel,
)
from packages.core.services.provider_keys import (
    canonical_provider_key,
    provider_key_aliases,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/integrations", tags=["integrations"])

_HIDDEN_CATALOG_SERVER_KEYS = {
    "nango",          # plumbing layer; per-provider cards own user flows
}


def _is_hidden_catalog_server_key(server_key: str | None) -> bool:
    key = str(server_key or "").strip().lower()
    return key.startswith("manor_mcp_") or key in _HIDDEN_CATALOG_SERVER_KEYS


def _hide_coming_soon_in_dev() -> bool:
    """Whether to omit ``coming_soon`` cards from the catalog response.

    True only when ALL of the following hold:
      * MANOR_ENV says we're in a dev/local/development environment
      * MANOR_SHOW_COMING_SOON is NOT explicitly enabled (operator
        opt-in to see the in-progress cards on their own machine)

    Production always shows the full catalog (with the "Coming soon"
    badge as before) so users can see what's on the roadmap.
    """
    if not is_dev():
        return False
    show_override = os.environ.get("MANOR_SHOW_COMING_SOON", "").strip().lower()
    if show_override in {"1", "true", "yes", "on"}:
        return False
    return True


# ── MCP server status (per-user + per-entity live view) ─────────────────────

class WiringStatus(BaseModel):
    """Sub-check for providers with an inbound webhook: is the upstream
    actually configured to deliver to our server?"""
    ok: bool | None = None
    detail: str | None = None
    configured_url: str | None = None
    expected_url: str | None = None
    last_error: str | None = None
    pending_update_count: int | None = None


class HealthStatus(BaseModel):
    ok: bool | None = None           # None when never tested
    detail: str | None = None
    latency_ms: float | None = None
    checked_at: str | None = None
    wiring: WiringStatus | None = None


class UserOAuthConnection(BaseModel):
    """A single OAuth account a user has connected for this provider."""
    id: str                          # oauth_accounts.id
    display_name: str | None = None  # e.g. "jane@work.com" or profile name
    provider_user_id: str            # the provider's user id
    expires_at: str | None = None    # ISO; null = never expires / unknown
    is_default: bool = False
    connected_at: str | None = None  # ISO; useful for "most recent"
    health: HealthStatus | None = None


class EntityAccountConnection(BaseModel):
    """A single entity-level account for this provider — used by
    credential/api-key based integrations that can have multiple rows
    per (entity, provider): multiple email inboxes, WhatsApp senders,
    WeChat bots, webhook endpoints, etc."""
    id: str                          # integrations.id
    name: str | None = None          # admin-set label ("Support inbox")
    display_name: str | None = None  # summary pulled from credentials
    is_default: bool = False
    created_at: str | None = None
    status: str = "active"
    health: HealthStatus | None = None


class MCPServerStatus(BaseModel):
    server_key: str
    name: str
    category: str | None = None
    description: str | None = None
    auth_type: str              # oauth2 | api_key | bearer | none
    scopes: str | None = None

    # Discovery metadata — drives the catalog card visuals
    tagline: str | None = None
    docs_url: str | None = None
    setup_hint: str | None = None
    color_hex: str | None = None
    supports_multi_account: bool = False

    # "What can my agent do once connected?" — surfaced via the ?
    # info button on each card. Empty list hides the button.
    capabilities: list[str] = []
    # 1-3 example agent prompts that exercise this integration.
    # Shown as quoted bullets in the same popover.
    example_prompts: list[str] = []

    # Per-user personal connections — zero or more OAuth accounts
    connections: list[UserOAuthConnection] = []

    # Per-entity credentials — zero or more accounts for credential /
    # api-key providers (email, WeChat, WhatsApp, Telegram bot, Twilio,
    # webhook, …). OAuth providers usually have this empty.
    entity_accounts: list[EntityAccountConnection] = []

    # Per-entity shared credential — legacy bool kept in sync with
    # entity_accounts presence.
    entity_connected: bool
    required_permission: str | None = None
    user_has_required_permission: bool = False

    # What an agent acting as the current user can actually do right now
    agent_can_use: bool
    hint: str

    # ── Nango bridge metadata ──
    # When the entity has a Nango admin Integration set up AND the
    # provider is configured in Nango admin, the frontend prefers a
    # Nango-managed Connect popup over direct OAuth. The end user never
    # sees "Nango" in the UI -- this flag just controls which connect
    # endpoint the card's button hits.
    nango_provider_config_key: str | None = None

    # ── OAuth readiness ──
    # True when this deployment has client_id/secret configured for the
    # provider (either via env bootstrap or admin-UI override). Drives
    # the "Connect" CTA — if false, end users see "OAuth not configured
    # yet" instead of a dead button.
    oauth_configured: bool = False

    # ── Type-specific spec for non-OAuth/non-credentials AI tools ──
    # Populated for browser-session tools so the frontend can render the
    # headed-login flow.
    cli_spec: dict | None = None
    browser_spec: dict | None = None

    # Whether this integration is not yet production-ready. The frontend
    # disables the connect button and shows "Coming soon" when true.
    # Single source of truth: _COMING_SOON_SERVERS in integration_service.
    coming_soon: bool = False

    # Legacy flags — kept for frontend compat. Mirror the first/any element
    # of connections[] so existing UIs don't break.
    user_connected: bool = False
    user_expires_at: str | None = None


def _first_display_value(*values: object) -> str | None:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def _safe_entity_account_display_name(row, *, requester_id: str) -> str | None:
    """Return a user-facing account label without exposing secrets.

    New integrations store credentials in the credential vault, so
    ``row.credentials`` is often empty. Prefer non-secret config fields
    written by setup forms, and only lease credentials to read safe
    identifiers such as email/username when old rows lack config labels.
    """
    cfg = row.config or {}
    profile = cfg.get("profile") if isinstance(cfg.get("profile"), dict) else {}
    legacy_creds = row.credentials or {}

    display = _first_display_value(
        profile.get("display_name"),
        profile.get("email"),
        profile.get("name"),
        cfg.get("display_name"),
        cfg.get("email"),
        cfg.get("from_address"),
        cfg.get("from_email"),
        cfg.get("username"),
        cfg.get("phone_number"),
        cfg.get("account_sid"),
        cfg.get("app_id"),
        cfg.get("url"),
        cfg.get("webhook_url"),
        legacy_creds.get("from_address"),
        legacy_creds.get("from_email"),
        legacy_creds.get("email"),
        legacy_creds.get("username"),
        legacy_creds.get("phone_number"),
        legacy_creds.get("account_sid"),
        legacy_creds.get("app_id"),
        legacy_creds.get("url"),
        legacy_creds.get("webhook_url"),
        cfg.get("name"),
    )
    if display:
        return display

    try:
        from packages.core.credentials import Requester, get_credential_service

        creds = get_credential_service().lease_integration(
            row,
            requester=Requester(kind="user", id=requester_id),
            reason="integrations.mcp_servers.entity_account_display",
        )
    except Exception:
        # Display-label lookup must never fail the listing. Any credential
        # error — a domain CredentialError, or the key backend being
        # unreachable (e.g. Vault down / not running in local dev) — falls
        # back to the plain config name.
        logger.debug("Could not lease credentials for integration display label", exc_info=True)
        return _first_display_value(cfg.get("name"))

    return _first_display_value(
        creds.get("from_address"),
        creds.get("from_email"),
        creds.get("email"),
        creds.get("username"),
        creds.get("phone_number"),
        creds.get("account_sid"),
        creds.get("app_id"),
        creds.get("url"),
        creds.get("webhook_url"),
        cfg.get("name"),
    )




def _string_list(value) -> list[str]:
    if not isinstance(value, (list, tuple, set)):
        return []
    out: list[str] = []
    for item in value:
        text = str(item or "").strip()
        if text and text not in out:
            out.append(text)
    return out


# Hardcoded display metadata per provider — same role the old Vue repo's
# ``apiKeyIntegrationDefs`` + card list played. Merged into the live
# ``mcp_servers`` row for the Integrations catalog. Keep lowercase keys
# matching ``mcp_servers.server_key``.
_PROVIDER_DISPLAY: dict[str, dict] = {
    "gmail": {"category": "Email", "tagline": "Send, read, and manage email from agents.",
              "docs_url": "https://developers.google.com/gmail/api",
              "setup_hint": "Enable the Gmail API in a Google Cloud project.",
              "color_hex": "#EA4335", "supports_multi_account": True},
    "email": {"category": "Email", "tagline": "Read, write, and send email on any IMAP/SMTP account.",
              "capabilities": [
                  "Read inbox, mark as read, file into folders",
                  "Reply / forward / send new emails",
                  "Attach files, schedule sends",
                  "Trigger agents on incoming mail (channel binding)",
              ],
              "example_prompts": [
                  "Every hour read new mail from billing@ and create a Stripe customer if it's a signup.",
                  "Reply to support@ inquiries in my voice, escalate to me only if the customer is angry.",
                  "Send a weekly digest summarizing all customer feedback emails by topic.",
              ],
              "docs_url": "https://en.wikipedia.org/wiki/Internet_Message_Access_Protocol",
              "setup_hint": "Point to your IMAP + SMTP servers; one credential bundle covers inbox + outbox.",
              "color_hex": "#0EA5E9", "supports_multi_account": False},
    "google_calendar": {"category": "Productivity", "tagline": "Read and create calendar events.",
                        "docs_url": "https://developers.google.com/calendar/api",
                        "setup_hint": "Enable the Calendar API in a Google Cloud project.",
                        "color_hex": "#4285F4", "supports_multi_account": True},
    "google_drive": {"category": "Productivity", "tagline": "List, download, and upload Drive files.",
                     "docs_url": "https://developers.google.com/drive/api",
                     "setup_hint": "Enable the Drive API in a Google Cloud project.",
                     "color_hex": "#0F9D58", "supports_multi_account": True},
    "notion": {"category": "Productivity", "tagline": "Read and write Notion pages and databases.",
               "capabilities": [
                   "Read + write pages and database rows",
                   "Search across your workspace",
                   "Create new pages, append blocks, format rich text",
               ],
               "example_prompts": [
                   "When a new feature ships, append a release note to the 'Changelog' page.",
                   "Each Friday, summarize this week's customer calls into the CRM database.",
                   "Find every page tagged 'TODO' older than 30 days and ping the owner.",
               ],
               "docs_url": "https://developers.notion.com",
               "setup_hint": "Create an internal integration at notion.so/my-integrations.",
               "color_hex": "#000000", "supports_multi_account": True},
    "slack": {"category": "Messaging", "tagline": "Send messages and alerts to Slack channels.",
              "capabilities": [
                  "Post to channels + threads as the bot",
                  "Read channel history, search messages",
                  "React, post files, mention users",
                  "Receive @mentions and route to agents (channel binding)",
              ],
              "example_prompts": [
                  "Whenever a Stripe charge fails, post a heads-up in #revenue with the customer + reason.",
                  "Summarize #engineering's discussion every evening and post the digest in #leadership.",
                  "Auto-reply when someone asks 'where is the runbook?' with the Notion link.",
              ],
              "docs_url": "https://api.slack.com/start/apps",
              "setup_hint": "Create a Slack app at api.slack.com/apps and install it.",
              "color_hex": "#4A154B", "supports_multi_account": True},
    "discord": {"category": "Messaging", "tagline": "Post messages and reactions in Discord servers.",
                "docs_url": "https://discord.com/developers/docs/intro",
                "setup_hint": "Create a bot at discord.com/developers/applications.",
                "color_hex": "#5865F2", "supports_multi_account": True},
    "telegram": {"category": "Messaging", "tagline": "Send messages, photos, and files via a bot.",
                 "capabilities": [
                     "Receive any DM / group message routed to the bot",
                     "Reply with text, photo, document, voice",
                     "Trigger agents on inbound (channel binding)",
                 ],
                 "example_prompts": [
                     "When a user sends a question to my bot, answer in their language.",
                     "Forward urgent messages from VIP contacts to my email if I haven't replied in 30 min.",
                 ],
                 "docs_url": "https://core.telegram.org/bots/api",
                 "setup_hint": "Create a bot by chatting with @BotFather in Telegram.",
                 "color_hex": "#229ED9", "supports_multi_account": True},
    "wechat_personal": {"category": "Messaging",
                        "tagline": "Personal WeChat bot — groups and 1:1 chats via QR login.",
                        "docs_url": "https://itchat.readthedocs.io/",
                        "setup_hint": "Point the bot runner URL; scan the QR code to log the bot in.",
                        "color_hex": "#07C160", "supports_multi_account": True},
    "wechat_official": {"category": "Messaging",
                        "tagline": "WeChat Official Account (公众号) — customer service and template messages.",
                        "docs_url": "https://developers.weixin.qq.com/doc/offiaccount/Getting_Started/Overview.html",
                        "setup_hint": "Create a Subscription or Service Account at mp.weixin.qq.com; copy AppID + AppSecret.",
                        "color_hex": "#07C160", "supports_multi_account": False},
    "whatsapp": {"category": "Messaging", "tagline": "Send WhatsApp messages (Twilio-backed).",
                 "docs_url": "https://www.twilio.com/docs/whatsapp",
                 "setup_hint": "Register a WhatsApp sender in Twilio.",
                 "color_hex": "#25D366", "supports_multi_account": True},
    "twilio": {"category": "Messaging", "tagline": "SMS and voice calls.",
               "docs_url": "https://www.twilio.com/docs/usage/api",
               "setup_hint": "Find Account SID + Auth Token at console.twilio.com.",
               "color_hex": "#F22F46", "supports_multi_account": False},
    "linkedin": {"category": "Social", "tagline": "Post to LinkedIn, fetch profile, engage connections.",
                 "docs_url": "https://learn.microsoft.com/en-us/linkedin/",
                 "setup_hint": "Register an app at linkedin.com/developers.",
                 "color_hex": "#0A66C2", "supports_multi_account": True,
                 "capabilities": [
                     "Publish posts (text, link, image carousel) to your profile",
                     "Comment + react on posts as you",
                     "Fetch profile insights and connection list",
                 ],
                 "example_prompts": [
                     "Draft a 3-tweet thread about today's launch and post the LinkedIn version.",
                     "Reply to the top 5 comments on my latest post in my voice.",
                 ]},
    "twitter_x": {"category": "Social", "tagline": "Tweet, search, and fetch user profiles.",
                  "docs_url": "https://developer.x.com/en/docs",
                  "setup_hint": "Create a Project + App in the X Developer Portal.",
                  "color_hex": "#000000", "supports_multi_account": True,
                  "capabilities": [
                      "Tweet (text, image, thread)",
                      "Search the firehose, pull user timelines",
                      "Like, retweet, reply on your behalf",
                  ],
                  "example_prompts": [
                      "When @competitor tweets a new product, draft a thread comparing ours.",
                      "Find tweets about 'agent OS' from the last day and summarize the top opinions.",
                  ]},
    "github": {"category": "Developer", "tagline": "Manage repos, issues, and pull requests.",
               "docs_url": "https://docs.github.com/en/rest",
               "setup_hint": "Register an OAuth App at github.com/settings/developers.",
               "color_hex": "#24292E", "supports_multi_account": True,
               "capabilities": [
                   "Create + comment on issues, label, assign",
                   "Open pull requests, request reviews, merge",
                   "Read repo contents, commits, releases",
                   "Trigger workflow_dispatch on Actions",
               ],
               "example_prompts": [
                   "Triage new issues every morning: label, assign, drop a Slack summary.",
                   "When a PR has been waiting > 48h for review, ping the reviewer in Slack.",
                   "Open a release PR from dev → main with the changelog from the last 20 commits.",
               ]},
    "webhook": {"category": "Developer", "tagline": "Outbound HTTP calls to any URL with a bearer token.",
                "docs_url": None,
                "setup_hint": "Enter the target URL and a bearer token (or 'none').",
                "color_hex": "#0F766E", "supports_multi_account": False},
    "quickbooks": {"category": "Finance", "tagline": "Invoices, customers, and accounting data.",
                   "docs_url": "https://developer.intuit.com/app/developer/qbo/docs",
                   "setup_hint": "Create an app at developer.intuit.com.",
                   "color_hex": "#2CA01C", "supports_multi_account": False},
    "stripe": {"category": "Finance", "tagline": "Connect your Stripe account — agent posts payments, manages subscriptions, handles invoices and disputes.",
               "docs_url": "https://docs.stripe.com/mcp",
               "setup_hint": "Click Connect → log in to Stripe → authorize Manor. No API key copy-paste needed.",
               "color_hex": "#635BFF", "supports_multi_account": True,
               "capabilities": [
                   "Create and capture payments, issue refunds",
                   "Manage customers, subscriptions, and pricing",
                   "Read invoices, balance, and transaction history",
                   "Triage and respond to disputes",
               ],
               "example_prompts": [
                   "List failed charges from last month and email each customer to retry.",
                   "Create a $99/mo subscription for customer cus_X starting next Monday.",
                   "Show me total MRR by product over the past 6 months.",
               ]},
    # ── AI platforms ──
    # LLM Chat APIs (OpenAI / Anthropic / Doubao / Kimi / Qwen /
    # Deepseek) intentionally NOT here — model selection + API key
    # for Manor's own LLM use lives in the Account page picker, not
    # in /integrations. Browser-session AI cards belong here.
    # AI Tools (api_key) — generation + research APIs that any agent
    # can call once the user pastes a key. Distinct from /account
    # model picker (which sets Manor's primary brain) — these are
    # task-specific tools agents reach for in the middle of workflows.
    "replicate":      {"category": "AI Tools", "tagline": "Hundreds of open-source models (Flux, Luma, Whisper, …) on one API.",
                       "docs_url": "https://replicate.com/account/api-tokens",
                       "setup_hint": "Get an API token at replicate.com → Account → API Tokens.",
                       "color_hex": "#000000",
                       "supports_multi_account": True},
    "elevenlabs":     {"category": "AI Tools", "tagline": "Studio-grade TTS + voice cloning. Multilingual.",
                       "docs_url": "https://elevenlabs.io/app/settings/api-keys",
                       "setup_hint": "Get an API key at elevenlabs.io → Settings → API Keys.",
                       "color_hex": "#000000",
                       "supports_multi_account": True},
    "tavily":         {"category": "AI Tools", "tagline": "Agent-tuned web search + extraction. 1000 calls/month free.",
                       "docs_url": "https://app.tavily.com/home",
                       "setup_hint": "Sign up at tavily.com to get an API key (starts with tvly-).",
                       "color_hex": "#0F766E",
                       "supports_multi_account": True},
    "jimeng":         {"category": "AI Tools", "tagline": "即梦 — Chinese image + video gen via reverse-engineered gateway.",
                       "docs_url": "https://jimeng.jianying.com",
                       "setup_hint": "Sign in to jimeng.jianying.com → DevTools → Cookies → copy ``sessionid``.",
                       "color_hex": "#FF2C55",
                       "supports_multi_account": True},
    "midjourney_web": {"category": "Browser Automation", "tagline": "Midjourney via the web app.",
                       "docs_url": "https://www.midjourney.com",
                       "setup_hint": "Sign in once with your Discord-linked Midjourney account.",
                       "color_hex": "#000000"},
    "notebooklm":     {"category": "Browser Automation", "tagline": "NotebookLM — research notebooks via web automation (cookies, not API).",
                       "docs_url": "https://notebooklm.google.com",
                       "setup_hint": "Sign in to notebooklm.google.com → use a cookie-export extension (Cookie-Editor) → paste the JSON here.",
                       "color_hex": "#1A73E8",
                       "supports_multi_account": True},
    "claude_ai_web":  {"category": "Browser Automation", "tagline": "Claude.ai web — agents use your Claude Pro/Max subscription, not API credits.",
                       "docs_url": "https://claude.ai",
                       "setup_hint": "Sign in to claude.ai → export cookies (Cookie-Editor → JSON) → paste here.",
                       "color_hex": "#D97757",
                       "supports_multi_account": True},
    "chatgpt_web":    {"category": "Browser Automation", "tagline": "ChatGPT web — uses your Plus/Team subscription quota, no API spend.",
                       "docs_url": "https://chatgpt.com",
                       "setup_hint": "Sign in to chatgpt.com → export cookies (Cookie-Editor → JSON) → paste here.",
                       "color_hex": "#10A37F",
                       "supports_multi_account": True},
    "gemini_web":     {"category": "Browser Automation", "tagline": "Gemini web — leverages your Gemini Advanced subscription.",
                       "docs_url": "https://gemini.google.com",
                       "setup_hint": "Sign in to gemini.google.com → export Google session cookies → paste here.",
                       "color_hex": "#4285F4",
                       "supports_multi_account": True},
    "perplexity_web": {"category": "Browser Automation", "tagline": "Perplexity Pro — unlimited Sonar + research without API metering.",
                       "docs_url": "https://www.perplexity.ai",
                       "setup_hint": "Sign in to perplexity.ai (Pro account) → export cookies → paste here.",
                       "color_hex": "#0F766E",
                       "supports_multi_account": True},
    "producthunt":    {"category": "Marketing", "tagline": "Product Hunt — launch-day stats, comments, and posting.",
                       "docs_url": "https://api.producthunt.com/v2/docs",
                       "setup_hint": "Create a PH OAuth app at api.producthunt.com → API → Applications, then connect.",
                       "color_hex": "#DA552F",
                       "supports_multi_account": False},
    "facebook":       {"category": "Social", "tagline": "Post to your Facebook Pages, reply to comments, and handle Messenger DMs.",
                       "docs_url": "https://developers.facebook.com/docs/pages",
                       "setup_hint": "Click Connect → sign in to Facebook → choose which Pages the agent can manage.",
                       "color_hex": "#1877F2",
                       "supports_multi_account": True,
                       "capabilities": [
                           "Post to your Page (text, link, photo, scheduled)",
                           "Auto-reply to comments on your posts",
                           "Hide spam, delete posts, fetch insights",
                           "Send Messenger DMs (within 24h window)",
                       ],
                       "example_prompts": [
                           "Every weekday at 10am post our top product of the day to the Page.",
                           "Whenever a comment is negative, draft a polite reply for me to approve.",
                           "Pull last week's reach + engagement and summarize trends.",
                       ]},
    "youtube":        {"category": "Social", "tagline": "Search YouTube, read video & channel stats, manage comments and playlists.",
                       "docs_url": "https://developers.google.com/youtube/v3",
                       "setup_hint": "Rides on your Google OAuth client — enable the YouTube Data API v3 and whitelist this provider's redirect URI.",
                       "color_hex": "#FF0000",
                       "supports_multi_account": True,
                       "capabilities": [
                           "Search videos, channels, and playlists",
                           "Read video/channel stats, comments, and captions",
                           "Post, reply to, and delete comments; like/dislike videos",
                           "Edit your video's title/description/tags and manage playlists",
                       ],
                       "example_prompts": [
                           "Find the top comments on my latest video and draft replies in my voice.",
                           "Pull view + like stats for my last 10 uploads and tell me what's trending.",
                       ]},
    "tiktok":         {"category": "Social", "tagline": "Read your TikTok profile and videos, publish video/photo posts from a URL.",
                       "docs_url": "https://developers.tiktok.com/doc/overview",
                       "setup_hint": "Create an app at developers.tiktok.com (Login Kit + Content Posting API).",
                       "color_hex": "#000000",
                       "supports_multi_account": True,
                       "capabilities": [
                           "Read your profile and video list (Display API)",
                           "Query video stats by id",
                           "Publish video or photo posts from a hosted URL (Content Posting API)",
                           "Track publish status of a post",
                       ],
                       "example_prompts": [
                           "Post this video URL to TikTok with a caption about our launch.",
                           "Pull stats for my last 10 TikToks and tell me which performed best.",
                       ]},
    "shopify":        {"category": "E-commerce", "tagline": "Manage your Shopify store — products, orders, customers, and inventory.",
                       "docs_url": "https://shopify.dev/docs/api/admin-rest",
                       "setup_hint": "Create a custom app in your Shopify admin and paste its Admin API access token.",
                       "color_hex": "#95BF47",
                       "supports_multi_account": False,
                       "capabilities": [
                           "Browse/search products, orders, and customers",
                           "Create + update products and customers",
                           "Adjust available inventory per location",
                           "Tag orders for fulfillment / follow-up",
                       ],
                       "example_prompts": [
                           "Tag all paid, unfulfilled orders from today for the warehouse.",
                           "Create a draft product for our new SKU with this description.",
                       ]},
    "woocommerce":    {"category": "E-commerce", "tagline": "Manage your WooCommerce store — products, orders, stock, and customers.",
                       "docs_url": "https://woocommerce.github.io/woocommerce-rest-api-docs/",
                       "setup_hint": "Generate REST API keys in WooCommerce → Settings → Advanced → REST API.",
                       "color_hex": "#7F54B3",
                       "supports_multi_account": False,
                       "capabilities": [
                           "List/search products, orders, and customers",
                           "Create + update products, set managed stock quantity",
                           "Advance order status (fulfill / cancel / refund)",
                       ],
                       "example_prompts": [
                           "Mark order #1234 as completed and note it for the customer.",
                           "Set stock to 0 on out-of-season products and list what changed.",
                       ]},
    "square":         {"category": "E-commerce", "tagline": "Manage Square catalog, orders, customers, and inventory.",
                       "docs_url": "https://developer.squareup.com/reference/square",
                       "setup_hint": "Create an app at developer.squareup.com and paste the access token.",
                       "color_hex": "#006AFF",
                       "supports_multi_account": False,
                       "capabilities": [
                           "Browse locations, catalog items, orders, and customers",
                           "Create catalog items and customers",
                           "Read + adjust inventory counts per location",
                       ],
                       "example_prompts": [
                           "Which catalog items are low on inventory at the downtown location?",
                           "Add a new $12 item to the catalog with one variation.",
                       ]},
    "tiktok_shop":    {"category": "E-commerce", "tagline": "Manage your TikTok Shop — orders, products, prices, and inventory.",
                       "docs_url": "https://partner.tiktokshop.com/docv2",
                       "setup_hint": "Register on the TikTok Shop Partner Center and authorize your shop.",
                       "color_hex": "#FE2C55",
                       "supports_multi_account": True,
                       "capabilities": [
                           "List authorized shops",
                           "Search + read orders and products",
                           "Update SKU prices and inventory",
                       ],
                       "example_prompts": [
                           "Bump the price of SKU X by 10% across my TikTok Shop.",
                           "Summarize today's TikTok Shop orders.",
                       ]},
    "amazon":         {"category": "E-commerce", "tagline": "Manage Amazon Seller orders, catalog, inventory, and listings (SP-API).",
                       "docs_url": "https://developer-docs.amazon.com/sp-api/",
                       "setup_hint": "Register as an SP-API developer in Seller Central and authorize the app.",
                       "color_hex": "#FF9900",
                       "supports_multi_account": True,
                       "capabilities": [
                           "List + read orders and their line items",
                           "Search the catalog and read items by ASIN",
                           "Read FBA inventory summaries",
                           "Create / update listings (price, quantity, attributes)",
                       ],
                       "example_prompts": [
                           "List my orders from the last 24h that aren't shipped yet.",
                           "Lower the price on ASIN B0XXXX by 5% via a listing patch.",
                       ]},
    "linkedin_browser": {"category": "Browser Automation",
                       "tagline": "LinkedIn search / messaging / jobs — covers what the official API doesn't.",
                       "docs_url": "https://www.linkedin.com",
                       "setup_hint": "Click Connect → sign in once in the embedded Chromium. Use a DEDICATED account; this path violates LinkedIn ToS §8.2.",
                       "color_hex": "#0A66C2",
                       "supports_multi_account": True,
                       "capabilities": [
                           "Search people / companies / jobs",
                           "View third-party profiles + posts",
                           "Send DMs and post comments (with confirm gate)",
                           "Easy Apply automation (with confirm gate)",
                       ],
                       "example_prompts": [
                           "Find 5 series A founders in fintech and summarise their LinkedIn headlines.",
                           "Watch jobs/search for 'staff engineer remote' and email me new posts daily.",
                           "Reply to my last 10 unread messages with a friendly acknowledgement.",
                       ]},
    # Remote MCP servers (transport=http, vendor-hosted)
    "paypal":     {"category": "Finance", "tagline": "Connect your PayPal account — agent creates orders, invoices, handles disputes, manages subscriptions.",
                       "capabilities": [
                           "Create orders, capture payments, issue refunds",
                           "Generate and send invoices, mark paid",
                           "Manage subscription plans + active subscribers",
                           "Triage disputes; submit evidence for chargebacks",
                       ],
                       "example_prompts": [
                           "Send invoice #1284 to acme@example.com for $1,200 with 15-day terms.",
                           "List subscriptions paused in the last 30 days and message each subscriber.",
                           "When a dispute is opened, gather order + shipping evidence and pre-fill the response.",
                       ],
                       "docs_url": "https://docs.paypal.ai/developer/tools/ai/mcp-quickstart",
                       "setup_hint": "Create OAuth app at developer.paypal.com (sandbox + live each get a separate app) → set PAYPAL_CLIENT_ID/SECRET in .env.",
                       "color_hex": "#003087",
                       "supports_multi_account": True},
    # ── Microsoft 365 (one Azure AD app powers all 5) ──────────────────
    "outlook":    {"category": "Email", "tagline": "Outlook — read / send / draft mail, manage folders, flag and categorize.",
                       "capabilities": [
                           "List, read, send, reply, forward email",
                           "Drafts CRUD — let agent prepare, you approve before send",
                           "Flag, categorize, mark read, move between folders",
                           "Download attachments; create custom folders",
                       ],
                       "example_prompts": [
                           "Read my unread inbox and triage into Action / FYI / Newsletter folders.",
                           "Draft a reply to the proposal email — keep it warm, push for next Tuesday.",
                           "Find emails from billing@ in the last 7 days and flag for follow-up.",
                       ],
                       "docs_url": "https://learn.microsoft.com/en-us/graph/api/resources/mail-api-overview",
                       "setup_hint": "portal.azure.com → App registrations → register a multi-tenant app → set MS_CLIENT_ID/SECRET in .env. All 5 MS modules share the same app.",
                       "color_hex": "#0078D4",
                       "supports_multi_account": True},
    "onedrive":   {"category": "Productivity", "tagline": "OneDrive — list, read, upload, share files; manage permissions and versions.",
                       "capabilities": [
                           "Browse + search files, get share / preview links",
                           "Upload text files, copy / move / rename",
                           "Manage per-user permissions, mint share links",
                           "Versions: list + restore prior revisions",
                       ],
                       "example_prompts": [
                           "Find every Q1 report in /Reports and email me view-only links.",
                           "Upload this transcript to /Meetings and share with the team.",
                           "Roll the budget spreadsheet back to yesterday's version.",
                       ],
                       "docs_url": "https://learn.microsoft.com/en-us/graph/api/resources/onedrive",
                       "setup_hint": "Same Azure AD app as Outlook — add Files.ReadWrite + Files.ReadWrite.All scopes in API permissions.",
                       "color_hex": "#0364B8",
                       "supports_multi_account": True},
    "ms_calendar": {"category": "Productivity", "tagline": "Microsoft Calendar — events CRUD, RSVP, find meeting times across attendees.",
                       "capabilities": [
                           "Create / update / cancel events with attendees",
                           "RSVP to invites (accept / decline / tentative)",
                           "Free/busy lookup across multiple calendars",
                           "find_meeting_times — AI scheduling across attendees",
                       ],
                       "example_prompts": [
                           "Find a 30-min slot next week when both me and Sarah are free.",
                           "Move tomorrow's standup to 10am and notify all attendees.",
                           "List events I haven't responded to and accept anything from my team.",
                       ],
                       "docs_url": "https://learn.microsoft.com/en-us/graph/api/resources/calendar",
                       "setup_hint": "Same Azure AD app as Outlook — add Calendars.ReadWrite + MailboxSettings.Read.",
                       "color_hex": "#0078D4",
                       "supports_multi_account": True},
    "ms_teams":   {"category": "Messaging", "tagline": "Microsoft Teams — channel + chat messaging, online meetings, presence.",
                       "capabilities": [
                           "Read + post in team channels, reply in threads",
                           "Send DMs (1:1 and group chats); start new chats",
                           "Spin up Teams meeting links with attendees",
                           "Get / set your presence (Available / Busy / DND)",
                       ],
                       "example_prompts": [
                           "Post weekly summary in the #engineering channel every Friday at 5pm.",
                           "If a customer DMs me on Teams, draft a reply for my review.",
                           "Schedule a 30-min Teams meeting with @sarah for tomorrow afternoon.",
                       ],
                       "docs_url": "https://learn.microsoft.com/en-us/graph/teams-concept-overview",
                       "setup_hint": "Same Azure AD app as Outlook — add Team.ReadBasic.All + Channel + Chat + OnlineMeetings scopes.",
                       "color_hex": "#6264A7",
                       "supports_multi_account": True},
    "ms_excel":   {"category": "Productivity", "tagline": "Excel workbooks — live cell-level reads / writes via the Workbook API.",
                       "capabilities": [
                           "Read worksheets, ranges, used range; live values",
                           "Append rows to tables (auto-grows; ideal for reports)",
                           "Write ranges, update single cells, clear ranges",
                           "Manage worksheets + named ranges; trigger recalc",
                       ],
                       "example_prompts": [
                           "Append today's sales numbers as a new row in the CRM table.",
                           "Read the budget sheet and tell me which categories are over by 10%.",
                           "Add a new tab named '2026-Q2' and seed it with last quarter's headers.",
                       ],
                       "docs_url": "https://learn.microsoft.com/en-us/graph/api/resources/excel",
                       "setup_hint": "Same Azure AD app as Outlook — workbooks live in OneDrive, so Files.ReadWrite covers it.",
                       "color_hex": "#107C41",
                       "supports_multi_account": True},
}

_CATEGORY_ORDER = [
    "Email", "Messaging", "Productivity", "Social", "Developer", "Finance",
    "Marketing", "AI Tools",
    # Categorize by execution mechanism rather than use-case domain —
    # a "Local CLI" tool isn't necessarily AI (could be git, docker, …),
    # and "Browser Automation" covers any GUI-only platform (Jimeng,
    # Midjourney, douyin, BOSS直聘, …).
    "Local CLI", "Browser Automation",
]


@router.get("/mcp-servers", response_model=list[MCPServerStatus])
async def list_mcp_server_status(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List every MCP server + per-user + per-entity connection status.

    Drives the Integrations page's MCP section. For each of the 8 seeded
    servers, returns whether:
      * the user has a personal OAuth token in oauth_accounts
      * the entity has a shared credential in integrations
      * the user's role grants the integration's required_permission
      * an agent acting on their behalf can actually call this server
    """
    from datetime import datetime, timedelta, timezone
    from sqlalchemy import select
    from packages.core.models.document import Integration
    from packages.core.models.mcp import MCPServer
    from packages.core.models.user import OAuthAccount
    from packages.core.permissions import user_has_permission

    servers = (await db.execute(
        select(MCPServer).where(MCPServer.status == "active").order_by(MCPServer.name)
    )).scalars().all()
    servers = [s for s in servers if not _is_hidden_catalog_server_key(s.server_key)]

    # Pull the Nango catalog (providers configured in Nango admin) so we
    # can (a) augment overlapping built-in cards with a nango Connect
    # path and (b) surface Nango-only platforms (HubSpot, Linear, ...)
    # as their own cards. Failure is non-fatal -- we just skip the
    # augmentation if Nango isn't reachable / configured.
    nango_provider_keys = await _collect_nango_provider_keys(db, user.entity_id)
    nango_config_key_by_server = {
        canonical_provider_key(nango_key): nango_key
        for nango_key in nango_provider_keys
    }

    # Resolve OAuth readiness for each provider in one pass — drives the
    # "OAuth not configured" hint vs an active Connect CTA.
    from packages.core.services.oauth_provider_config import (
        is_oauth_provider, oauth_client_configured,
    )
    oauth_configured_keys: set[str] = set()
    for s in servers:
        if is_oauth_provider(s.server_key) and oauth_client_configured(s.server_key, s):
            oauth_configured_keys.add(s.server_key)

    # Bulk-load type-specific specs for the new auth_type values. Each
    # is keyed by mcp_server_id so we can attach to the right card in
    # one pass without per-row joins.
    from packages.core.models.ai_tool_spec import BrowserToolSpec
    browser_specs_by_id: dict[str, BrowserToolSpec] = {}
    browser_keys = {s.id for s in servers if s.auth_type == "browser_session"}
    if browser_keys:
        rows = (await db.execute(
            select(BrowserToolSpec).where(BrowserToolSpec.mcp_server_id.in_(browser_keys))
        )).scalars().all()
        browser_specs_by_id = {r.mcp_server_id: r for r in rows}

    now = datetime.now(timezone.utc)

    # Bulk-load the user's OAuth accounts for the relevant providers.
    # A user may have MULTIPLE accounts per provider (e.g. personal +
    # work Gmail), so we group by provider → list.
    server_keys = [s.server_key for s in servers]
    # Include Nango-only provider keys so virtual cards for HubSpot /
    # Linear / etc. can show their connected state from mirrored
    # Integration rows.
    lookup_keys = list({
        *server_keys,
        *nango_provider_keys.keys(),
        *(alias for key in server_keys for alias in provider_key_aliases(key)),
    })
    all_user_oauth = (await db.execute(
        select(OAuthAccount).where(
            OAuthAccount.user_id == user.id,
            OAuthAccount.provider.in_(lookup_keys),
        ).order_by(OAuthAccount.created_at.asc())
    )).scalars().all()
    user_oauth_by_provider: dict[str, list] = {}
    for row in all_user_oauth:
        user_oauth_by_provider.setdefault(row.provider, []).append(row)
        canonical = canonical_provider_key(row.provider)
        if canonical != row.provider:
            user_oauth_by_provider.setdefault(canonical, []).append(row)
    # Group every active Integration row by provider so we can expose a
    # full entity_accounts list — credential-based providers support
    # multiple accounts per entity.
    all_entity_rows = (await db.execute(
        select(Integration).where(
            Integration.entity_id == user.entity_id,
            Integration.provider.in_(lookup_keys),
            Integration.status == "active",
        ).order_by(Integration.created_at.desc())
    )).scalars().all()
    entity_accounts_by_provider: dict[str, list[Integration]] = {}
    for row in all_entity_rows:
        entity_accounts_by_provider.setdefault(row.provider, []).append(row)
        canonical = canonical_provider_key(row.provider)
        if canonical != row.provider:
            entity_accounts_by_provider.setdefault(canonical, []).append(row)

    out: list[MCPServerStatus] = []
    for s in servers:
        oauth_rows = user_oauth_by_provider.get(s.server_key, [])

        # Build typed connections list
        connections: list[UserOAuthConnection] = []
        for r in oauth_rows:
            profile = r.profile or {}
            display_name = (
                profile.get("email")
                or profile.get("display_name")
                or profile.get("name")
                or r.provider_user_id
            )
            health_raw = profile.get("last_health_check") if profile else None
            connections.append(UserOAuthConnection(
                id=r.id,
                display_name=display_name,
                provider_user_id=r.provider_user_id,
                expires_at=r.token_expires_at.isoformat() if r.token_expires_at else None,
                is_default=bool(profile.get("is_default", False)),
                connected_at=r.created_at.isoformat() if r.created_at else None,
                health=HealthStatus(**health_raw) if health_raw else None,
            ))

        user_connected = len(connections) > 0

        # Build entity_accounts list from every Integration row for this
        # (entity, provider). Ordered: default first, then newest.
        entity_rows = entity_accounts_by_provider.get(s.server_key, [])
        entity_accounts: list[EntityAccountConnection] = []
        for r in entity_rows:
            cfg = r.config or {}
            display_name = _safe_entity_account_display_name(r, requester_id=user.id)
            health_raw = cfg.get("last_health_check")
            entity_accounts.append(EntityAccountConnection(
                id=r.id,
                name=cfg.get("name") or None,
                display_name=display_name,
                is_default=bool(cfg.get("is_default", False)),
                created_at=r.created_at.isoformat() if r.created_at else None,
                status=r.status,
                health=HealthStatus(**health_raw) if health_raw else None,
            ))
        entity_accounts.sort(key=lambda a: (0 if a.is_default else 1, a.created_at or ""), reverse=False)

        # Prefer the default entity row (if marked); else most recent
        primary_entity_row = next(
            (r for r in entity_rows if (r.config or {}).get("is_default")),
            entity_rows[0] if entity_rows else None,
        )
        entity_connected = bool(
            primary_entity_row
            and (
                primary_entity_row.credentials
                or primary_entity_row.credential_ref
                or (primary_entity_row.config or {}).get("nango")
            )
        )
        required_permission = (
            primary_entity_row.required_permission if primary_entity_row else None
        )

        has_perm = True
        if required_permission:
            has_perm = await user_has_permission(
                db, user.id, user.entity_id, required_permission,
            )

        # Agent's effective access right now
        handled_agent_access = False
        if not handled_agent_access:
            if user_connected:
                agent_can_use = True
                count = len(connections)
                hint = (
                    "Personal connection active — agents can call this on your behalf."
                    if count == 1
                    else f"{count} personal connections — agents will use the default."
                )
            elif entity_connected and has_perm:
                agent_can_use = True
                hint = (
                    f"Using company-level {s.name} credentials."
                    if not required_permission
                    else f"Company credentials active (your role grants '{required_permission}')."
                )
            elif entity_connected and not has_perm:
                agent_can_use = False
                hint = (
                    f"{s.name} is connected at the company level, but your role "
                    f"lacks '{required_permission}'. Ask an admin to invite you with "
                    f"a higher role, or connect your own account below."
                )
            else:
                agent_can_use = False
                hint = f"Connect {s.name} to let agents act on your behalf."

        # Warn on expired tokens (the first/default one)
        if oauth_rows:
            primary = next((r for r in oauth_rows if (r.profile or {}).get("is_default")), oauth_rows[0])
            if primary.token_expires_at and primary.token_expires_at < now:
                hint = f"Your {s.name} connection expired — reconnect to continue."
                agent_can_use = False

        display = _PROVIDER_DISPLAY.get(s.server_key, {})
        legacy_expires = connections[0].expires_at if connections else None
        # Use the function form so MANOR_PREVIEW_INTEGRATIONS overrides
        # take effect on every request without a process restart-quirk
        # (the module-level constant is computed at import time).
        from packages.core.services.integration_service import coming_soon_servers
        _is_coming_soon = s.server_key in coming_soon_servers()

        # In dev environments, hide coming-soon entries entirely instead
        # of just badging them. They're broken / untested / incomplete
        # and clutter the catalog while developers iterate. Set
        # MANOR_SHOW_COMING_SOON=1 in your local .env if you're
        # actively building one of them and want it visible.
        # (MANOR_PREVIEW_INTEGRATIONS removes a specific provider from
        # the coming-soon set entirely — orthogonal to this gate.)
        if _is_coming_soon and _hide_coming_soon_in_dev():
            continue

        out.append(MCPServerStatus(
            server_key=s.server_key,
            name=s.name,
            category=display.get("category"),
            description=s.description,
            auth_type=s.auth_type,
            scopes=s.scopes,
            tagline=display.get("tagline"),
            docs_url=display.get("docs_url"),
            setup_hint=display.get("setup_hint"),
            color_hex=display.get("color_hex"),
            supports_multi_account=bool(display.get("supports_multi_account")),
            capabilities=list(display.get("capabilities") or []),
            example_prompts=list(display.get("example_prompts") or []),
            connections=connections,
            entity_accounts=entity_accounts,
            user_connected=user_connected,      # legacy
            user_expires_at=legacy_expires,     # legacy
            entity_connected=entity_connected,
            required_permission=required_permission,
            user_has_required_permission=has_perm,
            agent_can_use=agent_can_use if not _is_coming_soon else False,
            hint=hint if not _is_coming_soon else "Coming soon",
            coming_soon=_is_coming_soon,
            # Built-in card lights up Nango Connect button when the
            # platform is also configured in our self-hosted Nango.
            nango_provider_config_key=nango_config_key_by_server.get(s.server_key),
            oauth_configured=s.server_key in oauth_configured_keys,
            cli_spec=_cli_spec_payload(cli_specs_by_id.get(s.id)),
            browser_spec=_browser_spec_payload(browser_specs_by_id.get(s.id)),
        ))

    # Append Nango-only providers (no built-in MCP module) as virtual
    # cards so the user sees them in the same grid alongside built-ins.
    builtin_keys = {s.server_key for s in servers}
    for nango_key, nango_provider in nango_provider_keys.items():
        if canonical_provider_key(nango_key) in builtin_keys:
            continue  # already covered by built-in card above
        # The connection / account counts come from any mirrored
        # Integration row that the sync flow wrote.
        entity_rows = entity_accounts_by_provider.get(nango_key, [])
        primary = entity_rows[0] if entity_rows else None
        connected = bool(primary)
        out.append(MCPServerStatus(
            server_key=nango_key,
            name=_humanize_provider(nango_provider or nango_key),
            category=None,
            description=f"Connect via {_humanize_provider(nango_provider or nango_key)} OAuth.",
            auth_type="oauth2",
            scopes=None,
            tagline=None,
            docs_url=None,
            setup_hint=None,
            color_hex=None,
            supports_multi_account=False,
            connections=[],
            entity_accounts=[
                EntityAccountConnection(
                    id=r.id,
                    name=(r.config or {}).get("name"),
                    display_name=_safe_entity_account_display_name(r, requester_id=user.id),
                    is_default=bool((r.config or {}).get("is_default", False)),
                    created_at=r.created_at.isoformat() if r.created_at else None,
                    status=r.status,
                    health=None,
                )
                for r in entity_rows
            ],
            entity_connected=connected,
            required_permission=None,
            user_has_required_permission=True,
            agent_can_use=connected,
            hint=(
                f"Connected — agents can act on your {_humanize_provider(nango_provider or nango_key)} account."
                if connected
                else f"Click Connect to authorize {_humanize_provider(nango_provider or nango_key)}."
            ),
            nango_provider_config_key=nango_key,
        ))

    # Stable sort: by category order, then name
    cat_rank = {name: i for i, name in enumerate(_CATEGORY_ORDER)}
    out.sort(key=lambda m: (cat_rank.get(m.category or "", 999), m.name))
    return out




def _cli_spec_payload(spec) -> dict | None:
    if spec is None:
        return None
    return {
        "command_template": spec.command_template,
        "supported_subcommands": spec.supported_subcommands or [],
        "requires_local_paths": bool(spec.requires_local_paths),
        "timeout_seconds": int(spec.timeout_seconds or 120),
        "output_format": spec.output_format or "text",
    }


def _browser_spec_payload(spec) -> dict | None:
    if spec is None:
        return None
    return {
        "login_url": spec.login_url,
        "session_check_selector": spec.session_check_selector,
        "provider_module": spec.provider_module,
        "tool_actions": spec.tool_actions or {},
        "cookie_ttl_days": int(spec.cookie_ttl_days or 30),
    }


def _humanize_provider(slug: str) -> str:
    return slug.replace("_", " ").replace("-", " ").title() if slug else "Unknown"


async def _collect_nango_provider_keys(db: AsyncSession, entity_id: str) -> dict[str, str | None]:
    """Return a map ``{provider_config_key: provider}`` of integrations
    configured in this Manor instance's Nango admin. Empty dict if
    Nango is not set up or unreachable -- callers treat that as
    "no Nango bridge."""
    from packages.core.ai.mcp.nango import _NANGO_BASE, get_nango_secret
    import httpx

    secret = await get_nango_secret(db, entity_id)
    if not secret:
        return {}

    try:
        async with httpx.AsyncClient(timeout=8.0) as cx:
            r = await cx.get(
                f"{_NANGO_BASE}/config",
                headers={"Authorization": f"Bearer {secret}"},
            )
            r.raise_for_status()
            body = r.json()
    except Exception:
        return {}

    out: dict[str, str | None] = {}
    raw_configs = body.get("configs") if isinstance(body, dict) else body
    for cfg in (raw_configs or []):
        key = cfg.get("unique_key") or cfg.get("provider_config_key")
        if not key:
            continue
        out[key] = cfg.get("provider") or key
    return out


# ── Per-account management: set default, disconnect ─────────────────────────

@router.post("/mcp-servers/{server_key}/connections/{connection_id}/set-default", status_code=204)
async def set_default_connection(
    server_key: str,
    connection_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Mark one of the user's OAuth accounts for this provider as default.
    Agents resolving a bearer token pick the default account first."""
    from sqlalchemy import select
    from packages.core.models.user import OAuthAccount

    rows = (await db.execute(
        select(OAuthAccount).where(
            OAuthAccount.user_id == user.id,
            OAuthAccount.provider == server_key,
        )
    )).scalars().all()

    found = False
    for r in rows:
        profile = dict(r.profile or {})
        is_target = r.id == connection_id
        profile["is_default"] = is_target
        r.profile = profile
        found = found or is_target

    if not found:
        raise HTTPException(404, "Connection not found")
    await db.commit()


@router.delete("/mcp-servers/{server_key}/connections/{connection_id}", status_code=204)
async def disconnect_account(
    server_key: str,
    connection_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Remove one OAuth account connection. If it was the default, no new
    default is auto-chosen — next agent call falls back to most-recent."""
    from sqlalchemy import select
    from packages.core.models.user import OAuthAccount

    row = (await db.execute(
        select(OAuthAccount).where(
            OAuthAccount.id == connection_id,
            OAuthAccount.user_id == user.id,
            OAuthAccount.provider == server_key,
        )
    )).scalar_one_or_none()
    if not row:
        raise HTTPException(404, "Connection not found")
    await db.delete(row)
    await db.commit()


# ── Manual health check ("Test now" button) ─────────────────────────────────

@router.post(
    "/health-check/entity-accounts/{account_id}",
    response_model=HealthStatus,
)
async def test_entity_account(
    account_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Run the provider's test_connection for one entity-level account
    synchronously. Returns the probe result; the stored
    ``config.last_health_check`` is also refreshed."""
    from packages.core.services.integration_health import run_and_persist_integration
    from packages.core.services.integration_service import get_integration

    existing = await get_integration(db, account_id, user.entity_id)
    if not existing:
        raise HTTPException(404, "Account not found")

    result = await run_and_persist_integration(db, account_id)
    await db.commit()
    return HealthStatus(**result)


_WECHAT_RUNNER_URL = os.getenv(
    "WECHAT_RUNNER_URL", "http://wechat-runner:8800",
).rstrip("/")
_WECHAT_RUNNER_BEARER = os.getenv("WECHAT_RUNNER_BEARER_TOKEN", "")


def _wechat_runner_headers() -> dict[str, str]:
    h: dict[str, str] = {"Accept": "application/json"}
    if _WECHAT_RUNNER_BEARER:
        h["Authorization"] = f"Bearer {_WECHAT_RUNNER_BEARER}"
    return h


def _wechat_session_id_from_creds(integ) -> str | None:
    return ((integ.credentials or {}).get("session_id") or "").strip() or None


def _wechat_runner_url_from_creds(integ) -> str:
    creds = integ.credentials or {}
    return (creds.get("runner_url") or _WECHAT_RUNNER_URL).rstrip("/")


# ── Pre-integration scan flow ────────────────────────────────────────
#
# The user clicks Connect → API creates a session on the runner and
# returns its id. The frontend opens a modal that polls
# /sessions/{sid}/status. Once ``online`` flips true, the frontend
# calls /finish, which is when Manor actually persists an Integration
# row. Mid-scan tear-downs go through DELETE.

@router.post("/wechat-personal/sessions")
async def wechat_personal_start_session(
    user: User = Depends(get_current_user),
):
    """Spawn a new iLink session on the runner. No DB writes — the
    Integration row only gets created on /finish. Returns the
    runner-side session_id the frontend uses for polling."""
    import httpx as _httpx
    try:
        async with _httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                f"{_WECHAT_RUNNER_URL}/sessions",
                headers=_wechat_runner_headers(),
            )
    except _httpx.RequestError as exc:
        raise HTTPException(
            502, f"WeChat runner unreachable at {_WECHAT_RUNNER_URL}: {exc}",
        )
    if not r.is_success:
        raise HTTPException(502, f"Runner /sessions: {r.status_code} {r.text[:200]}")
    return r.json()


@router.get("/wechat-personal/sessions/{session_id}/status")
async def wechat_personal_session_status(
    session_id: str,
    user: User = Depends(get_current_user),
):
    """Pre-integration polling — same shape as the per-account status
    endpoint but addressed by runner session_id."""
    import httpx as _httpx
    try:
        async with _httpx.AsyncClient(timeout=8) as client:
            r = await client.get(
                f"{_WECHAT_RUNNER_URL}/sessions/{session_id}/status",
                headers=_wechat_runner_headers(),
            )
    except _httpx.RequestError as exc:
        return {"online": False, "qr_pending": False,
                "last_error": f"Runner unreachable: {exc}"}
    if r.status_code == 404:
        raise HTTPException(404, "Session not found — start a fresh one.")
    if not r.is_success:
        return {"online": False, "qr_pending": False,
                "last_error": f"Runner HTTP {r.status_code}"}
    return r.json()


@router.get("/wechat-personal/sessions/{session_id}/qr.png")
async def wechat_personal_session_qr(
    session_id: str,
):
    """Proxy the runner's per-session QR image. Unauthenticated so it
    works as an ``<img src>`` (browsers can't add a JWT header). The
    session_id is opaque (96-bit random) and short-lived; intercepting
    it doesn't help an attacker — they'd need to also intercept the
    user's WeChat scan."""
    import httpx as _httpx
    from fastapi.responses import Response as _Response
    try:
        async with _httpx.AsyncClient(timeout=8) as client:
            r = await client.get(
                f"{_WECHAT_RUNNER_URL}/sessions/{session_id}/qr.png",
            )
    except _httpx.HTTPError as exc:
        raise HTTPException(502, f"Runner unreachable: {exc}")
    if r.status_code == 404:
        raise HTTPException(404, "No QR available yet — give the runner a moment.")
    if not r.is_success:
        raise HTTPException(502, f"Runner HTTP {r.status_code}")
    return _Response(
        content=r.content,
        media_type=r.headers.get("content-type", "image/png"),
        headers={"Cache-Control": "no-store"},
    )


class WechatPersonalFinishRequest(BaseModel):
    session_id: str
    name: str | None = None  # optional friendly label for the cards UI


@router.post("/wechat-personal/sessions/{session_id}/finish", status_code=201)
async def wechat_personal_finish_session(
    session_id: str,
    req: WechatPersonalFinishRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    # Return type intentionally unannotated. ``IntegrationResponse`` is
    # defined further down in this module; FastAPI eagerly resolves
    # return annotations at registration time and chokes on the
    # ForwardRef under ``from __future__ import annotations``.
    """Promote a successfully-scanned runner session into an
    Integration row. Refuses to save unless the runner says
    ``online: true`` — otherwise the user would end up with a dead row.
    """
    import httpx as _httpx
    if req.session_id != session_id:
        raise HTTPException(400, "session_id mismatch")
    try:
        async with _httpx.AsyncClient(timeout=8) as client:
            r = await client.get(
                f"{_WECHAT_RUNNER_URL}/sessions/{session_id}/status",
                headers=_wechat_runner_headers(),
            )
    except _httpx.RequestError as exc:
        raise HTTPException(502, f"Runner unreachable: {exc}")
    if not r.is_success:
        raise HTTPException(502, f"Runner HTTP {r.status_code}")
    status_data = r.json()
    if not status_data.get("online"):
        raise HTTPException(
            409,
            "Session not online yet — finish the QR scan first "
            f"(state: {status_data}).",
        )

    account = status_data.get("account") or {}
    config = {
        "name": req.name or account.get("nick_name") or account.get("user_name") or "WeChat (personal)",
        "ilink_account": account,
    }
    creds = {
        "runner_url": _WECHAT_RUNNER_URL,
        "bearer_token": _WECHAT_RUNNER_BEARER,
        "session_id": session_id,
    }
    integration = await create_integration(
        db, user.entity_id, "wechat_personal",
        config=config, credentials=creds,
    )
    await _sync_channel_config_if_needed(
        db,
        entity_id=user.entity_id,
        provider="wechat_personal",
        integration_id=integration.id,
        credentials=creds,
    )
    await db.commit()
    return _integration_resp(integration)


@router.delete("/wechat-personal/sessions/{session_id}", status_code=204)
async def wechat_personal_cancel_session(
    session_id: str,
    user: User = Depends(get_current_user),
):
    """Tear down a runner session — used when the user closes the
    modal before scanning. Best-effort; the runner GCs orphaned
    sessions anyway."""
    import httpx as _httpx
    try:
        async with _httpx.AsyncClient(timeout=6) as client:
            await client.delete(
                f"{_WECHAT_RUNNER_URL}/sessions/{session_id}",
                headers=_wechat_runner_headers(),
            )
    except _httpx.RequestError:
        pass
    return None


# ── Post-integration status endpoints (cards UI) ─────────────────────


@router.get("/wechat-personal/{account_id}/status")
async def wechat_personal_status(
    account_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Same shape as the start-session status endpoint, but addressed
    by Integration id — used by the cards UI to show per-account
    online state."""
    import httpx as _httpx
    existing = await get_integration(db, account_id, user.entity_id)
    if not existing or existing.provider != "wechat_personal":
        raise HTTPException(404, "Not a wechat_personal account")
    sid = _wechat_session_id_from_creds(existing)
    if not sid:
        return {"online": False, "qr_pending": False,
                "last_error": "Integration has no session_id — re-scan needed."}
    runner_url = _wechat_runner_url_from_creds(existing)
    try:
        async with _httpx.AsyncClient(timeout=6) as client:
            r = await client.get(
                f"{runner_url}/sessions/{sid}/status",
                headers=_wechat_runner_headers(),
            )
        if r.status_code == 404:
            return {"online": False, "qr_pending": False,
                    "last_error": "Session lost on runner — re-scan needed."}
        if r.status_code == 401:
            return {"online": False, "qr_pending": False,
                    "last_error": "Runner rejected bearer token"}
        if not r.is_success:
            return {"online": False, "qr_pending": False,
                    "last_error": f"Runner HTTP {r.status_code}"}
        return r.json()
    except _httpx.ConnectError:
        return {"online": False, "qr_pending": False,
                "last_error": f"Cannot reach runner at {runner_url}"}
    except _httpx.TimeoutException:
        return {"online": False, "qr_pending": False,
                "last_error": "Runner timed out"}


@router.get("/wechat-personal/{account_id}/qr.png")
async def wechat_personal_qr(
    account_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Proxy the per-session QR image, addressed by Integration id."""
    import httpx as _httpx
    from fastapi.responses import Response as _Response

    existing = await get_integration(db, account_id, user.entity_id)
    if not existing or existing.provider != "wechat_personal":
        raise HTTPException(404, "Not a wechat_personal account")
    sid = _wechat_session_id_from_creds(existing)
    if not sid:
        raise HTTPException(404, "Integration has no session_id.")
    runner_url = _wechat_runner_url_from_creds(existing)
    try:
        async with _httpx.AsyncClient(timeout=8) as client:
            r = await client.get(f"{runner_url}/sessions/{sid}/qr.png")
    except _httpx.HTTPError as e:
        raise HTTPException(502, f"Runner unreachable: {e}")
    if r.status_code == 404:
        raise HTTPException(404, "No QR available — runner may already be logged in.")
    if not r.is_success:
        raise HTTPException(502, f"Runner HTTP {r.status_code}")
    return _Response(
        content=r.content,
        media_type=r.headers.get("content-type", "image/png"),
        headers={"Cache-Control": "no-store"},
    )


@router.post("/wiring/entity-accounts/{account_id}/register")
async def register_wiring_for_account(
    account_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Force-register the inbound webhook for a credential/api-key
    account. For Telegram: calls ``getMe`` → ``setWebhook``. For other
    providers that auto-register (Slack, WhatsApp) it's a no-op; those
    are registered at credential-save time.
    """
    from sqlalchemy import select as _select
    from packages.core.models.channel import ChannelConfig

    existing = await get_integration(db, account_id, user.entity_id)
    if not existing:
        raise HTTPException(404, "Account not found")

    # Find the ChannelConfig that bridges this Integration
    cc = (await db.execute(
        _select(ChannelConfig).where(
            ChannelConfig.entity_id == user.entity_id,
            ChannelConfig.config["integration_id"].astext == account_id,
        )
    )).scalar_one_or_none()
    if not cc:
        raise HTTPException(
            400,
            "No ChannelConfig found for this integration — provider isn't a channel.",
        )

    from packages.core.services.channels import get_adapter
    adapter = get_adapter(cc.channel_type)
    if not adapter:
        raise HTTPException(400, f"No adapter registered for {cc.channel_type}.")

    try:
        result = await adapter.register_webhook(cc)
    except Exception as e:
        logger.exception("Manual webhook register failed")
        raise HTTPException(500, f"Register failed: {e}")

    # Refresh the wiring part of the health check so the UI lights up
    try:
        from packages.core.services.integration_health import run_and_persist_integration
        await run_and_persist_integration(db, account_id)
        await db.commit()
    except Exception:
        logger.debug("Post-register health refresh failed", exc_info=True)

    return result


@router.post(
    "/health-check/connections/{connection_id}",
    response_model=HealthStatus,
)
async def test_oauth_connection(
    connection_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Run the provider's test for one of the user's OAuth accounts."""
    from sqlalchemy import select as _select
    from packages.core.models.user import OAuthAccount
    from packages.core.services.integration_health import run_and_persist_oauth

    row = (await db.execute(
        _select(OAuthAccount).where(
            OAuthAccount.id == connection_id,
            OAuthAccount.user_id == user.id,
        )
    )).scalar_one_or_none()
    if not row:
        raise HTTPException(404, "Connection not found")

    result = await run_and_persist_oauth(db, connection_id)
    await db.commit()
    return HealthStatus(**result)


# ── Channel bindings (ChannelConfig ↔ Agent) ───────────────────────────────

class ChannelBindingItem(BaseModel):
    channel_config_id: str
    channel_type: str
    provider: str
    name: str | None = None
    display_name: str
    status: str
    bound_channel_id: str | None = None
    bound_agent_id: str | None = None
    agent_name: str | None = None
    binding_status: str | None = None
    last_inbound_at: str | None = None
    last_outbound_at: str | None = None


class UpsertChannelBindingRequest(BaseModel):
    channel_config_id: str
    agent_id: str | None = None   # null = unassigned


@router.get("/channel-bindings", response_model=list[ChannelBindingItem])
async def list_channel_bindings_endpoint(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Every ChannelConfig for the entity + which agent it's routing to
    (if any). Drives the Agent Channels tab."""
    from packages.core.services.integration_service import list_channel_bindings
    rows = await list_channel_bindings(db, user.entity_id)
    return [ChannelBindingItem(**r) for r in rows]


@router.post(
    "/channel-bindings",
    response_model=ChannelBindingItem,
)
async def upsert_channel_binding_endpoint(
    req: UpsertChannelBindingRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Bind (or reassign) a ChannelConfig to an agent. Null agent_id is
    valid — leaves the channel unassigned."""
    from packages.core.services.integration_service import (
        upsert_channel_binding, list_channel_bindings,
    )
    try:
        await upsert_channel_binding(
            db, entity_id=user.entity_id,
            channel_config_id=req.channel_config_id,
            agent_id=req.agent_id,
        )
    except ValueError as e:
        raise HTTPException(404, str(e))
    await db.commit()

    # Return the refreshed row so the UI can reconcile
    rows = await list_channel_bindings(db, user.entity_id)
    match = next(
        (r for r in rows if r["channel_config_id"] == req.channel_config_id),
        None,
    )
    if not match:
        raise HTTPException(404, "Binding not found after upsert")
    return ChannelBindingItem(**match)


@router.delete("/channel-bindings/{channel_id}", status_code=204)
async def delete_channel_binding_endpoint(
    channel_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Unbind a channel — removes the Channel row, leaves the
    ChannelConfig in place so inbound still parses + logs (just no
    agent dispatch)."""
    from packages.core.services.integration_service import delete_channel_binding
    removed = await delete_channel_binding(db, user.entity_id, channel_id)
    if not removed:
        raise HTTPException(404, "Channel binding not found")
    await db.commit()


# ── Message logs (inbound + outbound across every channel) ──────────────────

class MessageLogItem(BaseModel):
    id: str
    channel_type: str
    channel_config_id: str | None = None
    direction: str
    from_address: str | None = None
    to_address: str | None = None
    subject: str | None = None
    content: str | None = None
    status: str
    error_message: str | None = None
    external_id: str | None = None
    created_at: str


@router.get("/logs", response_model=list[MessageLogItem])
async def list_channel_logs(
    channel_type: str | None = Query(None, description="Filter: email | sms | telegram | whatsapp | …"),
    direction: str | None = Query(None, description="inbound | outbound"),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return a paged feed of channel message_logs for the current entity.
    Drives the Integrations → Logs tab."""
    from packages.core.services.channel_service import list_messages

    rows = await list_messages(
        db, user.entity_id,
        channel_type=channel_type, direction=direction,
        limit=limit, offset=offset,
    )
    return [
        MessageLogItem(
            id=r.id,
            channel_type=r.channel_type,
            channel_config_id=r.channel_config_id,
            direction=r.direction,
            from_address=r.from_address,
            to_address=r.to_address,
            subject=r.subject,
            content=r.content,
            status=r.status,
            error_message=r.error_message,
            external_id=r.external_id,
            created_at=r.created_at.isoformat() if r.created_at else "",
        )
        for r in rows
    ]


# ── Entity-level accounts (credential / api-key providers) ──────────────────

@router.post(
    "/mcp-servers/{server_key}/entity-accounts/{account_id}/set-default",
    status_code=204,
)
async def set_default_entity_account(
    server_key: str,
    account_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Mark one entity-level Integration row as the default for its
    provider. Agents pick this row first when no account_id is supplied.
    """
    from packages.core.services.integration_service import (
        get_integration, set_default_integration,
    )
    target = await get_integration(db, account_id, user.entity_id)
    if not target or target.provider != server_key:
        raise HTTPException(404, "Account not found")
    await set_default_integration(db, user.entity_id, account_id)
    await db.commit()


@router.delete(
    "/mcp-servers/{server_key}/entity-accounts/{account_id}",
    status_code=204,
)
async def delete_entity_account(
    server_key: str,
    account_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Remove one entity-level Integration row. Paired ChannelConfig row
    (if any) is also removed so inbound routing stops."""
    target = await get_integration(db, account_id, user.entity_id)
    if not target or target.provider != server_key:
        raise HTTPException(404, "Account not found")

    await _delete_integration_channel_bridges(
        db,
        entity_id=user.entity_id,
        integration_id=account_id,
    )
    await db.delete(target)
    await db.commit()


# ── OAuth flow — start / callback ────────────────────────────────────────────

class OAuthStartResponse(BaseModel):
    authorize_url: str
    state: str
    server_key: str
    source: str       # "db" | "env" — where client creds came from


def _safe_oauth_return_path(raw: str | None) -> str | None:
    value = (raw or "").strip()
    if not value or len(value) > 2048 or "\r" in value or "\n" in value:
        return None
    parsed = urlparse(value)
    if parsed.scheme or parsed.netloc:
        return None
    if not parsed.path.startswith("/") or parsed.path.startswith("//"):
        return None
    return value


def _oauth_success_path(return_to: str | None, server_key: str) -> str:
    target = return_to or "/integrations"
    parts = urlsplit(target)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query["connected"] = server_key
    return urlunsplit(("", "", parts.path or "/", urlencode(query), parts.fragment))


@router.get("/oauth/{server_key}/start", response_model=OAuthStartResponse)
async def oauth_start(
    server_key: str,
    return_to: str | None = Query(None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Build the provider's authorize URL for the current user.

    State carries the user_id + provider so the callback can verify which
    account to write back to. For cloud deployments, the OAuth client
    credentials come from env vars; OSS deployments set them via
    POST /mcp-servers/{server_key}/oauth-config.
    """
    import os
    from packages.core.services.oauth_provider_config import (
        is_oauth_provider, resolve_oauth_config,
    )
    from packages.core.services.oauth_flow import begin_authorization

    if not is_oauth_provider(server_key):
        raise HTTPException(400, f"{server_key} is not an OAuth provider")

    config = await resolve_oauth_config(db, server_key)
    if not config:
        raise HTTPException(
            501,
            f"{server_key} OAuth is not configured for this deployment. "
            f"Set the provider's client_id and client_secret (env or admin UI).",
        )

    app_url = os.getenv("APP_URL", "http://localhost:3010").rstrip("/")
    redirect_uri = f"{app_url}{config.redirect_path}"
    safe_return_to = _safe_oauth_return_path(return_to)
    start = begin_authorization(
        config=config,
        user_id=user.id,
        redirect_uri=redirect_uri,
        return_to=safe_return_to,
    )
    return OAuthStartResponse(
        authorize_url=start.authorize_url,
        state=start.state,
        server_key=server_key,
        source=config.source,
    )


@router.get("/oauth/{server_key}/callback")
async def oauth_callback(
    server_key: str,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    """Exchange the authorization code for tokens and persist to
    oauth_accounts. Unauthenticated — the state param is the proof.

    All query params are optional so we can surface provider-side
    errors (``?error=unauthorized_scope_error&error_description=...``)
    as a readable HTML page instead of FastAPI's 422 "missing code".
    """
    import os
    from packages.core.models.base import generate_ulid
    from packages.core.models.user import OAuthAccount
    from packages.core.services.oauth_provider_config import resolve_oauth_config
    from packages.core.services.oauth_flow import (
        complete_authorization, render_oauth_error_page, OAuthFlowError,
        get_pending_return_to, validate_pending_state,
    )

    # Provider rejected (scope, cancel, app not approved) → human page.
    if error or not code or not state:
        return render_oauth_error_page(server_key, error, error_description)

    try:
        validate_pending_state(state, server_key=server_key)
        return_to = get_pending_return_to(state, server_key=server_key)
    except OAuthFlowError as exc:
        raise HTTPException(exc.status, exc.message)

    config = await resolve_oauth_config(db, server_key)
    if not config:
        raise HTTPException(
            501, f"{server_key} OAuth is not configured for this deployment."
        )

    app_url = os.getenv("APP_URL", "http://localhost:3010").rstrip("/")
    redirect_uri = f"{app_url}{config.redirect_path}"

    try:
        user_id, tokens = await complete_authorization(
            server_key=server_key,
            code=code,
            state=state,
            redirect_uri=redirect_uri,
            config=config,
        )
    except OAuthFlowError as exc:
        raise HTTPException(exc.status, exc.message)

    access_token = tokens.access_token
    refresh_token = tokens.refresh_token
    token_expires_at = tokens.expires_at
    data = tokens.raw

    # Upsert oauth_accounts row
    from sqlalchemy import select
    existing = (await db.execute(
        select(OAuthAccount).where(
            OAuthAccount.user_id == user_id,
            OAuthAccount.provider == server_key,
        )
    )).scalar_one_or_none()

    if existing:
        existing.access_token = access_token
        if refresh_token:
            existing.refresh_token = refresh_token
        existing.token_expires_at = token_expires_at
        oauth_row_id = existing.id
    else:
        new_row = OAuthAccount(
            id=generate_ulid(),
            user_id=user_id,
            provider=server_key,
            provider_user_id=str(data.get("user_id") or data.get("id") or ""),
            access_token=access_token,
            refresh_token=refresh_token,
            token_expires_at=token_expires_at,
            profile={},
        )
        db.add(new_row)
        oauth_row_id = new_row.id
    await db.flush()
    await db.commit()

    # Fire-and-forget health probe so the card lights up green as soon
    # as the user lands back on the Integrations page.
    try:
        from packages.core.tasks.channel_tasks import health_check_task
        health_check_task.delay(oauth_account_id=oauth_row_id)
    except Exception:
        logger.debug("Could not enqueue oauth health check", exc_info=True)

    # Redirect the browser back to the caller's page, defaulting to Integrations.
    from fastapi.responses import RedirectResponse
    return RedirectResponse(
        url=f"{app_url}{_oauth_success_path(return_to, server_key)}",
        status_code=302,
    )


# ── Admin: set OAuth client credentials (OSS self-host use case) ────────────

class OAuthConfigRequest(BaseModel):
    client_id: str
    client_secret: str
    scopes: str | None = None


@router.post("/mcp-servers/{server_key}/oauth-config", status_code=204)
async def set_oauth_config(
    server_key: str,
    req: OAuthConfigRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Persist OAuth client_id/secret to mcp_servers.default_config.

    OSS admins use this to wire their own registered OAuth apps. Cloud
    deployments don't need this — env vars cover it.

    Requires ``users.manage`` permission (admin / owner only).
    """
    from packages.core.permissions import Permission, check_permission
    from packages.core.services.oauth_provider_config import (
        is_oauth_provider, save_oauth_config,
    )

    check_permission(user.role, Permission.USERS_MANAGE)

    if not is_oauth_provider(server_key):
        raise HTTPException(400, f"{server_key} is not an OAuth provider")

    ok = await save_oauth_config(
        db, server_key,
        client_id=req.client_id.strip(),
        client_secret=req.client_secret.strip(),
        scopes=(req.scopes or "").strip() or None,
    )
    if not ok:
        raise HTTPException(404, f"MCP server {server_key} not found")
    await db.commit()


# ── Pydantic models ──

class IntegrationResponse(BaseModel):
    id: str
    entity_id: str
    provider: str
    status: str
    config: dict = {}
    # Sanitized edit helper: non-secret fields are included and
    # secret-like fields are replaced by the sentinel below. The raw
    # ``credentials`` object is intentionally never serialized.
    credential_preview: dict = {}
    created_at: str | None = None
    updated_at: str | None = None


class CreateIntegrationRequest(BaseModel):
    provider: str
    config: dict | None = None
    credentials: dict | None = None


class UpdateIntegrationRequest(BaseModel):
    provider: str | None = None
    status: str | None = None
    config: dict | None = None
    credentials: dict | None = None


class ChannelResponse(BaseModel):
    id: str
    entity_id: str
    user_id: str | None = None
    workspace_id: str | None = None
    type: str
    name: str | None = None
    config: dict = {}
    agent_id: str | None = None
    status: str
    created_at: str | None = None
    updated_at: str | None = None


class CreateChannelRequest(BaseModel):
    type: str
    name: str | None = None
    workspace_id: str | None = None
    agent_id: str | None = None
    config: dict | None = None


class UpdateChannelRequest(BaseModel):
    name: str | None = None
    type: str | None = None
    workspace_id: str | None = None
    agent_id: str | None = None
    config: dict | None = None
    status: str | None = None


# ── Helpers ──

_SECRET_MASK = "__unchanged__"
_SECRET_KEY_FRAGMENTS = ("password", "secret", "token", "api_key", "auth")


def _credential_preview(creds: dict) -> dict:
    """Return a sanitized credential preview for edit forms.

    The response must not expose a ``credentials`` field at all, but the
    UI still needs safe non-secret values and a marker that an existing
    secret should be preserved if the user leaves it untouched.
    """
    out: dict = {}
    for k, v in (creds or {}).items():
        if any(frag in k.lower() for frag in _SECRET_KEY_FRAGMENTS):
            out[k] = _SECRET_MASK if v else ""
        else:
            out[k] = v
    return out


def _integration_resp(i) -> IntegrationResponse:
    return IntegrationResponse(
        id=i.id, entity_id=i.entity_id, provider=i.provider,
        status=i.status, config=i.config,
        credential_preview=_credential_preview(i.credentials or {}),
        created_at=i.created_at.isoformat() if i.created_at else None,
        updated_at=i.updated_at.isoformat() if i.updated_at else None,
    )


def _channel_resp(c) -> ChannelResponse:
    return ChannelResponse(
        id=c.id, entity_id=c.entity_id, user_id=c.user_id,
        workspace_id=c.workspace_id,
        type=c.type, name=c.name, config=c.config,
        agent_id=c.agent_id, status=c.status,
        created_at=c.created_at.isoformat() if c.created_at else None,
        updated_at=c.updated_at.isoformat() if c.updated_at else None,
    )


# ── Integration endpoints ──

@router.get("", response_model=list[IntegrationResponse])
async def list_my_integrations(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    items = await list_integrations(db, user.entity_id)
    return [_integration_resp(i) for i in items]


@router.post("", response_model=IntegrationResponse, status_code=201)
async def create_new_integration(
    req: CreateIntegrationRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    integration = await create_integration(
        db, user.entity_id, req.provider,
        config=req.config, credentials=req.credentials,
    )

    # Channel-flavoured providers also get a ChannelConfig row + auto
    # webhook registration so inbound routing works without a separate
    # setup step.
    await _sync_channel_config_if_needed(
        db,
        entity_id=user.entity_id,
        provider=req.provider,
        integration_id=integration.id,
        credentials=req.credentials or {},
    )
    await db.commit()

    # Fire-and-forget health check so the user sees green/red on the
    # card within a couple of seconds of saving.
    try:
        from packages.core.tasks.channel_tasks import health_check_task
        health_check_task.delay(integration_id=integration.id)
    except Exception:
        logger.debug("Could not enqueue health check (Celery unreachable)", exc_info=True)

    # Readiness check (periodic task) will detect the new integration
    # and trigger Strategist review within 10 minutes.

    return _integration_resp(integration)


# ── Integration ↔ ChannelConfig bridge ────────────────────────────────

# integration.provider → [(channel_type, channel_provider), ...]
# Keep this list aligned with registered ChannelAdapters.
_INTEGRATION_TO_CHANNELS: dict[str, list[tuple[str, str]]] = {
    "telegram":        [("telegram",         "telegram_bot")],
    "whatsapp":        [("whatsapp",         "whatsapp_cloud")],
    "wechat_official": [("wechat",           "wechat_oa")],
    "wechat_personal": [("wechat_personal",  "itchat_runner")],
    "email":           [("email",            "smtp_imap")],
    "slack":           [("slack",            "slack_app")],
    "discord":         [("discord",          "discord_app")],
    # Twilio needs two channel types so one account can serve both SMS
    # and voice webhooks/dispatch without manual DB surgery.
    "twilio":          [("twilio_sms",       "twilio"), ("twilio_voice", "twilio")],
    "facebook":        [("facebook",         "facebook_graph")],
}


async def _sync_channel_config_if_needed(
    db: AsyncSession,
    *,
    entity_id: str,
    provider: str,
    integration_id: str,
    credentials: dict,
) -> None:
    """Mirror a channel-flavoured Integration into a ChannelConfig so
    inbound webhooks have credentials to verify + agents have a
    channel to bind to. Idempotent per (entity, channel_type).
    """
    mappings = _INTEGRATION_TO_CHANNELS.get(provider)
    if not mappings:
        return

    from sqlalchemy import select
    from packages.core.models.channel import ChannelConfig

    for channel_type, channel_provider in mappings:
        existing = (await db.execute(
            select(ChannelConfig).where(
                ChannelConfig.entity_id == entity_id,
                ChannelConfig.channel_type == channel_type,
                ChannelConfig.config["integration_id"].astext == integration_id,
            )
        )).scalar_one_or_none()

        if existing:
            existing.credentials = credentials
            existing.status = "active"
            cc = existing
        else:
            cc = ChannelConfig(
                entity_id=entity_id,
                channel_type=channel_type,
                provider=channel_provider,
                name=provider,
                config={"integration_id": integration_id},
                credentials=credentials,
                status="active",
            )
            db.add(cc)
            await db.flush()

        # Fire-and-forget webhook registration. Adapters that don't auto-
        # register (email, WeChat) leave this as a no-op.
        try:
            from packages.core.services.channels import get_adapter
            adapter = get_adapter(channel_type)
            if adapter is None:
                continue
            result = await adapter.register_webhook(cc)
            if result.get("registered"):
                logger.info(
                    "Channel webhook registered for %s config=%s url=%s",
                    channel_type, cc.id, result.get("url"),
                )
        except Exception:
            logger.exception(
                "Auto webhook registration failed for %s (ChannelConfig continues without it)",
                channel_type,
            )


async def _delete_integration_channel_bridges(
    db: AsyncSession,
    *,
    entity_id: str,
    integration_id: str,
) -> None:
    """Remove ChannelConfig/Channel rows mirrored from an Integration.

    Channel-flavoured integrations create shared ChannelConfig rows. Deleting
    the Integration must not leave those configs visible as attachable channels.
    """
    from packages.core.models.channel import ChannelConfig
    from packages.core.models.document import Channel

    cc_rows = (await db.execute(
        select(ChannelConfig).where(
            ChannelConfig.entity_id == entity_id,
            ChannelConfig.config["integration_id"].astext == integration_id,
        )
    )).scalars().all()
    cc_ids = [cc.id for cc in cc_rows]

    channel_filters = [
        Channel.entity_id == entity_id,
        Channel.config["integration_id"].astext == integration_id,
    ]
    if cc_ids:
        channel_filters.append(Channel.config["channel_config_id"].astext.in_(cc_ids))

    channel_rows = (await db.execute(
        select(Channel).where(or_(*channel_filters))
    )).scalars().all()
    for row in channel_rows:
        await db.delete(row)
    for row in cc_rows:
        await db.delete(row)


# ── Channel fixed paths (before parameterized integration paths) ──

@router.get("/channels", response_model=list[ChannelResponse])
async def list_all_channels(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List all channel bindings for the current user's entity."""
    channels = await list_channels(db, user.entity_id)
    return [_channel_resp(c) for c in channels]


@router.post("/channels", response_model=ChannelResponse, status_code=201)
async def create_new_channel(
    req: CreateChannelRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    channel = await create_channel(
        db, user.entity_id, req.type,
        name=req.name, user_id=user.id,
        workspace_id=req.workspace_id,
        agent_id=req.agent_id, config=req.config,
    )
    return _channel_resp(channel)


@router.get("/channels/{channel_id}", response_model=ChannelResponse)
async def get_one_channel(
    channel_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    channel = await get_channel(db, channel_id, user.entity_id)
    if not channel:
        raise HTTPException(404, "Channel not found")
    return _channel_resp(channel)


@router.put("/channels/{channel_id}", response_model=ChannelResponse)
async def update_one_channel(
    channel_id: str,
    req: UpdateChannelRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    channel = await update_channel(
        db, channel_id, user.entity_id,
        name=req.name, type=req.type, workspace_id=req.workspace_id,
        agent_id=req.agent_id, config=req.config, status=req.status,
    )
    if not channel:
        raise HTTPException(404, "Channel not found")
    return _channel_resp(channel)


@router.delete("/channels/{channel_id}", status_code=204)
async def delete_one_channel(
    channel_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    ok = await delete_channel(db, channel_id, user.entity_id)
    if not ok:
        raise HTTPException(404, "Channel not found")


# ── Integration parameterized paths ──

@router.get("/{integration_id}", response_model=IntegrationResponse)
async def get_one_integration(
    integration_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    integration = await get_integration(db, integration_id, user.entity_id)
    if not integration:
        raise HTTPException(404, "Integration not found")
    return _integration_resp(integration)


@router.put("/{integration_id}", response_model=IntegrationResponse)
async def update_one_integration(
    integration_id: str,
    req: UpdateIntegrationRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    # Honour the __unchanged__ sentinel for secret fields: when the
    # frontend sends back the sanitized credential_preview marker, we
    # preserve the existing stored value instead of overwriting it with
    # the mask string.
    credentials = req.credentials
    if credentials is not None:
        from packages.core.services.integration_service import get_integration as _get_int
        from packages.core.credentials import (
            CredentialError,
            Requester,
            get_credential_service,
        )
        current = await _get_int(db, integration_id, user.entity_id)
        if not current:
            raise HTTPException(404, "Integration not found")
        try:
            existing_creds = get_credential_service().lease_integration(
                current,
                requester=Requester(kind="user", id=user.id),
                reason="integration_update_preserve_existing_credentials",
            )
        except CredentialError as exc:
            logger.warning(
                "Could not lease credentials while updating integration %s: %s",
                integration_id,
                exc,
            )
            raise HTTPException(
                400,
                "Could not load existing credentials; re-enter credentials to update this integration.",
            ) from exc
        merged = dict(existing_creds)
        for k, v in credentials.items():
            if v == _SECRET_MASK:
                continue  # keep existing
            merged[k] = v
        credentials = merged

    integration = await update_integration(
        db, integration_id, user.entity_id,
        provider=req.provider, status=req.status,
        config=req.config, credentials=credentials,
    )
    if not integration:
        raise HTTPException(404, "Integration not found")

    # Keep ChannelConfig bridge rows in sync when credentials rotate.
    # Only run when this request actually carried credentials so we don't
    # overwrite ChannelConfig secrets with empty legacy JSONB payloads.
    if credentials is not None:
        try:
            await _sync_channel_config_if_needed(
                db,
                entity_id=user.entity_id,
                provider=integration.provider,
                integration_id=integration.id,
                credentials=credentials,
            )
        except Exception:
            logger.exception(
                "ChannelConfig sync failed after integration update (provider=%s id=%s)",
                integration.provider,
                integration.id,
            )

    # Refresh the health signal now that creds changed.
    try:
        from packages.core.tasks.channel_tasks import health_check_task
        health_check_task.delay(integration_id=integration.id)
    except Exception:
        logger.debug("Could not enqueue health check (Celery unreachable)", exc_info=True)

    return _integration_resp(integration)


@router.delete("/{integration_id}", status_code=204)
async def delete_one_integration(
    integration_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    integration = await get_integration(db, integration_id, user.entity_id)
    if not integration:
        raise HTTPException(404, "Integration not found")
    await _delete_integration_channel_bridges(
        db,
        entity_id=user.entity_id,
        integration_id=integration_id,
    )
    await db.delete(integration)
    await db.commit()


@router.get("/{integration_id}/channels", response_model=list[ChannelResponse])
async def list_integration_channels(
    integration_id: str,
    workspace_id: str | None = Query(None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    # Verify the integration belongs to this entity
    integration = await get_integration(db, integration_id, user.entity_id)
    if not integration:
        raise HTTPException(404, "Integration not found")
    channels = await list_channels(db, user.entity_id, workspace_id=workspace_id)
    return [_channel_resp(c) for c in channels]
