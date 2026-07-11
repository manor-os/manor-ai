"""Credential Vault — encrypted storage for integration secrets.

Two layers:

  KeyProvider   — low-level encrypt/decrypt primitive.
                  Pluggable: VaultKeyProvider (Transit), DevKeyProvider
                  (local Fernet, dev-only), LegacyKeyProvider (passthrough).

  CredentialService — high-level entry point. ``lease(integration, ...)``
                      returns the plaintext secret dict regardless of the
                      underlying scheme (legacy JSONB or vault ref). New
                      writes go through ``store(integration, ...)`` which
                      always uses the configured KeyProvider.

Audit: every decrypt/lease writes a row to vault_audit_log via the
DBAuditSink. Plaintext is never logged — only credential refs (truncated)
and the requester identity.
"""
from packages.core.credentials.base import (
    KeyProvider,
    Requester,
    HealthResult,
    CredentialError,
    CredentialNotFound,
    CredentialDecryptError,
)
from packages.core.credentials.service import CredentialService
from packages.core.credentials.factory import (
    build_key_provider,
    get_credential_service,
)

__all__ = [
    "KeyProvider",
    "Requester",
    "HealthResult",
    "CredentialError",
    "CredentialNotFound",
    "CredentialDecryptError",
    "CredentialService",
    "build_key_provider",
    "get_credential_service",
]
