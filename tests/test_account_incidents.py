from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from packages.core.constants.notification_types import EVENTS_BY_KIND
from packages.core.services.account_incidents import (
    INCIDENT_KEY, apply_health_result, close_incident, open_incident,
)
from packages.core.services.oauth_account_credentials import (
    mark_oauth_account_credential_reconnect_required,
)
from packages.core.tasks.oauth_refresh import (
    _mark_integration_reauth_required, _mark_oauth_account_reauth_required,
)

NOW = datetime(2026, 10, 22, 9, 0, tzinfo=timezone.utc)
LATER = datetime(2026, 10, 22, 10, 0, tzinfo=timezone.utc)


def test_reminders_respect_quiet_hours_and_account_issues_are_warnings():
    assert EVENTS_BY_KIND["task_hitl_reminder"]["severity"] == "info"
    issue = EVENTS_BY_KIND["integration_account_issue"]
    assert issue["severity"] == "warn" and issue["category"] == "system"


def test_a_credential_failure_opens_one_incident_and_a_repeat_keeps_it():
    failed = {"ok": False, "detail": "401", "reason_code": "auth_failed"}
    cfg = apply_health_result({}, failed, now=NOW, credential_failure=True)
    incident = cfg[INCIDENT_KEY]
    assert incident == {"opened_at": NOW.isoformat(), "reason": "auth_failed", "notified_at": None}
    again = apply_health_result(cfg, {"ok": False, "detail": "401"}, now=LATER, credential_failure=True)
    assert again[INCIDENT_KEY] == incident


def test_a_transient_failure_neither_opens_nor_closes():
    timeout = {"ok": False, "detail": "Network error: timeout"}
    assert INCIDENT_KEY not in apply_health_result({}, timeout, now=NOW, credential_failure=False)
    opened = apply_health_result({}, {"ok": False, "detail": "x"}, now=NOW, credential_failure=True)
    assert apply_health_result(opened, timeout, now=LATER, credential_failure=False) == opened


def test_a_passing_check_closes_the_incident_and_never_opens_one():
    opened = apply_health_result({}, {"ok": False, "detail": "x"}, now=NOW, credential_failure=True)
    assert INCIDENT_KEY not in apply_health_result(opened, {"ok": True}, now=LATER, credential_failure=False)
    assert INCIDENT_KEY not in apply_health_result({}, {"ok": True}, now=NOW, credential_failure=False)


def test_an_unknown_result_changes_nothing():
    opened = apply_health_result({}, {"ok": False, "detail": "x"}, now=NOW, credential_failure=True)
    assert apply_health_result(opened, {"detail": "no ok field"}, now=LATER, credential_failure=True) == opened


@pytest.mark.parametrize(("probe", "opens"), [
    ({"ok": False, "detail": "Network error: timeout"}, False),
    ({"ok": False, "detail": "HTTP 503 from provider"}, False),
    ({"ok": False, "detail": "HTTP 429 rate limited"}, False),
    ({"ok": False, "detail": "Slack token rejected (HTTP 401)"}, True),
    ({"ok": False, "detail": "stale", "needs_reconnect": True}, True),
])
@pytest.mark.asyncio
async def test_only_a_credential_failure_in_a_health_check_opens_an_incident(monkeypatch, probe, opens):
    import packages.core.credentials as credentials_mod
    import packages.core.services.integration_health as health_mod

    class _Service:
        def lease_integration(self, row, **kwargs):
            return {"token": "t"}

    async def _none(*_args, **_kwargs):
        return None

    async def _probe(*_args, **_kwargs):
        return dict(probe)

    row = SimpleNamespace(id="int_1", provider="slack", status="active", updated_at=NOW, config={})

    class _DB:
        async def execute(self, *_args, **_kwargs):
            return SimpleNamespace(scalar_one_or_none=lambda: row)

        async def flush(self):
            return None

    monkeypatch.setattr(credentials_mod, "get_credential_service", lambda: _Service())
    monkeypatch.setattr(health_mod, "release_read_transaction", AsyncMock(return_value=False))
    monkeypatch.setattr(health_mod, "_resolve_nango_runtime_credentials", AsyncMock(return_value={}))
    monkeypatch.setattr(health_mod, "_wiring_ctx_for_integration", _none)
    monkeypatch.setattr(health_mod, "run_test", _probe)

    await health_mod.run_and_persist_integration(_DB(), "int_1")

    assert (INCIDENT_KEY in row.config) is opens
    assert row.config["last_health_check"]["ok"] is False


@pytest.mark.parametrize(("probe", "already_open", "open_after"), [
    ({"ok": False, "detail": "Gmail token rejected (HTTP 401)"}, False, True),
    ({"ok": False, "detail": "Network error: timeout"}, False, False),
    ({"ok": False, "detail": "Network error: timeout"}, True, True),
    ({"ok": True, "detail": "ok"}, True, False),
])
@pytest.mark.asyncio
async def test_a_personal_oauth_health_check_opens_and_closes_its_incident(
    monkeypatch, probe, already_open, open_after,
):
    import packages.core.services.integration_health as health_mod
    import packages.core.services.oauth_account_credentials as oauth_credentials_mod

    async def _probe(*_args, **_kwargs):
        return dict(probe)

    profile = open_incident({}, reason="reauth_required", now=NOW) if already_open else {}
    row = SimpleNamespace(id="oauth_1", provider="gmail", updated_at=NOW, profile=profile)

    class _DB:
        async def execute(self, *_args, **_kwargs):
            return SimpleNamespace(scalar_one_or_none=lambda: row)

        async def flush(self):
            return None

    monkeypatch.setattr(oauth_credentials_mod, "lease_oauth_account_tokens", lambda *_a, **_k: {"access_token": "t"})
    monkeypatch.setattr(health_mod, "release_read_transaction", AsyncMock(return_value=False))
    monkeypatch.setattr(health_mod, "run_test", _probe)

    await health_mod.run_and_persist_oauth(_DB(), "oauth_1")

    assert (INCIDENT_KEY in row.profile) is open_after
    assert row.profile["last_health_check"]["ok"] is probe["ok"]


@pytest.mark.parametrize(("code", "credential"), [
    ("credentials_rejected", True),
    ("permission_denied", True),
    ("provider_unavailable", False),
    ("webhook_config_missing", False),
])
def test_nango_auth_codes_count_as_credential_failures(code, credential):
    from packages.core.services.integration_health import is_credential_failure

    assert is_credential_failure({"ok": False, "detail": "x", "reason_code": code}) is credential


def test_reauth_markers_open_and_reconnect_closes():
    cfg = open_incident({"a": 1}, reason="reauth_required", now=NOW)
    assert cfg["a"] == 1 and cfg[INCIDENT_KEY]["reason"] == "reauth_required"
    assert open_incident(cfg, reason="other", now=LATER)[INCIDENT_KEY]["reason"] == "reauth_required"
    assert close_incident(cfg) == {"a": 1}


def test_reauth_markers_open_an_incident():
    integration = SimpleNamespace(config={}, credentials={}, token_expires_at=None, provider="linkedin")
    _mark_integration_reauth_required(integration, "linkedin", {"error": "invalid_grant"})
    assert integration.config[INCIDENT_KEY]["reason"] == "reauth_required"

    account = SimpleNamespace(profile={}, token_expires_at=None, provider="google")
    _mark_oauth_account_reauth_required(account, "google", {"error": "invalid_grant"})
    assert account.profile[INCIDENT_KEY]["reason"] == "reauth_required"

    broken = SimpleNamespace(profile={}, token_expires_at=None)
    mark_oauth_account_credential_reconnect_required(broken, provider="google")
    assert broken.profile[INCIDENT_KEY]["reason"] == "credential_decrypt_failed"


def test_the_digest_is_an_info_kind_users_can_configure():
    digest = EVENTS_BY_KIND["workspace_digest"]
    assert digest["severity"] == "info" and digest["category"] == "system"
