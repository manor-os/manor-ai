"""High-level credential entry point.

Callers should not interact with KeyProvider directly. Instead:

    from packages.core.credentials import get_credential_service, Requester

    creds_svc = get_credential_service()
    plaintext = creds_svc.lease_integration(
        integration,
        requester=Requester(kind="step", id=step.id, step_id=step.id),
        reason="action:gmail.send_email",
    )

The service handles three storage schemes transparently:

  legacy_jsonb  — historical rows whose ``credentials`` JSONB column
                  holds the plaintext. Returned as-is. Audit event still
                  logged so the operator can see who's still on the old
                  scheme.

  vault_transit — primary. Decrypts via the configured KeyProvider.

  dev_fernet    — local development. Same flow as vault_transit.

New writes always upgrade to the configured KeyProvider's scheme — the
``store_*`` helpers encrypt + clear the JSONB + set ``credential_ref`` +
``credential_scheme`` atomically. Legacy reads continue to work until a
backfill job rewrites them.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Optional

from packages.core.credentials.audit import AuditEvent, AuditSink, NullAuditSink
from packages.core.credentials.base import (
    CredentialError,
    CredentialDecryptError,
    CredentialNotFound,
    HealthResult,
    KeyProvider,
    Requester,
)

if TYPE_CHECKING:
    from packages.core.models.channel import ChannelConfig
    from packages.core.models.document import Integration
    from packages.core.models.integration_session import IntegrationSession
    from packages.core.models.mcp import MCPServer
    from packages.core.models.model_provider import PlatformModelProviderKey

logger = logging.getLogger(__name__)

_MCP_SERVER_CONTEXT_VERSION = "mcp_server_key_v1"
_MCP_SERVER_LEGACY_CONTEXT_VERSION = "mcp_server_id_v1"
_MCP_SERVER_CONTEXT_CONFIG_KEY = "_credential_context"


@dataclass
class _StoredCreds:
    ref: Optional[str]
    scheme: str
    legacy_plaintext: Optional[dict]


class CredentialService:
    def __init__(
        self,
        key_provider: KeyProvider,
        *,
        audit_sink: Optional[AuditSink] = None,
    ):
        self._kp = key_provider
        self._audit = audit_sink or NullAuditSink()

    @property
    def backend(self) -> str:
        return self._kp.backend

    def health(self) -> HealthResult:
        """Return the configured key provider's readiness state."""
        return self._kp.health()

    # ── Integration ──

    def lease_integration(
        self,
        integration: "Integration",
        *,
        requester: Requester,
        reason: str,
    ) -> dict:
        """Return the plaintext credential dict for an Integration row.

        Routes by ``credential_scheme``. For legacy rows, the JSONB
        ``credentials`` column is returned directly (no decrypt) but an
        audit event still fires so we can track the migration tail.
        """
        scheme = (integration.credential_scheme or "legacy_jsonb").lower()
        context = self._integration_context(integration)
        return self._lease(
            scheme=scheme,
            ref=integration.credential_ref,
            legacy_plaintext=integration.credentials,
            context=context,
            requester=requester,
            reason=reason,
        )

    def store_integration(
        self,
        integration: "Integration",
        plaintext: dict,
    ) -> None:
        """Encrypt + persist on the Integration row in place. Caller is
        responsible for committing the surrounding transaction.

        Mutates: ``credential_ref``, ``credential_scheme``, and clears
        ``credentials`` JSONB to prevent legacy-and-encrypted divergence.
        """
        context = self._integration_context(integration)
        ref = self._kp.encrypt(json.dumps(plaintext).encode("utf-8"), context)
        integration.credential_ref = ref
        integration.credential_scheme = self._kp.backend
        integration.credentials = {}

    # ── ChannelConfig ──

    def lease_channel_config(
        self,
        cc: "ChannelConfig",
        *,
        requester: Requester,
        reason: str,
    ) -> dict:
        scheme = (cc.credential_scheme or "legacy_jsonb").lower()
        context = self._channel_context(cc)
        return self._lease(
            scheme=scheme,
            ref=cc.credential_ref,
            legacy_plaintext=cc.credentials,
            context=context,
            requester=requester,
            reason=reason,
        )

    def store_channel_config(
        self,
        cc: "ChannelConfig",
        plaintext: dict,
    ) -> None:
        context = self._channel_context(cc)
        ref = self._kp.encrypt(json.dumps(plaintext).encode("utf-8"), context)
        cc.credential_ref = ref
        cc.credential_scheme = self._kp.backend
        cc.credentials = {}

    # ── IntegrationSession (browser cookies / storage_state) ──

    def lease_browser_session(
        self,
        session: "IntegrationSession",
        *,
        requester: Requester,
        reason: str,
    ) -> dict:
        """Decrypt a captured browser ``storage_state`` JSON blob.

        Returns the Playwright-compatible dict — ``{"cookies": [...],
        "origins": [{"origin": "...", "localStorage": [...]}]}``. Empty
        ``{}`` if the session has no ref yet (still in 'pending' capture).
        """
        scheme = (session.credential_scheme or "vault_transit").lower()
        if not session.session_state_ref:
            # Still in capture; let caller decide whether that's an error.
            return {}
        context = self._session_context(session)
        return self._lease(
            scheme=scheme,
            ref=session.session_state_ref,
            legacy_plaintext=None,
            context=context,
            requester=requester,
            reason=reason,
        )

    def store_browser_session(
        self,
        session: "IntegrationSession",
        storage_state: dict,
    ) -> None:
        """Encrypt + persist a Playwright ``storage_state`` payload on
        the session row in place. Caller commits.

        Mutates: ``session_state_ref``, ``credential_scheme``."""
        context = self._session_context(session)
        ref = self._kp.encrypt(json.dumps(storage_state).encode("utf-8"), context)
        session.session_state_ref = ref
        session.credential_scheme = self._kp.backend

    # ── Convenience: password_pair (username/password tuples) ──
    #
    # Many GUI-only platforms (douyin operator, kuaishou,
    # boss-zhipin, ...) don't expose an OAuth or API-key flow. The
    # operator stores their login as a password_pair Integration, then
    # the Playwright MCP server leases it just-in-time to log in,
    # extracts a session, and persists that session under an
    # IntegrationSession (cookie_jar) so subsequent runs reuse cookies.
    #
    # Storage shape (what gets encrypted):
    #     {"username": "...", "password": "...", "two_factor_hint": "..."}

    def store_password_pair(
        self,
        integration: "Integration",
        *,
        username: str,
        password: str,
        two_factor_hint: str | None = None,
    ) -> None:
        if not username or not password:
            raise ValueError("username and password are required")
        payload = {"username": username, "password": password}
        if two_factor_hint:
            payload["two_factor_hint"] = two_factor_hint
        self.store_integration(integration, payload)

    def lease_password_pair(
        self,
        integration: "Integration",
        *,
        requester: Requester,
        reason: str,
    ) -> dict:
        """Decrypt and return the {username, password} pair. Raises if
        the Integration was stored with a different shape."""
        plaintext = self.lease_integration(
            integration, requester=requester, reason=reason,
        )
        if "username" not in plaintext or "password" not in plaintext:
            raise ValueError(
                "Integration credentials are not a password_pair "
                "(missing username/password)."
            )
        return plaintext

    # ── MCPServer (system-level secrets — OAuth client_secret) ──
    #
    # MCPServer rows are global, not per-entity. Used to encrypt the
    # OAuth client_secret (and any future system-level secrets) so
    # Manor's prod DB never holds OAuth credentials in plaintext. The
    # client_id stays in ``default_config`` (it's not secret).

    def lease_mcp_server(
        self,
        server: "MCPServer",
        *,
        requester: Requester,
        reason: str,
    ) -> dict:
        scheme = (server.credential_scheme or "legacy_jsonb").lower()
        # Legacy path: read oauth_client_secret out of default_config
        # so existing rows keep working until they're re-seeded.
        legacy_plaintext = None
        cfg = server.default_config or {}
        if scheme == "legacy_jsonb":
            secret = cfg.get("oauth_client_secret") if isinstance(cfg, dict) else None
            if secret:
                legacy_plaintext = {"oauth_client_secret": secret}
        contexts = self._mcp_server_contexts_for_lease(server)
        return self._lease_with_contexts(
            scheme=scheme,
            ref=server.credential_ref,
            legacy_plaintext=legacy_plaintext,
            contexts=contexts,
            requester=requester,
            reason=reason,
        )

    def store_mcp_server(
        self,
        server: "MCPServer",
        plaintext: dict,
    ) -> None:
        """Encrypt + persist secrets onto the MCPServer row in place.

        Caller commits the surrounding transaction. Mutates
        ``credential_ref`` + ``credential_scheme`` and clears any
        plaintext ``oauth_client_secret`` from ``default_config``.
        """
        context = self._mcp_server_context(server)
        ref = self._kp.encrypt(json.dumps(plaintext).encode("utf-8"), context)
        server.credential_ref = ref
        server.credential_scheme = self._kp.backend
        # Strip plaintext secret from default_config — non-secret keys
        # (oauth_client_id, oauth_scopes, _oauth_source) stay.
        cfg = dict(server.default_config or {})
        cfg.pop("oauth_client_secret", None)
        cfg[_MCP_SERVER_CONTEXT_CONFIG_KEY] = _MCP_SERVER_CONTEXT_VERSION
        server.default_config = cfg

    # ── PlatformModelProviderKey (platform official model tokens) ──

    def lease_model_provider_key(
        self,
        row: "PlatformModelProviderKey",
        *,
        requester: Requester,
        reason: str,
    ) -> dict:
        scheme = (row.credential_scheme or "legacy_jsonb").lower()
        return self._lease(
            scheme=scheme,
            ref=row.credential_ref,
            legacy_plaintext=None,
            context=self._model_provider_key_context(row),
            requester=requester,
            reason=reason,
        )

    def store_model_provider_key(
        self,
        row: "PlatformModelProviderKey",
        plaintext: dict,
    ) -> None:
        context = self._model_provider_key_context(row)
        ref = self._kp.encrypt(json.dumps(plaintext).encode("utf-8"), context)
        row.credential_ref = ref
        row.credential_scheme = self._kp.backend

    # ── Convenience: cookie_jar (alias for browser_session) ──
    #
    # cookie_jar IS the Playwright ``storage_state`` blob. It's a thin
    # alias so the rest of the codebase can refer to "cookie jar" by
    # its conceptual name without callers having to know about the
    # IntegrationSession table.

    def store_cookie_jar(
        self,
        session: "IntegrationSession",
        cookie_jar: dict,
    ) -> None:
        self.store_browser_session(session, cookie_jar)

    def lease_cookie_jar(
        self,
        session: "IntegrationSession",
        *,
        requester: Requester,
        reason: str,
    ) -> dict:
        return self.lease_browser_session(session, requester=requester, reason=reason)

    # ── Internals ──

    @staticmethod
    def _session_context(session: "IntegrationSession") -> dict[str, str]:
        return {
            "kind": "integration_session",
            "entity_id": str(session.entity_id or ""),
            "session_id": str(session.id or ""),
            "provider": str(session.provider or ""),
        }

    @staticmethod
    def _integration_context(integration: "Integration") -> dict[str, str]:
        return {
            "kind": "integration",
            "entity_id": str(integration.entity_id or ""),
            "integration_id": str(integration.id or ""),
            "provider": str(integration.provider or ""),
        }

    @staticmethod
    def _channel_context(cc: "ChannelConfig") -> dict[str, str]:
        return {
            "kind": "channel_config",
            "entity_id": str(cc.entity_id or ""),
            "channel_config_id": str(cc.id or ""),
            "provider": str(cc.provider or ""),
        }

    @staticmethod
    def _mcp_server_context(server: "MCPServer") -> dict[str, str]:
        return {
            "kind": "mcp_server",
            "server_key": str(server.server_key or ""),
        }

    @staticmethod
    def _model_provider_key_context(row: "PlatformModelProviderKey") -> dict[str, str]:
        return {
            "kind": "platform_model_provider_key",
            "provider": str(row.provider or ""),
        }

    @staticmethod
    def _mcp_server_legacy_context(server: "MCPServer") -> dict[str, str]:
        return {
            "kind": "mcp_server",
            "mcp_server_id": str(server.id or ""),
            "server_key": str(server.server_key or ""),
        }

    @staticmethod
    def _mcp_server_contexts_for_lease(server: "MCPServer") -> list[dict[str, str]]:
        cfg = server.default_config if isinstance(server.default_config, dict) else {}
        context_version = cfg.get(_MCP_SERVER_CONTEXT_CONFIG_KEY)
        stable = CredentialService._mcp_server_context(server)
        legacy = CredentialService._mcp_server_legacy_context(server)

        if context_version == _MCP_SERVER_CONTEXT_VERSION:
            contexts = [stable, legacy]
        elif context_version == _MCP_SERVER_LEGACY_CONTEXT_VERSION:
            contexts = [legacy, stable]
        else:
            # Rows written before the context marker used the database id in
            # the Vault AAD. Keep the first read cheap for healthy existing
            # rows; new key-only writes carry the marker above.
            contexts = [legacy]

        unique: list[dict[str, str]] = []
        for context in contexts:
            if context not in unique:
                unique.append(context)
        return unique

    def _lease_with_contexts(
        self,
        *,
        scheme: str,
        ref: Optional[str],
        legacy_plaintext: Optional[dict],
        contexts: list[dict[str, str]],
        requester: Requester,
        reason: str,
    ) -> dict:
        last_decrypt_error: CredentialDecryptError | None = None
        for context in contexts:
            try:
                return self._lease(
                    scheme=scheme,
                    ref=ref,
                    legacy_plaintext=legacy_plaintext,
                    context=context,
                    requester=requester,
                    reason=reason,
                )
            except CredentialDecryptError as exc:
                last_decrypt_error = exc

        if last_decrypt_error is not None:
            raise last_decrypt_error
        return {}

    def _lease(
        self,
        *,
        scheme: str,
        ref: Optional[str],
        legacy_plaintext: Optional[dict],
        context: dict[str, str],
        requester: Requester,
        reason: str,
    ) -> dict:
        if scheme == "legacy_jsonb":
            self._audit.log(AuditEvent(
                credential_ref=None,
                action="lease_legacy",
                requester_kind=requester.kind,
                requester_id=requester.id,
                step_id=requester.step_id,
                reason=reason,
            ))
            return dict(legacy_plaintext or {})

        if not ref:
            raise CredentialNotFound(
                f"row marked scheme={scheme} but credential_ref is empty"
            )

        # vault_transit, dev_fernet, etc — all delegate to the provider.
        plain = self._kp.decrypt(
            ref, context, reason=reason, requester=requester,
        )
        if not plain:
            return {}
        try:
            return json.loads(plain.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CredentialError("decrypted payload is not JSON") from exc
