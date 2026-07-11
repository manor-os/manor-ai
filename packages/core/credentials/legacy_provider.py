"""Passthrough provider for legacy unencrypted JSONB credentials.

Used during in-place upgrades when the deployment hasn't installed Vault
yet but already has rows in ``integrations.credentials`` / ``channel_configs.credentials``.
The ref format is ``legacy::`` and the actual plaintext lives in the
caller-supplied ``credentials`` JSONB column — this provider just round-trips
through that column.

In practice the CredentialService routes ``credential_scheme='legacy_jsonb'``
rows to its own legacy branch (reading the JSONB directly) and never calls
into this provider at all. We keep the class to make the KeyProvider
Protocol total — every scheme has a corresponding provider — and to give
operators a single ``CREDENTIAL_BACKEND=legacy`` switch for "no vault
running yet, do nothing dangerous on writes" mode.
"""
from __future__ import annotations

import logging

from packages.core.credentials.audit import AuditEvent, AuditSink, NullAuditSink
from packages.core.credentials.base import (
    CredentialError,
    HealthResult,
    Requester,
)

logger = logging.getLogger(__name__)


class LegacyKeyProvider:
    backend = "legacy_jsonb"

    def __init__(self, audit_sink: AuditSink | None = None):
        self._audit = audit_sink or NullAuditSink()

    def encrypt(self, plaintext: bytes, context: dict[str, str]) -> str:
        # Refuse new-write encryption — legacy mode is read-only for safety.
        # New writes should always go through a real KMS.
        raise CredentialError(
            "LegacyKeyProvider is read-only — set CREDENTIAL_BACKEND=vault "
            "or CREDENTIAL_BACKEND=dev to enable writes."
        )

    def decrypt(
        self,
        ref: str,
        context: dict[str, str],
        *,
        reason: str,
        requester: Requester,
    ) -> bytes:
        # The CredentialService should never route here — legacy rows are
        # read from the JSONB column, not via decrypt. If we land here,
        # something's mis-routed.
        self._audit.log(AuditEvent(
            credential_ref=ref,
            action="decrypt",
            requester_kind=requester.kind,
            requester_id=requester.id,
            step_id=requester.step_id,
            reason=reason,
        ))
        raise CredentialError("legacy provider does not decrypt refs directly")

    def rotate(self) -> None:
        return None

    def health(self) -> HealthResult:
        return HealthResult(ok=True, detail="legacy passthrough")
