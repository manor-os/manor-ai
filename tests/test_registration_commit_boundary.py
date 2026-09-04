"""Registration must be durable before issuing a token or verification code."""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi import Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from apps.api.routers import auth
from packages.core.models.base import generate_ulid
from packages.core.models.staff import Staff
from packages.core.models.user import Entity, User, UserMembership
from packages.core.services.auth_service import register_user, verify_password


async def _registration(db, monkeypatch, mode):
    email = f"commit-{generate_ulid().lower()}@example.test"
    req = auth.RegisterRequest(
        email=email,
        username="Commit boundary",
        password="new-password-123",
        entity_name="Commit boundary",
    )
    monkeypatch.setattr(auth, "CAPTCHA_ENABLED", False)
    monkeypatch.setattr(auth, "_CLOUD_FEATURES_ENABLED", mode in {"verification", "resend"})
    monkeypatch.setattr(auth, "create_verification", AsyncMock(return_value="12345678"))
    monkeypatch.setattr(auth, "send_verification_email", AsyncMock())
    if mode == "staff_invite":
        entity = Entity(name="Inviting entity")
        db.add(entity)
        await db.flush()
        req.invite_token = generate_ulid()
        db.add(Staff(
            entity_id=entity.id,
            name="Invited member",
            email=email,
            status="invited",
            meta={"invite_token": req.invite_token},
        ))
        await db.commit()
    elif mode == "resend":
        user, _ = await register_user(db, email=email, password="old-password-123")
        user.status = "pending"
        await db.commit()
    return req


@pytest.mark.parametrize("mode", ["password", "staff_invite", "verification", "resend"])
async def test_registration_commits_before_success_or_verification(db_session, monkeypatch, mode):
    req = await _registration(db_session, monkeypatch, mode)
    observer_sessions = async_sessionmaker(db_session.bind, expire_on_commit=False)

    async def assert_committed():
        async with observer_sessions() as observer:
            user = await observer.scalar(select(User).where(User.email == req.email))
            assert user is not None, "Registration returned before its user was committed"
            assert verify_password(req.password, user.password_hash)
            assert user.status == ("pending" if mode in {"verification", "resend"} else "active")
            assert await observer.get(Entity, user.entity_id) is not None
            assert await observer.scalar(select(UserMembership.id).where(
                UserMembership.user_id == user.id,
                UserMembership.entity_id == user.entity_id,
            )) is not None
            if mode == "staff_invite":
                staff = await observer.scalar(select(Staff).where(Staff.email == req.email))
                assert staff.user_id == user.id and staff.status == "active"
                assert "invite_token" not in staff.meta

    async def verification_after_commit(*_args, **_kwargs):
        await assert_committed()
        return "12345678"

    monkeypatch.setattr(auth, "create_verification", verification_after_commit)
    result = await auth.register(req, Request({"type": "http"}), db_session)
    await assert_committed()
    if mode in {"verification", "resend"}:
        assert result["requires_verification"] is True
        auth.send_verification_email.assert_awaited_once()
    else:
        assert result.access_token


@pytest.mark.parametrize("mode", ["password", "staff_invite", "verification", "resend"])
async def test_registration_commit_failure_does_not_report_success(db_session, monkeypatch, mode):
    req = await _registration(db_session, monkeypatch, mode)
    monkeypatch.setattr(db_session, "commit", AsyncMock(side_effect=RuntimeError("commit failed")))
    with pytest.raises(RuntimeError, match="commit failed"):
        await auth.register(req, Request({"type": "http"}), db_session)
    auth.create_verification.assert_not_awaited()
    auth.send_verification_email.assert_not_awaited()
    await db_session.rollback()
    user = await db_session.scalar(select(User).where(User.email == req.email))
    if mode == "resend":
        assert user is not None and verify_password("old-password-123", user.password_hash)
    else:
        assert user is None
    if mode == "staff_invite":
        staff = await db_session.scalar(select(Staff).where(Staff.email == req.email))
        assert staff.status == "invited" and staff.user_id is None
        assert staff.meta["invite_token"] == req.invite_token
