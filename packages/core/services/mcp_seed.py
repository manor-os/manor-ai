"""Idempotent MCP server catalog seeder.

Guarantees the 16 built-in MCP servers exist in the ``mcp_servers`` table
whenever ``scripts/init_db.py`` runs or the API boots fresh. The row set
must match the catalog the UI renders; empty table → empty "For Agents"
tab on the Integrations page.

Base inserts use ``ON CONFLICT DO NOTHING``; selected shipped catalog rows are
refreshed explicitly so existing deployments receive auth-contract and managed
remote-endpoint changes. The seeder remains safe to call repeatedly.
"""
from __future__ import annotations

import logging

from sqlalchemy.engine import Engine
from sqlalchemy import text

from packages.core.integrations.registry import register_integration
from packages.core.services.official_remote_mcp import (
    OfficialRemoteMCPFactory,
    OfficialRemoteMCPProvider,
)

logger = logging.getLogger(__name__)


_MCP_CATALOG: list[tuple[str, str, str, str, str, str, str | None]] = [
    # (server_key, name, description, transport, endpoint, auth_type, scopes)
    ("gmail", "Gmail",
     "Send, read and manage Gmail messages.",
     "builtin", "packages.core.ai.mcp.gmail", "oauth2",
     "https://www.googleapis.com/auth/gmail.modify"),
    ("email", "Email (IMAP + SMTP)",
     "Read, write, send, and organize email on any IMAP/SMTP account.",
     "builtin", "packages.core.ai.mcp.email", "credentials", None),
    ("google_calendar", "Google Calendar",
     "Read and manage calendar events.",
     "builtin", "packages.core.ai.mcp.google_calendar", "oauth2",
     "https://www.googleapis.com/auth/calendar.events,"
     "https://www.googleapis.com/auth/calendar.calendarlist.readonly,"
     "https://www.googleapis.com/auth/calendar.events.freebusy"),
    ("manor_mcp_calendar", "Manor Calendar",
     "Manage Manor calendar settings, booking links, working hours, "
     "daily agenda, and booking records.",
     "builtin", "packages.core.ai.mcp.manor_mcp_calendar", "none", None),

    ("manor_mcp_minutes", "Manor Minutes",
     "Meeting notes from Minutes: search meetings, read transcripts, "
     "summaries, key points, and action items; ask questions about a meeting.",
     "builtin", "packages.core.ai.mcp.manor_mcp_minutes", "none", None),
    ("google_drive", "Google Drive",
     "Search and read user-requested Drive files; create and manage files "
     "that Manor creates or the user explicitly opens or shares with Manor.",
     "builtin", "packages.core.ai.mcp.google_drive", "oauth2",
     "https://www.googleapis.com/auth/drive.file,"
     "https://www.googleapis.com/auth/drive.readonly"),
    ("linkedin", "LinkedIn (Posting & Analytics)",
     "✅ Compliant official LinkedIn API for posting and analytics. "
     "Post to LinkedIn (text, images, video, documents, carousels), "
     "manage your own posts and comments, publish on company pages "
     "you admin, and see engagement stats on your posts. "
     "People/company search, third-party profile reads, jobs, DMs, "
     "and feed browsing are not available through LinkedIn's official API.",
     "builtin", "packages.core.ai.mcp.linkedin", "oauth2",
     "w_member_social,openid,profile,email"),
    ("github", "GitHub",
     "GitHub repos, issues, pull requests.",
     "builtin", "packages.core.ai.mcp.github", "oauth2",
     "repo,read:org"),
    ("twitter_x", "Twitter / X",
     "X API v2 — tweets, timeline, users, likes, follows.",
     "builtin", "packages.core.ai.mcp.twitter_x", "oauth2",
     "tweet.read,tweet.write,users.read,like.read,like.write,"
     "follows.read,follows.write,offline.access"),
    ("quickbooks", "QuickBooks",
     "QuickBooks Online — accounts, invoices, customers.",
     "builtin", "packages.core.ai.mcp.quickbooks", "oauth2",
     "com.intuit.quickbooks.accounting"),
    # Stripe — uses the vendor-hosted MCP at mcp.stripe.com via OAuth.
    # The legacy in-process api_key wrapper (packages/core/ai/mcp/stripe.py)
    # is no longer surfaced in the catalog — kept on disk so any
    # existing Integration rows continue to import without crashing,
    # but new connects always go through OAuth.
    (OfficialRemoteMCPProvider.STRIPE.value, "Stripe",
     "Manage your Stripe payments, customers, subscriptions, invoices, "
     "and disputes — agents can run charges, issue refunds, update "
     "billing, and pull revenue reports.",
     "http", OfficialRemoteMCPFactory.create(OfficialRemoteMCPProvider.STRIPE).endpoint,
     "oauth2", "read_write"),
    # PayPal remote MCP — only path Manor offers for PayPal (no
    # in-process wrapper). Sandbox vs live OAuth and MCP endpoints are
    # selected from PAYPAL_ENVIRONMENT by OfficialRemoteMCPFactory.
    (OfficialRemoteMCPProvider.PAYPAL.value, "PayPal",
     "Manage PayPal orders, invoices, subscriptions, disputes, and "
     "transactions — let agents create invoices, refund payments, and "
     "pull payout reports.",
     "http", OfficialRemoteMCPFactory.create(OfficialRemoteMCPProvider.PAYPAL).endpoint,
     "oauth2", OfficialRemoteMCPFactory.paypal_oauth().scopes),
    (OfficialRemoteMCPProvider.ROBINHOOD.value, "Robinhood",
     "Connect your own Robinhood accounts through the official Trading MCP. "
     "Read portfolios, watchlists and market data; account consent may also "
     "permit Agentic trading. External writes follow Manor approval policy.",
     "http", OfficialRemoteMCPFactory.create(OfficialRemoteMCPProvider.ROBINHOOD).endpoint,
     "oauth2", "internal"),
    ("slack", "Slack",
     "Send messages to channels, react, post to threads.",
     "builtin", "packages.core.ai.mcp.slack", "oauth2",
     "chat:write,channels:read,channels:history,users:read,"
     "app_mentions:read,im:history"),
    ("notion", "Notion",
     "Read/write Notion pages and databases.",
     "builtin", "packages.core.ai.mcp.notion", "oauth2",
     "read_content,update_content,insert_content"),
    ("twilio", "Twilio",
     "SMS and voice via Twilio.",
     "builtin", "packages.core.ai.mcp.twilio", "api_key", None),
    ("whatsapp", "WhatsApp Business",
     "Send WhatsApp Cloud messages and manage Meta message templates.",
     "builtin", "packages.core.ai.mcp.whatsapp", "api_key", None),
    ("webhook", "Webhook",
     "Generic outbound HTTP webhook calls.",
     "builtin", "packages.core.ai.mcp.webhook", "bearer", None),
    ("discord", "Discord",
     "Inspect the connected Discord Server, list its channels, send messages, "
     "and add reactions within that Server.",
     "builtin", "packages.core.ai.mcp.discord", "oauth2",
     "bot,applications.commands"),
    ("telegram", "Telegram",
     "Send messages, photos, and files via Telegram Bot API.",
     "builtin", "packages.core.ai.mcp.telegram", "api_key", None),
    ("wechat_personal", "WeChat (Personal)",
     "Personal WeChat account bot — group and 1:1 messages via a QR-login bot runner.",
     "builtin", "packages.core.ai.mcp.wechat_personal", "credentials", None),
    ("wechat_official", "WeChat Official Account",
     "Official Account (公众号) API — customer service messages, templates, and menu.",
     "builtin", "packages.core.ai.mcp.wechat_official", "credentials", None),
    ("nango", "Nango (200+ apps)",
     "Connect to 200+ SaaS apps in one place — HubSpot, Linear, "
     "Salesforce, Airtable, Intercom, and many more. Once your admin "
     "sets up Nango, each connected app appears as its own card here.",
     "builtin", "packages.core.ai.mcp.nango", "api_key", None),

    # ── AI platforms — CLI Worker auth ──
    # (LLM Chat APIs — OpenAI / Anthropic / Doubao / Kimi / Qwen /
    # Deepseek — were intentionally NOT added here. The Account page's
    # model picker is the single source of truth for "which LLM
    # Manor uses internally"; treating them as MCP integrations would
    # double-up the same API-key configuration. Multi-model agent
    # workflows that genuinely need a SECOND LLM as a callable tool
    # should be handled at the agent-level tool binding layer instead.
    # The packages/core/ai/mcp/openai_api.py wrapper module is kept
    # around for that future use, just not seeded as a /integrations
    # card by default.)


    # ── AI generation platforms — clean HTTP API providers ──
    # All standard api_key auth: user pastes a token in /integrations
    # and agents call mcp__<key>__* tools.
    ("replicate", "Replicate",
     "Run hundreds of open-source AI models — image (Flux, Stable "
     "Diffusion, Ideogram), video (Luma, LTX, MiniMax), audio "
     "(Whisper, MusicGen) — through one API and one billing line.",
     "builtin", "packages.core.ai.mcp.replicate", "api_key", None),
    ("elevenlabs", "ElevenLabs",
     "High-quality TTS + voice cloning. Multilingual; emits MP3 to "
     "Manor's filesystem so subsequent steps can attach or stream it.",
     "builtin", "packages.core.ai.mcp.elevenlabs", "api_key", None),
    ("tavily", "Tavily",
     "AI-tuned web search + content extraction. Returns synthesized "
     "snippets, optional inline article body, and a 1-sentence answer. "
     "Free tier: 1000 calls/month — enough for most agent workloads.",
     "builtin", "packages.core.ai.mcp.tavily", "api_key", None),
    ("alpaca_market_data", "Alpaca Market Data",
     "Read-only US stock quotes, trades, snapshots, bars, and news. "
     "This integration cannot access brokerage accounts or place orders.",
     "builtin", "packages.core.ai.mcp.alpaca_market_data", "credentials", None),
    ("alpha_vantage", "Alpha Vantage",
     "Read-only global equity quotes, daily history, company overviews, "
     "and reported fundamentals.",
     "builtin", "packages.core.ai.mcp.alpha_vantage", "api_key", None),
    ("twelve_data", "Twelve Data",
     "Read-only quotes, prices, historical OHLCV, and technical indicators "
     "for stocks, ETFs, forex, and crypto.",
     "builtin", "packages.core.ai.mcp.twelve_data", "api_key", None),

    # Jimeng has a reverse-engineered HTTP gateway (iptag/jimeng-api
    # sidecar; --profile jimeng) so we treat it as a normal api_key
    # provider — the user pastes their sessionid cookie as the "key".
    ("jimeng", "即梦 (Jimeng)",
     "ByteDance Jimeng — image and short-video generation. Drives a "
     "self-hosted reverse-engineered HTTP gateway (iptag/jimeng-api "
     "sidecar) so each call is a clean OpenAI-compatible POST. User "
     "pastes their jimeng.jianying.com ``sessionid`` cookie as the "
     "API key.",
     "builtin", "packages.core.ai.mcp.jimeng", "api_key", None),
    # ── Launch / community platforms (OAuth) ──
    ("producthunt", "Product Hunt",
     "Product Hunt v2 GraphQL API — search posts, fetch details, list "
     "and post comments. Critical for launch-day workflows: monitor "
     "your post's stats and reply to commenters in real time.",
     "builtin", "packages.core.ai.mcp.producthunt", "oauth2",
     "public,private"),

    # ── Social platforms (provider OAuth) ──
    ("facebook", "Facebook (Pages + Messenger)",
     "Let agents post to your Facebook Pages (text, photos, video, "
     "scheduled), reply to comments, hide spam, pull reach + engagement "
     "stats, and handle Messenger DMs on your behalf.",
     "builtin", "packages.core.ai.mcp.facebook", "oauth2",
     "public_profile,pages_show_list,pages_read_engagement,pages_manage_posts,"
     "pages_manage_engagement,pages_messaging,pages_manage_metadata,"
     "read_insights,instagram_basic,instagram_content_publish,"
     "instagram_manage_comments,instagram_manage_insights"),

    # ── Video platforms (official API + OAuth) ──
    # Instagram Reels publishing is part of the `facebook` module above
    # (Meta Graph API: create_instagram_media media_type=REELS).
    ("youtube", "YouTube",
     "YouTube Data API v3 — search videos/channels, read video & channel "
     "stats, list comments and captions, plus publish: post/reply/delete "
     "comments, like/dislike, edit your video's title/description/tags, "
     "manage playlists, and upload new videos from a public HTTPS URL.",
     "builtin", "packages.core.ai.mcp.youtube", "oauth2",
     "https://www.googleapis.com/auth/youtube.force-ssl"),
    ("tiktok", "TikTok",
     "TikTok API v2 — read your profile and videos (Display API) and "
     "publish video / photo posts from a hosted URL with status tracking "
     "(Content Posting API).",
     "builtin", "packages.core.ai.mcp.tiktok", "oauth2",
     "user.info.basic,user.info.profile,user.info.stats,"
     "video.list,video.publish,video.upload"),

    # ── E-commerce platforms (credentials: store domain + API token /
    # consumer key+secret, stored per-entity). Read + write: products,
    # orders, customers, inventory. ──
    ("shopify", "Shopify",
     "Shopify Admin GraphQL API — read products, orders and customers, "
     "and write: create/update products, tag orders, create customers, "
     "and adjust inventory. Credentials: shop_domain + Admin API "
     "access_token.",
     "builtin", "packages.core.ai.mcp.shopify", "credentials", None),
    ("woocommerce", "WooCommerce",
     "WooCommerce REST API — read and write products, orders and "
     "customers, update order status, and manage stock. Credentials: "
     "site_url + consumer_key + consumer_secret.",
     "builtin", "packages.core.ai.mcp.woocommerce", "credentials", None),
    ("square", "Square",
     "Square API — read locations, catalog, orders and customers, and "
     "write: create catalog items, create/update customers, and adjust "
     "inventory. Credentials: access_token (+ optional location_id, "
     "environment).",
     "builtin", "packages.core.ai.mcp.square", "credentials", None),

    # ── Marketplace seller APIs (signed / token-exchange auth) ──
    ("tiktok_shop", "TikTok Shop",
     "TikTok Shop Partner API — list authorized shops, search orders and "
     "products, and write: update SKU price and inventory. Each request is "
     "HMAC-SHA256 signed. Credentials: app_key + app_secret + access_token "
     "(+ shop_cipher). Separate from the consumer 'TikTok' content API.",
     "builtin", "packages.core.ai.mcp.tiktok_shop", "credentials", None),
    ("amazon", "Amazon (Selling Partner)",
     "Amazon SP-API — read orders, catalog, FBA inventory and your "
     "listings, and write listings (patch price/qty or full upsert). "
     "Credentials: LWA refresh_token + client id/secret + region + "
     "marketplace_id (+ seller_id).",
     "builtin", "packages.core.ai.mcp.amazon", "credentials", None),


    # ── Microsoft 365 (Graph API, single shared OAuth app) ──
    # All 5 share one Azure AD App Registration — a single consent
    # screen requests the union of scopes; users get one connect flow.
    # Set MS_TENANT=common (default) for multi-tenant SaaS, or pin to
    # a specific tenant GUID for enterprise installs.
    ("outlook", "Outlook Mail",
     "Read, send, and organize Outlook mail. Reply, forward, manage "
     "drafts and folders, flag and categorize messages, handle "
     "attachments — the Outlook counterpart to Gmail.",
     "builtin", "packages.core.ai.mcp.outlook", "oauth2",
     "Mail.Read Mail.ReadWrite Mail.Send User.Read offline_access"),
    ("onedrive", "OneDrive",
     "Read, upload, share, copy, and organize files in your OneDrive. "
     "Manage permissions and file versions — the Microsoft counterpart "
     "to Google Drive.",
     "builtin", "packages.core.ai.mcp.onedrive", "oauth2",
     "Files.ReadWrite Files.ReadWrite.All User.Read offline_access"),
    ("ms_calendar", "Microsoft Calendar",
     "Read your calendar, create and update events, RSVP to invites, "
     "expand recurring meetings, and find meeting times that work for "
     "everyone (free/busy across multiple calendars).",
     "builtin", "packages.core.ai.mcp.ms_calendar", "oauth2",
     "Calendars.ReadWrite MailboxSettings.Read User.Read offline_access"),
    ("ms_teams", "Microsoft Teams",
     "Read and post messages in Teams channels, send 1:1 and group "
     "DMs, and create Teams meeting links. (Recording and transcript "
     "access requires admin permissions and isn't included.)",
     "builtin", "packages.core.ai.mcp.ms_teams", "oauth2",
     "Team.ReadBasic.All Channel.ReadBasic.All ChannelMessage.Read.All ChannelMessage.Send Chat.ReadWrite OnlineMeetings.ReadWrite Presence.ReadWrite User.Read offline_access"),
    ("ms_excel", "Microsoft Excel (workbooks)",
     "Read and update .xlsx workbooks stored in OneDrive — list and "
     "read worksheets, append rows to a table, write to a cell range, "
     "and recalculate formulas.",
     "builtin", "packages.core.ai.mcp.ms_excel", "oauth2",
     "Files.ReadWrite Files.ReadWrite.All User.Read offline_access"),
]

_MANAGED_MCP_SERVER_KEYS = frozenset(row[0] for row in _MCP_CATALOG)


def is_managed_mcp_server_key(server_key: object) -> bool:
    """Return whether ``server_key`` belongs to Manor's managed catalog."""
    return str(server_key or "").strip().lower() in _MANAGED_MCP_SERVER_KEYS

# The MCP catalog is the built-in Integration identity catalog. Register every
# server key even when it has no dedicated health checker or Stat measurer so
# callers can distinguish "known but unsupported" from a truly unknown key.
for _catalog_row in _MCP_CATALOG:
    register_integration(_catalog_row[0])


# 1-to-1 spec rows for the new auth_types. Keyed by server_key so the
# seeder can attach them after the base mcp_servers row is upserted.

_BROWSER_SPECS: dict[str, dict] = {}


async def seed_mcp_catalog(engine: Engine) -> int:
    """Upsert the built-in MCP servers. Returns number of rows inserted."""
    inserted = 0
    async with engine.begin() as conn:
        # Startup/test suites can invoke this seeder concurrently. The cleanup
        # statements below touch multiple catalog tables, so serialize the whole
        # transaction to avoid Postgres deadlocks during parallel boot.
        await conn.execute(text(
            "SELECT pg_advisory_xact_lock(hashtext('manor.mcp_seed_catalog'), 0)"
        ))

        # File operations use native tools; retire the duplicate internal MCP.
        await conn.execute(text(
            "DELETE FROM mcp_servers WHERE server_key = 'manor_mcp_file_engine'"
        ))

        # The old generic local-browser MCP was superseded by the explicit
        # Chrome MCP. Remove the catalog row and any stale private bindings;
        # no runtime alias remains after this migration.
        await conn.execute(text(
            "DELETE FROM agent_mcp_bindings WHERE mcp_server_id IN ("
            "SELECT id FROM mcp_servers WHERE server_key = 'local_browser')"
        ))
        await conn.execute(text(
            "DELETE FROM mcp_account_tool_catalogs WHERE provider = 'local_browser'"
        ))
        await conn.execute(text(
            "DELETE FROM integrations WHERE provider = 'local_browser'"
        ))
        await conn.execute(text(
            "DELETE FROM oauth_accounts WHERE provider = 'local_browser'"
        ))
        await conn.execute(text(
            "DELETE FROM mcp_servers WHERE server_key = 'local_browser'"
        ))

        # One-time migration: the SMTP-only provider was merged into a
        # combined IMAP+SMTP provider keyed ``email``. Drop any stale row
        # and re-point existing Integration rows to the new provider key.
        await conn.execute(text(
            "UPDATE integrations SET provider = 'email' WHERE provider = 'email_smtp'"
        ))
        await conn.execute(text(
            "DELETE FROM mcp_servers WHERE server_key = 'email_smtp'"
        ))

        # One-time migration: the single ``wechat`` provider was split
        # into two separate providers — ``wechat_personal`` (QR bot) and
        # ``wechat_official`` (公众号 API). Existing Integration rows
        # can't be auto-classified; leave them with provider='wechat' so
        # admins can see + reconfigure. Drop the stale catalog row.
        await conn.execute(text(
            "DELETE FROM mcp_servers WHERE server_key = 'wechat'"
        ))
        await conn.execute(text(
            "DELETE FROM browser_tool_specs "
            "WHERE mcp_server_id IN ("
            "SELECT id FROM mcp_servers WHERE server_key IN "
            "('midjourney_web', 'notebooklm', 'claude_ai_web', "
            "'chatgpt_web', 'gemini_web', 'perplexity_web', "
            "'linkedin_browser'))"
        ))
        await conn.execute(text(
            "DELETE FROM mcp_servers WHERE server_key IN "
            "('midjourney_web', 'notebooklm', 'claude_ai_web', "
            "'chatgpt_web', 'gemini_web', 'perplexity_web', "
            "'linkedin_browser')"
        ))

        # One-time migration: LLM chat APIs (OpenAI / Anthropic /
        # Doubao / Kimi / Qwen / Deepseek) were briefly seeded as MCP
        # cards then pulled. Account page's model picker is the
        # canonical home for that. Clean up if the rows are still here
        # from a prior boot.
        await conn.execute(text(
            "DELETE FROM mcp_servers WHERE server_key IN "
            "('openai', 'anthropic', 'doubao', 'deepseek', 'kimi', 'qwen')"
        ))

        # One-time migration: jimeng was moved from legacy GUI auth
        # to api_key auth (HTTP gateway sidecar). Re-point its
        # row + drop the obsolete browser spec.
        await conn.execute(text(
            "UPDATE mcp_servers "
            "SET endpoint = 'packages.core.ai.mcp.jimeng', "
            "    auth_type = 'api_key' "
            "WHERE server_key = 'jimeng'"
        ))
        await conn.execute(text(
            "DELETE FROM browser_tool_specs "
            "WHERE mcp_server_id IN "
            "(SELECT id FROM mcp_servers WHERE server_key = 'jimeng')"
        ))
        # Stripe consolidation: drop the retired interim row. Then refresh
        # both vendor-hosted MCP rows so existing installs receive endpoint
        # and environment changes despite the INSERT ... DO NOTHING below.
        await conn.execute(text(
            "DELETE FROM mcp_servers WHERE server_key = 'stripe_mcp'"
        ))
        refreshed_catalog_keys = {
            *(provider.value for provider in OfficialRemoteMCPProvider),
            # Keep the user-visible Google catalog aligned with the exact
            # reviewed static manifest. Historical installs were initially
            # seeded with redundant/full scopes, so INSERT ... DO NOTHING is
            # insufficient for those existing rows.
            "gmail",
            "google_calendar",
            "google_drive",
            "youtube",
            "whatsapp",
        }
        for key, name, desc, transport, endpoint, auth_type, scopes in _MCP_CATALOG:
            if key not in refreshed_catalog_keys:
                continue
            await conn.execute(
                text("""
                    UPDATE mcp_servers
                    SET name = :name,
                        description = :description,
                        transport = :transport,
                        endpoint = :endpoint,
                        auth_type = :auth_type,
                        scopes = :scopes,
                        updated_at = now()
                    WHERE server_key = :server_key
                """),
                {
                    "server_key": key,
                    "name": name,
                    "description": desc,
                    "transport": transport,
                    "endpoint": endpoint,
                    "auth_type": auth_type,
                    "scopes": scopes,
                },
            )

        cli_spec_count = 0

        for (key, name, desc, transport, endpoint, auth_type, scopes) in _MCP_CATALOG:
            result = await conn.execute(
                text("""
                    INSERT INTO mcp_servers
                      (id, server_key, name, description, transport, endpoint,
                       auth_type, scopes, status, created_at)
                    VALUES
                      (:id, :server_key, :name, :description, :transport, :endpoint,
                       :auth_type, :scopes, 'active', now())
                    ON CONFLICT (server_key) DO NOTHING
                """),
                {
                    "id": f"mcp_{key}".ljust(26, "_")[:26],
                    "server_key": key,
                    "name": name,
                    "description": desc,
                    "transport": transport,
                    "endpoint": endpoint,
                    "auth_type": auth_type,
                    "scopes": scopes,
                },
            )
            if result.rowcount and result.rowcount > 0:
                inserted += 1

        # Workflow/Planner surfaces read ``tools_cached`` rather than the Chat
        # ToolPool. Seed a credential-free fallback for official providers, but
        # never overwrite a non-empty catalog discovered from the live vendor.
        for provider in OfficialRemoteMCPProvider:
            tools_cached = _json(OfficialRemoteMCPFactory.tools_cache(provider))
            await conn.execute(
                text("""
                    UPDATE mcp_servers
                    SET tools_cached = CASE
                            WHEN tools_cached IS NULL
                              OR tools_cached IN ('{}'::jsonb, '[]'::jsonb)
                              OR tools_cached->>'source' = 'official_fallback'
                            THEN CAST(:tools_cached AS jsonb)
                            ELSE tools_cached
                        END,
                        tools_cached_at = CASE
                            WHEN tools_cached IS NULL
                              OR tools_cached IN ('{}'::jsonb, '[]'::jsonb)
                              OR tools_cached->>'source' = 'official_fallback'
                            THEN NULL
                            ELSE tools_cached_at
                        END,
                        updated_at = now()
                    WHERE server_key = :server_key
                """),
                {
                    "server_key": provider.value,
                    "tools_cached": tools_cached,
                },
            )

        discord_row = next(row for row in _MCP_CATALOG if row[0] == "discord")
        await conn.execute(
            text("""
                UPDATE mcp_servers
                SET name = :name,
                    description = :description,
                    transport = :transport,
                    endpoint = :endpoint,
                    auth_type = :auth_type,
                    scopes = :scopes,
                    updated_at = now()
                WHERE server_key = 'discord'
            """),
            {
                "name": discord_row[1],
                "description": discord_row[2],
                "transport": discord_row[3],
                "endpoint": discord_row[4],
                "auth_type": discord_row[5],
                "scopes": discord_row[6],
            },
        )

        await conn.execute(text("""
            UPDATE mcp_servers
            SET scopes = 'tweet.read,tweet.write,users.read,like.read,like.write,follows.read,follows.write,offline.access',
                description = 'X API v2 — tweets, timeline, users, likes, follows.',
                updated_at = now()
            WHERE server_key = 'twitter_x'
              AND (
                scopes IS DISTINCT FROM 'tweet.read,tweet.write,users.read,like.read,like.write,follows.read,follows.write,offline.access'
                OR description IS DISTINCT FROM 'X API v2 — tweets, timeline, users, likes, follows.'
              )
        """))

        # Side-table specs for cli_worker auth types.
        # ON CONFLICT on the unique mcp_server_id keeps re-runs idempotent.

        for key, spec in _BROWSER_SPECS.items():
            await conn.execute(
                text("""
                    INSERT INTO browser_tool_specs
                      (id, mcp_server_id, login_url, session_check_selector,
                       provider_module, tool_actions, cookie_ttl_days, created_at)
                    SELECT
                      :id, m.id, :login_url, :session_check_selector,
                      :provider_module, CAST(:tool_actions AS jsonb),
                      :cookie_ttl_days, now()
                    FROM mcp_servers m
                    WHERE m.server_key = :server_key
                    ON CONFLICT (mcp_server_id) DO NOTHING
                """),
                {
                    "id": f"brw_{key}".ljust(26, "_")[:26],
                    "server_key": key,
                    "login_url": spec["login_url"],
                    "session_check_selector": spec.get("session_check_selector"),
                    "provider_module": spec["provider_module"],
                    "tool_actions": _json(spec["tool_actions"]),
                    "cookie_ttl_days": spec["cookie_ttl_days"],
                },
            )

    logger.info(
        "MCP catalog seeded: %d new rows, %d total in catalog definition "
        "(+ %d CLI specs, %d browser specs)",
        inserted, len(_MCP_CATALOG), cli_spec_count, len(_BROWSER_SPECS),
    )
    return inserted


def _json(v) -> str:
    """tiny helper — Postgres JSONB cast wants a JSON string."""
    import json as _j
    return _j.dumps(v)
