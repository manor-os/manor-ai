from __future__ import annotations

import json
from types import SimpleNamespace

import jwt
import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException
from starlette.requests import Request

from apps.api.main import _cors_allowed_origins, redact_sensitive_log_text
from apps.api.routers.filesystem import _signed_file_response_metadata
from packages.core.credentials import Requester
from packages.core.credentials.audit import NullAuditSink
from packages.core.credentials.dev_provider import DevKeyProvider
from packages.core.credentials.service import CredentialService
from packages.core.services import auth_rate_limit
from packages.core.services.auth_service import create_access_token, validate_password_strength
from packages.core.services.email_verification_service import _generate_code
from packages.core.services.upload_security import (
    UploadSecurityError,
    inspect_upload_content,
    validate_upload_header,
)
from packages.core.services.oauth_account_credentials import clear_oauth_account_tokens


def test_access_tokens_are_short_lived_unique_and_strictly_scoped():
    first = create_access_token(
        "user-1",
        "entity-1",
        "owner",
        remember=True,
        token_version=7,
        mfa_authenticated=True,
    )
    second = create_access_token("user-1", "entity-1", "owner", remember=True, token_version=7)

    claims = jwt.decode(
        first,
        options={"verify_signature": False, "verify_aud": False},
    )
    assert claims["iss"] == "manor-api"
    assert claims["aud"] == "manor-api"
    assert claims["token_version"] == 7
    assert claims["amr"] == ["mfa"]
    assert claims["exp"] - claims["iat"] <= 60 * 60
    assert claims["jti"] != jwt.decode(
        second,
        options={"verify_signature": False, "verify_aud": False},
    )["jti"]


def test_cloud_jwt_secret_gate_rejects_short_and_shipped_values():
    from packages.core.config import is_insecure_jwt_secret

    assert is_insecure_jwt_secret("")
    assert is_insecure_jwt_secret("short")
    assert is_insecure_jwt_secret("replace-with-openssl-rand-hex-32")
    assert not is_insecure_jwt_secret("a" * 32)


def test_production_password_policy_rejects_short_and_common_passwords(monkeypatch):
    monkeypatch.setenv("PASSWORD_POLICY_ENFORCED", "true")
    with pytest.raises(ValueError, match="at least 12"):
        validate_password_strength("short")
    with pytest.raises(ValueError, match="commonly breached"):
        validate_password_strength("password1234")
    validate_password_strength("correct horse battery staple")


def test_email_verifiers_are_alphanumeric_and_do_not_regress_to_six_digits():
    for _ in range(100):
        code = _generate_code()
        assert len(code) == 8
        assert code.isalnum()
        assert any(character.isalpha() for character in code)
        assert any(character.isdigit() for character in code)


def test_sensitive_log_redaction_covers_codes_tokens_and_query_strings():
    value = (
        "GET /callback?code=oauth-code&access_token=access-value "
        "verification code for user@example.com: A2B3C4D5"
    )
    redacted = redact_sensitive_log_text(value)
    assert "oauth-code" not in redacted
    assert "access-value" not in redacted
    assert "A2B3C4D5" not in redacted
    assert redacted.count("<redacted>") == 3


def test_cloud_cors_defaults_to_the_first_party_app(monkeypatch):
    monkeypatch.setenv("DEPLOYMENT_MODE", "cloud")
    monkeypatch.delenv("CORS_ALLOWED_ORIGINS", raising=False)
    monkeypatch.setenv("APP_URL", "https://app.manorai.xyz/")
    assert _cors_allowed_origins() == ["https://app.manorai.xyz"]


@pytest.mark.parametrize("origin", ["*", "null", "https://*.example.com", "http://app.example.com"])
def test_cloud_cors_rejects_unsafe_configured_origins(monkeypatch, origin):
    monkeypatch.setenv("DEPLOYMENT_MODE", "cloud")
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", origin)
    with pytest.raises(RuntimeError, match="CORS|HTTPS"):
        _cors_allowed_origins()


def test_active_content_is_forced_to_download(tmp_path):
    path = tmp_path / "payload.svg"
    path.write_text("<svg/>", encoding="utf-8")
    media_type, headers = _signed_file_response_metadata(path, "image/svg+xml")
    assert media_type == "application/octet-stream"
    assert headers["Content-Disposition"].startswith("attachment;")
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert "sandbox" in headers["Content-Security-Policy"]


def test_upload_gate_rejects_disguised_executables_and_bad_magic():
    with pytest.raises(UploadSecurityError, match="Executable"):
        validate_upload_header(filename="photo.png", header=b"MZ" + b"0" * 20)
    with pytest.raises(UploadSecurityError, match="does not match"):
        validate_upload_header(filename="report.pdf", header=b"not a pdf")
    assert validate_upload_header(
        filename="photo.png",
        header=b"\x89PNG\r\n\x1a\n" + b"0" * 20,
        declared_content_type="image/png",
    ) == "image/png"


@pytest.mark.asyncio
async def test_required_malware_scanner_fails_closed(monkeypatch):
    from packages.core.services import upload_security

    monkeypatch.setattr(upload_security, "_av_required", lambda: True)
    monkeypatch.delenv("CLAMAV_HOST", raising=False)
    with pytest.raises(UploadSecurityError) as raised:
        await inspect_upload_content(b"hello", filename="note.txt", declared_content_type="text/plain")
    assert raised.value.status_code == 503


def test_oauth_account_tokens_encrypt_and_clear_legacy_columns():
    service = CredentialService(
        DevKeyProvider(key=Fernet.generate_key().decode("ascii")),
        audit_sink=NullAuditSink(),
    )
    account = SimpleNamespace(
        id="oauth-1",
        user_id="user-1",
        provider="google",
        access_token="plain-access",
        refresh_token="plain-refresh",
        credential_ref=None,
        credential_scheme="legacy_columns",
    )
    service.store_oauth_account(
        account,
        {"access_token": "secret-access", "refresh_token": "secret-refresh"},
    )
    assert account.access_token is None
    assert account.refresh_token is None
    assert account.credential_ref.startswith("dev:v1:")
    assert "secret-access" not in account.credential_ref
    assert service.lease_oauth_account(
        account,
        requester=Requester(kind="test", id="casa"),
        reason="security-test",
    ) == {"access_token": "secret-access", "refresh_token": "secret-refresh"}

    clear_oauth_account_tokens(account)
    assert account.credential_ref is None
    assert account.access_token is None
    assert account.refresh_token is None


@pytest.mark.asyncio
async def test_login_rate_limit_uses_account_failure_budget(monkeypatch):
    monkeypatch.setenv("AUTH_RATE_LIMIT_ENABLED", "true")
    monkeypatch.setattr(auth_rate_limit, "_ACCOUNT_LIMIT", 2)
    auth_rate_limit._MEMORY_BUCKETS.clear()

    async def no_redis(*_args, **_kwargs):
        return None

    monkeypatch.setattr(auth_rate_limit, "_redis_check", no_redis)
    monkeypatch.setattr(auth_rate_limit, "_redis_increment", no_redis)
    assert (await auth_rate_limit.record_login_failure("user@example.com", "192.0.2.1")).allowed
    assert (await auth_rate_limit.record_login_failure("user@example.com", "192.0.2.1")).allowed
    decision = await auth_rate_limit.check_login_allowed("user@example.com", "192.0.2.1")
    assert not decision.allowed
    assert decision.retry_after > 0


def test_login_rate_limit_fallback_has_bounded_cardinality(monkeypatch):
    auth_rate_limit._MEMORY_BUCKETS.clear()
    monkeypatch.setattr(auth_rate_limit, "_MEMORY_MAX_BUCKETS", 3)
    monkeypatch.setattr(auth_rate_limit, "_MEMORY_LAST_CLEANUP", 0.0)
    for index in range(10):
        auth_rate_limit._memory_increment(
            f"account:{index}",
            limit=10,
            window=900,
        )
    assert len(auth_rate_limit._MEMORY_BUCKETS) == 3
    assert list(auth_rate_limit._MEMORY_BUCKETS) == [
        "account:7",
        "account:8",
        "account:9",
    ]


@pytest.mark.asyncio
async def test_code_monitor_never_executes_on_the_api_host(monkeypatch):
    from packages.core.ai.tools import code_tool

    def forbidden(*_args, **_kwargs):
        raise AssertionError("host subprocess execution must not be reachable")

    monkeypatch.setattr(code_tool.subprocess, "Popen", forbidden)
    response = json.loads(await code_tool._handle_monitor_start({"command": "id"}, "entity-1"))
    assert "unavailable on the API host" in response["error"]


