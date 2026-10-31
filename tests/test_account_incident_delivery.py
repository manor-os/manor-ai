from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import text

from packages.core.models.base import generate_ulid
from packages.core.models.document import Integration
from packages.core.models.user import OAuthAccount, User, UserMembership
from packages.core.models.workspace import Workspace, WorkspaceStaff
from packages.core.services import account_incident_delivery as delivery
from packages.core.services.account_incidents import INCIDENT_KEY, open_incident

NOW = datetime(2026, 10, 22, 9, 0, tzinfo=timezone.utc)


class _SessionContext:
    def __init__(self, db_session) -> None:
        self.db_session = db_session

    async def __aenter__(self):
        return self.db_session

    async def __aexit__(self, *_args):
        return False


@pytest.fixture(autouse=True)
def _use_test_session(db_session, monkeypatch):
    monkeypatch.setattr(delivery, "async_session", lambda: _SessionContext(db_session))


async def _member(db, entity_id: str, *, role: str = "member") -> str:
    user = User(id=generate_ulid(), entity_id=entity_id, email=f"u-{generate_ulid()}@example.com",
                password_hash="x", role=role, status="active")
    db.add_all([user, UserMembership(user_id=user.id, entity_id=entity_id, role=role, status="active")])
    await db.flush()
    return user.id


def _for_entity(sent, entity_id: str):
    return [c.kwargs for c in sent.await_args_list if c.kwargs["entity_id"] == entity_id]


@pytest.mark.asyncio
async def test_an_incident_notifies_affected_owners_and_the_connector_once(db_session, monkeypatch):
    entity_id = generate_ulid()
    connector = await _member(db_session, entity_id)
    owner = await _member(db_session, entity_id)
    workspace = Workspace(id=generate_ulid(), entity_id=entity_id, name="Growth", status="active")
    db_session.add_all([
        workspace,
        WorkspaceStaff(workspace_id=workspace.id, user_id=owner, role="owner", status="active"),
    ])
    integration = Integration(
        id=generate_ulid(), entity_id=entity_id, provider="linkedin", status="active",
        owner_user_id=connector,
        config=open_incident({"last_health_check": {"ok": False}}, reason="auth_failed", now=NOW),
    )
    db_session.add(integration)
    await db_session.commit()
    monkeypatch.setattr(delivery, "_workspace_providers", AsyncMock(return_value={"linkedin"}))
    sent = AsyncMock()
    monkeypatch.setattr(delivery, "notify", sent)

    await delivery.deliver_account_incidents(now=NOW)
    calls = _for_entity(sent, entity_id)
    assert sorted(call["user_id"] for call in calls) == sorted([connector, owner])
    call = calls[0]
    assert call["type"] == "integration_account_issue" and call["severity"] == "warn"
    assert "Growth" in call["body"] and call["link"] == "/integrations"
    assert "may be affected until you reconnect" in call["body"]

    await db_session.refresh(integration)
    assert integration.config[INCIDENT_KEY]["notified_at"] == NOW.isoformat()
    sent.reset_mock()
    await delivery.deliver_account_incidents(now=NOW)
    assert _for_entity(sent, entity_id) == []


@pytest.mark.asyncio
async def test_a_recovered_account_is_not_notified(db_session, monkeypatch):
    entity_id = generate_ulid()
    connector = await _member(db_session, entity_id)
    integration = Integration(
        id=generate_ulid(), entity_id=entity_id, provider="slack", status="active",
        owner_user_id=connector,
        config=open_incident(
            {"last_health_check": {"ok": True, "checked_at": (NOW + timedelta(minutes=5)).isoformat()}},
            reason="x", now=NOW,
        ),
    )
    db_session.add(integration)
    await db_session.commit()
    sent = AsyncMock()
    monkeypatch.setattr(delivery, "notify", sent)
    await delivery.deliver_account_incidents(now=NOW + timedelta(minutes=10))
    assert _for_entity(sent, entity_id) == []
    await db_session.refresh(integration)
    assert INCIDENT_KEY not in integration.config


@pytest.mark.asyncio
async def test_a_pass_from_before_the_incident_does_not_close_it(db_session, monkeypatch):
    """A Nango auth failure opens an incident without a new check: an older pass must not hide it."""
    entity_id = generate_ulid()
    connector = await _member(db_session, entity_id)
    integration = Integration(
        id=generate_ulid(), entity_id=entity_id, provider="linkedin", status="active",
        owner_user_id=connector,
        config=open_incident(
            {"last_health_check": {"ok": True, "checked_at": (NOW - timedelta(hours=3)).isoformat()}},
            reason="nango_auth_failed", now=NOW,
        ),
    )
    db_session.add(integration)
    await db_session.commit()
    sent = AsyncMock()
    monkeypatch.setattr(delivery, "notify", sent)
    await delivery.deliver_account_incidents(now=NOW)
    assert [c["user_id"] for c in _for_entity(sent, entity_id)] == [connector]
    await db_session.refresh(integration)
    assert integration.config[INCIDENT_KEY]["notified_at"] == NOW.isoformat()


@pytest.mark.asyncio
async def test_only_workspaces_that_use_the_provider_are_named_and_notified(db_session, monkeypatch):
    """Two unrelated Workspaces: a Gmail incident reaches only the one using Gmail."""
    entity_id = generate_ulid()
    connector = await _member(db_session, entity_id)
    growth_owner = await _member(db_session, entity_id)
    support_owner = await _member(db_session, entity_id)
    growth = Workspace(id=generate_ulid(), entity_id=entity_id, name="Growth", status="active")
    support = Workspace(id=generate_ulid(), entity_id=entity_id, name="Support", status="active")
    db_session.add_all([
        growth, support,
        WorkspaceStaff(workspace_id=growth.id, user_id=growth_owner, role="owner", status="active"),
        WorkspaceStaff(workspace_id=support.id, user_id=support_owner, role="owner", status="active"),
        Integration(id=generate_ulid(), entity_id=entity_id, provider="gmail", status="active",
                    owner_user_id=connector,
                    config=open_incident({"last_health_check": {"ok": False}}, reason="x", now=NOW)),
    ])
    await db_session.commit()
    providers = {growth.id: {"linkedin"}, support.id: {"gmail"}}

    async def by_workspace(_db, workspace, _user_id):
        return providers[workspace.id]

    monkeypatch.setattr(delivery, "_workspace_providers", by_workspace)
    sent = AsyncMock()
    monkeypatch.setattr(delivery, "notify", sent)
    await delivery.deliver_account_incidents(now=NOW)
    calls = _for_entity(sent, entity_id)
    assert sorted(c["user_id"] for c in calls) == sorted([connector, support_owner])
    assert "Support" in calls[0]["body"]
    assert "Growth" not in calls[0]["body"]


@pytest.mark.asyncio
async def test_a_database_error_scanning_one_workspace_leaves_the_others(db_session, monkeypatch):
    entity_id = generate_ulid()
    owner = await _member(db_session, entity_id)
    workspaces = [Workspace(id=generate_ulid(), entity_id=entity_id, name=f"W{n}", status="active")
                  for n in range(2)]
    db_session.add_all([
        *workspaces,
        *[WorkspaceStaff(workspace_id=w.id, user_id=owner, role="owner", status="active") for w in workspaces],
    ])
    await db_session.commit()
    scanned: list[str] = []

    async def scan(db, workspace, _user_id):
        scanned.append(workspace.name)
        if len(scanned) == 1:
            await db.execute(text("SELECT 1/0"))
        return {"gmail"}

    monkeypatch.setattr(delivery, "_workspace_providers", scan)

    result = await delivery.scan_entity_workspaces(db_session, entity_id, NOW)

    assert [(name, owners, providers) for name, owners, providers in result] == [(scanned[1], [owner], {"gmail"})]


@pytest.mark.asyncio
async def test_one_run_scans_each_workspace_once_for_all_incidents_in_an_entity(db_session, monkeypatch):
    entity_id = generate_ulid()
    owner = await _member(db_session, entity_id)
    workspaces = [Workspace(id=generate_ulid(), entity_id=entity_id, name=f"W{n}", status="active")
                  for n in range(2)]
    db_session.add_all([
        *workspaces,
        *[WorkspaceStaff(workspace_id=w.id, user_id=owner, role="owner", status="active") for w in workspaces],
        *[Integration(id=generate_ulid(), entity_id=entity_id, provider=provider, status="active",
                      config=open_incident({}, reason="x", now=NOW)) for provider in ("gmail", "slack")],
    ])
    await db_session.commit()
    scanned: list[str] = []

    async def scan(_db, workspace, _user_id):
        scanned.append(workspace.id)
        return {"gmail", "slack"}

    monkeypatch.setattr(delivery, "_workspace_providers", scan)
    monkeypatch.setattr(delivery, "notify", AsyncMock())
    await delivery.deliver_account_incidents(now=NOW)
    ours = {w.id for w in workspaces}
    assert sorted(s for s in scanned if s in ours) == sorted(ours)


@pytest.mark.asyncio
async def test_a_failing_delivery_is_retried_then_given_up_after_three_attempts(db_session, monkeypatch):
    entity_id = generate_ulid()
    connector = await _member(db_session, entity_id)
    integration = Integration(
        id=generate_ulid(), entity_id=entity_id, provider="slack", status="active",
        owner_user_id=connector,
        config=open_incident({"last_health_check": {"ok": False}}, reason="x", now=NOW),
    )
    db_session.add(integration)
    await db_session.commit()
    sent = AsyncMock(side_effect=RuntimeError("channel down"))
    monkeypatch.setattr(delivery, "notify", sent)

    for attempt in (1, 2):
        await delivery.deliver_account_incidents(now=NOW)
        await db_session.refresh(integration)
        incident = integration.config[INCIDENT_KEY]
        assert incident["notified_at"] is None and incident["delivery_attempts"] == attempt

    await delivery.deliver_account_incidents(now=NOW)
    await db_session.refresh(integration)
    incident = integration.config[INCIDENT_KEY]
    assert incident["notified_at"] == NOW.isoformat() and incident["delivery_failed"] is True

    sent.reset_mock()
    await delivery.deliver_account_incidents(now=NOW)
    assert _for_entity(sent, entity_id) == []


async def _personal_account(db, user_id: str, profile: dict) -> OAuthAccount:
    account = OAuthAccount(id=generate_ulid(), user_id=user_id, provider="google_calendar",
                           provider_user_id=f"g-{generate_ulid()}", profile=profile)
    db.add(account)
    await db.commit()
    return account


@pytest.mark.asyncio
async def test_a_personal_oauth_incident_notifies_only_its_owner_once(db_session, monkeypatch):
    entity_id = generate_ulid()
    owner = await _member(db_session, entity_id)
    admin = await _member(db_session, entity_id, role="owner")
    workspace = Workspace(id=generate_ulid(), entity_id=entity_id, name="Growth", status="active")
    db_session.add_all([
        workspace,
        WorkspaceStaff(workspace_id=workspace.id, user_id=admin, role="owner", status="active"),
    ])
    account = await _personal_account(db_session, owner, open_incident({}, reason="reauth_required", now=NOW))
    monkeypatch.setattr(delivery, "_workspace_providers", AsyncMock(return_value={"google_calendar"}))
    sent = AsyncMock()
    monkeypatch.setattr(delivery, "notify", sent)

    await delivery.deliver_oauth_account_incidents(now=NOW)
    calls = _for_entity(sent, entity_id)
    assert [call["user_id"] for call in calls] == [owner]
    call = calls[0]
    assert call["type"] == "integration_account_issue" and call["severity"] == "warn"
    assert call["link"] == "/integrations"
    assert call["title"] == "Google Calendar needs reconnecting"
    assert call["body"] == ("Your Google Calendar account stopped working. Anything that uses it "
                            "may be affected until you reconnect.")
    assert call["idempotency_key"] == f"oauth-incident:{account.id}:{NOW.isoformat()}"

    await db_session.refresh(account)
    assert account.profile[INCIDENT_KEY]["notified_at"] == NOW.isoformat()
    sent.reset_mock()
    await delivery.deliver_oauth_account_incidents(now=NOW)
    assert _for_entity(sent, entity_id) == []


@pytest.mark.asyncio
async def test_a_recovered_personal_oauth_account_closes_silently(db_session, monkeypatch):
    entity_id = generate_ulid()
    owner = await _member(db_session, entity_id)
    account = await _personal_account(db_session, owner, open_incident(
        {"last_health_check": {"ok": True, "checked_at": (NOW + timedelta(minutes=5)).isoformat()}},
        reason="reauth_required", now=NOW,
    ))
    sent = AsyncMock()
    monkeypatch.setattr(delivery, "notify", sent)
    await delivery.deliver_oauth_account_incidents(now=NOW + timedelta(minutes=10))
    assert _for_entity(sent, entity_id) == []
    await db_session.refresh(account)
    assert INCIDENT_KEY not in account.profile


@pytest.mark.asyncio
async def test_an_inactive_owner_gets_nothing_and_the_incident_is_not_retried(db_session, monkeypatch):
    entity_id = generate_ulid()
    owner = await _member(db_session, entity_id)
    (await db_session.get(User, owner)).status = "inactive"
    account = await _personal_account(db_session, owner, open_incident({}, reason="reauth_required", now=NOW))
    sent = AsyncMock()
    monkeypatch.setattr(delivery, "notify", sent)

    await delivery.deliver_oauth_account_incidents(now=NOW)
    assert _for_entity(sent, entity_id) == []
    await db_session.refresh(account)
    incident = account.profile[INCIDENT_KEY]
    assert incident["notified_at"] == NOW.isoformat() and incident["delivery_failed"] is True
