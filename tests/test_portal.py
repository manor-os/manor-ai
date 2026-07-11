"""E2E tests: client portal — token auth, tickets, comments."""

import pytest
from httpx import AsyncClient


async def _auth(client: AsyncClient, username: str = "portaladmin") -> dict:
    """Register a user and return JWT auth headers."""
    resp = await client.post(
        "/api/v1/auth/register",
        json={
            "username": username,
            "email": f"{username}@test.com",
            "password": "pass123",
            "entity_name": "Portal Corp",
        },
    )
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def _create_client_and_token(client: AsyncClient, headers: dict, name: str = "Acme Inc") -> tuple[str, str]:
    """Create a client and generate a portal token. Returns (client_id, token)."""
    resp = await client.post("/api/v1/clients", headers=headers, json={"name": name})
    client_id = resp.json()["id"]

    resp = await client.post(f"/api/v1/portal/tokens/{client_id}", headers=headers)
    assert resp.status_code == 200
    token = resp.json()["token"]
    return client_id, token


@pytest.mark.asyncio
async def test_generate_portal_token(client: AsyncClient):
    """Admin creates a portal token for a client."""
    headers = await _auth(client, "ptk1")
    resp = await client.post("/api/v1/clients", headers=headers, json={"name": "Token Client"})
    assert resp.status_code == 201
    client_id = resp.json()["id"]

    resp = await client.post(f"/api/v1/portal/tokens/{client_id}", headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["client_id"] == client_id
    assert len(data["token"]) > 20


@pytest.mark.asyncio
async def test_submit_ticket(client: AsyncClient):
    """Client submits a ticket via portal token."""
    headers = await _auth(client, "ptk2")
    _, token = await _create_client_and_token(client, headers, "Ticket Client")

    portal_headers = {"X-Portal-Token": token}
    resp = await client.post(
        "/api/v1/portal/tickets",
        headers=portal_headers,
        json={
            "title": "My printer is broken",
            "description": "It keeps jamming on page 2",
        },
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["title"] == "My printer is broken"
    assert data["status"] == "pending"
    assert data["task_type"] == "client_ticket"
    assert data["details"]["source"] == "portal"


@pytest.mark.asyncio
async def test_list_client_tickets(client: AsyncClient):
    """Submit 2 tickets, list them, verify count."""
    headers = await _auth(client, "ptk3")
    _, token = await _create_client_and_token(client, headers, "List Client")

    portal_headers = {"X-Portal-Token": token}
    await client.post("/api/v1/portal/tickets", headers=portal_headers, json={"title": "Ticket A"})
    await client.post("/api/v1/portal/tickets", headers=portal_headers, json={"title": "Ticket B"})

    resp = await client.get("/api/v1/portal/tickets", headers=portal_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 2
    titles = {t["title"] for t in data}
    assert titles == {"Ticket A", "Ticket B"}


@pytest.mark.asyncio
async def test_ticket_comment(client: AsyncClient):
    """Submit a ticket, add a comment, verify the comment."""
    headers = await _auth(client, "ptk4")
    _, token = await _create_client_and_token(client, headers, "Comment Client")

    portal_headers = {"X-Portal-Token": token}
    resp = await client.post(
        "/api/v1/portal/tickets",
        headers=portal_headers,
        json={
            "title": "Need help",
        },
    )
    ticket_id = resp.json()["id"]

    resp = await client.post(
        f"/api/v1/portal/tickets/{ticket_id}/comments",
        headers=portal_headers,
        json={"content": "Any update on this?"},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["content"] == "Any update on this?"
    assert data["id"]


@pytest.mark.asyncio
async def test_invalid_portal_token(client: AsyncClient):
    """Using an invalid token returns 401."""
    portal_headers = {"X-Portal-Token": "totally-bogus-token"}

    resp = await client.get("/api/v1/portal/tickets", headers=portal_headers)
    assert resp.status_code == 401

    resp = await client.post(
        "/api/v1/portal/tickets",
        headers=portal_headers,
        json={
            "title": "Should fail",
        },
    )
    assert resp.status_code == 401

    resp = await client.get("/api/v1/portal/profile", headers=portal_headers)
    assert resp.status_code == 401
