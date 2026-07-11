"""Process-wide CredentialService factory.

Reads the configured backend from settings and constructs a single
shared CredentialService instance. The factory is lazy + cached so
importing the credentials package never opens a connection to Vault.

Usage:

    from packages.core.credentials import get_credential_service
    creds_svc = get_credential_service()

Tests can override by passing an explicit service into the call sites,
or by patching ``_INSTANCE`` to None and re-calling.
"""
from __future__ import annotations

import logging
import os
from functools import lru_cache
from typing import Optional

from packages.core.config import get_settings
from packages.core.credentials.audit import AuditSink, DBAuditSink, LoggingAuditSink
from packages.core.credentials.base import KeyProvider
from packages.core.credentials.dev_provider import DevKeyProvider
from packages.core.credentials.legacy_provider import LegacyKeyProvider
from packages.core.credentials.service import CredentialService

logger = logging.getLogger(__name__)


def build_key_provider(
    backend: Optional[str] = None,
    *,
    audit_sink: Optional[AuditSink] = None,
) -> KeyProvider:
    """Construct a KeyProvider matching the requested backend.

    ``backend`` defaults to ``CREDENTIAL_BACKEND`` from settings. The
    audit sink is wired into the provider so encrypt/decrypt events are
    recorded even when a caller bypasses CredentialService (rare).
    """
    settings = get_settings()
    backend = (backend or settings.CREDENTIAL_BACKEND).lower()
    sink = audit_sink or _default_audit_sink()

    if backend == "vault":
        if not settings.VAULT_TOKEN:
            logger.warning(
                "CREDENTIAL_BACKEND=vault but VAULT_TOKEN is missing; "
                "using dev credential provider for local/test runtime"
            )
            return DevKeyProvider(
                key=settings.DEV_CREDENTIAL_KEY or os.environ.get("DEV_CREDENTIAL_KEY") or None,
                audit_sink=sink,
            )
        from packages.core.credentials.vault_provider import VaultKeyProvider

        return VaultKeyProvider(
            addr=settings.VAULT_ADDR,
            token=settings.VAULT_TOKEN,
            transit_key=settings.VAULT_TRANSIT_KEY,
            audit_sink=sink,
        )
    if backend == "dev":
        return DevKeyProvider(
            key=settings.DEV_CREDENTIAL_KEY or None,
            audit_sink=sink,
        )
    if backend == "legacy":
        return LegacyKeyProvider(audit_sink=sink)
    raise ValueError(
        f"unknown CREDENTIAL_BACKEND={backend!r}; expected vault|dev|legacy"
    )


def _default_audit_sink() -> AuditSink:
    """Use the DB sink when DATABASE_URL_SYNC is set, else fall back to
    logging. Tests can pass NullAuditSink explicitly."""
    try:
        return DBAuditSink()
    except Exception as exc:  # noqa: BLE001
        logger.warning("falling back to LoggingAuditSink: %s", exc)
        return LoggingAuditSink()


@lru_cache(maxsize=1)
def get_credential_service() -> CredentialService:
    """Process-wide singleton. Safe to call from any layer."""
    sink = _default_audit_sink()
    kp = build_key_provider(audit_sink=sink)
    logger.info("CredentialService initialised with backend=%s", kp.backend)
    return CredentialService(kp, audit_sink=sink)
