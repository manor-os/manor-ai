"""Manor AI configuration — loaded from environment variables."""
from __future__ import annotations

import os
from functools import lru_cache

# The insecure built-in JWT signing key. Because it ships in the source, any
# deployment left on this value has forgeable auth tokens. App startup refuses
# to boot on it in cloud mode and warns loudly in OSS mode (see apps/api/main.py).
INSECURE_DEFAULT_JWT_SECRET = "dev-secret-change-in-production-32-bytes"
_INSECURE_JWT_SECRET_PLACEHOLDERS = frozenset({
    INSECURE_DEFAULT_JWT_SECRET,
    "change-this-to-a-random-string",
    "replace-with-openssl-rand-hex-32",
})


def is_insecure_jwt_secret(value: str) -> bool:
    """Return true for empty, short, or shipped JWT signing secrets."""
    return (
        not value
        or len(value.encode("utf-8")) < 32
        or value in _INSECURE_JWT_SECRET_PLACEHOLDERS
    )


def _env_bool(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).lower() in ("1", "true", "yes", "on")


class Settings:
    # API process/runtime controls. OSS keeps one API process by default; cloud
    # deployments can raise these through compose/env overlays.
    API_WORKERS: int
    API_LIMIT_CONCURRENCY: int
    API_BACKLOG: int
    API_TIMEOUT_KEEP_ALIVE: int

    # Database
    DATABASE_URL: str = os.getenv(
        "DATABASE_URL", "postgresql+asyncpg://manor:manor_secret@localhost:5434/manor"
    )
    DATABASE_URL_SYNC: str = os.getenv(
        "DATABASE_URL_SYNC", "postgresql://manor:manor_secret@localhost:5434/manor"
    )
    DATABASE_DIRECT_URL: str
    DATABASE_DIRECT_URL_SYNC: str
    DATABASE_ECHO: bool = os.getenv("DATABASE_ECHO", "false").lower() == "true"
    DATABASE_POOL_MODE: str
    DATABASE_POOL_SIZE: int
    DATABASE_MAX_OVERFLOW: int
    DATABASE_POOL_TIMEOUT: int
    DATABASE_POOL_RECYCLE: int

    # Redis
    REDIS_URL: str = os.getenv("REDIS_URL", "redis://localhost:6389/0")
    REDIS_RATE_LIMIT_ENABLED: bool

    # Emergency load shedding. Disabled by default so OSS behavior does not
    # change unless an operator deliberately flips the switch.
    DEGRADED_MODE: bool
    DEGRADED_DISABLE_CHAT_STREAM: bool
    DEGRADED_DISABLE_SANDBOX: bool
    DEGRADED_DISABLE_MEDIA_GENERATION: bool
    DEGRADED_DISABLE_LARGE_UPLOADS: bool

    # Auth
    JWT_SECRET_KEY: str = os.getenv("JWT_SECRET_KEY", INSECURE_DEFAULT_JWT_SECRET)
    JWT_ALGORITHM: str = os.getenv("JWT_ALGORITHM", "HS256")
    JWT_EXPIRE_MINUTES: int = int(os.getenv("JWT_EXPIRE_MINUTES", "60"))

    # LLM
    LLM_MODEL: str = os.getenv("LLM_MODEL", "anthropic/claude-sonnet-4")

    # Embedding
    EMBEDDING_API_KEY: str = os.getenv("EMBEDDING_API_KEY", "")
    EMBEDDING_BASE_URL: str = os.getenv("EMBEDDING_BASE_URL", "http://localhost:11434/v1")
    EMBEDDING_MODEL: str = os.getenv("EMBEDDING_MODEL", "mxbai-embed-large")
    EMBEDDING_DIMENSIONS: int = int(os.getenv("EMBEDDING_DIMENSIONS", "1024"))

    # Live dashboard market data
    FINNHUB_API_KEY: str = os.getenv("FINNHUB_API_KEY", "")

    # Storage
    MANOR_FS_ENABLED: bool = os.getenv("MANOR_FS_ENABLED", "false").lower() in ("true", "1")
    MANOR_FS_ROOT: str = os.getenv("MANOR_FS_ROOT", "/mnt/manor")
    MANOR_MAX_UPLOAD_MB: int = int(os.getenv("MANOR_MAX_UPLOAD_MB", "500"))
    MANOR_CHAT_UPLOAD_CLEANUP_ENABLED: bool = os.getenv("MANOR_CHAT_UPLOAD_CLEANUP_ENABLED", "true").lower() in ("true", "1")
    MANOR_CHAT_UPLOAD_RETENTION_DAYS: int = int(os.getenv("MANOR_CHAT_UPLOAD_RETENTION_DAYS", "30"))
    # Temporary product switch: disable AI file-operation HITL approval cards.
    # Set MANOR_AI_FILE_HITL_ENABLED=true to restore per-file approval prompts.
    MANOR_AI_FILE_HITL_ENABLED: bool = os.getenv("MANOR_AI_FILE_HITL_ENABLED", "false").lower() in ("true", "1")

    # MinIO (object storage for skill files and other workspace assets)
    MINIO_ENDPOINT: str = os.getenv("MINIO_ENDPOINT", "")
    MINIO_ACCESS_KEY: str = os.getenv("MINIO_ACCESS_KEY", "")
    MINIO_SECRET_KEY: str = os.getenv("MINIO_SECRET_KEY", "")
    MINIO_BUCKET: str = os.getenv("MINIO_BUCKET", "manor")
    MINIO_SKILL_PREFIX: str = os.getenv("MINIO_SKILL_PREFIX", "skills")

    # Sandbox Service — external Docker-based skill execution sandbox
    SANDBOX_SERVICE_URL: str = os.getenv("SANDBOX_SERVICE_URL", "http://localhost:8000")
    SANDBOX_API_TOKEN: str = os.getenv("SANDBOX_API_TOKEN", "")
    SANDBOX_COORDINATION_MODE: str = os.getenv("SANDBOX_COORDINATION_MODE", "local").lower()
    SANDBOX_RUNNERS_JSON: str = os.getenv("SANDBOX_RUNNERS_JSON", "")
    # Positive values cap observed runner capacity; zero leaves it uncapped.
    SANDBOX_RUNNER_ACTIVE_LIMIT: int = int(
        os.getenv("SANDBOX_RUNNER_ACTIVE_LIMIT", "5")
    )
    SANDBOX_RUNNER_EXECUTING_LIMIT: int = int(
        os.getenv("SANDBOX_RUNNER_EXECUTING_LIMIT", "2")
    )
    SANDBOX_QUEUE_MAX_PENDING: int = int(os.getenv("SANDBOX_QUEUE_MAX_PENDING", "100"))
    SANDBOX_QUEUE_MAX_PENDING_PER_USER: int = int(
        os.getenv("SANDBOX_QUEUE_MAX_PENDING_PER_USER", "3")
    )
    SANDBOX_QUEUE_MAX_WAIT_SECONDS: int = int(
        os.getenv("SANDBOX_QUEUE_MAX_WAIT_SECONDS", "300")
    )
    SANDBOX_QUEUE_POLL_SECONDS: int = int(os.getenv("SANDBOX_QUEUE_POLL_SECONDS", "5"))
    SANDBOX_RUNNER_HEALTH_INTERVAL_SECONDS: int = int(
        os.getenv("SANDBOX_RUNNER_HEALTH_INTERVAL_SECONDS", "15")
    )
    SANDBOX_ALLOCATION_LEASE_SECONDS: int = int(
        os.getenv("SANDBOX_ALLOCATION_LEASE_SECONDS", "240")
    )

    # Deployment
    DEPLOYMENT_MODE: str = os.getenv("DEPLOYMENT_MODE", "oss")  # oss | cloud
    MANOR_SERVICE_ROLE: str = os.getenv("MANOR_SERVICE_ROLE", "api")
    MANOR_AUTOSCALING_METRICS_ENABLED: bool = _env_bool(
        "MANOR_AUTOSCALING_METRICS_ENABLED"
    )
    MANOR_RUNTIME_EXECUTION_MODE: str = os.getenv(
        "MANOR_RUNTIME_EXECUTION_MODE", "inline"
    ).lower()
    RUNTIME_EVENT_STREAM_TTL_SECONDS: int = int(
        os.getenv("RUNTIME_EVENT_STREAM_TTL_SECONDS", "3600")
    )
    RUNTIME_EVENT_STREAM_MAX_EVENTS: int = int(
        os.getenv("RUNTIME_EVENT_STREAM_MAX_EVENTS", "2000")
    )
    APP_URL: str = os.getenv("APP_URL", "")
    CLI_PUBLIC_API_URL: str = os.getenv("CLI_PUBLIC_API_URL", "")

    # Public base domain for published static sites, e.g. "sites.manorai.xyz".
    # Each site is served at {slug}.{MANOR_SITES_DOMAIN}; the domain is also the
    # CNAME target for user-owned custom domains. Empty disables site hosting.
    MANOR_SITES_DOMAIN: str = os.getenv("MANOR_SITES_DOMAIN", "")

    # Public base URL — used to construct channel webhook URLs (Telegram,
    # WhatsApp, WeChat, …). Must be an https endpoint reachable from the
    # provider's servers. Overridden per-deployment via env var.
    PUBLIC_BASE_URL: str = os.getenv("PUBLIC_BASE_URL", "http://localhost:8000")

    # Telegram inbound mode: "webhook" | "polling" | "auto".
    # - webhook  → the integration bridge calls setWebhook on save; requires
    #              an HTTPS PUBLIC_BASE_URL reachable from Telegram.
    # - polling  → a background asyncio task inside the API calls
    #              getUpdates per bot. No public URL needed.
    # - auto     → webhook when PUBLIC_BASE_URL is HTTPS, else polling.
    TELEGRAM_MODE: str = os.getenv("TELEGRAM_MODE", "auto").lower()

    # ── Credential vault ──
    # Backend: "vault" (HashiCorp Vault Transit) | "dev" (local Fernet,
    # not for production) | "legacy" (no encryption — passthrough JSONB,
    # only useful for in-place upgrades before the vault is online).
    CREDENTIAL_BACKEND: str = os.getenv("CREDENTIAL_BACKEND", "vault").lower()
    VAULT_ADDR: str = os.getenv("VAULT_ADDR", "http://localhost:8210")
    VAULT_TOKEN: str = os.getenv("VAULT_TOKEN", "")
    VAULT_TRANSIT_KEY: str = os.getenv("VAULT_TRANSIT_KEY", "manor-keys")
    # Dev backend key — base64 32-byte Fernet key. Auto-generated on first
    # boot when missing (with a loud warning).
    DEV_CREDENTIAL_KEY: str = os.getenv("DEV_CREDENTIAL_KEY", "")

    def __init__(self) -> None:
        self.API_WORKERS = int(os.getenv("API_WORKERS", "1"))
        self.API_LIMIT_CONCURRENCY = int(os.getenv("API_LIMIT_CONCURRENCY", "120"))
        self.API_BACKLOG = int(os.getenv("API_BACKLOG", "256"))
        self.API_TIMEOUT_KEEP_ALIVE = int(os.getenv("API_TIMEOUT_KEEP_ALIVE", "5"))
        self.DATABASE_URL = os.getenv("DATABASE_URL", self.DATABASE_URL)
        self.DATABASE_URL_SYNC = os.getenv("DATABASE_URL_SYNC", self.DATABASE_URL_SYNC)
        self.DATABASE_DIRECT_URL = os.getenv("DATABASE_DIRECT_URL", self.DATABASE_URL)
        self.DATABASE_DIRECT_URL_SYNC = os.getenv(
            "DATABASE_DIRECT_URL_SYNC", self.DATABASE_URL_SYNC
        )
        self.DATABASE_POOL_MODE = os.getenv("DATABASE_POOL_MODE", "sqlalchemy").strip().lower()
        self.DATABASE_POOL_SIZE = int(os.getenv("DATABASE_POOL_SIZE", "5"))
        self.DATABASE_MAX_OVERFLOW = int(os.getenv("DATABASE_MAX_OVERFLOW", "2"))
        self.DATABASE_POOL_TIMEOUT = int(os.getenv("DATABASE_POOL_TIMEOUT", "10"))
        self.DATABASE_POOL_RECYCLE = int(os.getenv("DATABASE_POOL_RECYCLE", "1800"))
        self.REDIS_RATE_LIMIT_ENABLED = _env_bool("REDIS_RATE_LIMIT_ENABLED")
        self.DEGRADED_MODE = _env_bool("DEGRADED_MODE")
        self.DEGRADED_DISABLE_CHAT_STREAM = _env_bool("DEGRADED_DISABLE_CHAT_STREAM", "true")
        self.DEGRADED_DISABLE_SANDBOX = _env_bool("DEGRADED_DISABLE_SANDBOX", "true")
        self.DEGRADED_DISABLE_MEDIA_GENERATION = _env_bool("DEGRADED_DISABLE_MEDIA_GENERATION", "true")
        self.DEGRADED_DISABLE_LARGE_UPLOADS = _env_bool("DEGRADED_DISABLE_LARGE_UPLOADS", "true")


@lru_cache()
def get_settings() -> Settings:
    return Settings()
