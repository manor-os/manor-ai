"""HashiCorp Vault Transit-engine KeyProvider.

Why Transit:
  * AES-256-GCM with derived keys — context-bound, so a ciphertext from
    one (entity, integration) row cannot be replayed into another row.
  * Native key rotation: rewrap leaves old refs valid until they're
    re-encrypted, so credential migrations don't need a downtime window.
  * Plaintext never touches Vault storage — only the wrapped DEK does.

Operational notes:
  * In dev (docker-compose) Vault runs in ``-dev`` mode: in-memory,
    auto-unsealed, single root token. Fine for local/CI; **not** for
    production.
  * In production, run Vault in server mode with auto-unseal (cloud KMS
    or Shamir) and an AppRole / token-renewable identity for Manor.
  * The transit key is created on first use with derived=True. Existing
    deployments without derived keys will fail decrypt — by design — and
    must be migrated.
"""
from __future__ import annotations

import base64
import json
import logging
from typing import Optional

from packages.core.credentials.audit import AuditEvent, AuditSink, NullAuditSink
from packages.core.credentials.base import (
    CredentialError,
    CredentialDecryptError,
    CredentialNotFound,
    HealthResult,
    KeyProvider,
    Requester,
)

logger = logging.getLogger(__name__)


def _ctx_b64(context: dict[str, str]) -> str:
    """Canonical, deterministic JSON encoding of the context dict.

    Vault's derived-key feature requires the same context bytes for both
    encrypt and decrypt — sorting keys + compact separators avoids any
    ambiguity from Python dict iteration order.
    """
    canonical = json.dumps(context, sort_keys=True, separators=(",", ":"))
    return base64.b64encode(canonical.encode("utf-8")).decode("ascii")


class VaultKeyProvider:
    backend = "vault_transit"

    def __init__(
        self,
        addr: str,
        token: str,
        *,
        transit_key: str = "manor-keys",
        audit_sink: Optional[AuditSink] = None,
        mount_point: str = "transit",
    ):
        if not addr:
            raise ValueError("VaultKeyProvider requires VAULT_ADDR")
        if not token:
            raise ValueError("VaultKeyProvider requires VAULT_TOKEN")

        # Lazy-import hvac so dev/test environments without it can still
        # import this module via the credentials factory; only Vault-
        # backed deployments actually need the dependency.
        try:
            import hvac as _hvac  # noqa: F401
            from hvac.exceptions import (  # noqa: F401
                InvalidPath, InvalidRequest, VaultError,
            )
        except ImportError as exc:  # noqa: BLE001
            raise RuntimeError(
                "hvac is required for VaultKeyProvider. "
                "Install with `pip install hvac`, or set "
                "CREDENTIAL_BACKEND=dev to use the in-process provider."
            ) from exc
        self._hvac_module = _hvac
        self._hvac_exceptions = {
            "InvalidPath": InvalidPath,
            "InvalidRequest": InvalidRequest,
            "VaultError": VaultError,
        }

        self._client = _hvac.Client(url=addr, token=token)
        self._key = transit_key
        self._mount = mount_point
        self._audit = audit_sink or NullAuditSink()
        self._key_ensured = False

    # ── Setup ──

    def _ensure_transit_key(self) -> None:
        if self._key_ensured:
            return
        try:
            # Production runtime tokens should only need transit access. Avoid
            # sys/mounts unless the key is missing and we're bootstrapping a
            # local/dev Vault.
            self._client.secrets.transit.read_key(
                name=self._key, mount_point=self._mount,
            )
            self._key_ensured = True
            return
        except self._hvac_exceptions["InvalidPath"]:
            pass
        except self._hvac_exceptions["VaultError"] as exc:
            raise CredentialError(f"vault transit key check failed: {exc}") from exc

        # The transit secret engine itself must be enabled. In dev mode we
        # enable it on demand; in production this belongs to deploy-time Vault
        # bootstrap and should not be reached for a healthy runtime token.
        try:
            self._client.sys.enable_secrets_engine(
                backend_type="transit", path=self._mount,
            )
        except self._hvac_exceptions["InvalidRequest"]:
            # Already mounted — fine.
            pass
        except self._hvac_exceptions["VaultError"] as exc:
            raise CredentialError(f"vault transit mount unavailable: {exc}") from exc

        try:
            self._client.secrets.transit.read_key(
                name=self._key, mount_point=self._mount,
            )
        except self._hvac_exceptions["InvalidPath"]:
            try:
                self._client.secrets.transit.create_key(
                    name=self._key,
                    key_type="aes256-gcm96",
                    derived=True,
                    exportable=False,
                    mount_point=self._mount,
                )
            except self._hvac_exceptions["VaultError"] as exc:
                raise CredentialError(f"vault transit key create failed: {exc}") from exc
        except self._hvac_exceptions["VaultError"] as exc:
            raise CredentialError(f"vault transit key check failed: {exc}") from exc
        self._key_ensured = True

    # ── KeyProvider API ──

    def encrypt(self, plaintext: bytes, context: dict[str, str]) -> str:
        self._ensure_transit_key()
        if not plaintext:
            # Empty round-trip — store an empty ciphertext sentinel so
            # the decrypt path can short-circuit without calling Vault.
            return "vault:empty:"
        try:
            resp = self._client.secrets.transit.encrypt_data(
                name=self._key,
                plaintext=base64.b64encode(plaintext).decode("ascii"),
                context=_ctx_b64(context),
                mount_point=self._mount,
            )
        except self._hvac_exceptions["VaultError"] as exc:
            raise CredentialError(f"vault transit encrypt failed: {exc}") from exc
        ref = resp["data"]["ciphertext"]
        self._audit.log(AuditEvent(
            credential_ref=ref,
            action="encrypt",
            requester_kind=None,
            requester_id=None,
            step_id=None,
            reason=None,
        ))
        return ref

    def decrypt(
        self,
        ref: str,
        context: dict[str, str],
        *,
        reason: str,
        requester: Requester,
    ) -> bytes:
        if not ref:
            raise CredentialNotFound("empty credential ref")

        self._audit.log(AuditEvent(
            credential_ref=ref,
            action="decrypt",
            requester_kind=requester.kind,
            requester_id=requester.id,
            step_id=requester.step_id,
            reason=reason,
        ))

        if ref == "vault:empty:":
            return b""

        self._ensure_transit_key()
        try:
            resp = self._client.secrets.transit.decrypt_data(
                name=self._key,
                ciphertext=ref,
                context=_ctx_b64(context),
                mount_point=self._mount,
            )
        except self._hvac_exceptions["VaultError"] as exc:
            raise CredentialDecryptError(str(exc)) from exc
        return base64.b64decode(resp["data"]["plaintext"])

    def rotate(self) -> None:
        self._ensure_transit_key()
        self._client.secrets.transit.rotate_key(
            name=self._key, mount_point=self._mount,
        )
        self._audit.log(AuditEvent(
            credential_ref=None,
            action="rotate",
            requester_kind="system",
            requester_id="vault_provider",
            step_id=None,
            reason="manual_rotate",
        ))

    def health(self) -> HealthResult:
        try:
            is_authenticated = getattr(self._client, "is_authenticated", None)
            if callable(is_authenticated) and not is_authenticated():
                return HealthResult(ok=False, detail="vault token is invalid or unauthorized")
            sealed = self._client.sys.is_sealed()
            if sealed:
                return HealthResult(ok=False, detail="vault is sealed")
            try:
                self._client.secrets.transit.read_key(
                    name=self._key, mount_point=self._mount,
                )
            except self._hvac_exceptions["InvalidPath"]:
                return HealthResult(
                    ok=False,
                    detail=f"vault transit key {self._key!r} is missing",
                )
            except self._hvac_exceptions["VaultError"] as exc:
                return HealthResult(ok=False, detail=f"vault transit key check failed: {exc}")
            try:
                self._client.secrets.transit.encrypt_data(
                    name=self._key,
                    plaintext=base64.b64encode(b"health").decode("ascii"),
                    context=_ctx_b64({"purpose": "credential_health"}),
                    mount_point=self._mount,
                )
            except self._hvac_exceptions["VaultError"] as exc:
                return HealthResult(ok=False, detail=f"vault transit encrypt failed: {exc}")
            return HealthResult(ok=True, detail=f"key={self._key}")
        except Exception as exc:  # noqa: BLE001
            return HealthResult(ok=False, detail=str(exc))

    # Re-export for the assertion in CredentialService — runtime-checkable
    # Protocol matches duck-typed attributes already, but mypy is happier
    # with an explicit cast.
    if False:  # pragma: no cover — type-check assist only
        _: KeyProvider = None  # type: ignore[assignment]
