"""Integration & Channel endpoints — CRUD for external integrations and channels."""
from __future__ import annotations

import logging
import os
import secrets
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlsplit, urlunsplit

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.deps import get_current_user
from packages.core.credentials import CredentialError, get_credential_service
from packages.core.constants.plans import is_dev
from packages.core.database import get_db
from packages.core.models.integration_session import WechatPersonalSession
from packages.core.models.user import User
from packages.core.services.integration_health import is_credential_rejection
from packages.core.services.discord_app_config import (
    resolve_discord_app_config,
    validate_discord_app_config,
)
from packages.core.services.channels.discord_adapter import (
    leave_discord_guild,
    register_discord_guild_command,
)
from packages.core.services.integration_operation_catalog import (
    MCPServerKind,
    MCPServerKindFactory,
    nango_provider_capability,
)
from packages.core.services.integration_account_service import (
    IntegrationAccountAvailability,
    IntegrationAccountCatalogAccount,
    IntegrationAccountKind,
    load_integration_catalog_accounts,
    lock_runtime_integration_account_scope,
    normalize_runtime_integration_account_defaults,
    set_default_runtime_integration_account,
)
from packages.core.services.oauth_account_credentials import (
    lease_oauth_account_tokens,
    oauth_account_is_runtime_usable,
)
from packages.core.services.integration_service import (
    IntegrationCredentialConflictError,
    IntegrationProviderImmutableError,
    list_integrations, get_integration, create_integration, update_integration,
    delete_integration, list_channels, get_channel, create_channel,
    update_channel, delete_channel,
    list_integration_channels as list_owned_integration_channels,
)
from packages.core.services.provider_keys import (
    canonical_provider_key,
    provider_key_aliases,
)
from packages.core.ai.mcp.nango import get_nango_secret
from packages.core.services.whatsapp_business_config import (
    load_whatsapp_business_config,
)
from packages.core.services.whatsapp_business_provisioning import (
    delete_nango_connection,
    disconnect_whatsapp_business_account,
    provision_whatsapp_business_number,
)
from packages.core.constants.execution import WorkerStatus

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/integrations", tags=["integrations"])

_HIDDEN_CATALOG_SERVER_KEYS = {
    "nango",          # plumbing layer; per-provider cards own user flows
}


def _is_hidden_catalog_server_key(server_key: str | None) -> bool:
    key = str(server_key or "").strip().lower()
    if key.startswith("manor_mcp_") or key in _HIDDEN_CATALOG_SERVER_KEYS:
        return True
    return False


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
    mode: str | None = None
    configured_url: str | None = None
    expected_url: str | None = None
    last_error: str | None = None
    pending_update_count: int | None = None


class HealthStatus(BaseModel):
    ok: bool | None = None           # None when never tested
    reason_code: str | None = None
    detail: str | None = None
    latency_ms: float | None = None
    checked_at: str | None = None
    wiring: WiringStatus | None = None
    integration_id: str | None = None
    channel_config_id: str | None = None
    phone_number_id: str | None = None
    waba_id: str | None = None
    checks: dict[str, dict[str, Any]] | None = None


class UserOAuthConnection(BaseModel):
    """A single OAuth account a user has connected for this provider."""
    id: str                          # oauth_accounts.id
    display_name: str | None = None  # e.g. "jane@work.com" or profile name
    provider_user_id: str            # the provider's user id
    expires_at: str | None = None    # ISO; null = never expires / unknown
    is_default: bool = False
    connected_at: str | None = None  # ISO; useful for "most recent"
    health: HealthStatus | None = None
    kind: str = "oauth_account"
    ownership: str = "mine"
    owner_user_id: str | None = None
    owner_display_name: str | None = None
    can_manage: bool = True
    can_share: bool = False
    runtime_callable: bool = True
    availability: str = IntegrationAccountAvailability.CALLABLE.value


class EntityAccountConnection(BaseModel):
    """A user-owned non-OAuth account for this provider.

    The legacy response-model name is retained for API compatibility; fields
    record the actual owner and whether the requester only has shared use.
    """
    id: str                          # integrations.id
    name: str | None = None          # admin-set label ("Support inbox")
    display_name: str | None = None  # credential-free account label
    is_default: bool = False
    created_at: str | None = None
    status: str = "active"
    health: HealthStatus | None = None
    nango_backed: bool = False
    whatsapp_readiness_code: str | None = None
    kind: str = "integration"
    ownership: str = "mine"
    owner_user_id: str | None = None
    owner_display_name: str | None = None
    can_manage: bool = True
    can_share: bool = False
    runtime_callable: bool = True
    availability: str = IntegrationAccountAvailability.CALLABLE.value


class MCPServerStatus(BaseModel):
    server_key: str
    server_kind: MCPServerKind
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
    requires_explicit_account: bool = False
    hint: str

    # ── Nango bridge metadata ──
    # When the entity has a Nango admin Integration set up AND the
    # provider is configured in Nango admin, the frontend prefers a
    # Nango-managed Connect popup over direct OAuth. The end user never
    # sees "Nango" in the UI -- this flag just controls which connect
    # endpoint the card's button hits.
    nango_provider_config_key: str | None = None
    # Nango is only a transport for the declared provider contract. These
    # fields keep the UI from inferring native webhook/chat support from the
    # presence of a Nango connection.
    nango_mode: str | None = None
    nango_operations: dict[str, bool] = {}

    # ── OAuth readiness ──
    # True when this deployment has client_id/secret configured for the
    # provider (either via env bootstrap or admin-UI override). Drives
    # the "Connect" CTA — if false, end users see "OAuth not configured
    # yet" instead of a dead button.
    oauth_configured: bool = False
    oauth_client_secret_required: bool = True

    # ── Type-specific spec for non-OAuth/non-credentials AI tools ──
    # Populated for browser-session tools so the frontend can render the
    # headed-login flow.
    browser_spec: dict | None = None

    # Whether this integration is not yet production-ready. The frontend
    # disables the connect button and shows "Coming soon" when true.
    # Single source of truth: _COMING_SOON_SERVERS in integration_service.
    coming_soon: bool = False

    # Legacy flags — kept for frontend compat. Mirror the first/any element
    # of connections[] so existing UIs don't break.
    user_connected: bool = False
    user_expires_at: str | None = None


class MCPToolOperation(BaseModel):
    name: str
    tool_name: str
    label: str
    resource: str
    description: str = ""
    effect: str
    input_schema: dict = Field(default_factory=dict)
    output_schema: dict | None = None
    account_ids: list[str] = Field(default_factory=list)
    account_options: list[dict] = Field(default_factory=list)
    account_input_schemas: dict[str, dict] = Field(default_factory=dict)
    account_output_schemas: dict[str, dict] = Field(default_factory=dict)
    requires_explicit_account: bool = False
    supports_all_accounts: bool | None = None


class MCPToolCatalogResponse(BaseModel):
    server_key: str
    server_kind: MCPServerKind
    source: str
    operations: list[MCPToolOperation] = Field(default_factory=list)


class ConnectionGrantRequest(BaseModel):
    user_id: str


class ConnectionGrantResponse(BaseModel):
    id: str
    user_id: str
    capabilities: list[str]


def _connection_resource_type(kind: str) -> str | None:
    from packages.core.models.permission import ResourceType

    return {
        "integration": ResourceType.INTEGRATION,
        "oauth_account": ResourceType.OAUTH_ACCOUNT,
    }.get(kind)


async def _require_owned_connection(
    db: AsyncSession,
    *,
    kind: str,
    connection_id: str,
    user: User,
) -> None:
    if _connection_resource_type(kind) is None:
        raise HTTPException(404, "Connection not found")

    from packages.core.services.integration_access import resolve_integration_access

    decision = await resolve_integration_access(
        db,
        kind=kind,
        connection_id=connection_id,
        entity_id=user.entity_id,
        user_id=user.id,
        action="share",
    )
    if not decision.allowed:
        raise HTTPException(404, "Connection not found")


def _whatsapp_integration_is_ready(row: Any) -> bool:
    if canonical_provider_key(row.provider) != "whatsapp":
        return True
    cfg = row.config if isinstance(row.config, dict) else {}
    whatsapp = cfg.get("whatsapp") if isinstance(cfg.get("whatsapp"), dict) else {}
    return (
        whatsapp.get("provisioning_status") == "ready"
        and whatsapp.get("readiness_code") == "ready"
    )


class _CatalogConnectionFactory:
    """Build API-safe account projections from an authorized catalog account.

    The catalog loader intentionally carries no credential-bearing ORM rows.
    Callers authorize there first, then provide the matching persistence row
    solely for management and display fields.
    """

    @staticmethod
    def from_oauth(
        account: IntegrationAccountCatalogAccount,
        row: Any,
        *,
        owner_display_names: dict[str, str],
        can_share_owned_connections: bool,
    ) -> UserOAuthConnection:
        profile = row.profile if isinstance(row.profile, dict) else {}
        health_raw = profile.get("last_health_check")
        return UserOAuthConnection(
            id=row.id,
            display_name=account.display_name,
            provider_user_id=row.provider_user_id,
            expires_at=(
                row.token_expires_at.isoformat() if row.token_expires_at else None
            ),
            is_default=account.is_default,
            connected_at=row.created_at.isoformat() if row.created_at else None,
            health=(
                HealthStatus(**health_raw)
                if isinstance(health_raw, dict)
                else None
            ),
            ownership=account.ownership,
            owner_user_id=account.owner_user_id,
            owner_display_name=owner_display_names.get(account.owner_user_id or ""),
            can_manage=account.ownership == "mine",
            can_share=(
                account.ownership == "mine" and can_share_owned_connections
            ),
            runtime_callable=account.runtime_callable,
            availability=account.availability.value,
        )

    @staticmethod
    def from_integration(
        account: IntegrationAccountCatalogAccount,
        row: Any,
        *,
        owner_display_names: dict[str, str],
        can_share_owned_connections: bool,
        include_health: bool = True,
    ) -> EntityAccountConnection:
        cfg = row.config if isinstance(row.config, dict) else {}
        whatsapp = (
            cfg.get("whatsapp")
            if isinstance(cfg.get("whatsapp"), dict)
            else {}
        )
        health_raw = cfg.get("last_health_check") if include_health else None
        return EntityAccountConnection(
            id=row.id,
            name=cfg.get("name") or None,
            display_name=account.display_name,
            is_default=account.is_default,
            created_at=row.created_at.isoformat() if row.created_at else None,
            status=row.status,
            health=(
                HealthStatus(**health_raw)
                if isinstance(health_raw, dict)
                else None
            ),
            nango_backed=isinstance(cfg.get("nango"), dict),
            whatsapp_readiness_code=(
                str(whatsapp.get("readiness_code") or "").strip() or None
            ),
            ownership=account.ownership,
            owner_user_id=account.owner_user_id,
            owner_display_name=owner_display_names.get(account.owner_user_id or ""),
            can_manage=account.ownership == "mine",
            can_share=(
                account.ownership == "mine" and can_share_owned_connections
            ),
            runtime_callable=(
                account.runtime_callable and _whatsapp_integration_is_ready(row)
            ),
            availability=account.availability.value,
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
    "wechat_official": {"category": "Social",
                        "tagline": "WeChat Official Account (公众号) — customer service and template messages.",
                        "docs_url": "https://developers.weixin.qq.com/doc/offiaccount/Getting_Started/Overview.html",
                        "setup_hint": "Create a Subscription or Service Account at mp.weixin.qq.com; copy AppID + AppSecret.",
                        "color_hex": "#07C160", "supports_multi_account": False},
    "whatsapp": {"category": "Messaging", "tagline": "Send messages and manage templates with the WhatsApp Business Cloud API.",
                 "docs_url": "https://developers.facebook.com/docs/whatsapp/cloud-api/",
                 "setup_hint": "Add WhatsApp to a Meta app, then copy its access token, phone number ID, business account ID, verify token, and App Secret.",
                 "color_hex": "#25D366", "supports_multi_account": True,
                 "capabilities": [
                     "Send text, template, image, document, audio, and video messages",
                     "List, create, and delete WhatsApp message templates",
                     "Receive inbound messages and delivery/read status via webhooks",
                     "Read and update the public WhatsApp Business profile",
                 ],
                 "example_prompts": [
                     "Send the approved hello_world template to my test recipient.",
                     "Create a utility template for order status updates.",
                     "Show my WhatsApp phone quality rating and approved templates.",
                 ]},
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
                     "Fetch your profile and engagement insights for your posts",
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
    "youtube":        {"category": "Social",
                       "description": "YouTube Data API v3 — search videos and channels, read video, channel, comment, and caption data, upload videos from a user-approved public HTTPS URL, and manage owned video metadata, privacy, scheduling, comments, ratings, and playlists.",
                       "tagline": "Search YouTube, upload videos, and manage video metadata, comments, ratings, and playlists.",
                       "docs_url": "https://developers.google.com/youtube/v3",
                       "setup_hint": "Rides on your Google OAuth client — enable the YouTube Data API v3 and whitelist this provider's redirect URI.",
                       "color_hex": "#FF0000",
                       "supports_multi_account": True,
                       "capabilities": [
                           "Search videos, channels, and playlists",
                           "Read video/channel stats, comments, and captions",
                           "Upload videos from a user-approved public HTTPS URL",
                           "Post, reply to, and delete comments; like/dislike videos",
                           "Edit your video's metadata, privacy, scheduling, and playlists",
                       ],
                       "example_prompts": [
                           "Upload this approved HTTPS video URL as Private with this title and description.",
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
    "robinhood": {"category": "Finance",
                  "tagline": "Connect your own Robinhood accounts through official OAuth and Trading MCP.",
                  "capabilities": [
                      "Read portfolios, positions, watchlists and market data",
                      "Discover tools and parameter schemas from your connected account",
                      "Official consent can include Agentic trading; writes follow Manor approval policy",
                  ],
                  "example_prompts": ["Summarize my portfolio and the stocks on my watchlists."],
                  "docs_url": "https://robinhood.com/us/en/support/articles/agentic-trading-overview/",
                  "setup_hint": "Register this deployment's exact OAuth callback with Robinhood, then set ROBINHOOD_CLIENT_ID. No client secret is used. Each user must complete Robinhood consent; the internal scope is not read-only. This is not a shared market-data redistribution license.",
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
    from packages.core.models.mcp import MCPServer
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
        is_oauth_provider, oauth_client_configured, oauth_client_secret_required,
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

    # Resolve accounts through the connection-access service. Direct Entity
    # queries here would allow Entity owners/admins to enumerate another
    # member's private account from the catalog.
    server_keys = [s.server_key for s in servers]
    # Include Nango-only provider keys so virtual cards for HubSpot /
    # Linear / etc. can show their connected state from mirrored
    # Integration rows.
    lookup_keys = list({
        *server_keys,
        *nango_provider_keys.keys(),
        *(alias for key in server_keys for alias in provider_key_aliases(key)),
    })
    catalog_snapshot = await load_integration_catalog_accounts(
        db,
        user_id=user.id,
        entity_id=user.entity_id,
        provider_keys=lookup_keys,
    )
    runtime_accounts_by_provider = {
        provider: list(catalog_snapshot.accounts_for(provider))
        for provider in lookup_keys
    }

    # The runtime registry is the authorization boundary and deliberately
    # contains no ORM/credential references. Bulk-load source rows only for
    # account IDs the registry has already approved for this actor.
    authorized_accounts = [
        account
        for accounts in runtime_accounts_by_provider.values()
        for account in accounts
    ]
    oauth_account_ids = {
        account.id
        for account in authorized_accounts
        if account.kind is IntegrationAccountKind.OAUTH_ACCOUNT
    }
    integration_account_ids = {
        account.id
        for account in authorized_accounts
        if account.kind is IntegrationAccountKind.INTEGRATION
    }
    from packages.core.models.document import Integration
    from packages.core.models.user import OAuthAccount

    oauth_rows_by_id = {}
    if oauth_account_ids:
        oauth_source_rows = (await db.execute(
            select(OAuthAccount).where(OAuthAccount.id.in_(oauth_account_ids))
        )).scalars().all()
        oauth_rows_by_id = {row.id: row for row in oauth_source_rows}

    integration_rows_by_id = {}
    if integration_account_ids:
        integration_source_rows = (await db.execute(
            select(Integration).where(Integration.id.in_(integration_account_ids))
        )).scalars().all()
        integration_rows_by_id = {row.id: row for row in integration_source_rows}

    owner_ids = {
        account.owner_user_id
        for accounts in runtime_accounts_by_provider.values()
        for account in accounts
        if account.owner_user_id
    }
    owner_display_names: dict[str, str] = {}
    if owner_ids:
        owner_rows = (await db.execute(
            select(User).where(User.id.in_(owner_ids))
        )).scalars().all()
        owner_display_names = {
            row.id: row.display_name or row.email
            for row in owner_rows
        }

    from packages.core.permissions import Permission

    can_share_owned_connections = await user_has_permission(
        db,
        user.id,
        user.entity_id,
        Permission.INTEGRATIONS_SHARE,
    )

    out: list[MCPServerStatus] = []
    for s in servers:
        provider_accounts = runtime_accounts_by_provider.get(s.server_key, [])
        catalog_binding = catalog_snapshot.integration(s.server_key)
        requires_explicit_account = bool(
            catalog_binding and catalog_binding.requires_explicit_account
        )
        oauth_accounts = [
            (account, oauth_rows_by_id[account.id])
            for account in provider_accounts
            if (
                account.kind is IntegrationAccountKind.OAUTH_ACCOUNT
                and account.id in oauth_rows_by_id
            )
        ]
        integration_accounts = [
            (account, integration_rows_by_id[account.id])
            for account in provider_accounts
            if (
                account.kind is IntegrationAccountKind.INTEGRATION
                and account.id in integration_rows_by_id
            )
        ]
        usable_oauth_rows = [
            row
            for account, row in oauth_accounts
            if account.runtime_callable and oauth_account_is_runtime_usable(row)
        ]

        # Build typed connections list
        connections = [
            _CatalogConnectionFactory.from_oauth(
                account,
                row,
                owner_display_names=owner_display_names,
                can_share_owned_connections=can_share_owned_connections,
            )
            for account, row in oauth_accounts
        ]

        user_connected = bool(usable_oauth_rows)

        # Keep unavailable Nango rows visible to their owner as reconnectable,
        # but never turn an inaccessible connection into a catalog hint.
        live_integration_accounts = [
            (account, row)
            for account, row in integration_accounts
            if account.runtime_callable and _whatsapp_integration_is_ready(row)
        ]
        has_missing_nango_connection = any(
            account.availability
            is IntegrationAccountAvailability.RECONNECT_REQUIRED
            and isinstance(row.config, dict)
            and isinstance(row.config.get("nango"), dict)
            for account, row in integration_accounts
        )
        entity_accounts = [
            _CatalogConnectionFactory.from_integration(
                account,
                row,
                owner_display_names=owner_display_names,
                can_share_owned_connections=can_share_owned_connections,
            )
            for account, row in integration_accounts
        ]
        entity_accounts.sort(key=lambda a: (0 if a.is_default else 1, a.created_at or ""), reverse=False)

        # Prefer the default entity row (if marked); else most recent
        primary_entity_account = next(
            (
                account_and_row
                for account_and_row in live_integration_accounts
                if account_and_row[0].is_default
            ),
            live_integration_accounts[0] if live_integration_accounts else None,
        )
        primary_entity_row = (
            primary_entity_account[1] if primary_entity_account else None
        )
        permission_blocked_entity_account = next(
            (
                account_and_row
                for account_and_row in integration_accounts
                if account_and_row[0].availability
                is IntegrationAccountAvailability.PERMISSION_DENIED
            ),
            None,
        )
        permission_blocked_entity_row = (
            permission_blocked_entity_account[1]
            if permission_blocked_entity_account
            else None
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
            primary_entity_row.required_permission
            if primary_entity_row
            else (
                permission_blocked_entity_row.required_permission
                if permission_blocked_entity_row
                else None
            )
        )

        # The registry already checked every callable account's permission.
        # A blocked sibling must not disable a separate callable account.
        has_perm = (
            primary_entity_row is not None
            or permission_blocked_entity_row is None
        )

        # Agent's effective access right now
        handled_agent_access = False
        if not handled_agent_access:
            if user_connected:
                agent_can_use = True
                count = len(usable_oauth_rows)
                hint = (
                    "Personal connection active — agents can call this on your behalf."
                    if count == 1
                    else f"{count} personal connections — agents will use the default."
                )
            elif entity_connected and has_perm:
                agent_can_use = True
                hint = f"{s.name} connection active — agents can call this on your behalf."
            elif not has_perm:
                agent_can_use = False
                hint = (
                    f"Your role lacks '{required_permission}' for this {s.name} connection."
                )
            elif has_missing_nango_connection:
                agent_can_use = False
                hint = (
                    f"Nango connection is no longer available. Reconnect {s.name} "
                    "to let agents act on your behalf."
                )
            else:
                agent_can_use = False
                hint = f"Connect {s.name} to let agents act on your behalf."

        if requires_explicit_account and agent_can_use:
            usable_oauth_ids = {row.id for row in usable_oauth_rows}
            live_integration_ids = {
                account.id
                for account, _row in live_integration_accounts
            }
            explicit_callable = False
            for account in provider_accounts:
                if not account.runtime_callable:
                    continue
                if account.kind is IntegrationAccountKind.OAUTH_ACCOUNT:
                    row = oauth_rows_by_id.get(account.id)
                    explicit_callable = bool(
                        account.id in usable_oauth_ids
                        and row is not None
                        and (
                            row.token_expires_at is None
                            or row.token_expires_at >= now
                        )
                    )
                elif account.id in live_integration_ids:
                    health = next(
                        (
                            item.health
                            for item in entity_accounts
                            if item.id == account.id
                        ),
                        None,
                    )
                    explicit_callable = not (
                        health
                        and health.ok is False
                        and is_credential_rejection(health.detail)
                    )
                if explicit_callable:
                    break
            agent_can_use = explicit_callable

        if requires_explicit_account:
            hint = (
                f"{s.name} account metadata is incomplete — select an account "
                "explicitly."
                if agent_can_use
                else (
                    f"{s.name} account metadata is incomplete — repair or "
                    "reconnect an account to continue."
                )
            )

        effective_default = next(
            (
                account
                for account in provider_accounts
                if account.runtime_callable and account.is_default
            ),
            None,
        )

        # A credential the provider has actually refused is not usable, no
        # matter that a row exists. Only refusals count: a health check can
        # also fail because the network was down, and taking a working
        # integration away from agents over a DNS blip is worse than
        # letting one call fail.
        if (
            agent_can_use
            and effective_default is not None
            and effective_default.kind is IntegrationAccountKind.INTEGRATION
        ):
            primary_health = next(
                (
                    account.health
                    for account in entity_accounts
                    if account.id == effective_default.id
                ),
                None,
            )
            if (
                primary_health
                and primary_health.ok is False
                and is_credential_rejection(primary_health.detail)
            ):
                agent_can_use = False
                hint = (
                    f"{s.name} credentials were rejected by the provider — "
                    "re-enter them to let agents use this again."
                )

        # Only the cross-kind effective default controls runtime readiness.
        primary_oauth_row = (
            oauth_rows_by_id.get(effective_default.id)
            if (
                effective_default is not None
                and effective_default.kind is IntegrationAccountKind.OAUTH_ACCOUNT
            )
            else None
        )
        if primary_oauth_row is not None:
            if (
                primary_oauth_row.token_expires_at
                and primary_oauth_row.token_expires_at < now
            ):
                hint = f"Your {s.name} connection expired — reconnect to continue."
                agent_can_use = False

        display = _PROVIDER_DISPLAY.get(s.server_key, {})
        legacy_oauth_row = primary_oauth_row or next(
            (
                row
                for row in usable_oauth_rows
                if (row.profile or {}).get("is_default")
            ),
            usable_oauth_rows[0] if usable_oauth_rows else None,
        )
        legacy_expires = (
            legacy_oauth_row.token_expires_at.isoformat()
            if legacy_oauth_row and legacy_oauth_row.token_expires_at
            else None
        )
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
            server_kind=MCPServerKindFactory.from_server(
                s.server_key,
                s.transport,
            ),
            name=s.name,
            category=display.get("category"),
            description=display.get("description", s.description),
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
            requires_explicit_account=requires_explicit_account,
            hint=hint if not _is_coming_soon else "Coming soon",
            coming_soon=_is_coming_soon,
            # Built-in card lights up Nango Connect button when the
            # platform is also configured in our self-hosted Nango.
            nango_provider_config_key=nango_config_key_by_server.get(s.server_key),
            nango_mode=(
                nango_provider_capability(
                    nango_config_key_by_server.get(s.server_key) or ""
                ) or {}
            ).get("mode"),
            nango_operations=(
                nango_provider_capability(
                    nango_config_key_by_server.get(s.server_key) or ""
                ) or {}
            ).get("operations") or {},
            oauth_configured=s.server_key in oauth_configured_keys,
            oauth_client_secret_required=oauth_client_secret_required(s.server_key),
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
        provider_accounts = runtime_accounts_by_provider.get(nango_key, [])
        catalog_binding = catalog_snapshot.integration(nango_key)
        requires_explicit_account = bool(
            catalog_binding and catalog_binding.requires_explicit_account
        )
        integration_accounts = [
            (account, integration_rows_by_id[account.id])
            for account in provider_accounts
            if (
                account.kind is IntegrationAccountKind.INTEGRATION
                and account.id in integration_rows_by_id
            )
        ]
        primary = next(
            (
                account_and_row
                for account_and_row in integration_accounts
                if account_and_row[0].runtime_callable
            ),
            None,
        )
        connected = bool(primary)
        out.append(MCPServerStatus(
            server_key=nango_key,
            server_kind=MCPServerKind.MANAGED,
            name=_humanize_provider(nango_provider or nango_key),
            category=None,
            description=f"Connect via {_humanize_provider(nango_provider or nango_key)} OAuth.",
            auth_type="oauth2",
            scopes=None,
            tagline=None,
            docs_url=None,
            setup_hint=None,
            color_hex=None,
            supports_multi_account=True,
            connections=[],
            entity_accounts=[
                _CatalogConnectionFactory.from_integration(
                    account,
                    row,
                    owner_display_names=owner_display_names,
                    can_share_owned_connections=can_share_owned_connections,
                    include_health=False,
                )
                for account, row in integration_accounts
            ],
            entity_connected=connected,
            required_permission=None,
            user_has_required_permission=True,
            agent_can_use=connected,
            requires_explicit_account=requires_explicit_account,
            hint=(
                "Account metadata is incomplete — select an account explicitly "
                "or retry after the connection is repaired."
                if requires_explicit_account
                else (
                    f"Connected — agents can act on your {_humanize_provider(nango_provider or nango_key)} account."
                    if connected
                    else f"Click Connect to authorize {_humanize_provider(nango_provider or nango_key)}."
                )
            ),
            nango_provider_config_key=nango_key,
            nango_mode=(nango_provider_capability(nango_key) or {}).get("mode"),
            nango_operations=(
                nango_provider_capability(nango_key) or {}
            ).get("operations") or {},
        ))

    # Stable sort: by category order, then name
    cat_rank = {name: i for i, name in enumerate(_CATEGORY_ORDER)}
    out.sort(key=lambda m: (cat_rank.get(m.category or "", 999), m.name))
    return out


@router.get(
    "/connections/{kind}/{connection_id}/grants",
    response_model=list[ConnectionGrantResponse],
)
async def list_connection_grants(
    kind: str,
    connection_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List recipients for a connection the current user owns."""
    from packages.core.models.permission import GrantStatus, ResourceGrant, SubjectType
    from packages.core.permissions import Permission, check_effective_user_permission

    await _require_owned_connection(
        db,
        kind=kind,
        connection_id=connection_id,
        user=user,
    )
    await check_effective_user_permission(db, user, Permission.INTEGRATIONS_SHARE)
    resource_type = _connection_resource_type(kind)
    assert resource_type is not None
    rows = (await db.execute(
        select(ResourceGrant).where(
            ResourceGrant.entity_id == user.entity_id,
            ResourceGrant.resource_type == resource_type,
            ResourceGrant.resource_id == connection_id,
            ResourceGrant.subject_type == SubjectType.USER,
            ResourceGrant.status == GrantStatus.ACTIVE,
        ).order_by(ResourceGrant.granted_at.asc())
    )).scalars().all()
    return [
        ConnectionGrantResponse(
            id=row.id,
            user_id=row.subject_id,
            capabilities=list(row.capabilities or []),
        )
        for row in rows
    ]


@router.post(
    "/connections/{kind}/{connection_id}/grants",
    response_model=ConnectionGrantResponse,
    status_code=201,
)
async def create_connection_grant(
    kind: str,
    connection_id: str,
    req: ConnectionGrantRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Grant one named Entity member permission to use an owned connection."""
    from packages.core.permissions import Permission, check_effective_user_permission
    from packages.core.services.integration_access import grant_connection_use

    await _require_owned_connection(
        db,
        kind=kind,
        connection_id=connection_id,
        user=user,
    )
    await check_effective_user_permission(db, user, Permission.INTEGRATIONS_SHARE)
    try:
        grant = await grant_connection_use(
            db,
            kind=kind,
            connection_id=connection_id,
            entity_id=user.entity_id,
            owner_user_id=user.id,
            grantee_user_id=req.user_id,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    await db.commit()
    return ConnectionGrantResponse(
        id=grant.id,
        user_id=grant.subject_id,
        capabilities=list(grant.capabilities or []),
    )


@router.delete("/connections/{kind}/{connection_id}/grants/{grant_id}", status_code=204)
async def revoke_connection_grant(
    kind: str,
    connection_id: str,
    grant_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Revoke one recipient's use grant from an owned connection."""
    from packages.core.permissions import Permission, check_effective_user_permission
    from packages.core.services.integration_access import revoke_connection_use

    await _require_owned_connection(
        db,
        kind=kind,
        connection_id=connection_id,
        user=user,
    )
    await check_effective_user_permission(db, user, Permission.INTEGRATIONS_SHARE)
    revoked = await revoke_connection_use(
        db,
        kind=kind,
        connection_id=connection_id,
        grant_id=grant_id,
        entity_id=user.entity_id,
        owner_user_id=user.id,
    )
    if not revoked:
        raise HTTPException(404, "Grant not found")
    await db.commit()


@router.get(
    "/mcp-servers/{server_key}/tools",
    response_model=MCPToolCatalogResponse,
)
async def list_mcp_server_tools(
    server_key: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return credential-free operation schemas for the Workflow editor.

    Built-in servers are read from their executable ``list_tools()`` surface;
    remote MCP servers use the last discovered tools cache. Authentication is
    still resolved only when the Workflow executes the selected operation.
    """
    from packages.core.models.mcp import MCPServer
    from packages.core.services.integration_operation_catalog import (
        actor_integration_operation_catalog,
    )

    server = (await db.execute(
        select(MCPServer).where(
            MCPServer.server_key == server_key,
            MCPServer.status == "active",
        )
    )).scalar_one_or_none()
    if server is None or _is_hidden_catalog_server_key(server.server_key):
        raise HTTPException(404, "Integration operation catalog not found")

    operations, source = await actor_integration_operation_catalog(
        db,
        user_id=user.id,
        entity_id=user.entity_id,
        server_key=server.server_key,
        transport=server.transport,
        endpoint=server.endpoint,
        tools_cached=server.tools_cached,
    )
    return MCPToolCatalogResponse(
        server_key=server.server_key,
        server_kind=MCPServerKindFactory.from_server(
            server.server_key,
            server.transport,
        ),
        source=source,
        operations=operations,
    )




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
    """Prefer one callable OAuth account for the current user."""
    updated = await set_default_runtime_integration_account(
        db,
        kind=IntegrationAccountKind.OAUTH_ACCOUNT,
        user_id=user.id,
        entity_id=user.entity_id,
        provider=server_key,
        account_id=connection_id,
    )
    if not updated:
        raise HTTPException(404, "Connection not found")
    await db.commit()


@router.delete("/mcp-servers/{server_key}/connections/{connection_id}", status_code=204)
async def disconnect_account(
    server_key: str,
    connection_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Remove one OAuth account and promote a sibling when needed."""
    from sqlalchemy import select
    from packages.core.models.user import OAuthAccount

    await lock_runtime_integration_account_scope(
        db,
        kind=IntegrationAccountKind.OAUTH_ACCOUNT,
        user_id=user.id,
        entity_id=user.entity_id,
        provider=server_key,
    )
    row = (await db.execute(
        select(OAuthAccount).where(
            OAuthAccount.id == connection_id,
            OAuthAccount.user_id == user.id,
            OAuthAccount.provider.in_(provider_key_aliases(server_key)),
        )
    )).scalar_one_or_none()
    if not row:
        raise HTTPException(404, "Connection not found")
    if canonical_provider_key(server_key) == "discord":
        guild_id = str((row.profile or {}).get("guild_id") or "").strip()
        app = await resolve_discord_app_config(db)
        if app is not None and guild_id:
            try:
                await leave_discord_guild(app, guild_id)
            except Exception:
                logger.warning(
                    "Discord Guild leave failed for guild=%s; "
                    "continuing authoritative local disconnect",
                    guild_id,
                )
    await _delete_oauth_channel_bridges(
        db,
        entity_id=user.entity_id,
        owner_user_id=user.id,
        oauth_account_id=connection_id,
    )
    await db.delete(row)
    await db.flush()
    await normalize_runtime_integration_account_defaults(
        db,
        kind=IntegrationAccountKind.OAUTH_ACCOUNT,
        user_id=user.id,
        entity_id=user.entity_id,
        provider=server_key,
    )
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
    """Run the provider's test_connection for one user-owned account
    synchronously. Returns the probe result; the stored
    ``config.last_health_check`` is also refreshed."""
    from packages.core.services.integration_health import run_and_persist_integration
    from packages.core.services.integration_service import get_integration

    existing = await get_integration(
        db, account_id, user.entity_id, user.id, action="manage",
    )
    if not existing:
        raise HTTPException(404, "Account not found")

    result = await run_and_persist_integration(db, account_id)
    await db.commit()
    return HealthStatus(**result)


_WECHAT_RUNNER_URL = os.getenv(
    "WECHAT_RUNNER_URL", "http://wechat-runner:8800",
).rstrip("/")
_WECHAT_RUNNER_BEARER = os.getenv("WECHAT_RUNNER_BEARER_TOKEN", "").strip()


def _wechat_runner_headers(bearer_token: str | None = None) -> dict[str, str]:
    h: dict[str, str] = {"Accept": "application/json"}
    token = bearer_token or _WECHAT_RUNNER_BEARER
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


async def _require_wechat_runner_bearer(
    authorization: str | None = Header(None),
) -> None:
    """Authorize runner-only endpoints with the deployment shared secret."""
    if not _WECHAT_RUNNER_BEARER:
        raise HTTPException(503, "WeChat runner restore is not configured")
    supplied = (authorization or "").strip()
    expected = f"Bearer {_WECHAT_RUNNER_BEARER}"
    if not secrets.compare_digest(supplied, expected):
        raise HTTPException(401, "Bad WeChat runner bearer token")


def _wechat_session_id_from_creds(credentials: dict[str, Any]) -> str | None:
    return (str(credentials.get("session_id") or "").strip() or None)


def _wechat_runner_url_from_creds(credentials: dict[str, Any]) -> str:
    return str(credentials.get("runner_url") or _WECHAT_RUNNER_URL).rstrip("/")


async def _wechat_personal_session_for_owner(
    db: AsyncSession,
    *,
    session_id: str,
    entity_id: str,
    user_id: str,
    for_update: bool = False,
) -> WechatPersonalSession:
    query = select(WechatPersonalSession).where(
        WechatPersonalSession.session_id == session_id,
        WechatPersonalSession.entity_id == entity_id,
        WechatPersonalSession.owner_user_id == user_id,
    )
    if for_update:
        query = query.with_for_update()
    session = (await db.execute(query)).scalar_one_or_none()
    if not session:
        raise HTTPException(404, "Session not found — start a fresh one.")
    return session


def _wechat_personal_credentials(integration, user_id: str) -> dict[str, Any]:
    from packages.core.credentials import Requester, get_credential_service

    return get_credential_service().lease_integration(
        integration,
        requester=Requester(kind="user", id=user_id),
        reason="wechat_personal_runner_request",
    )


async def _wechat_restore_callback_url(
    db: AsyncSession,
    *,
    entity_id: str,
    integration_id: str,
) -> str | None:
    """Build the callback URL for a persisted WeChat integration."""
    from packages.core.config import get_settings
    from packages.core.models.channel import ChannelConfig

    channel_config = (await db.execute(
        select(ChannelConfig).where(
            ChannelConfig.entity_id == entity_id,
            ChannelConfig.credential_source_kind == "integration",
            ChannelConfig.credential_source_id == integration_id,
            ChannelConfig.channel_type == "wechat_personal",
        )
    )).scalar_one_or_none()
    if not channel_config:
        return None
    base = (get_settings().PUBLIC_BASE_URL or "").rstrip("/")
    if not base:
        return None
    return f"{base}/api/v1/channels/wechat_personal/callback?config_id={channel_config.id}"


@router.get(
    "/wechat-personal/internal/sessions",
    dependencies=[Depends(_require_wechat_runner_bearer)],
)
async def wechat_personal_restore_sessions(
    db: AsyncSession = Depends(get_db),
) -> list[dict[str, Any]]:
    """Return active token-backed sessions to a freshly started runner.

    This is an internal bearer-protected endpoint. It returns only the
    credentials required to recreate the in-memory iLink client; cursor,
    peer context, and QR state are intentionally not persisted.
    """
    from packages.core.models.document import Integration
    from packages.core.credentials import Requester, get_credential_service

    rows = (await db.execute(
        select(WechatPersonalSession).where(
            WechatPersonalSession.status == "active",
            WechatPersonalSession.integration_id.is_not(None),
        )
    )).scalars().all()
    response: list[dict[str, Any]] = []
    credentials_changed = False
    for session in rows:
        integration = (await db.execute(
            select(Integration).where(
                Integration.id == session.integration_id,
                Integration.entity_id == session.entity_id,
                Integration.provider == "wechat_personal",
            )
        )).scalar_one_or_none()
        if not integration:
            continue
        try:
            credentials = get_credential_service().lease_integration(
                integration,
                requester=Requester(kind="system", id="wechat-runner"),
                reason="wechat_personal_runner_restore",
            )
        except CredentialError:
            logger.warning(
                "Cannot lease credentials for WeChat session %s during restore",
                session.session_id,
            )
            continue
        bot_token = str(credentials.get("bot_token") or "").strip()
        if not bot_token:
            # Integrations created before token persistence need a new QR scan.
            continue

        runner_url = _wechat_runner_url_from_creds(credentials)
        if runner_url != _WECHAT_RUNNER_URL:
            # This endpoint serves the built-in singleton only. A custom
            # runner owns its own restore mechanism and must not receive its
            # token or have its bearer rewritten here.
            continue

        # A runner bearer rotation should not strand existing integrations.
        # Keep custom runner credentials untouched; the built-in runner uses
        # the current deployment secret for all restored accounts.
        if _WECHAT_RUNNER_BEARER and credentials.get("bearer_token") != _WECHAT_RUNNER_BEARER:
            credentials = {**credentials, "bearer_token": _WECHAT_RUNNER_BEARER}
            get_credential_service().store_integration(integration, credentials)
            credentials_changed = True

        response.append({
            "session_id": session.session_id,
            "bot_token": bot_token,
            "base_url": credentials.get("base_url"),
            "account": (integration.config or {}).get("ilink_account")
            if isinstance(integration.config, dict) else None,
            "callback_url": await _wechat_restore_callback_url(
                db,
                entity_id=session.entity_id,
                integration_id=integration.id,
            ),
            "callback_bearer": credentials.get("bearer_token"),
        })
    if credentials_changed:
        await db.commit()
    return response


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
    db: AsyncSession = Depends(get_db),
):
    """Spawn a runner session and persist its owner before returning it."""
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
    payload = r.json()
    session_id = str(payload.get("session_id") or "").strip()
    if not session_id:
        raise HTTPException(502, "Runner /sessions response did not include session_id")
    db.add(WechatPersonalSession(
        session_id=session_id,
        entity_id=user.entity_id,
        owner_user_id=user.id,
        status="pending",
    ))
    await db.flush()
    return payload


@router.get("/wechat-personal/sessions/{session_id}/status")
async def wechat_personal_session_status(
    session_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Pre-integration polling — same shape as the per-account status
    endpoint but addressed by runner session_id."""
    import httpx as _httpx
    await _wechat_personal_session_for_owner(
        db,
        session_id=session_id,
        entity_id=user.entity_id,
        user_id=user.id,
    )
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
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Proxy a QR image only to the owner of its pairing session."""
    import httpx as _httpx
    from fastapi.responses import Response as _Response
    await _wechat_personal_session_for_owner(
        db,
        session_id=session_id,
        entity_id=user.entity_id,
        user_id=user.id,
    )
    try:
        async with _httpx.AsyncClient(timeout=8) as client:
            r = await client.get(
                f"{_WECHAT_RUNNER_URL}/sessions/{session_id}/qr.png",
                headers=_wechat_runner_headers(),
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
    pairing_session = await _wechat_personal_session_for_owner(
        db,
        session_id=session_id,
        entity_id=user.entity_id,
        user_id=user.id,
        for_update=True,
    )
    if pairing_session.integration_id:
        integration = await get_integration(
            db,
            pairing_session.integration_id,
            user.entity_id,
            user.id,
            action="manage",
        )
        if not integration:
            raise HTTPException(404, "WeChat integration not found")
        return _integration_resp(
            integration,
            creator_names=await _creator_names(db, [integration]),
        )
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

    # The status surface is deliberately credential-free. Fetch the token
    # through the runner's bearer-protected internal endpoint only after the
    # QR flow reports an online session, then immediately vault it with the
    # Integration row.
    try:
        async with _httpx.AsyncClient(timeout=8) as client:
            credentials_response = await client.get(
                f"{_WECHAT_RUNNER_URL}/sessions/{session_id}/credentials",
                headers=_wechat_runner_headers(),
            )
    except _httpx.RequestError as exc:
        raise HTTPException(502, f"Runner unreachable: {exc}")
    if not credentials_response.is_success:
        raise HTTPException(
            502,
            "Runner did not return the paired bot token; deploy the current "
            f"runner image and retry (HTTP {credentials_response.status_code}).",
        )
    runner_credentials = credentials_response.json()
    bot_token = str(runner_credentials.get("bot_token") or "").strip()
    if not bot_token:
        raise HTTPException(502, "Runner credentials response did not include bot_token")

    account = status_data.get("account") or {}
    config = {
        "name": req.name or account.get("nick_name") or account.get("user_name") or "WeChat (personal)",
        "ilink_account": account,
    }
    creds = {
        "runner_url": _WECHAT_RUNNER_URL,
        "bearer_token": _WECHAT_RUNNER_BEARER,
        "session_id": session_id,
        "bot_token": bot_token,
        "base_url": runner_credentials.get("base_url"),
    }
    try:
        integration = await create_integration(
            db, user.entity_id, "wechat_personal",
            config=config, credentials=creds,
            created_by_user_id=user.id,
            owner_user_id=user.id,
        )
        await _sync_channel_config_if_needed(
            db,
            entity_id=user.entity_id,
            owner_user_id=user.id,
            provider="wechat_personal",
            integration_id=integration.id,
        )
        pairing_session.integration_id = integration.id
        pairing_session.status = "active"
        await db.commit()
    except CredentialError as exc:
        await _raise_credential_backend_unavailable(db, exc, action="wechat_personal_finish")
    await _register_integration_channel_webhooks(
        db, entity_id=user.entity_id, integration_id=integration.id,
    )
    return _integration_resp(
        integration,
        creator_names=await _creator_names(db, [integration]),
    )


@router.delete("/wechat-personal/sessions/{session_id}", status_code=204)
async def wechat_personal_cancel_session(
    session_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Tear down a runner session — used when the user closes the
    modal before scanning. Best-effort; the runner GCs orphaned
    sessions anyway."""
    import httpx as _httpx
    pairing_session = await _wechat_personal_session_for_owner(
        db,
        session_id=session_id,
        entity_id=user.entity_id,
        user_id=user.id,
        for_update=True,
    )
    if pairing_session.integration_id:
        raise HTTPException(409, "Session is already connected")
    try:
        async with _httpx.AsyncClient(timeout=6) as client:
            await client.delete(
                f"{_WECHAT_RUNNER_URL}/sessions/{session_id}",
                headers=_wechat_runner_headers(),
            )
    except _httpx.RequestError:
        pass
    await db.delete(pairing_session)
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
    existing = await get_integration(
        db, account_id, user.entity_id, user.id, action="manage",
    )
    if not existing or existing.provider != "wechat_personal":
        raise HTTPException(404, "Not a wechat_personal account")
    try:
        credentials = _wechat_personal_credentials(existing, user.id)
    except CredentialError as exc:
        await _raise_credential_backend_unavailable(
            db, exc, action="wechat_personal_status",
        )
    sid = _wechat_session_id_from_creds(credentials)
    if not sid:
        return {"online": False, "qr_pending": False,
                "last_error": "Integration has no session_id — re-scan needed."}
    runner_url = _wechat_runner_url_from_creds(credentials)
    try:
        async with _httpx.AsyncClient(timeout=6) as client:
            r = await client.get(
                f"{runner_url}/sessions/{sid}/status",
                headers=_wechat_runner_headers(credentials.get("bearer_token")),
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

    existing = await get_integration(
        db, account_id, user.entity_id, user.id, action="manage",
    )
    if not existing or existing.provider != "wechat_personal":
        raise HTTPException(404, "Not a wechat_personal account")
    try:
        credentials = _wechat_personal_credentials(existing, user.id)
    except CredentialError as exc:
        await _raise_credential_backend_unavailable(
            db, exc, action="wechat_personal_qr",
        )
    sid = _wechat_session_id_from_creds(credentials)
    if not sid:
        raise HTTPException(404, "Integration has no session_id.")
    runner_url = _wechat_runner_url_from_creds(credentials)
    try:
        async with _httpx.AsyncClient(timeout=8) as client:
            r = await client.get(
                f"{runner_url}/sessions/{sid}/qr.png",
                headers=_wechat_runner_headers(credentials.get("bearer_token")),
            )
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

    existing = await get_integration(
        db, account_id, user.entity_id, user.id, action="manage",
    )
    if not existing:
        raise HTTPException(404, "Account not found")

    if existing.provider == "telegram":
        try:
            await _repair_telegram_source_security(
                db, integration=existing, requester_user_id=user.id,
            )
            # The adapter leases source credentials from an independent
            # short-lived session, so make the repaired source visible first.
            await db.commit()
        except TelegramBotAlreadyConnected as exc:
            raise HTTPException(409, str(exc)) from exc

    # Find the ChannelConfig that bridges this Integration
    cc = (await db.execute(
        _select(ChannelConfig).where(
            ChannelConfig.entity_id == user.entity_id,
            ChannelConfig.owner_user_id == user.id,
            ChannelConfig.credential_source_kind == "integration",
            ChannelConfig.credential_source_id == account_id,
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
    bound_agent_subscription_id: str | None = None
    bound_workspace_id: str | None = None
    agent_name: str | None = None
    workspace_name: str | None = None
    binding_status: str | None = None
    last_inbound_at: str | None = None
    last_outbound_at: str | None = None


class UpsertChannelBindingRequest(BaseModel):
    channel_config_id: str
    agent_id: str | None = None   # null = unassigned
    agent_subscription_id: str | None = None


class WhatsAppProvisioningRetryRequest(BaseModel):
    registration_pin: str = Field(
        min_length=6,
        max_length=6,
        pattern=r"^[0-9]{6}$",
    )


class WhatsAppProvisioningRetryResponse(BaseModel):
    ok: bool
    integration_id: str
    readiness_code: str
    detail: str


@router.post(
    "/entity-accounts/{account_id}/whatsapp/provisioning/retry",
    response_model=WhatsAppProvisioningRetryResponse,
)
async def retry_whatsapp_business_provisioning(
    account_id: str,
    req: WhatsAppProvisioningRetryRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    integration = await get_integration(
        db,
        account_id,
        user.entity_id,
        user.id,
        action="manage",
    )
    if (
        integration is None
        or integration.owner_user_id != user.id
        or canonical_provider_key(integration.provider) != "whatsapp"
        or not isinstance((integration.config or {}).get("nango"), dict)
    ):
        raise HTTPException(404, "WhatsApp Business integration not found")
    try:
        outcome = await _provision_whatsapp_integration_account(
            db,
            entity_id=user.entity_id,
            integration_id=integration.id,
            registration_pin=req.registration_pin,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(503, "WhatsApp Business setup is unavailable") from exc
    return WhatsAppProvisioningRetryResponse(**outcome)


@router.get("/channel-bindings", response_model=list[ChannelBindingItem])
async def list_channel_bindings_endpoint(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Every ChannelConfig for the entity + which agent it's routing to
    (if any). Drives the Agent Channels tab."""
    from packages.core.services.integration_service import list_channel_bindings
    rows = await list_channel_bindings(db, user.entity_id, user.id)
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
            user_id=user.id,
            channel_config_id=req.channel_config_id,
            agent_id=req.agent_id,
            agent_subscription_id=req.agent_subscription_id,
            user_role=user.role,
        )
    except ValueError as e:
        raise HTTPException(404, str(e))
    await db.commit()

    # Return the refreshed row so the UI can reconcile
    rows = await list_channel_bindings(db, user.entity_id, user.id)
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
    removed = await delete_channel_binding(db, user.entity_id, user.id, channel_id)
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
        db, user.entity_id, user.id,
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


# ── User-owned accounts (credential / api-key providers) ───────────────────

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
    """Prefer one callable entity account for the current user."""
    updated = await set_default_runtime_integration_account(
        db,
        kind=IntegrationAccountKind.INTEGRATION,
        user_id=user.id,
        entity_id=user.entity_id,
        provider=server_key,
        account_id=account_id,
    )
    if not updated:
        raise HTTPException(404, "Account not found")
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
    """Remove one owned Integration row. Paired ChannelConfig row
    (if any) is also removed so inbound routing stops."""
    target = await get_integration(
        db, account_id, user.entity_id, user.id, action="manage",
    )
    if (
        not target
        or canonical_provider_key(target.provider)
        != canonical_provider_key(server_key)
    ):
        raise HTTPException(404, "Account not found")

    if canonical_provider_key(target.provider) == "whatsapp":
        try:
            await _disconnect_whatsapp_integration_once(
                db,
                entity_id=user.entity_id,
                integration_id=account_id,
            )
        except WhatsAppDisconnectPending:
            _enqueue_whatsapp_disconnect_retry(
                entity_id=user.entity_id,
                integration_id=account_id,
            )
        return

    await _delete_integration_channel_bridges(
        db,
        entity_id=user.entity_id,
        integration_id=account_id,
    )
    removed = await delete_integration(
        db,
        account_id,
        user.entity_id,
        user.id,
    )
    if not removed:
        raise HTTPException(404, "Account not found")
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
    connection_id: str | None = Query(None),
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
    from packages.core.services.oauth_flow import OAuthFlowError, begin_authorization

    if not is_oauth_provider(server_key):
        raise HTTPException(400, f"{server_key} is not an OAuth provider")

    if connection_id:
        from packages.core.models.user import OAuthAccount
        from packages.core.services.provider_keys import provider_key_aliases

        reconnect_row = (await db.execute(
            select(OAuthAccount).where(
                OAuthAccount.id == connection_id,
                OAuthAccount.user_id == user.id,
                OAuthAccount.provider.in_(provider_key_aliases(server_key)),
            )
        )).scalar_one_or_none()
        if not reconnect_row:
            raise HTTPException(404, "Connection not found")

    config = await resolve_oauth_config(db, server_key)
    if not config:
        raise HTTPException(
            501,
            f"{server_key} OAuth is not configured for this deployment. "
            f"Set the provider's client_id and client_secret (env or admin UI).",
        )

    if server_key == "discord":
        discord_app = await resolve_discord_app_config(db)
        if discord_app is None or not await validate_discord_app_config(discord_app):
            raise HTTPException(
                503,
                "Discord App runtime configuration is invalid",
            )

    app_url = os.getenv("APP_URL", "http://localhost:3010").rstrip("/")
    redirect_uri = f"{app_url}{config.redirect_path}"
    safe_return_to = _safe_oauth_return_path(return_to)
    try:
        start = await begin_authorization(
            config=config,
            user_id=user.id,
            redirect_uri=redirect_uri,
            entity_id=user.entity_id,
            return_to=safe_return_to,
            connection_id=connection_id,
        )
    except OAuthFlowError as exc:
        raise HTTPException(exc.status, exc.message)
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
    realm_id: str | None = Query(default=None, alias="realmId"),
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
    from packages.core.models.user import OAuthAccount, UserMembership
    from packages.core.services.oauth_provider_config import resolve_oauth_config
    from packages.core.services.oauth_flow import (
        complete_authorization, render_oauth_error_page, OAuthFlowError,
        get_pending_state,
        resolve_oauth_identity,
    )

    # Provider rejected (scope, cancel, app not approved) → human page.
    if error or not code or not state:
        return render_oauth_error_page(server_key, error, error_description)

    try:
        pending = await get_pending_state(state, server_key=server_key)
        return_to = pending.get("return_to")
        reconnect_connection_id = pending.get("connection_id")
        oauth_entity_id = pending.get("entity_id")
        if not oauth_entity_id:
            raise OAuthFlowError(400, "OAuth state is missing its Entity context")
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
    try:
        provider_user_id, identity_profile = await resolve_oauth_identity(
            server_key,
            tokens,
            application_id=config.client_id,
        )
    except OAuthFlowError as exc:
        raise HTTPException(exc.status, exc.message) from exc
    if server_key == "quickbooks" and str(realm_id or "").strip():
        identity_profile["realm_id"] = str(realm_id).strip()
    oauth_owner = (await db.execute(
        select(User).where(
            User.id == user_id,
            User.status == "active",
            User.deleted_at.is_(None),
        )
    )).scalar_one_or_none()
    if oauth_owner is None:
        raise HTTPException(404, "OAuth connection owner not found")
    active_membership = (await db.execute(
        select(UserMembership.id).where(
            UserMembership.user_id == user_id,
            UserMembership.entity_id == oauth_entity_id,
            UserMembership.status == "active",
            UserMembership.deleted_at.is_(None),
        )
    )).scalar_one_or_none()
    if active_membership is None:
        raise HTTPException(403, "OAuth connection Entity membership is no longer active")

    # Upsert one external account without overwriting sibling connections.
    from packages.core.services.provider_keys import provider_key_aliases

    aliases = provider_key_aliases(server_key)
    await lock_runtime_integration_account_scope(
        db,
        kind=IntegrationAccountKind.OAUTH_ACCOUNT,
        user_id=user_id,
        entity_id=oauth_entity_id,
        provider=server_key,
    )
    reconnect_row = None
    if reconnect_connection_id:
        reconnect_row = (await db.execute(
            select(OAuthAccount).where(
                OAuthAccount.id == reconnect_connection_id,
                OAuthAccount.user_id == user_id,
                OAuthAccount.provider.in_(aliases),
            )
        )).scalar_one_or_none()
    identity_row = (await db.execute(
        select(OAuthAccount).where(
            OAuthAccount.user_id == user_id,
            OAuthAccount.provider.in_(aliases),
            OAuthAccount.provider_user_id == provider_user_id,
        )
    )).scalar_one_or_none()
    # If a reconnect flow signs into an account that is already connected in
    # another row, update that identity rather than overwriting the requested
    # row or violating the per-user external-account uniqueness constraint.
    existing = identity_row or reconnect_row

    if existing:
        preserved_refresh_token = refresh_token
        if (
            server_key != "facebook"
            and not preserved_refresh_token
            and oauth_account_is_runtime_usable(existing)
        ):
            preserved_refresh_token = lease_oauth_account_tokens(
                existing,
                requester_id=user_id,
                requester_kind="user",
                reason="oauth.token.rotate_preserve_refresh_before_provider_update",
            ).get("refresh_token")
        existing.provider = server_key
        existing.provider_user_id = provider_user_id
        from packages.core.services.oauth_account_credentials import store_oauth_account_tokens
        store_oauth_account_tokens(
            existing,
            access_token=access_token,
            refresh_token=preserved_refresh_token,
            # Any old refresh token was leased before provider canonicalization
            # so the encrypted credential's original AAD remains readable.
            preserve_existing_refresh=False,
            requester_id=user_id,
        )
        existing.token_expires_at = token_expires_at
        profile = dict(existing.profile or {})
        if server_key == "slack":
            for key in (
                "app_id",
                "team_id",
                "team_name",
                "enterprise_id",
                "enterprise_name",
                "bot_user_id",
                "authed_user_id",
            ):
                profile.pop(key, None)
        if server_key == "discord":
            for key in ("application_id", "guild_id", "guild_name"):
                profile.pop(key, None)
        profile.update({
            key: value
            for key, value in identity_profile.items()
            if key != "is_default"
        })
        # A user just completed OAuth again, so any previous "auth failed /
        # reconnect required" health result is stale. The async health probe
        # below will write a fresh result after it validates the new token.
        profile.pop("last_health_check", None)
        profile.pop("oauth_refresh", None)
        existing.profile = profile
        oauth_row_id = existing.id
    else:
        profile = {
            key: value
            for key, value in identity_profile.items()
            if key != "is_default"
        }
        profile["is_default"] = False
        new_row = OAuthAccount(
            id=generate_ulid(),
            user_id=user_id,
            provider=server_key,
            provider_user_id=provider_user_id,
            token_expires_at=token_expires_at,
            profile=profile,
        )
        from packages.core.services.oauth_account_credentials import store_oauth_account_tokens
        store_oauth_account_tokens(
            new_row,
            access_token=access_token,
            refresh_token=refresh_token,
            preserve_existing_refresh=False,
            requester_id=user_id,
        )
        db.add(new_row)
        oauth_row_id = new_row.id
    await db.flush()
    await normalize_runtime_integration_account_defaults(
        db,
        kind=IntegrationAccountKind.OAUTH_ACCOUNT,
        user_id=user_id,
        entity_id=oauth_entity_id,
        provider=server_key,
    )
    try:
        await _sync_oauth_channel_config_if_needed(
            db,
            entity_id=oauth_entity_id,
            owner_user_id=user_id,
            provider=server_key,
            oauth_account_id=oauth_row_id,
            connection_profile=dict(
                existing.profile if existing else new_row.profile
            ),
        )
    except IntegrityError as exc:
        await db.rollback()
        if server_key == "discord":
            raise HTTPException(
                409,
                "This Discord Server is already connected. "
                "Ask its owner to share the connection instead.",
            ) from exc
        raise
    if server_key == "discord":
        guild_id = str(
            (existing.profile if existing else new_row.profile).get("guild_id")
            or ""
        ).strip()
        app = await resolve_discord_app_config(db)
        if (
            app is None
            or app.application_id != config.client_id
            or not guild_id
        ):
            await db.rollback()
            raise HTTPException(
                503,
                "Discord App runtime configuration is invalid",
            )
        try:
            await register_discord_guild_command(app, guild_id)
        except Exception as exc:
            await db.rollback()
            raise HTTPException(
                502,
                "Could not register the Discord /manor command",
            ) from exc
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
    client_secret: str = ""
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
    from packages.core.permissions import Permission, check_effective_user_permission
    from packages.core.services.oauth_provider_config import (
        is_oauth_provider, save_oauth_config, oauth_client_secret_required,
    )

    await check_effective_user_permission(db, user, Permission.USERS_MANAGE)

    if not is_oauth_provider(server_key):
        raise HTTPException(400, f"{server_key} is not an OAuth provider")

    if not req.client_id.strip() or (
        oauth_client_secret_required(server_key) and not req.client_secret.strip()
    ):
        raise HTTPException(400, "Required OAuth client credentials are missing")

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
    # Audit provenance remains distinct from the current connection owner.
    created_by_user_id: str | None = None
    created_by_name: str | None = None
    owner_user_id: str | None = None
    owner_display_name: str | None = None
    ownership: str = "mine"
    can_manage: bool = True
    can_share: bool = False
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


def _integration_config_preview(config: object) -> dict:
    """Return user-editable config without server-owned credential pointers."""
    out = dict(config) if isinstance(config, dict) else {}
    out.pop("nango", None)
    return out


def _integration_resp(
    i,
    *,
    creator_names: dict[str, str] | None = None,
    owner_names: dict[str, str] | None = None,
    requester_user_id: str | None = None,
    can_share: bool = False,
) -> IntegrationResponse:
    creator_id = getattr(i, "created_by_user_id", None)
    owner_id = getattr(i, "owner_user_id", None)
    is_owner = owner_id == requester_user_id if requester_user_id else True
    return IntegrationResponse(
        id=i.id, entity_id=i.entity_id, provider=i.provider,
        status=i.status, config=_integration_config_preview(i.config),
        credential_preview=_credential_preview(i.credentials or {}),
        created_by_user_id=creator_id,
        created_by_name=(creator_names or {}).get(creator_id) if creator_id else None,
        owner_user_id=owner_id,
        owner_display_name=(owner_names or {}).get(owner_id) if owner_id else None,
        ownership="mine" if is_owner else "shared",
        can_manage=is_owner,
        can_share=is_owner and can_share,
        created_at=i.created_at.isoformat() if i.created_at else None,
        updated_at=i.updated_at.isoformat() if i.updated_at else None,
    )


async def _creator_names(db: AsyncSession, integrations: list) -> dict[str, str]:
    """Display names for the users who connected these integrations.

    Rows predating ``created_by_user_id`` have no creator, and a user may have
    been deleted since — both simply resolve to no name rather than an error.
    """
    ids = {
        getattr(i, "created_by_user_id", None)
        for i in integrations
        if getattr(i, "created_by_user_id", None)
    }
    if not ids:
        return {}
    rows = (
        await db.execute(
            select(User.id, User.display_name, User.email).where(User.id.in_(ids))
        )
    ).all()
    return {
        str(uid): (display_name or email or "")
        for uid, display_name, email in rows
    }


async def _owner_names(db: AsyncSession, integrations: list) -> dict[str, str]:
    ids = {
        getattr(i, "owner_user_id", None)
        for i in integrations
        if getattr(i, "owner_user_id", None)
    }
    if not ids:
        return {}
    rows = (
        await db.execute(
            select(User.id, User.display_name, User.email).where(User.id.in_(ids))
        )
    ).all()
    return {
        str(uid): (display_name or email or "")
        for uid, display_name, email in rows
    }


async def _raise_credential_backend_unavailable(
    db: AsyncSession,
    exc: CredentialError,
    *,
    action: str,
) -> None:
    try:
        await db.rollback()
    except Exception:
        logger.debug("Could not roll back transaction after credential backend failure", exc_info=True)
    logger.warning("Credential backend unavailable during %s: %s", action, exc)
    raise HTTPException(
        503,
        "Credential backend is unavailable. Please try again later or contact an operator.",
    ) from exc


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
    from packages.core.permissions import Permission, effective_user_has_permission

    items = await list_integrations(db, user.entity_id, user.id)
    names = await _creator_names(db, items)
    owner_names = await _owner_names(db, items)
    can_share = await effective_user_has_permission(
        db, user, Permission.INTEGRATIONS_SHARE,
    )
    return [
        _integration_resp(
            i,
            creator_names=names,
            owner_names=owner_names,
            requester_user_id=user.id,
            can_share=can_share,
        )
        for i in items
    ]


@router.post("", response_model=IntegrationResponse, status_code=201)
async def create_new_integration(
    req: CreateIntegrationRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if canonical_provider_key(req.provider) in {"whatsapp", "whatsapp_cloud"}:
        raise HTTPException(
            400,
            "Connect WhatsApp Business with OAuth from the Integrations catalog.",
        )
    credentials = _prepare_channel_credentials(req.provider, req.credentials)
    telegram_bot_id = (
        await _telegram_bot_id(credentials)
        if canonical_provider_key(req.provider) == "telegram"
        else None
    )
    whatsapp_phone_number_id = (
        str((credentials or {}).get("phone_number_id") or "").strip() or None
        if canonical_provider_key(req.provider) == "whatsapp"
        else None
    )
    try:
        integration = await create_integration(
            db, user.entity_id, req.provider,
            config=req.config, credentials=credentials,
            created_by_user_id=user.id,
            owner_user_id=user.id,
        )

        # Channel-flavoured providers also get a ChannelConfig row + auto
        # webhook registration so inbound routing works without a separate
        # setup step.
        await _sync_channel_config_if_needed(
            db,
            entity_id=user.entity_id,
            owner_user_id=user.id,
            provider=integration.provider,
            integration_id=integration.id,
            telegram_bot_id=telegram_bot_id,
            whatsapp_phone_number_id=whatsapp_phone_number_id,
        )
        await db.commit()
    except CredentialError as exc:
        await _raise_credential_backend_unavailable(db, exc, action="create_integration")
    except TelegramBotAlreadyConnected as exc:
        raise HTTPException(409, str(exc)) from exc
    except WhatsAppPhoneAlreadyConnected as exc:
        raise HTTPException(409, str(exc)) from exc

    await _register_integration_channel_webhooks(
        db, entity_id=user.entity_id, integration_id=integration.id,
    )

    # Fire-and-forget health check so the user sees green/red on the
    # card within a couple of seconds of saving.
    try:
        from packages.core.tasks.channel_tasks import health_check_task
        health_check_task.delay(integration_id=integration.id)
    except Exception:
        logger.debug("Could not enqueue health check (Celery unreachable)", exc_info=True)

    # Readiness check (periodic task) will detect the new integration
    # and trigger Strategist review within 10 minutes.

    return _integration_resp(
        integration,
        creator_names=await _creator_names(db, [integration]),
        owner_names=await _owner_names(db, [integration]),
        requester_user_id=user.id,
    )


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
    "ms_teams":        [("ms_teams",          "microsoft_graph")],
    "outlook":         [("outlook",           "microsoft_graph")],
    # Twilio needs two channel types so one account can serve both SMS
    # and voice webhooks/dispatch without manual DB surgery.
    "twilio":          [("twilio_sms",       "twilio"), ("twilio_voice", "twilio")],
    "facebook":        [("facebook",         "facebook_graph")],
}


class TelegramBotAlreadyConnected(ValueError):
    """Raised when a Telegram bot already has a webhook owner in Manor."""


class WhatsAppPhoneAlreadyConnected(ValueError):
    """Raised when a WhatsApp phone number is already routed in Manor."""


class WhatsAppDisconnectPending(RuntimeError):
    """Raised after fail-closed state is durable but provider cleanup is pending."""


def _prepare_channel_credentials(provider: str, credentials: dict | None) -> dict | None:
    """Validate provider webhook credentials before vaulting."""
    if credentials is None:
        return None
    prepared = dict(credentials)
    if canonical_provider_key(provider) == "telegram":
        if not str(prepared.get("bot_token") or "").strip():
            raise HTTPException(422, "Telegram bot_token is required")
        # Telegram allows URL-safe characters and requires this value on every
        # delivery. It is generated by Manor, never accepted from browser UI.
        prepared["secret_token"] = secrets.token_urlsafe(32)
    elif canonical_provider_key(provider) == "whatsapp":
        from packages.core.services.whatsapp_business_config import (
            load_whatsapp_business_config,
        )

        if not str(prepared.get("phone_number_id") or "").strip():
            raise HTTPException(422, "WhatsApp phone_number_id is required")
        if not str(prepared.get("access_token") or "").strip():
            raise HTTPException(422, "WhatsApp access_token is required")
        try:
            load_whatsapp_business_config()
        except RuntimeError as exc:
            raise HTTPException(422, str(exc)) from exc
        prepared.pop("app_secret", None)
        prepared.pop("verify_token", None)
    elif canonical_provider_key(provider) == "wechat_official":
        if not str(prepared.get("app_id") or "").strip():
            raise HTTPException(422, "WeChat app_id is required")
        if not str(prepared.get("app_secret") or "").strip():
            raise HTTPException(422, "WeChat app_secret is required")
        if not str(prepared.get("token") or "").strip():
            raise HTTPException(422, "WeChat callback token is required")
        if str(prepared.get("encoding_aes_key") or "").strip():
            raise HTTPException(
                422,
                "WeChat encrypted callbacks are not supported; use plain-text mode and omit encoding_aes_key.",
            )
    return prepared


async def _assert_whatsapp_phone_available(
    db: AsyncSession,
    *,
    phone_number_id: str,
    integration_id: str,
) -> None:
    from packages.core.models.channel import ChannelConfig

    other = (await db.execute(
        select(ChannelConfig).where(
            ChannelConfig.whatsapp_phone_number_id == phone_number_id,
            or_(
                ChannelConfig.credential_source_id.is_(None),
                ChannelConfig.credential_source_id != integration_id,
            ),
        )
    )).scalar_one_or_none()
    if other:
        raise WhatsAppPhoneAlreadyConnected(
            "This WhatsApp phone number is already connected by another user."
        )


async def _telegram_bot_id(credentials: dict | None) -> str | None:
    if not credentials:
        return None
    token = str(credentials.get("bot_token") or "").strip()
    if not token:
        raise HTTPException(422, "Telegram bot_token is required")
    from packages.core.services.channels.telegram_adapter import TelegramAdapter

    try:
        bot = await TelegramAdapter(token).get_me()
    except Exception as exc:
        raise HTTPException(400, f"Telegram rejected the bot token: {exc}") from exc
    bot_id = str((bot or {}).get("id") or "").strip()
    if not bot_id or not bool((bot or {}).get("is_bot")):
        raise HTTPException(400, "Telegram token did not resolve to a bot")
    return bot_id


async def _assert_telegram_bot_available(
    db: AsyncSession,
    *,
    bot_id: str,
    integration_id: str,
) -> None:
    from packages.core.models.channel import ChannelConfig

    other_owner = (await db.execute(
        select(ChannelConfig).where(
            ChannelConfig.telegram_bot_id == bot_id,
            ChannelConfig.credential_source_id != integration_id,
        )
    )).scalar_one_or_none()
    if other_owner:
        raise TelegramBotAlreadyConnected(
            "This Telegram bot is already connected by another user. "
            "Ask its owner to share the connection instead."
        )


async def _repair_telegram_source_security(
    db: AsyncSession,
    *,
    integration,
    requester_user_id: str,
) -> None:
    """Backfill the server-owned webhook secret on a legacy Telegram bot."""
    from packages.core.credentials import Requester, get_credential_service

    credential_service = get_credential_service()
    credentials = credential_service.lease_integration(
        integration,
        requester=Requester(kind="user", id=requester_user_id),
        reason="telegram_manual_webhook_registration",
    )
    if not credentials.get("secret_token"):
        credentials = dict(credentials)
        credentials["secret_token"] = secrets.token_urlsafe(32)
        credential_service.store_integration(integration, credentials)

    telegram_bot_id = await _telegram_bot_id(credentials)
    await _assert_telegram_bot_available(
        db, bot_id=telegram_bot_id, integration_id=integration.id,
    )
    for channel_config in await _channel_configs_for_integration(
        db, entity_id=integration.entity_id, integration_id=integration.id,
    ):
        if channel_config.channel_type == "telegram":
            channel_config.telegram_bot_id = telegram_bot_id
            channel_config.credentials = {}
            channel_config.credential_ref = None
    await db.flush()


async def _channel_configs_for_integration(
    db: AsyncSession,
    *,
    entity_id: str,
    integration_id: str,
):
    from packages.core.models.channel import ChannelConfig

    return (await db.execute(
        select(ChannelConfig).where(
            ChannelConfig.entity_id == entity_id,
            ChannelConfig.credential_source_kind == "integration",
            ChannelConfig.credential_source_id == integration_id,
        )
    )).scalars().all()


def _classify_whatsapp_provisioning_result(result) -> tuple[str, str, str, bool]:
    from packages.core.services.whatsapp_business_provisioning import (
        WhatsAppPhoneState,
    )

    if result.ok:
        return (
            "ready",
            "ready",
            "WhatsApp Business account is ready.",
            False,
        )
    if result.phone_state is WhatsAppPhoneState.REGISTRATION_REQUIRED:
        return (
            "registration_required",
            "phone_not_registered",
            "Enter the WhatsApp Business number's six-digit registration PIN.",
            False,
        )
    if not result.exact_app_subscribed:
        return (
            "failed",
            "app_not_subscribed",
            "The Manor Meta App subscription is not confirmed yet.",
            True,
        )
    return (
        "failed",
        "provider_unavailable",
        "WhatsApp Business provider setup is temporarily unavailable.",
        True,
    )


async def _set_whatsapp_provisioning_state(
    db: AsyncSession,
    *,
    integration_id: str,
    provisioning_status: str,
    readiness_code: str,
    detail: str,
) -> None:
    from packages.core.models.document import Integration

    integration = await db.get(Integration, integration_id)
    if integration is None or canonical_provider_key(integration.provider) != "whatsapp":
        return
    config = dict(integration.config or {})
    whatsapp = dict(config.get("whatsapp") or {})
    ready = provisioning_status == "ready" and readiness_code == "ready"
    whatsapp["provisioning_status"] = provisioning_status
    whatsapp["readiness_code"] = readiness_code
    whatsapp["provisioning_attempt_count"] = (
        int(whatsapp.get("provisioning_attempt_count") or 0) + 1
    )
    whatsapp["provisioning_last_attempt_at"] = datetime.now(timezone.utc).isoformat()
    # Keep the legacy projection until every inventory/readiness consumer has
    # moved to provisioning_status.
    whatsapp["subscription_status"] = "ready" if ready else "failed"
    if ready or provisioning_status == "registration_required":
        whatsapp.pop("provisioning_error", None)
        whatsapp.pop("subscription_error", None)
    else:
        whatsapp["provisioning_error"] = detail
        whatsapp["subscription_error"] = detail
    config["whatsapp"] = whatsapp
    config["last_health_check"] = {
        "ok": ready,
        "reason_code": "healthy" if ready else readiness_code,
        "detail": detail,
        "latency_ms": 0,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }
    integration.config = config

    for channel_config in await _channel_configs_for_integration(
        db,
        entity_id=integration.entity_id,
        integration_id=integration.id,
    ):
        if channel_config.channel_type != "whatsapp":
            continue
        channel_config.status = "active" if ready else "error"
        channel_config.config = {
            **(channel_config.config or {}),
            "whatsapp_provisioning_status": provisioning_status,
            "whatsapp_readiness_code": readiness_code,
            "whatsapp_registration_pending": not ready,
            "whatsapp_last_registration_error": (
                None if ready or provisioning_status == "registration_required" else detail
            ),
        }


async def _set_whatsapp_subscription_state(
    db: AsyncSession,
    *,
    integration_id: str,
    ready: bool,
    detail: str,
) -> None:
    """Compatibility wrapper for legacy callers during the provisioning rollout."""
    await _set_whatsapp_provisioning_state(
        db,
        integration_id=integration_id,
        provisioning_status="ready" if ready else "failed",
        readiness_code="ready" if ready else "app_not_subscribed",
        detail=detail,
    )


async def _stage_whatsapp_reconnect(
    db: AsyncSession,
    *,
    entity_id: str,
    owner_user_id: str,
    integration_id: str,
    provider_config_key: str,
    connection_id: str,
    waba_id: str,
    phone_number_id: str,
    display_name: str | None,
    synced_at: str | None,
) -> tuple[str, str]:
    """Persist a non-secret replacement snapshot without changing the live route."""
    from packages.core.models.document import Integration

    integration = (await db.execute(
        select(Integration).where(
            Integration.id == integration_id,
            Integration.entity_id == entity_id,
            Integration.owner_user_id == owner_user_id,
            Integration.status == "active",
        ).with_for_update().execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if integration is None:
        raise ValueError("Integration selected for reconnect was not found")
    config = dict(integration.config or {})
    live_nango = config.get("nango")
    if not isinstance(live_nango, dict) or not live_nango.get("connection_id"):
        raise ValueError("Only an active Nango-backed Integration can be reconnected")
    live_provider_config_key = str(
        live_nango.get("provider_config_key") or integration.provider
    ).strip()
    if canonical_provider_key(provider_config_key) not in {
        canonical_provider_key(integration.provider),
        canonical_provider_key(live_provider_config_key),
    }:
        raise ValueError("Reconnect provider does not match the selected Integration")

    previous_pending = config.get("whatsapp_reconnect")
    previous_attempts = (
        int(previous_pending.get("attempt_count") or 0)
        if isinstance(previous_pending, dict)
        and previous_pending.get("connection_id") == connection_id
        else 0
    )
    config["whatsapp_reconnect"] = {
        "status": "provisioning",
        "readiness_code": "provider_unavailable",
        "provider_config_key": provider_config_key,
        "connection_id": connection_id,
        "waba_id": waba_id,
        "phone_number_id": phone_number_id,
        "display_name": display_name,
        "synced_at": synced_at,
        "connected_by_user_id": owner_user_id,
        "attempt_count": previous_attempts + 1,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    integration.config = config
    await db.flush()
    return str(live_nango["connection_id"]), live_provider_config_key


async def _apply_whatsapp_reconnect_result(
    db: AsyncSession,
    *,
    entity_id: str,
    owner_user_id: str,
    integration_id: str,
    connection_id: str,
    provisioning_status: str,
    readiness_code: str,
    detail: str,
    retryable: bool,
    credential_service=None,
) -> tuple[bool, tuple[str, str] | None]:
    """Update pending evidence or atomically replace the live account pointer."""
    from packages.core.models.document import Integration

    integration = (await db.execute(
        select(Integration).where(
            Integration.id == integration_id,
            Integration.entity_id == entity_id,
            Integration.owner_user_id == owner_user_id,
            Integration.status == "active",
        ).with_for_update().execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if integration is None:
        raise ValueError("Integration selected for reconnect was not found")
    config = dict(integration.config or {})
    pending = config.get("whatsapp_reconnect")
    if not isinstance(pending, dict) or pending.get("connection_id") != connection_id:
        raise ValueError("WhatsApp reconnect state changed before provisioning completed")

    pending = {
        **pending,
        "status": provisioning_status,
        "readiness_code": readiness_code,
        "retryable": retryable,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    if readiness_code == "ready" or provisioning_status == "registration_required":
        pending.pop("detail", None)
    else:
        pending["detail"] = detail
    config["whatsapp_reconnect"] = pending
    integration.config = config
    if readiness_code != "ready":
        await db.flush()
        return False, None

    phone_number_id = str(pending.get("phone_number_id") or "").strip()
    waba_id = str(pending.get("waba_id") or "").strip()
    provider_config_key = str(
        pending.get("provider_config_key") or "whatsapp"
    ).strip()
    if not phone_number_id or not waba_id or not provider_config_key:
        raise ValueError("WhatsApp reconnect assets are incomplete")
    await _assert_whatsapp_phone_available(
        db,
        phone_number_id=phone_number_id,
        integration_id=integration_id,
    )
    live_nango = config.get("nango")
    if not isinstance(live_nango, dict) or not live_nango.get("connection_id"):
        raise ValueError("The live WhatsApp Nango connection is unavailable")
    old_connection_id = str(live_nango["connection_id"])
    old_provider_config_key = str(
        live_nango.get("provider_config_key") or integration.provider
    ).strip()
    superseded_ids = [
        item
        for item in live_nango.get("superseded_connection_ids", [])
        if isinstance(item, str) and item != connection_id
    ]
    if old_connection_id != connection_id and old_connection_id not in superseded_ids:
        superseded_ids.append(old_connection_id)
    new_nango = {
        "connection_id": connection_id,
        "provider_config_key": provider_config_key,
        "synced_at": pending.get("synced_at"),
        "connected_by_user_id": owner_user_id,
    }
    if superseded_ids:
        new_nango["superseded_connection_ids"] = superseded_ids
    config["nango"] = new_nango
    config["whatsapp"] = {
        "waba_id": waba_id,
        "phone_number_id": phone_number_id,
        "display_name": pending.get("display_name"),
        "provisioning_status": "ready",
        "readiness_code": "ready",
        "subscription_status": "ready",
    }
    config.pop("whatsapp_reconnect", None)
    if old_connection_id != connection_id:
        config["whatsapp_retirement"] = {
            "status": "pending",
            "provider_config_key": old_provider_config_key,
            "connection_id": old_connection_id,
            "attempt_count": 0,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
    integration.config = config
    (credential_service or get_credential_service()).store_integration(
        integration,
        {
            "via": "nango",
            "connection_id": connection_id,
            "provider_config_key": provider_config_key,
        },
    )
    await _sync_channel_config_if_needed(
        db,
        entity_id=entity_id,
        owner_user_id=owner_user_id,
        provider="whatsapp",
        integration_id=integration_id,
        whatsapp_phone_number_id=phone_number_id,
        whatsapp_provisioning_pending=False,
    )
    await _set_whatsapp_provisioning_state(
        db,
        integration_id=integration_id,
        provisioning_status="ready",
        readiness_code="ready",
        detail=detail,
    )
    await db.flush()
    retirement = (
        (old_provider_config_key, old_connection_id)
        if old_connection_id != connection_id
        else None
    )
    return True, retirement


async def _clear_whatsapp_retirement_state(
    db: AsyncSession,
    *,
    entity_id: str,
    integration_id: str,
    connection_id: str,
) -> None:
    from packages.core.models.document import Integration

    integration = (await db.execute(
        select(Integration).where(
            Integration.id == integration_id,
            Integration.entity_id == entity_id,
        ).with_for_update().execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if integration is None:
        return
    config = dict(integration.config or {})
    retirement = config.get("whatsapp_retirement")
    if isinstance(retirement, dict) and retirement.get("connection_id") == connection_id:
        config.pop("whatsapp_retirement", None)
        integration.config = config
        await db.flush()


async def _provision_whatsapp_integration_account(
    db: AsyncSession,
    *,
    entity_id: str,
    integration_id: str,
    registration_pin: str | None = None,
) -> dict[str, Any]:
    import httpx

    from packages.core.models.document import Integration

    integration = await db.get(Integration, integration_id)
    if (
        integration is None
        or integration.entity_id != entity_id
        or canonical_provider_key(integration.provider) != "whatsapp"
    ):
        raise ValueError("WhatsApp Business integration not found")
    config = integration.config if isinstance(integration.config, dict) else {}
    nango = config.get("nango") if isinstance(config.get("nango"), dict) else {}
    whatsapp = (
        config.get("whatsapp")
        if isinstance(config.get("whatsapp"), dict)
        else {}
    )
    reconnect = (
        config.get("whatsapp_reconnect")
        if isinstance(config.get("whatsapp_reconnect"), dict)
        else None
    )
    source_nango = reconnect or nango
    source_whatsapp = reconnect or whatsapp
    connection_id = str(source_nango.get("connection_id") or "").strip()
    provider_config_key = str(
        source_nango.get("provider_config_key") or "whatsapp"
    ).strip()
    phone_number_id = str(source_whatsapp.get("phone_number_id") or "").strip()
    waba_id = str(source_whatsapp.get("waba_id") or "").strip()
    if not connection_id or not provider_config_key or not phone_number_id or not waba_id:
        raise ValueError("WhatsApp Business account assets are incomplete")
    nango_secret = await get_nango_secret(db, entity_id)
    if not nango_secret:
        raise RuntimeError("Nango is not configured")
    deployment = load_whatsapp_business_config()

    # Release all database locks before provider I/O. The exact connection and
    # asset snapshot is revalidated when Task 9 adds replacement staging.
    await db.commit()
    try:
        result = await provision_whatsapp_business_number(
            nango_secret=nango_secret,
            provider_config_key=provider_config_key,
            connection_id=connection_id,
            phone_number_id=phone_number_id,
            waba_id=waba_id,
            expected_app_id=deployment.app_id,
            registration_pin=registration_pin,
        )
    except (httpx.HTTPError, RuntimeError):
        provisioning_status = "failed"
        readiness_code = "provider_unavailable"
        detail = "WhatsApp Business provider setup is temporarily unavailable."
        retryable = True
    else:
        (
            provisioning_status,
            readiness_code,
            detail,
            retryable,
        ) = _classify_whatsapp_provisioning_result(result)
    retirement: tuple[str, str] | None = None
    if reconnect is not None:
        _activated, retirement = await _apply_whatsapp_reconnect_result(
            db,
            entity_id=entity_id,
            owner_user_id=str(integration.owner_user_id or ""),
            integration_id=integration_id,
            connection_id=connection_id,
            provisioning_status=provisioning_status,
            readiness_code=readiness_code,
            detail=detail,
            retryable=retryable,
        )
    else:
        await _set_whatsapp_provisioning_state(
            db,
            integration_id=integration_id,
            provisioning_status=provisioning_status,
            readiness_code=readiness_code,
            detail=detail,
        )
    await db.commit()
    if retirement is not None:
        old_provider_config_key, old_connection_id = retirement
        try:
            await delete_nango_connection(
                nango_secret=nango_secret,
                provider_config_key=old_provider_config_key,
                connection_id=old_connection_id,
            )
        except Exception:
            try:
                from packages.core.tasks.channel_tasks import (
                    retire_nango_connection_task,
                )

                retire_nango_connection_task.delay(
                    entity_id=entity_id,
                    integration_id=integration_id,
                )
            except Exception:
                logger.warning(
                    "Could not enqueue Nango retirement for integration=%s",
                    integration_id,
                )
        else:
            await _clear_whatsapp_retirement_state(
                db,
                entity_id=entity_id,
                integration_id=integration_id,
                connection_id=old_connection_id,
            )
            await db.commit()
    return {
        "ok": readiness_code == "ready",
        "retryable": retryable,
        "integration_id": integration_id,
        "readiness_code": readiness_code,
        "detail": detail,
    }


async def _register_integration_channel_webhooks(
    db: AsyncSession,
    *,
    entity_id: str,
    integration_id: str,
) -> list[str]:
    """Register provider webhooks after their source credentials are committed."""
    from packages.core.services.channels import get_adapter

    changed = False
    failures: list[str] = []
    for channel_config in await _channel_configs_for_integration(
        db, entity_id=entity_id, integration_id=integration_id,
    ):
        if channel_config.channel_type == "whatsapp":
            try:
                outcome = await _provision_whatsapp_integration_account(
                    db,
                    entity_id=entity_id,
                    integration_id=integration_id,
                )
            except Exception:
                failures.append(channel_config.id)
                logger.exception(
                    "WhatsApp Business provisioning retry failed for config=%s",
                    channel_config.id,
                )
            else:
                changed = True
                if not outcome["ok"]:
                    failures.append(channel_config.id)
            continue
        adapter = get_adapter(channel_config.channel_type)
        if not adapter:
            continue
        try:
            result = await adapter.register_webhook(channel_config)
            if result.get("registered"):
                changed = True
                if channel_config.channel_type == "whatsapp":
                    await _set_whatsapp_subscription_state(
                        db,
                        integration_id=integration_id,
                        ready=True,
                        detail=(
                            result.get("detail")
                            or "WhatsApp Business webhook subscription confirmed."
                        ),
                    )
                if channel_config.channel_type in {"ms_teams", "outlook"}:
                    pending_key = f"{channel_config.channel_type.replace('ms_teams', 'teams')}_registration_pending"
                    channel_config.config = {
                        **(channel_config.config or {}),
                        pending_key: False,
                    }
                logger.info(
                    "Channel webhook registered for %s config=%s",
                    channel_config.channel_type,
                    channel_config.id,
                )
            elif channel_config.channel_type in {"whatsapp", "ms_teams", "outlook"}:
                if channel_config.channel_type == "whatsapp":
                    await _set_whatsapp_subscription_state(
                        db,
                        integration_id=integration_id,
                        ready=False,
                        detail=(
                            result.get("detail")
                            or "WhatsApp Business webhook subscription is pending."
                        ),
                    )
                pending_key = f"{channel_config.channel_type.replace('ms_teams', 'teams')}_registration_pending"
                if channel_config.channel_type != "whatsapp":
                    channel_config.config = {
                        **(channel_config.config or {}),
                        pending_key: True,
                    }
                changed = True
                failures.append(channel_config.id)
                logger.warning(
                    "Teams webhook registration was not confirmed for config=%s: %s",
                    channel_config.id,
                    result,
                )
        except Exception:
            if channel_config.channel_type in {"whatsapp", "ms_teams", "outlook"}:
                if channel_config.channel_type == "whatsapp":
                    await _set_whatsapp_subscription_state(
                        db,
                        integration_id=integration_id,
                        ready=False,
                        detail="WhatsApp Business webhook subscription is pending.",
                    )
                pending_key = f"{channel_config.channel_type.replace('ms_teams', 'teams')}_registration_pending"
                if channel_config.channel_type != "whatsapp":
                    channel_config.config = {
                        **(channel_config.config or {}),
                        pending_key: True,
                    }
                changed = True
                failures.append(channel_config.id)
            logger.exception(
                "Auto webhook registration failed for %s config=%s",
                channel_config.channel_type,
                channel_config.id,
            )
    if changed:
        # Teams stores its Graph subscription id/clientState on ChannelConfig.
        # Persist that routing state after the provider confirms creation.
        await db.commit()
    return failures


async def _unregister_telegram_webhooks(
    db: AsyncSession,
    *,
    entity_id: str,
    integration_id: str,
    credentials: dict | None = None,
) -> None:
    """Remove Telegram's external webhook using current or supplied credentials."""
    from packages.core.services.channels import get_adapter

    for channel_config in await _channel_configs_for_integration(
        db, entity_id=entity_id, integration_id=integration_id,
    ):
        if channel_config.channel_type != "telegram":
            continue
        adapter = get_adapter(channel_config.channel_type)
        if not adapter:
            continue
        try:
            if credentials is None:
                result = await adapter.unregister_webhook(channel_config)
            else:
                result = await adapter.unregister_webhook(
                    channel_config, credentials=credentials,
                )
        except Exception as exc:
            raise HTTPException(
                502,
                "Could not remove the Telegram webhook; retry before changing this connection.",
            ) from exc
        if not result.get("unregistered"):
            raise HTTPException(
                502,
                "Telegram did not confirm webhook removal; retry before changing this connection.",
            )


async def _unregister_teams_webhooks(
    db: AsyncSession,
    *,
    entity_id: str,
    integration_id: str,
) -> None:
    """Best-effort removal of Graph subscriptions before deleting a bridge."""
    from packages.core.services.channels import get_adapter

    for channel_config in await _channel_configs_for_integration(
        db, entity_id=entity_id, integration_id=integration_id,
    ):
        if channel_config.channel_type != "ms_teams":
            continue
        adapter = get_adapter(channel_config.channel_type)
        if not adapter:
            continue
        try:
            await adapter.unregister_webhook(channel_config)
        except Exception:
            # Deletion must still remove the Manor bridge. A stale Graph
            # subscription is harmless after routing state is gone and can be
            # cleaned up from the provider console if Graph is unavailable.
            logger.warning(
                "Could not remove Teams subscription for config=%s during deletion",
                channel_config.id,
                exc_info=True,
            )


async def _sync_oauth_channel_config_if_needed(
    db: AsyncSession,
    *,
    entity_id: str,
    owner_user_id: str,
    provider: str,
    oauth_account_id: str,
    connection_profile: dict | None = None,
) -> None:
    mappings = _INTEGRATION_TO_CHANNELS.get(provider)
    if not mappings:
        return

    from packages.core.models.channel import ChannelConfig

    routing_config: dict[str, str] = {}
    if provider == "slack":
        profile = connection_profile or {}
        routing_config = {
            target: str(profile[source])
            for target, source in {
                "slack_app_id": "app_id",
                "slack_team_id": "team_id",
                "slack_enterprise_id": "enterprise_id",
                "slack_bot_user_id": "bot_user_id",
            }.items()
            if profile.get(source)
        }
    elif provider == "discord":
        profile = connection_profile or {}
        if not profile.get("application_id") or not profile.get("guild_id"):
            raise ValueError("Discord connection profile is missing Guild identity")
        routing_config = {
            "discord_application_id": str(profile["application_id"]),
            "discord_guild_id": str(profile["guild_id"]),
        }

    for channel_type, channel_provider in mappings:
        existing = (await db.execute(
            select(ChannelConfig).where(
                ChannelConfig.entity_id == entity_id,
                ChannelConfig.owner_user_id == owner_user_id,
                ChannelConfig.channel_type == channel_type,
                ChannelConfig.credential_source_kind == "oauth_account",
                ChannelConfig.credential_source_id == oauth_account_id,
            )
        )).scalar_one_or_none()
        if existing:
            existing.status = "active"
            existing_config = dict(existing.config or {})
            if provider == "slack":
                for key in (
                    "slack_app_id",
                    "slack_team_id",
                    "slack_enterprise_id",
                    "slack_bot_user_id",
                ):
                    existing_config.pop(key, None)
            existing.config = {
                **existing_config,
                "connection_kind": "oauth_account",
                "connection_id": oauth_account_id,
                **routing_config,
            }
            if provider == "slack" and (connection_profile or {}).get("team_name"):
                existing.name = str(connection_profile["team_name"])
            if provider == "discord":
                existing.discord_application_id = routing_config[
                    "discord_application_id"
                ]
                existing.discord_guild_id = routing_config["discord_guild_id"]
                existing.name = str(
                    (connection_profile or {}).get("guild_name") or "Discord"
                )
            continue
        db.add(ChannelConfig(
            entity_id=entity_id,
            owner_user_id=owner_user_id,
            channel_type=channel_type,
            provider=channel_provider,
            name=(connection_profile or {}).get("team_name")
            or (connection_profile or {}).get("guild_name")
            or provider,
            credential_source_kind="oauth_account",
            credential_source_id=oauth_account_id,
            config={
                "connection_kind": "oauth_account",
                "connection_id": oauth_account_id,
                **routing_config,
            },
            credentials={},
            discord_application_id=(
                routing_config.get("discord_application_id")
                if provider == "discord"
                else None
            ),
            discord_guild_id=(
                routing_config.get("discord_guild_id")
                if provider == "discord"
                else None
            ),
            status="active",
        ))
    await db.flush()


async def _sync_channel_config_if_needed(
    db: AsyncSession,
    *,
    entity_id: str,
    owner_user_id: str,
    provider: str,
    integration_id: str,
    telegram_bot_id: str | None = None,
    whatsapp_phone_number_id: str | None = None,
    whatsapp_provisioning_pending: bool = False,
) -> None:
    """Mirror a channel-flavoured Integration into a ChannelConfig so
    inbound routing can bind to it. Credentials remain exclusively on the
    linked Integration. Idempotent per typed source account.
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
                ChannelConfig.owner_user_id == owner_user_id,
                ChannelConfig.channel_type == channel_type,
                ChannelConfig.credential_source_kind == "integration",
                ChannelConfig.credential_source_id == integration_id,
            )
        )).scalar_one_or_none()

        if provider == "telegram" and telegram_bot_id:
            await _assert_telegram_bot_available(
                db, bot_id=telegram_bot_id, integration_id=integration_id,
            )
        if provider == "whatsapp" and whatsapp_phone_number_id:
            await _assert_whatsapp_phone_available(
                db,
                phone_number_id=whatsapp_phone_number_id,
                integration_id=integration_id,
            )
        if existing:
            # Source-linked ChannelConfigs must never maintain a second token
            # copy. Clear legacy values left by the entity-scoped bridge.
            existing.credentials = {}
            existing.credential_ref = None
            existing.status = "active"
            existing_config = dict(existing.config or {})
            existing.config = {
                **existing_config,
                "connection_kind": "integration",
                "connection_id": integration_id,
                "integration_id": integration_id,
            }
            if provider == "telegram":
                existing.telegram_bot_id = telegram_bot_id
            if provider == "whatsapp":
                effective_phone_number_id = (
                    whatsapp_phone_number_id or existing.whatsapp_phone_number_id
                )
                existing.whatsapp_phone_number_id = effective_phone_number_id
                if not effective_phone_number_id:
                    existing.status = "error"
                    existing_config["whatsapp_not_ready_reason"] = (
                        "phone_number_id is unavailable from the connection"
                    )
                else:
                    existing_config.pop("whatsapp_not_ready_reason", None)
                if whatsapp_provisioning_pending:
                    existing.status = "error"
                    existing_config.update({
                        "whatsapp_provisioning_status": "pending",
                        "whatsapp_readiness_code": "provider_unavailable",
                        "whatsapp_registration_pending": True,
                        "whatsapp_last_registration_error": None,
                    })
            if provider == "ms_teams":
                existing_config.setdefault("teams_registration_pending", True)
            existing.config = existing_config
        else:
            channel_config_values = {
                "connection_kind": "integration",
                "connection_id": integration_id,
                "integration_id": integration_id,
            }
            if provider == "ms_teams":
                channel_config_values["teams_registration_pending"] = True
            if provider == "whatsapp" and not whatsapp_phone_number_id:
                channel_config_values["whatsapp_not_ready_reason"] = (
                    "phone_number_id is unavailable from the connection"
                )
            if provider == "whatsapp" and whatsapp_provisioning_pending:
                channel_config_values.update({
                    "whatsapp_provisioning_status": "pending",
                    "whatsapp_readiness_code": "provider_unavailable",
                    "whatsapp_registration_pending": True,
                    "whatsapp_last_registration_error": None,
                })
            db.add(ChannelConfig(
                entity_id=entity_id,
                owner_user_id=owner_user_id,
                channel_type=channel_type,
                provider=channel_provider,
                name=provider,
                credential_source_kind="integration",
                credential_source_id=integration_id,
                config=channel_config_values,
                credentials={},
                telegram_bot_id=telegram_bot_id if provider == "telegram" else None,
                whatsapp_phone_number_id=(
                    whatsapp_phone_number_id if provider == "whatsapp" else None
                ),
                status=(
                    "active"
                    if provider != "whatsapp"
                    or (whatsapp_phone_number_id and not whatsapp_provisioning_pending)
                    else "error"
                ),
            ))
            await db.flush()


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
    from packages.core.models.document import Channel

    await _unregister_telegram_webhooks(
        db, entity_id=entity_id, integration_id=integration_id,
    )
    await _unregister_teams_webhooks(
        db, entity_id=entity_id, integration_id=integration_id,
    )
    cc_rows = await _channel_configs_for_integration(
        db, entity_id=entity_id, integration_id=integration_id,
    )
    cc_ids = [cc.id for cc in cc_rows]
    if cc_ids:
        from packages.core.services.voice.call_sessions import (
            cancel_pending_call_sessions,
        )

        await cancel_pending_call_sessions(
            db,
            channel_config_ids=cc_ids,
            reason="Twilio integration disconnected",
        )

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


def _whatsapp_disconnect_coordinates(integration) -> tuple[str, str, str] | None:
    if canonical_provider_key(integration.provider) != "whatsapp":
        return None
    config = integration.config if isinstance(integration.config, dict) else {}
    nango = config.get("nango") if isinstance(config.get("nango"), dict) else {}
    whatsapp = (
        config.get("whatsapp")
        if isinstance(config.get("whatsapp"), dict)
        else {}
    )
    connection_id = str(nango.get("connection_id") or "").strip()
    provider_config_key = str(
        nango.get("provider_config_key") or "whatsapp"
    ).strip()
    waba_id = str(whatsapp.get("waba_id") or "").strip()
    if not connection_id or not provider_config_key or not waba_id:
        return None
    return provider_config_key, connection_id, waba_id


async def _disconnect_whatsapp_integration_once(
    db: AsyncSession,
    *,
    entity_id: str,
    integration_id: str,
) -> bool:
    """Fail closed, retire delegated access, then remove Manor routing rows."""
    from packages.core.models.channel import ChannelConfig
    from packages.core.models.document import Integration

    integration = (await db.execute(
        select(Integration).where(
            Integration.id == integration_id,
            Integration.entity_id == entity_id,
            Integration.status.in_(("active", "disconnecting")),
        ).with_for_update().execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if integration is None:
        return True
    coordinates = _whatsapp_disconnect_coordinates(integration)
    owner_user_id = str(integration.owner_user_id or "").strip()

    config = dict(integration.config or {})
    previous_state = config.get("whatsapp_disconnect")
    attempt_count = (
        int(previous_state.get("attempt_count") or 0)
        if isinstance(previous_state, dict)
        else 0
    ) + 1
    config["whatsapp_disconnect"] = {
        "status": "disconnecting",
        "attempt_count": attempt_count,
        "app_unsubscribed": False,
        "nango_connection_deleted": False,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    integration.config = config
    integration.status = "disconnecting"
    channel_configs = (await db.execute(
        select(ChannelConfig).where(
            ChannelConfig.entity_id == entity_id,
            ChannelConfig.credential_source_kind == "integration",
            ChannelConfig.credential_source_id == integration_id,
            ChannelConfig.channel_type == "whatsapp",
        ).with_for_update().execution_options(populate_existing=True)
    )).scalars().all()
    for channel_config in channel_configs:
        channel_config.status = "disconnecting"
    await db.commit()

    if coordinates is None or not owner_user_id:
        retained = (await db.execute(
            select(Integration).where(
                Integration.id == integration_id,
                Integration.entity_id == entity_id,
            ).with_for_update().execution_options(populate_existing=True)
        )).scalar_one_or_none()
        if retained is not None:
            retained_config = dict(retained.config or {})
            disconnect_state = dict(
                retained_config.get("whatsapp_disconnect") or {}
            )
            disconnect_state.update({
                "status": "retry_pending",
                "last_error_code": "provider_metadata_incomplete",
                "updated_at": datetime.now(timezone.utc).isoformat(),
            })
            retained_config["whatsapp_disconnect"] = disconnect_state
            retained.config = retained_config
            await db.commit()
        raise WhatsAppDisconnectPending(
            "WhatsApp disconnect metadata is incomplete"
        )
    provider_config_key, connection_id, waba_id = coordinates

    try:
        nango_secret = await get_nango_secret(db, entity_id)
        if not nango_secret:
            raise RuntimeError("Nango is not configured")
        await db.commit()
        await disconnect_whatsapp_business_account(
            nango_secret=nango_secret,
            provider_config_key=provider_config_key,
            connection_id=connection_id,
            waba_id=waba_id,
        )
    except Exception:
        await db.rollback()
        retained = (await db.execute(
            select(Integration).where(
                Integration.id == integration_id,
                Integration.entity_id == entity_id,
            ).with_for_update().execution_options(populate_existing=True)
        )).scalar_one_or_none()
        if retained is not None:
            retained_config = dict(retained.config or {})
            disconnect_state = dict(
                retained_config.get("whatsapp_disconnect") or {}
            )
            disconnect_state.update({
                "status": "retry_pending",
                "last_error_code": "provider_cleanup_failed",
                "updated_at": datetime.now(timezone.utc).isoformat(),
            })
            retained_config["whatsapp_disconnect"] = disconnect_state
            retained.config = retained_config
            retained.status = "disconnecting"
            for channel_config in await _channel_configs_for_integration(
                db,
                entity_id=entity_id,
                integration_id=integration_id,
            ):
                if channel_config.channel_type == "whatsapp":
                    channel_config.status = "disconnecting"
            await db.commit()
        raise WhatsAppDisconnectPending(
            "WhatsApp provider cleanup is pending"
        ) from None

    await lock_runtime_integration_account_scope(
        db,
        kind=IntegrationAccountKind.INTEGRATION,
        user_id=owner_user_id,
        entity_id=entity_id,
        provider="whatsapp",
    )
    retained = (await db.execute(
        select(Integration).where(
            Integration.id == integration_id,
            Integration.entity_id == entity_id,
            Integration.owner_user_id == owner_user_id,
        ).with_for_update().execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if retained is None:
        await db.commit()
        return True
    await _delete_integration_channel_bridges(
        db,
        entity_id=entity_id,
        integration_id=integration_id,
    )
    await db.delete(retained)
    await db.flush()
    await normalize_runtime_integration_account_defaults(
        db,
        kind=IntegrationAccountKind.INTEGRATION,
        user_id=owner_user_id,
        entity_id=entity_id,
        provider="whatsapp",
    )
    await db.commit()
    return True


def _enqueue_whatsapp_disconnect_retry(*, entity_id: str, integration_id: str) -> None:
    try:
        from packages.core.tasks.channel_tasks import (
            disconnect_whatsapp_business_task,
        )

        disconnect_whatsapp_business_task.delay(
            entity_id=entity_id,
            integration_id=integration_id,
        )
    except Exception:
        logger.warning(
            "Could not enqueue WhatsApp disconnect retry for integration=%s",
            integration_id,
        )


async def _delete_oauth_channel_bridges(
    db: AsyncSession,
    *,
    entity_id: str,
    owner_user_id: str,
    oauth_account_id: str,
) -> None:
    """Remove a deleted OAuth account's routing and prior orphan bridges."""
    from packages.core.models.channel import ChannelConfig
    from packages.core.models.document import Channel
    from packages.core.models.user import OAuthAccount

    valid_oauth_account_ids = set((await db.execute(
        select(OAuthAccount.id).where(OAuthAccount.user_id == owner_user_id)
    )).scalars().all())

    cc_rows = (await db.execute(
        select(ChannelConfig).where(
            ChannelConfig.entity_id == entity_id,
            ChannelConfig.owner_user_id == owner_user_id,
            ChannelConfig.credential_source_kind == "oauth_account",
        )
    )).scalars().all()
    cc_rows = [
        row for row in cc_rows
        if row.credential_source_id == oauth_account_id
        or row.credential_source_id not in valid_oauth_account_ids
    ]
    cc_ids = [row.id for row in cc_rows]
    if not cc_ids:
        return

    channel_rows = (await db.execute(
        select(Channel).where(
            Channel.entity_id == entity_id,
            Channel.config["channel_config_id"].astext.in_(cc_ids),
        )
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
    channels = await list_channels(db, user.entity_id, user.id)
    return [_channel_resp(c) for c in channels]


@router.post("/channels", response_model=ChannelResponse, status_code=201)
async def create_new_channel(
    req: CreateChannelRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        channel = await create_channel(
            db, user.entity_id, req.type,
            name=req.name, user_id=user.id,
            workspace_id=req.workspace_id,
            agent_id=req.agent_id, config=req.config,
        )
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    return _channel_resp(channel)


@router.get("/channels/{channel_id}", response_model=ChannelResponse)
async def get_one_channel(
    channel_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    channel = await get_channel(db, channel_id, user.entity_id, user.id)
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
    try:
        channel = await update_channel(
            db, channel_id, user.entity_id, user.id,
            name=req.name, type=req.type, workspace_id=req.workspace_id,
            agent_id=req.agent_id, config=req.config, status=req.status,
        )
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    if not channel:
        raise HTTPException(404, "Channel not found")
    return _channel_resp(channel)


@router.delete("/channels/{channel_id}", status_code=204)
async def delete_one_channel(
    channel_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    ok = await delete_channel(db, channel_id, user.entity_id, user.id)
    if not ok:
        raise HTTPException(404, "Channel not found")


# ── Integration parameterized paths ──

@router.get("/{integration_id}", response_model=IntegrationResponse)
async def get_one_integration(
    integration_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    integration = await get_integration(
        db, integration_id, user.entity_id, user.id,
    )
    if not integration:
        raise HTTPException(404, "Integration not found")
    return _integration_resp(
        integration,
        creator_names=await _creator_names(db, [integration]),
        owner_names=await _owner_names(db, [integration]),
        requester_user_id=user.id,
    )


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
    current = None
    telegram_bot_id = None
    whatsapp_phone_number_id = None
    previous_telegram_credentials: dict | None = None
    expected_credentials: dict | None = None
    if req.provider is not None:
        current = await get_integration(
            db, integration_id, user.entity_id, user.id, action="manage",
        )
        if not current:
            raise HTTPException(404, "Integration not found")
        if canonical_provider_key(req.provider) != current.provider:
            raise HTTPException(
                422,
                "Integration provider cannot be changed; create a new connection instead.",
            )
    if credentials is not None:
        from packages.core.services.integration_service import get_integration as _get_int
        from packages.core.credentials import (
            CredentialError,
            Requester,
            get_credential_service,
        )
        current = await _get_int(
            db, integration_id, user.entity_id, user.id, action="manage",
        )
        if not current:
            raise HTTPException(404, "Integration not found")
        if (
            canonical_provider_key(current.provider) == "whatsapp"
            and isinstance((current.config or {}).get("nango"), dict)
        ):
            raise HTTPException(
                400,
                "WhatsApp Business credentials are managed through OAuth; reconnect the account instead.",
            )
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
        expected_credentials = existing_creds
        for k, v in credentials.items():
            if v == _SECRET_MASK:
                continue  # keep existing
            merged[k] = v
        target_provider = canonical_provider_key(req.provider or current.provider)
        # The Official Account adapter currently accepts plain XML only. A
        # legacy encrypted-mode value must be explicitly removed when an
        # operator edits the connection; the UI no longer offers that field.
        if target_provider == "wechat_official" and (
            "encoding_aes_key" not in credentials
            or credentials.get("encoding_aes_key") == _SECRET_MASK
        ):
            merged.pop("encoding_aes_key", None)
        credentials = _prepare_channel_credentials(target_provider, merged)
        if target_provider == "telegram":
            telegram_bot_id = await _telegram_bot_id(credentials)
            try:
                await _assert_telegram_bot_available(
                    db, bot_id=telegram_bot_id, integration_id=integration_id,
                )
            except TelegramBotAlreadyConnected as exc:
                raise HTTPException(409, str(exc)) from exc
        if target_provider == "whatsapp":
            whatsapp_phone_number_id = (
                str((credentials or {}).get("phone_number_id") or "").strip() or None
            )

        if current.provider == "telegram":
            # Keep the old token only for the post-commit external cleanup.
            # It is never written to ChannelConfig or logged.
            previous_telegram_credentials = existing_creds

    try:
        integration = await update_integration(
            db, integration_id, user.entity_id, user.id,
            provider=req.provider, status=req.status,
            config=req.config, credentials=credentials,
            expected_credentials=expected_credentials,
        )
    except IntegrationCredentialConflictError as exc:
        await db.rollback()
        raise HTTPException(
            409,
            "Integration credentials changed; reload and retry the update.",
        ) from exc
    except IntegrationProviderImmutableError as exc:
        raise HTTPException(422, str(exc)) from exc
    except CredentialError as exc:
        await _raise_credential_backend_unavailable(db, exc, action="update_integration")
    if not integration:
        raise HTTPException(404, "Integration not found")

    # Keep ChannelConfig bridge rows in sync when credentials rotate.
    if credentials is not None:
        try:
            await _sync_channel_config_if_needed(
                db,
                entity_id=user.entity_id,
                owner_user_id=user.id,
                provider=integration.provider,
                integration_id=integration.id,
                telegram_bot_id=telegram_bot_id,
                whatsapp_phone_number_id=whatsapp_phone_number_id,
            )
            # The adapter resolves credentials through a separate short-lived
            # session, so commit the new source before registration.
            await db.commit()
        except TelegramBotAlreadyConnected as exc:
            raise HTTPException(409, str(exc)) from exc
        except WhatsAppPhoneAlreadyConnected as exc:
            raise HTTPException(409, str(exc)) from exc

        # setWebhook replaces a webhook for the same bot, so cleanup is only
        # needed when the bot token itself changed. Do it after persistence so
        # a Vault/DB failure cannot strand the old bot without a source token.
        if (
            previous_telegram_credentials
            and previous_telegram_credentials.get("bot_token") != credentials.get("bot_token")
        ):
            try:
                await _unregister_telegram_webhooks(
                    db,
                    entity_id=user.entity_id,
                    integration_id=integration.id,
                    credentials=previous_telegram_credentials,
                )
            except HTTPException:
                logger.exception(
                    "Could not remove old Telegram webhook after updating integration %s",
                    integration.id,
                )

        await _register_integration_channel_webhooks(
            db, entity_id=user.entity_id, integration_id=integration.id,
        )

    # Refresh the health signal now that creds changed.
    try:
        from packages.core.tasks.channel_tasks import health_check_task
        health_check_task.delay(integration_id=integration.id)
    except Exception:
        logger.debug("Could not enqueue health check (Celery unreachable)", exc_info=True)

    return _integration_resp(
        integration,
        creator_names=await _creator_names(db, [integration]),
        owner_names=await _owner_names(db, [integration]),
        requester_user_id=user.id,
    )


@router.delete("/{integration_id}", status_code=204)
async def delete_one_integration(
    integration_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    integration = await get_integration(
        db, integration_id, user.entity_id, user.id, action="manage",
    )
    if not integration:
        raise HTTPException(404, "Integration not found")
    if canonical_provider_key(integration.provider) == "whatsapp":
        try:
            await _disconnect_whatsapp_integration_once(
                db,
                entity_id=user.entity_id,
                integration_id=integration_id,
            )
        except WhatsAppDisconnectPending:
            _enqueue_whatsapp_disconnect_retry(
                entity_id=user.entity_id,
                integration_id=integration_id,
            )
        return
    await _delete_integration_channel_bridges(
        db,
        entity_id=user.entity_id,
        integration_id=integration_id,
    )
    removed = await delete_integration(
        db,
        integration_id,
        user.entity_id,
        user.id,
    )
    if not removed:
        raise HTTPException(404, "Integration not found")
    await db.commit()


@router.get("/{integration_id}/channels", response_model=list[ChannelResponse])
async def list_integration_channels(
    integration_id: str,
    workspace_id: str | None = Query(None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    # Verify the integration belongs to this entity
    integration = await get_integration(
        db, integration_id, user.entity_id, user.id, action="manage",
    )
    if not integration:
        raise HTTPException(404, "Integration not found")
    channels = await list_owned_integration_channels(
        db,
        user.entity_id,
        user.id,
        integration_id,
        workspace_id=workspace_id,
    )
    return [_channel_resp(c) for c in channels]
