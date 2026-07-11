"""Health-check credential-lease error handling.

A stored credential whose ciphertext can no longer be decrypted (e.g. it
predates a Vault transit key change) makes ``lease_integration`` raise
``CredentialDecryptError`` on every health tick. That is an expected,
operator-actionable state — not an unexpected crash — so the health check
should mark the integration as needing reconnection and log concisely,
while genuinely unexpected errors stay generic.
"""

from __future__ import annotations

import logging

import pytest

import packages.core.credentials as credentials_mod
from packages.core.credentials import CredentialDecryptError
from packages.core.services.integration_health import run_and_persist_integration


class _FakeResult:
    def __init__(self, row):
        self._row = row

    def scalar_one_or_none(self):
        return self._row


class _FakeDB:
    def __init__(self, row):
        self._row = row

    async def execute(self, *args, **kwargs):
        return _FakeResult(self._row)


class _FakeIntegration:
    id = "int_1"
    provider = "slack"
    config: dict = {}


def _service_raising(exc: Exception):
    class _Service:
        def lease_integration(self, row, **kwargs):
            raise exc

    return lambda: _Service()


@pytest.mark.asyncio
async def test_decrypt_failure_marks_integration_needs_reconnect(monkeypatch, caplog):
    monkeypatch.setattr(
        credentials_mod,
        "get_credential_service",
        _service_raising(CredentialDecryptError("cipher: message authentication failed")),
    )

    with caplog.at_level(logging.ERROR):
        result = await run_and_persist_integration(_FakeDB(_FakeIntegration()), "int_1")

    assert result["ok"] is False
    assert result["needs_reconnect"] is True
    assert "reconnect" in result["detail"].lower()
    # Expected, recurring state — must not spam a full traceback at ERROR level.
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


@pytest.mark.asyncio
async def test_unexpected_lease_error_stays_generic(monkeypatch):
    monkeypatch.setattr(
        credentials_mod,
        "get_credential_service",
        _service_raising(RuntimeError("vault unreachable")),
    )

    result = await run_and_persist_integration(_FakeDB(_FakeIntegration()), "int_1")

    assert result["ok"] is False
    assert "needs_reconnect" not in result
