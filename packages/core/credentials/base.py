"""KeyProvider Protocol + shared types.

A KeyProvider is the narrow primitive that the CredentialService delegates
to. The interface is intentionally tiny so the same Protocol covers a real
Vault Transit backend, a dev-mode Fernet implementation, and a passthrough
for legacy unencrypted JSONB rows during in-place upgrades.

Conventions:
  * ``encrypt`` returns an opaque ref string. Callers persist the ref;
    they should not try to parse it. Vault uses ``vault:vN:...``; the dev
    backend uses ``dev:v1:...``; legacy uses ``legacy::``.
  * ``context`` lets the caller bind the ciphertext to its row identity
    (entity_id, integration_id, provider). Decrypt with a different
    context fails — protects against confused-deputy attacks where one
    row's ciphertext is moved into another row.
  * ``decrypt`` requires a ``reason`` and a ``Requester``. These flow
    straight to the audit sink. No silent decrypts.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol, runtime_checkable


@dataclass(frozen=True)
class Requester:
    """Who is asking to decrypt — recorded verbatim in the audit log."""

    kind: str
    """e.g. 'agent_subscription' | 'staff' | 'system' | 'step' | 'goal_measurement'"""

    id: str
    """Stable identifier of the requester within its kind."""

    step_id: Optional[str] = None
    """If decrypt happens inside a step run, the step's id (for traceability)."""


@dataclass(frozen=True)
class HealthResult:
    ok: bool
    detail: str = ""


class CredentialError(Exception):
    """Base class for credential subsystem failures."""


class CredentialNotFound(CredentialError):
    """The requested credential ref is empty / missing."""


class CredentialDecryptError(CredentialError):
    """Underlying KMS rejected the ciphertext (wrong context, key rotated
    out of recovery window, ciphertext tampered)."""


@runtime_checkable
class KeyProvider(Protocol):
    """Pluggable encryption backend.

    Implementations must be safe to share across threads — the
    CredentialService holds a single instance for the process.
    """

    backend: str
    """Stable identifier persisted in ``credential_scheme`` columns:
    'vault_transit' | 'dev_fernet' | 'legacy_jsonb'."""

    def encrypt(self, plaintext: bytes, context: dict[str, str]) -> str:
        """Encrypt and return an opaque ref. Never raises on empty input —
        an empty plaintext yields an empty-but-valid ciphertext, so the
        caller can always round-trip."""
        ...

    def decrypt(
        self,
        ref: str,
        context: dict[str, str],
        *,
        reason: str,
        requester: Requester,
    ) -> bytes:
        """Decrypt ``ref``. Raises CredentialDecryptError on failure.
        Implementations MUST emit an audit event."""
        ...

    def rotate(self) -> None:
        """Trigger a key rotation. No-op for backends without rotation."""
        ...

    def health(self) -> HealthResult:
        """Quick readiness probe — used by /healthz and integration_health."""
        ...
