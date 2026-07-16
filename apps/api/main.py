"""
Manor AI — FastAPI application factory.

Single backend serving:
  - REST API for all business operations
  - SSE streaming for chat (direct, no proxy)
  - WebSocket for real-time updates
  - Static file serving for JuiceFS content

Cloud features loaded as plugins via DEPLOYMENT_MODE env var.
"""
from __future__ import annotations

import asyncio
import os
import logging
import re
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from apps.api.middleware import setup_middleware

# ── Logging setup ────────────────────────────────────────────────────────
# Configure root logger so all manor.* loggers emit to stderr (Docker logs).
_SENSITIVE_QUERY_RE = re.compile(
    r"([?&](?:token|access_token|refresh_token|id_token|api_key|client_secret|password|code)=)"
    r"[^&\s\"']+",
    re.IGNORECASE,
)


def redact_sensitive_log_text(value: str) -> str:
    """Remove query-string credentials before log records reach handlers."""
    return _SENSITIVE_QUERY_RE.sub(r"\1<redacted>", value)


class SensitiveQueryStringFilter(logging.Filter):
    """Redact credentials in uvicorn/FastAPI access-log messages."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact_sensitive_log_text(record.msg)

        if isinstance(record.args, tuple):
            record.args = tuple(
                redact_sensitive_log_text(arg) if isinstance(arg, str) else arg
                for arg in record.args
            )
        elif isinstance(record.args, dict):
            record.args = {
                key: redact_sensitive_log_text(arg) if isinstance(arg, str) else arg
                for key, arg in record.args.items()
            }
        return True


def install_sensitive_log_filter() -> None:
    sensitive_filter = SensitiveQueryStringFilter()
    for logger_name in ("uvicorn.access", "uvicorn.error", "apps.api.middleware_core"):
        target = logging.getLogger(logger_name)
        if not any(isinstance(existing, SensitiveQueryStringFilter) for existing in target.filters):
            target.addFilter(sensitive_filter)


_log_level = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, _log_level, logging.INFO),
    format="%(asctime)s %(levelname)-5s [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stderr,
    force=True,  # override any prior basicConfig from library imports
)
install_sensitive_log_filter()
# Quiet noisy third-party loggers
for _quiet in ("httpx", "httpcore", "hpack", "urllib3", "sqlalchemy.engine"):
    logging.getLogger(_quiet).setLevel(logging.WARNING)

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown hooks."""
    logger.info("Manor AI starting — mode=%s", os.getenv("DEPLOYMENT_MODE", "oss"))

    # Refuse to run with the built-in JWT signing key: it is published in the
    # source, so anyone could forge tokens for any user. Hard-fail in cloud;
    # warn loudly in self-hosted/OSS mode (local dev may not set it yet).
    from packages.core.config import INSECURE_DEFAULT_JWT_SECRET, get_settings
    _settings = get_settings()
    if _settings.JWT_SECRET_KEY == INSECURE_DEFAULT_JWT_SECRET:
        if _settings.DEPLOYMENT_MODE.strip().lower() == "cloud":
            raise RuntimeError(
                "JWT_SECRET_KEY is the insecure built-in default; refusing to "
                "start in cloud mode. Set JWT_SECRET_KEY to a strong random value."
            )
        logger.warning(
            "SECURITY: JWT_SECRET_KEY is the built-in default — tokens are "
            "forgeable. Set JWT_SECRET_KEY to a strong random value before "
            "exposing this instance."
        )

    startup_tasks: list[asyncio.Task] = []

    # OTEL tracing (no-op when OTEL_ENABLED is not set).
    try:
        from packages.core.observability import init_tracing
        init_tracing(service_name="manor-api")
    except Exception as e:
        logger.warning("OTEL init skipped: %s", e)

    # Database schema is created by scripts/init_db.py on first setup.
    # Do NOT run create_all or alembic here — hot reloads would conflict.

    # Initialize runtime tool registry — must happen before any chat request.
    try:
        from packages.core.ai.runtime import runtime_ensure_tool_registry_initialized
        runtime_ensure_tool_registry_initialized()
    except Exception as e:
        logger.error("Runtime tool registry initialization failed: %s", e, exc_info=True)

    # Self-heal the MCP catalog. Test fixtures wipe the table, and OSS
    # deployments may skip init_db — both end up with an empty
    # Integrations page. Seed on every boot (idempotent via ON CONFLICT).
    try:
        from packages.core.database import engine as _engine
        from packages.core.services.mcp_seed import seed_mcp_catalog
        await seed_mcp_catalog(_engine)
    except Exception as e:
        logger.warning("MCP catalog auto-seed skipped: %s", e)

    # Refresh optional plan metadata when the deployment provides it.
    # OSS builds keep the local OSS plan and return immediately.
    try:
        from packages.core.database import async_session
        from packages.core.constants.plans import load_plans_into_cache
        async with async_session() as _db:
            await load_plans_into_cache(_db)
    except Exception as e:
        logger.warning("Plan cache load skipped: %s", e)

    if os.getenv("DEPLOYMENT_MODE", "oss").strip().lower() == "oss":
        try:
            from packages.core.database import async_session
            from packages.core.services.demo_account import ensure_demo_account
            async with async_session() as _db:
                result = await ensure_demo_account(_db)
                await _db.commit()
            if result.get("enabled"):
                logger.info("OSS demo account ready: %s", result.get("email"))
        except Exception as e:
            logger.warning("OSS demo account seed skipped: %s", e)

    # Warm optional model price metadata for runtime accounting displays.
    try:
        from packages.core.services.openrouter_pricing_sync import sync_openrouter_pricing_cache
        await sync_openrouter_pricing_cache(timeout_s=10.0)
    except Exception as e:
        logger.warning("OpenRouter pricing cache warmup skipped: %s", e)

    async def _oauth_env_bootstrap_task() -> None:
        # Bootstrap OAuth client credentials from env into the MCPServer
        # rows. This may talk to Vault, so keep it off the critical
        # FastAPI startup path; integrations can finish seeding after the
        # API starts accepting traffic.
        try:
            from packages.core.database import async_session
            from packages.core.services.oauth_provider_config import (
                seed_oauth_clients_from_env,
            )
            async with async_session() as _db:
                actions = await seed_oauth_clients_from_env(_db)
            seeded = [k for k, v in actions.items() if v in ("seeded", "refreshed")]
            errored = [(k, v) for k, v in actions.items() if str(v).startswith("error:")]
            if seeded:
                logger.info("OAuth bootstrap: %s", ", ".join(seeded))
            if errored:
                # Surface per-provider failures (e.g. Vault unavailable)
                # so they don't get swallowed silently.
                logger.warning(
                    "OAuth bootstrap had %d error(s): %s",
                    len(errored),
                    "; ".join(f"{k} → {v}" for k, v in errored),
                )
        except Exception as e:
            logger.warning("OAuth env bootstrap skipped: %s", e)

    # Bootstrap OAuth PROVIDER clients (Manor-as-IdP for downstream apps).
    # Distinct from the block above (that was for Manor-as-OAuth-consumer
    # of Gmail/Slack/etc). Reads MANOR_OAUTH_CLIENT_<NAME>_SECRET env vars
    # (set via the deploy workflow from GitHub Secrets) and upserts the
    # matching rows in oauth_client_apps. Idempotent: same secret → no
    # bcrypt re-hash, same redirect_uris → no DB write. Replaces the
    # manual scripts/seed_oauth_client_pms.py SSH workflow.
    try:
        from packages.core.database import async_session
        from packages.core.services.oauth_provider_service import (
            seed_clients_from_env as seed_oauth_provider_clients,
        )
        async with async_session() as _db:
            actions = await seed_oauth_provider_clients(_db)
        changed = [k for k, v in actions.items() if v in ("seeded", "rotated")]
        skipped = [k for k, v in actions.items() if str(v).startswith("skipped")]
        errored = [(k, v) for k, v in actions.items() if str(v).startswith("error")]
        if changed:
            logger.info("OAuth provider clients applied: %s", ", ".join(
                f"{k}={actions[k]}" for k in changed
            ))
        if skipped:
            logger.info("OAuth provider clients skipped (no env secret): %s",
                        ", ".join(skipped))
        if errored:
            logger.warning("OAuth provider client seed errors: %s",
                           "; ".join(f"{k} → {v}" for k, v in errored))
    except Exception as e:
        logger.warning("OAuth provider bootstrap skipped: %s", e)

    async def _nango_bootstrap_task() -> None:
        # Bootstrap Nango provider config and webhook settings. Nango is
        # optional for core API health, so do not block app startup on it.
        try:
            from packages.core.services.nango_bootstrap import seed_nango_from_env
            result = await seed_nango_from_env()
            if isinstance(result, dict):
                providers = result.get("providers") or {}
                seeded = [k for k, v in providers.items() if "error" not in str(v)]
                errored = [(k, v) for k, v in providers.items() if "error" in str(v)]
                if seeded:
                    logger.info("Nango bootstrap: providers=%s, webhook=%s",
                                ", ".join(seeded), result.get("webhook"))
                elif "skipped" in result:
                    logger.info("Nango bootstrap: %s", result["skipped"])
                elif providers:
                    logger.warning(
                        "Nango bootstrap: 0/%d providers seeded, webhook=%s, errors=%s",
                        len(providers),
                        result.get("webhook"),
                        "; ".join(f"{k}: {v}" for k, v in errored),
                    )
                else:
                    logger.info(
                        "Nango bootstrap: no NANGO_PROVIDER_* env vars declared, webhook=%s",
                        result.get("webhook"),
                    )
        except Exception as e:
            logger.warning("Nango env bootstrap skipped: %s", e)

    # Self-heal ChannelContact table. Added as a new model after the
    # initial schema shipped; CREATE IF NOT EXISTS keeps existing
    # deployments from hitting "relation channel_contacts does not
    # exist" on first inbound message. Also adds the user_id/role
    # identity columns if they're missing from an older deployment.
    try:
        from sqlalchemy import text as _sql_text
        from packages.core.database import engine as _engine
        from packages.core.models.base import Base
        from packages.core.models.channel import ChannelContact  # noqa: F401
        async with _engine.begin() as conn:
            await conn.run_sync(
                lambda c: Base.metadata.tables["channel_contacts"].create(c, checkfirst=True)
            )
            await conn.execute(_sql_text(
                "ALTER TABLE channel_contacts "
                "ADD COLUMN IF NOT EXISTS user_id VARCHAR(26)"
            ))
            await conn.execute(_sql_text(
                "ALTER TABLE channel_contacts "
                "ADD COLUMN IF NOT EXISTS role VARCHAR(32) NOT NULL DEFAULT 'external'"
            ))
    except Exception as e:
        logger.warning("ChannelContact table auto-heal skipped: %s", e)

    # Self-heal subscription-centric channel routing columns. All
    # nullable — legacy rows keep working (the gateway synthesises a
    # stub subscription from ``Channel.agent_id`` when these are empty).
    try:
        from sqlalchemy import text as _sql_text
        from packages.core.database import engine as _engine
        async with _engine.begin() as conn:
            await conn.execute(_sql_text(
                "ALTER TABLE channels "
                "ADD COLUMN IF NOT EXISTS agent_subscription_id VARCHAR(26)"
            ))
            await conn.execute(_sql_text(
                "ALTER TABLE channel_contacts "
                "ADD COLUMN IF NOT EXISTS agent_subscription_id VARCHAR(26)"
            ))
            await conn.execute(_sql_text(
                "ALTER TABLE conversations "
                "ADD COLUMN IF NOT EXISTS agent_subscription_id VARCHAR(26)"
            ))
            await conn.execute(_sql_text(
                "ALTER TABLE agent_subscriptions "
                "ADD COLUMN IF NOT EXISTS name VARCHAR(255)"
            ))
    except Exception as e:
        logger.warning("Subscription routing auto-heal skipped: %s", e)

    # Kick off the Telegram long-polling runner when TELEGRAM_MODE is
    # "polling" (or "auto" + non-HTTPS PUBLIC_BASE_URL). No-op otherwise.
    try:
        from packages.core.services.channels.telegram_poller import poller as _tg_poller
        await _tg_poller.start()
    except Exception as e:
        logger.warning("Telegram poller startup skipped: %s", e)

    # Start Redis pub/sub relay for WS broadcasts from worker
    try:
        from apps.api.routers.ws import start_redis_relay
        start_redis_relay()
        logger.info("Redis WS relay task started")
    except Exception as e:
        logger.warning("Redis WS relay startup skipped: %s", e)

    startup_tasks.append(asyncio.create_task(
        _oauth_env_bootstrap_task(),
        name="oauth-env-bootstrap",
    ))
    startup_tasks.append(asyncio.create_task(
        _nango_bootstrap_task(),
        name="nango-bootstrap",
    ))

    async def _knowledge_backfill_task() -> None:
        try:
            from packages.core.services.knowledge_backfill import run_startup_knowledge_backfill
            await run_startup_knowledge_backfill()
        except Exception as e:
            logger.warning("Knowledge startup backfill skipped: %s", e, exc_info=True)

    knowledge_backfill_task = asyncio.create_task(
        _knowledge_backfill_task(),
        name="knowledge-startup-backfill",
    )
    startup_tasks.append(knowledge_backfill_task)

    async def _workspace_operation_repair_task() -> None:
        try:
            from packages.core.services.workspace_operation_repair import (
                run_startup_workspace_operation_runtime_repair,
            )
            await run_startup_workspace_operation_runtime_repair()
        except Exception as e:
            logger.warning("Workspace operation startup repair skipped: %s", e, exc_info=True)

    workspace_operation_repair_task = asyncio.create_task(
        _workspace_operation_repair_task(),
        name="workspace-operation-startup-repair",
    )
    startup_tasks.append(workspace_operation_repair_task)

    async def _stale_stream_repair_task() -> None:
        try:
            from packages.core.database import async_session
            from packages.core.services.conversation_messages import (
                mark_stale_assistant_streams_interrupted,
            )
            async with async_session() as _db:
                count = await mark_stale_assistant_streams_interrupted(_db)
            if count:
                logger.info(
                    "Assistant stream startup repair marked %d stale checkpoint(s) interrupted",
                    count,
                )
        except Exception as e:
            logger.warning("Assistant stream startup repair skipped: %s", e, exc_info=True)

    stale_stream_repair_task = asyncio.create_task(
        _stale_stream_repair_task(),
        name="assistant-stream-startup-repair",
    )
    startup_tasks.append(stale_stream_repair_task)

    yield
    # Shutdown
    for startup_task in startup_tasks:
        if not startup_task.done():
            startup_task.cancel()
    try:
        from apps.api.routers.ws import stop_redis_relay
        stop_redis_relay()
    except Exception:
        pass
    try:
        from packages.core.services.channels.telegram_poller import poller as _tg_poller
        await _tg_poller.stop()
    except Exception:
        logger.debug("Telegram poller stop failed", exc_info=True)
    from packages.core.cache import cache
    await cache.close()
    try:
        from packages.core.observability import shutdown_tracing
        shutdown_tracing()
    except Exception:
        logger.debug("OTEL shutdown failed", exc_info=True)
    logger.info("Manor AI shutting down")


tags_metadata = [
    {"name": "auth", "description": "Authentication — register, login, JWT tokens"},
    {"name": "entities", "description": "Entity (organization) management"},
    {"name": "workspaces", "description": "Workspace CRUD"},
    {"name": "workspace-drafts", "description": "Conversational workspace draft creation"},
    {"name": "tasks", "description": "Task management — CRUD, status transitions, categories"},
    {"name": "chat", "description": "AI chat — SSE streaming, conversations, messages"},
    {"name": "agents", "description": "Agent CRUD, subscriptions, tool bindings"},
    {"name": "documents", "description": "Document management — upload, search, groups, RAG indexing"},
    {"name": "integrations", "description": "External integrations and channels"},
    {"name": "notifications", "description": "Notification management"},
    {"name": "admin", "description": "Admin — audit logs, settings, preferences"},
    {"name": "people", "description": "Clients and staff members"},
    {"name": "usage", "description": "Token usage tracking and analytics"},
    {"name": "dashboard", "description": "Dashboard analytics — stats, trends, activity"},
    {"name": "goals", "description": "AI goal execution — plan/execute/reflect"},
    {"name": "scheduler", "description": "Scheduled jobs and agent executions"},
    {"name": "search", "description": "Global cross-entity search"},
    {"name": "websocket", "description": "WebSocket real-time notifications"},
    {"name": "activity", "description": "Activity feed and event logging"},
    {"name": "bulk", "description": "Bulk operations — batch update/delete, CSV export/import"},
    {"name": "webhooks", "description": "Webhook endpoint management and delivery"},
    {"name": "api-keys", "description": "Entity-level LLM provider API key management"},
    {"name": "backup", "description": "Entity data backup and export"},
    {"name": "skills", "description": "Reusable prompt+tool skill bundles"},
    {"name": "custom-fields", "description": "Custom field definitions for extensible entity data"},
    {"name": "memories", "description": "Agent conversation memory — persistent facts and preferences"},
    {"name": "comments", "description": "Threaded comments on tasks, documents, and other resources"},
    {"name": "quotas", "description": "Entity usage quotas and limit enforcement"},
    {"name": "favorites", "description": "Pinned items, favorites, and bookmarks"},
    {"name": "tags", "description": "Universal tagging system for any resource"},
    {"name": "presence", "description": "Real-time collaboration presence"},
    {"name": "workflows", "description": "Agent workflow engine — multi-step pipelines"},
    {"name": "reports", "description": "Scheduled reports — task, usage, activity HTML/PDF generation"},
    {"name": "portal", "description": "Client portal — external self-service tickets and communication"},
    {"name": "orders", "description": "Business order / commerce management"},
    {"name": "docgen", "description": "Document generation — create Word, PDF, and PowerPoint files"},
    {"name": "browser", "description": "Browser automation — headless browser sessions for scraping and interaction"},
    {"name": "channels", "description": "Channel webhooks — WeChat, WhatsApp, and other inbound message callbacks"},
    {"name": "staff-management", "description": "Staff departments, schedules, and availability"},
    {"name": "health", "description": "Health check"},
    {"name": "filesystem", "description": "Entity filesystem — JuiceFS-backed POSIX file operations and wiki links"},
]


def create_app() -> FastAPI:
    app = FastAPI(
        title="Manor AI",
        description="The AI operating system for autonomous enterprise management",
        version="0.1.0",
        docs_url="/api/docs",
        redoc_url="/api/redoc",
        openapi_url="/api/openapi.json",
        openapi_tags=tags_metadata,
        lifespan=lifespan,
    )

    # CORS
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Production middleware (request ID, logging, rate limiting, error handling)
    setup_middleware(app)

    # Global exception handler: CreditExhaustedError → 402
    from packages.core.ai.llm_client import CreditExhaustedError
    from fastapi.responses import JSONResponse

    @app.exception_handler(CreditExhaustedError)
    async def _credit_exhausted_handler(request, exc: CreditExhaustedError):
        return JSONResponse(status_code=402, content={
            "detail": {
                "message": str(exc),
                "plan": exc.plan,
                "limit": exc.limit,
                "current": exc.current,
                "kind": "credit",
            }
        })

    # Global exception handler: StorageLimitExceeded → 402 (same upgrade overlay
    # as the plan gate). Safety net so any document-create path that slips past
    # an explicit require_plan dep still returns a clean 402, not a 500.
    from packages.core.services.document_service import StorageLimitExceeded

    @app.exception_handler(StorageLimitExceeded)
    async def _storage_limit_handler(request, exc: StorageLimitExceeded):
        return JSONResponse(status_code=402, content={
            "detail": {
                "message": exc.message,
                "plan": exc.plan,
                "limit": exc.limit,
                "current": exc.current,
                "kind": "storage",
            }
        })

    # ── Core routers (always loaded) ──
    from apps.api.routers import auth, health, entities, workspaces, workspace_drafts, tasks, chat, messages, agents, documents, integrations, headed_login, notifications, admin, admin_oauth, oauth_provider, people, usage, goals, plans, workspace_chat, workers, scheduler, ws, search, dashboard, bulk, activity, webhooks, api_keys, templates, backup, skills, custom_fields, memories, comments, quotas, favorites, tags, presence, workflows, reports, portal, orders, docgen, browser, staff_management, calendar_settings, filesystem, nango_oauth, nango_webhooks, platform_public, business, audio, media, permissions as permissions_router, permissions_v1, document_permissions, folder_permissions, client_errors, support
    from apps.api.routers.channels import wechat as wechat_channel
    from apps.api.routers.channels import twilio as twilio_channel
    from apps.api.routers.channels import whatsapp as whatsapp_channel
    from apps.api.routers.channels import telegram as telegram_channel
    from apps.api.routers.channels import facebook as facebook_channel
    from apps.api.routers.channels import generic as generic_channel
    from apps.api.routers.channels import voice_stream as voice_stream_channel
    app.include_router(auth.router)
    app.include_router(health.router)
    app.include_router(entities.router)
    app.include_router(workspaces.router)
    app.include_router(workspace_drafts.router)
    app.include_router(nango_oauth.router)
    app.include_router(nango_webhooks.router)
    app.include_router(templates.router)  # must be before tasks (same prefix path)
    app.include_router(tasks.router)
    app.include_router(calendar_settings.router)
    app.include_router(chat.router)
    app.include_router(messages.router)
    # Cloud marketplace routes must be registered before the base agents/skills
    # routers, otherwise /marketplace and /manor are shadowed by /{id} routes.
    app.include_router(agents.router)
    app.include_router(documents.router)
    app.include_router(document_permissions.router)         # /api/v1/documents/{id}/grants, /shares, /access-log, /access-requests
    app.include_router(document_permissions.public_router)  # /api/v1/shared-doc/{token} — unauth
    app.include_router(folder_permissions.router)            # /api/v1/folders/{id}/properties, /grants, /shares
    app.include_router(folder_permissions.public_router)     # /api/v1/shared-folder/{token} — unauth viewer
    app.include_router(integrations.router)
    app.include_router(headed_login.router)
    app.include_router(notifications.router)
    app.include_router(admin.router)
    app.include_router(admin_oauth.router)
    app.include_router(oauth_provider.router)  # Manor as OAuth IdP (for PMS etc.)
    app.include_router(platform_public.router)
    app.include_router(client_errors.router)
    app.include_router(business.router)
    app.include_router(support.router)
    app.include_router(audio.router)
    app.include_router(media.router)
    # permissions_router must load BEFORE people.router because its
    # /staff/roles, /staff/invite paths would otherwise be shadowed by
    # people.router's /staff/{staff_id} catch-all.
    app.include_router(permissions_router.router)  # /permissions, /staff/roles, /staff/invite
    app.include_router(permissions_v1.router)      # /permissions/v1 — classify/legal-hold/access-requests (RFC §13)
    app.include_router(people.router)
    app.include_router(usage.router)
    app.include_router(goals.router)
    app.include_router(plans.router)
    app.include_router(workspace_chat.router)
    app.include_router(workers.router)
    app.include_router(scheduler.jobs_router)
    app.include_router(scheduler.executions_router)
    app.include_router(ws.router)
    app.include_router(search.router)
    app.include_router(dashboard.router)
    app.include_router(bulk.router)
    app.include_router(activity.router)
    app.include_router(webhooks.router)
    app.include_router(api_keys.router)
    app.include_router(backup.router)
    app.include_router(skills.router)
    app.include_router(custom_fields.router)
    app.include_router(memories.router)
    app.include_router(comments.router)
    app.include_router(quotas.router)
    app.include_router(favorites.router)
    app.include_router(tags.router)
    app.include_router(presence.router)
    app.include_router(workflows.router)
    app.include_router(reports.router)
    app.include_router(portal.router)
    app.include_router(orders.router)
    app.include_router(docgen.router)
    app.include_router(browser.router)
    app.include_router(staff_management.router)
    app.include_router(filesystem.router)
    from apps.api.routers import public_task, public_chat
    app.include_router(public_task.router)
    app.include_router(public_chat.router)
    app.include_router(wechat_channel.router)
    app.include_router(generic_channel.router)
    app.include_router(voice_stream_channel.router)
    app.include_router(twilio_channel.router)
    app.include_router(whatsapp_channel.router)
    app.include_router(telegram_channel.router)
    app.include_router(facebook_channel.router)

    # ── M7 + M10 routers (goal templates / governance / pairing / sessions) ──
    from apps.api.routers import (
        blueprints as blueprints_router,
        channel_pairing as channel_pairing_router,
        goal_templates as goal_templates_router,
        governance as governance_router,
        integration_sessions as integration_sessions_router,
    )
    app.include_router(goal_templates_router.router)
    app.include_router(goal_templates_router.apply_router)
    app.include_router(governance_router.router)
    app.include_router(channel_pairing_router.router)
    app.include_router(integration_sessions_router.router)
    # ── M12.1 Workspace Blueprints / Marketplace ──
    app.include_router(blueprints_router.blueprint_router)
    app.include_router(blueprints_router.workspace_router)


    return app


app = create_app()


def run_cli() -> None:
    """`manor-api` console-script entry point.

    Boots the FastAPI app under uvicorn with sane defaults. Reads host /
    port / reload from env so ops setups don't need to wrap this in
    another shell script. For more advanced configs, drop down to
    ``uvicorn apps.api.main:app`` directly.
    """
    import uvicorn

    host = os.getenv("MANOR_HOST", "0.0.0.0")
    port = int(os.getenv("MANOR_PORT", "8000"))
    reload = os.getenv("MANOR_RELOAD", "false").lower() in ("1", "true", "yes")
    workers = int(os.getenv("API_WORKERS") or os.getenv("MANOR_WORKERS", "1"))
    limit_concurrency = int(os.getenv("API_LIMIT_CONCURRENCY", "120"))
    backlog = int(os.getenv("API_BACKLOG", "256"))
    timeout_keep_alive = int(os.getenv("API_TIMEOUT_KEEP_ALIVE", "5"))

    uvicorn.run(
        "apps.api.main:app",
        host=host,
        port=port,
        reload=reload,
        workers=workers if not reload else 1,
        limit_concurrency=limit_concurrency,
        backlog=backlog,
        timeout_keep_alive=timeout_keep_alive,
    )
