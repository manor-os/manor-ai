"""Local Fernet KeyProvider — for development without Vault.

Uses the cryptography Fernet primitive (AES-128-CBC + HMAC-SHA256). The
key comes from ``DEV_CREDENTIAL_KEY``; if missing, a random key is
generated on first use and a loud warning printed. The generated key is
NOT persisted — restarting the process makes existing ciphertexts
unreadable, which is appropriate for ephemeral dev usage.

For production: use ``VaultKeyProvider`` instead.

Context binding: Fernet has no built-in associated-data. We HMAC the
context into the AAD-equivalent prefix so decrypt with a different
context fails with InvalidToken.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

from packages.core.credentials.audit import AuditEvent, AuditSink, NullAuditSink
from packages.core.credentials.base import (
    CredentialDecryptError,
    CredentialNotFound,
    HealthResult,
    Requester,
)

logger = logging.getLogger(__name__)


def _ctx_tag(context: dict[str, str], key: bytes) -> str:
    """16-byte context tag, base32-encoded — small enough to inline in
    the ref but enough entropy to detect cross-row replays."""
    canonical = json.dumps(context, sort_keys=True, separators=(",", ":"))
    h = hmac.new(key, canonical.encode("utf-8"), hashlib.sha256).digest()[:16]
    return base64.b32encode(h).decode("ascii").rstrip("=")


class DevKeyProvider:
    backend = "dev_fernet"

    def __init__(
        self,
        key: Optional[str] = None,
        *,
        audit_sink: Optional[AuditSink] = None,
    ):
        if key:
            try:
                self._fernet = Fernet(key.encode("ascii"))
            except (ValueError, TypeError) as exc:
                raise ValueError(
                    "DEV_CREDENTIAL_KEY must be a urlsafe-base64 32-byte Fernet key"
                ) from exc
            self._key_bytes = base64.urlsafe_b64decode(key.encode("ascii"))
        else:
            new_key = Fernet.generate_key()
            logger.warning(
                "DevKeyProvider: no DEV_CREDENTIAL_KEY set — generated an "
                "ephemeral key. Existing ciphertexts will be unreadable "
                "after process restart. Set DEV_CREDENTIAL_KEY=%s to persist.",
                new_key.decode("ascii"),
            )
            self._fernet = Fernet(new_key)
            self._key_bytes = base64.urlsafe_b64decode(new_key)

        self._audit = audit_sink or NullAuditSink()

    def encrypt(self, plaintext: bytes, context: dict[str, str]) -> str:
        if not plaintext:
            return "dev:empty:"
        tag = _ctx_tag(context, self._key_bytes)
        ct = self._fernet.encrypt(plaintext).decode("ascii")
        ref = f"dev:v1:{tag}:{ct}"
        self._audit.log(AuditEvent(
            credential_ref=ref,
            action="encrypt",
            requester_kind=None, requester_id=None,
            step_id=None, reason=None,
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

        if ref == "dev:empty:":
            return b""
        if not ref.startswith("dev:v1:"):
            raise CredentialDecryptError(f"unrecognised dev ref: {ref[:20]}")
        try:
            _, _, tag, ct = ref.split(":", 3)
        except ValueError as exc:
            raise CredentialDecryptError("malformed dev ref") from exc

        expected_tag = _ctx_tag(context, self._key_bytes)
        if not hmac.compare_digest(tag, expected_tag):
            raise CredentialDecryptError("context mismatch")

        try:
            return self._fernet.decrypt(ct.encode("ascii"))
        except InvalidToken as exc:
            raise CredentialDecryptError("invalid fernet token") from exc

    def rotate(self) -> None:
        # Fernet has no in-place rotate; for dev we just log. Real rotation
        # happens via re-encrypting all rows with a new key.
        logger.info("DevKeyProvider.rotate is a no-op (use vault for real rotation)")

    def health(self) -> HealthResult:
        return HealthResult(ok=True, detail="dev fernet")
