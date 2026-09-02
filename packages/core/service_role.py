"""Helpers for process-role-aware startup behavior."""
from __future__ import annotations

import os

SERVICE_ROLE_API = "api"
SERVICE_ROLE_CHAT = "chat"
SERVICE_ROLE_WORKER = "worker"
SERVICE_ROLE_WORKER_HEAVY = "worker-heavy"
SERVICE_ROLE_BEAT = "beat"
SERVICE_ROLE_MIGRATION = "migration"
SERVICE_ROLE_SANDBOX = "sandbox"

SUPPORTED_SERVICE_ROLES = {
    SERVICE_ROLE_API,
    SERVICE_ROLE_CHAT,
    SERVICE_ROLE_WORKER,
    SERVICE_ROLE_WORKER_HEAVY,
    SERVICE_ROLE_BEAT,
    SERVICE_ROLE_MIGRATION,
    SERVICE_ROLE_SANDBOX,
}


def normalize_service_role(value: str | None = None) -> str:
    role = (value if value is not None else os.getenv("MANOR_SERVICE_ROLE", SERVICE_ROLE_API)).strip().lower()
    return role if role in SUPPORTED_SERVICE_ROLES else SERVICE_ROLE_API


def validate_service_role_for_deployment(
    value: str | None = None,
    *,
    deployment_mode: str | None = None,
) -> str | None:
    raw_role = value if value is not None else os.getenv("MANOR_SERVICE_ROLE")
    role = (raw_role or "").strip().lower()
    if not role or role in SUPPORTED_SERVICE_ROLES:
        return None
    mode = (deployment_mode if deployment_mode is not None else os.getenv("DEPLOYMENT_MODE", "oss")).strip().lower()
    if mode == "cloud":
        return f"Unsupported MANOR_SERVICE_ROLE={role} for DEPLOYMENT_MODE=cloud"
    return None


def _env_bool(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def api_startup_side_effects_enabled(
    role: str | None = None,
    *,
    deployment_mode: str | None = None,
    explicit_enabled: str | None = None,
) -> bool:
    normalized = normalize_service_role(role)
    if normalized != SERVICE_ROLE_API:
        return False
    if _env_bool(explicit_enabled if explicit_enabled is not None else os.getenv("MANOR_STARTUP_SIDE_EFFECTS_OWNER")):
        return True
    mode = (deployment_mode if deployment_mode is not None else os.getenv("DEPLOYMENT_MODE", "oss")).strip().lower()
    return mode != "cloud"


def telegram_polling_startup_enabled(
    role: str | None = None,
    *,
    deployment_mode: str | None = None,
    explicit_enabled: str | None = None,
) -> bool:
    """Return whether this process may own Telegram long polling.

    Local/OSS single-machine deployments historically ran the poller inside
    the API process. Cloud/K8s API and chat deployments can have multiple
    replicas, so polling needs a deliberately single-owned process.
    """
    normalized = normalize_service_role(role)
    if normalized == SERVICE_ROLE_CHAT:
        return False
    if _env_bool(explicit_enabled if explicit_enabled is not None else os.getenv("MANOR_TELEGRAM_POLLING_RUNNER")):
        return True
    mode = (deployment_mode if deployment_mode is not None else os.getenv("DEPLOYMENT_MODE", "oss")).strip().lower()
    return normalized == SERVICE_ROLE_API and mode != "cloud"


def otel_service_name_for_role(role: str | None = None) -> str:
    normalized = normalize_service_role(role)
    if normalized == SERVICE_ROLE_CHAT:
        return "manor-chat"
    if normalized == SERVICE_ROLE_WORKER_HEAVY:
        return "manor-worker-heavy"
    if normalized == SERVICE_ROLE_WORKER:
        return "manor-worker"
    if normalized == SERVICE_ROLE_BEAT:
        return "manor-beat"
    if normalized == SERVICE_ROLE_MIGRATION:
        return "manor-migration"
    if normalized == SERVICE_ROLE_SANDBOX:
        return "manor-sandbox"
    return "manor-api"
