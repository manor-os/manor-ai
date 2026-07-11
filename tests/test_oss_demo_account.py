from __future__ import annotations

import pytest
from sqlalchemy import func, select

from packages.core.models.user import User
from packages.core.services.auth_service import authenticate_user
from packages.core.services.demo_account import demo_account_config, ensure_demo_account

pytestmark = pytest.mark.oss


@pytest.mark.asyncio
async def test_oss_demo_account_seed_creates_login(db_session, monkeypatch):
    monkeypatch.setenv("DEPLOYMENT_MODE", "oss")
    monkeypatch.setenv("MANOR_DEMO_ACCOUNT_ENABLED", "true")
    monkeypatch.setenv("MANOR_DEMO_ACCOUNT_EMAIL", "demo-seed@test.local")
    monkeypatch.setenv("MANOR_DEMO_ACCOUNT_PASSWORD", "demo-pass-123")
    monkeypatch.setenv("MANOR_DEMO_ACCOUNT_ENTITY_NAME", "Demo Seed Org")

    result = await ensure_demo_account(db_session)
    await db_session.commit()

    assert result == {
        "enabled": True,
        "created": True,
        "email": "demo-seed@test.local",
    }

    user = (
        await db_session.execute(
            select(User).where(func.lower(User.email) == "demo-seed@test.local")
        )
    ).scalar_one()
    assert user.role == "owner"
    assert user.status == "active"
    assert user.preferences["demo_account"] is True

    authenticated = await authenticate_user(
        db_session,
        email="demo-seed@test.local",
        password="demo-pass-123",
    )
    assert authenticated is not None
    assert authenticated.id == user.id


@pytest.mark.asyncio
async def test_demo_account_disabled_in_cloud_even_when_env_enabled(db_session, monkeypatch):
    monkeypatch.setenv("DEPLOYMENT_MODE", "cloud")
    monkeypatch.setenv("MANOR_DEMO_ACCOUNT_ENABLED", "true")
    monkeypatch.setenv("MANOR_DEMO_ACCOUNT_EMAIL", "cloud-demo@test.local")
    monkeypatch.setenv("MANOR_DEMO_ACCOUNT_PASSWORD", "cloud-demo-pass")

    assert demo_account_config()["enabled"] is False

    result = await ensure_demo_account(db_session)
    assert result == {"enabled": False, "created": False, "email": None}
    user = (
        await db_session.execute(
            select(User).where(func.lower(User.email) == "cloud-demo@test.local")
        )
    ).scalar_one_or_none()
    assert user is None
