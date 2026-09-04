from __future__ import annotations

import pytest
from httpx import AsyncClient


async def _auth(client: AsyncClient, username: str) -> dict[str, str]:
    response = await client.post("/api/v1/auth/register", json={
        "username": username,
        "email": f"{username}@test.com",
        "password": "pass123",
        "entity_name": "Flow Template Corp",
    })
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


