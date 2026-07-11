from __future__ import annotations

import json

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from packages.core.ai.mcp import manor_mcp_calendar
from packages.core.ai.tools import mcp_builtin
from packages.core.models.base import generate_ulid
from packages.core.models.mcp import MCPServer
from packages.core.models.task import Task
from packages.core.models.user import User
from packages.core.services.agent_permission_service import can_use_integration


async def _register_owner(client: AsyncClient, username: str) -> tuple[dict[str, str], str, str]:
    resp = await client.post(
        "/api/v1/auth/register",
        json={
            "username": username,
            "email": f"{username}@example.com",
            "password": "securepass123",
            "entity_name": f"{username} Co",
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    headers = {"Authorization": f"Bearer {data['access_token']}"}

    me = await client.get("/api/v1/auth/me", headers=headers)
    assert me.status_code == 200, me.text
    return headers, data["user_id"], me.json()["entity_id"]


def _payload(result: dict) -> dict:
    assert result.get("isError") is False, result
    return json.loads(result["content"][0]["text"])


async def _ensure_manor_mcp_calendar_server() -> None:
    import packages.core.database as dbmod

    async with dbmod.async_session() as db:
        existing = (
            await db.execute(select(MCPServer).where(MCPServer.server_key == "manor_mcp_calendar"))
        ).scalar_one_or_none()
        if existing:
            return
        db.add(
            MCPServer(
                id=generate_ulid(),
                server_key="manor_mcp_calendar",
                name="Manor Calendar",
                description="Manage Manor calendar settings and booking links.",
                transport="builtin",
                endpoint="packages.core.ai.mcp.manor_mcp_calendar",
                auth_type="none",
                status="active",
            )
        )
        await db.commit()


def test_manor_mcp_calendar_tools_are_registered_in_mcp_catalog():
    names = {tool["name"] for tool in mcp_builtin._SERVER_TOOL_SCHEMAS["manor_mcp_calendar"]}
    assert {
        "get_calendar_settings",
        "list_booking_links",
        "create_booking_link",
        "update_working_hours",
        "get_daily_agenda",
        "list_bookings",
    } <= names

    registered = {schema["function"]["name"] for schema, _handler in mcp_builtin.get_tools()}
    assert "mcp__manor_mcp_calendar__create_booking_link" in registered


@pytest.mark.asyncio
async def test_manor_mcp_calendar_is_internal_mcp_not_external_integration(client: AsyncClient):
    headers, _user_id, _entity_id = await _register_owner(client, "manor_mcp_calendar_catalog")

    await _ensure_manor_mcp_calendar_server()

    external_only = await client.get("/api/v1/integrations/mcp-servers", headers=headers)
    assert external_only.status_code == 200, external_only.text
    assert "manor_mcp_calendar" not in {row["server_key"] for row in external_only.json()}


@pytest.mark.asyncio
async def test_manor_mcp_calendar_permission_is_first_party(client: AsyncClient):
    _, user_id, entity_id = await _register_owner(client, "manor_mcp_calendar_access")

    import packages.core.database as dbmod

    async with dbmod.async_session() as db:
        decision = await can_use_integration(
            db,
            user_id=user_id,
            entity_id=entity_id,
            provider="manor_mcp_calendar",
            allow_env_fallback=False,
        )

    assert decision.allowed is True
    assert decision.scope == "internal"


@pytest.mark.asyncio
async def test_manor_mcp_calendar_dispatcher_uses_runtime_context_without_token(client: AsyncClient):
    _, user_id, entity_id = await _register_owner(client, "manor_mcp_calendar_dispatch")
    await _ensure_manor_mcp_calendar_server()

    handler = next(
        handler
        for schema, handler in mcp_builtin.get_tools()
        if schema["function"]["name"] == "mcp__manor_mcp_calendar__create_booking_link"
    )
    result_text = await handler(
        entity_id=entity_id,
        user_id=user_id,
        name="Intro Call",
        duration_minutes=20,
    )

    result = json.loads(result_text)
    assert result["booking_link"]["slug"] == "intro-call"
    assert result["booking_link"]["url"].endswith(f"/book/u/{user_id}/intro-call")


@pytest.mark.asyncio
async def test_manor_mcp_calendar_mcp_creates_links_and_reads_agenda(client: AsyncClient):
    _, user_id, entity_id = await _register_owner(client, "manor_mcp_calendar_flow")

    manor_mcp_calendar.set_call_context({"user_id": user_id, "entity_id": entity_id})
    try:
        created = _payload(
            await manor_mcp_calendar.call_tool(
                "create_booking_link",
                {"name": "Coffee Chat", "duration_minutes": 25},
                "",
            )
        )
        link = created["booking_link"]
        assert link["slug"] == "coffee-chat"
        assert link["url"].endswith(f"/book/u/{user_id}/coffee-chat")

        listed = _payload(await manor_mcp_calendar.call_tool("list_booking_links", {}, ""))
        assert listed["booking_links"][0]["name"] == "Coffee Chat"

        updated_hours = _payload(
            await manor_mcp_calendar.call_tool(
                "update_working_hours",
                {
                    "working_hours": [
                        {"day_of_week": 0, "enabled": True, "start": "10:00", "end": "16:00"},
                        {"day_of_week": 1, "enabled": False, "start": "09:00", "end": "17:00"},
                    ],
                },
                "",
            )
        )
        assert updated_hours["working_hours"][0]["start"] == "10:00"
        assert updated_hours["working_hours"][1]["enabled"] is False

        preserved_hours = _payload(
            await manor_mcp_calendar.call_tool(
                "update_working_hours",
                {
                    "working_hours": [
                        {"day_of_week": 0, "enabled": True, "start": "11:00", "end": "15:00"},
                    ],
                },
                "",
            )
        )
        assert preserved_hours["working_hours"][0]["start"] == "11:00"
        assert preserved_hours["working_hours"][1]["enabled"] is False
    finally:
        manor_mcp_calendar.clear_call_context()

    import packages.core.database as dbmod

    async with dbmod.async_session() as db:
        user = (await db.execute(select(User).where(User.id == user_id))).scalar_one()
        prefs = dict(user.preferences or {})
        settings = dict(prefs["calendar_settings"])
        settings["bookings"] = [
            {
                "id": generate_ulid(),
                "booking_link_id": link["id"],
                "booking_link_slug": link["slug"],
                "guest_name": "Jane Doe",
                "guest_email": "jane@example.com",
                "starts_at": "2026-07-15T19:00:00+00:00",
                "ends_at": "2026-07-15T19:25:00+00:00",
                "timezone": "UTC",
                "status": "confirmed",
                "meeting_url": "https://meet.example.com/coffee",
                "email_sent": True,
            }
        ]
        user.preferences = {**prefs, "calendar_settings": settings}
        db.add(
            Task(
                id=generate_ulid(),
                entity_id=entity_id,
                title="Prepare client brief",
                details={
                    "scheduled_at": "2026-07-15T16:00:00+00:00",
                    "duration_minutes": 45,
                },
            )
        )
        await db.commit()

    manor_mcp_calendar.set_call_context({"user_id": user_id, "entity_id": entity_id})
    try:
        agenda = _payload(
            await manor_mcp_calendar.call_tool(
                "get_daily_agenda",
                {"day": "2026-07-15"},
                "",
            )
        )
        assert [item["source"] for item in agenda["items"]] == ["task", "booking"]
        assert agenda["items"][0]["title"] == "Prepare client brief"
        assert agenda["items"][1]["meeting_url"] == "https://meet.example.com/coffee"

        bookings = _payload(
            await manor_mcp_calendar.call_tool(
                "list_bookings",
                {"status": "confirmed", "booking_link_slug": "coffee-chat"},
                "",
            )
        )
        assert bookings["bookings"][0]["guest_email"] == "jane@example.com"
    finally:
        manor_mcp_calendar.clear_call_context()
