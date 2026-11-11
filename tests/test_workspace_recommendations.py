"""Suggested Workspaces come from Manor's records and public company information, never a person."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from auth_helpers import register_user_and_get_token
from packages.core.cache import cache
from packages.core.models.memory import AgentMemory
from packages.core.models.user import User
from packages.core.services import workspace_recommendations as wr

SUGGESTION = {"blueprint_id": None, "title": "Dealer support", "reason": "You sell through dealers.", "brief": "I want dealer support."}


@pytest.fixture
def fakes(monkeypatch):
    calls = {"search": [], "model": []}
    leases: dict[str, str] = {}

    async def acquire(key, token, ttl):
        if key in leases:
            return False
        leases[key] = token
        return True

    async def release(key, token):
        if leases.get(key) != token:
            return False
        del leases[key]
        return True

    async def search(domain):
        calls["search"].append(domain)
        return "Acme Bikes builds bicycles."

    async def ask(payload, catalog, *, entity_id, user_id):
        calls["model"].append(payload)
        return "Acme Bikes builds bicycles.", [dict(SUGGESTION)]

    monkeypatch.setattr(wr, "_search_company", search)
    monkeypatch.setattr(wr, "_ask_model", ask)
    monkeypatch.setattr("packages.core.constants.plans.is_cloud", lambda: True)
    monkeypatch.setattr(wr, "schedule_workspace_recommendations", lambda user_id: None)
    monkeypatch.setattr(cache, "acquire_lease", acquire)
    monkeypatch.setattr(cache, "release_lease", release)
    return calls


async def _register(client: AsyncClient, db_session, email: str) -> tuple[dict[str, str], str]:
    token = (await register_user_and_get_token(client, json={
        "username": email.split("@")[0], "email": email,
        "password": "Recommendations123!", "entity_name": "Acme Bikes",
    })).json()["access_token"]
    user = await db_session.scalar(select(User).where(User.email == email))
    return {"Authorization": f"Bearer {token}"}, user.id


async def _age_cache(db_session, user_id: str) -> None:
    db_session.expire_all()
    user = await db_session.get(User, user_id)
    cached = dict(user.preferences[wr._PREFERENCE_KEY])
    cached["generated_at"] = "2020-01-01T00:00:00+00:00"
    user.preferences = {**user.preferences, wr._PREFERENCE_KEY: cached}
    await db_session.commit()


@pytest.mark.parametrize(("email", "expected"), [
    ("ana@acme-bikes.com", "acme-bikes.com"),
    ("ana@GMAIL.com", None),
    ("ana@qq.com", None),
    ("not-an-email", None),
    ("ana@localhost", None),
    ("ana@manor.test", None),
])
def test_only_a_company_address_names_a_company(email, expected):
    assert wr.company_domain(email) == expected


def test_only_results_about_the_company_count_as_public_information():
    found = """{"results": [
        {"url": "https://www.acme-bikes.com/about", "title": "About", "snippet": "We build bicycles."},
        {"url": "https://news.example.org/a", "title": "Dealer news", "snippet": "acme-bikes.com opened a shop."},
        {"url": "https://www.youtube.com/watch?v=1", "title": "Acme bikes video", "snippet": "A ride."},
        {"url": "https://notacme-bikes.com/", "title": "Lookalike", "snippet": "Other bikes."}
    ]}"""
    kept = wr._results_about_company(found, "acme-bikes.com")
    assert [result["url"] for result in kept] == [
        "https://www.acme-bikes.com/about", "https://news.example.org/a",
    ]
    assert wr._results_about_company('{"error": "failed"}', "acme-bikes.com") == []
    assert wr._results_about_company("not json", "acme-bikes.com") == []


@pytest.mark.parametrize(("byok", "expected"), [
    (True, {"entity_id": "entity-1", "user_id": "user-1"}),
    (False, {"model": wr.RECOMMENDATION_MODEL}),
])
@pytest.mark.asyncio
async def test_own_model_keys_are_used_and_otherwise_no_tenant_is_charged(monkeypatch, byok, expected):
    captured: dict = {}

    async def route(**kwargs):
        return None, None, byok

    async def complete(messages, **kwargs):
        captured.update(kwargs)
        return type("Completion", (), {"content": '{"recommendations": []}'})()

    monkeypatch.setattr("packages.core.ai.runtime.completions.runtime_resolve_text_completion_route", route)
    monkeypatch.setattr("packages.core.ai.runtime.completions.runtime_execute_text_completion", complete)
    await wr._ask_model({}, [], entity_id="entity-1", user_id="user-1")
    assert {key: captured.get(key) for key in expected} == expected
    assert ("entity_id" in captured) is byok


def test_a_suggestion_must_be_a_real_blueprint_or_carry_a_brief():
    content = """{"company_summary": "Makes bikes.", "recommendations": [
        {"blueprint_id": "real", "title": "", "reason": "fits", "brief": "use it"},
        {"blueprint_id": "real", "title": "", "reason": "again", "brief": "use it again"},
        {"blueprint_id": "invented", "title": "Custom", "reason": "fits", "brief": "build it"},
        {"blueprint_id": "invented", "title": "", "reason": "fits", "brief": "no name"},
        {"blueprint_id": null, "title": "No brief", "reason": "fits", "brief": ""}
    ]}"""
    summary, items = wr._parse_recommendations(content, {"real"})
    assert summary == "Makes bikes."
    assert [(item["blueprint_id"], item["title"]) for item in items] == [("real", ""), (None, "Custom")]
    assert wr._parse_recommendations("not json", {"real"}) == ("", [])


@pytest.mark.asyncio
async def test_a_company_is_looked_up_once_and_a_deleted_memory_stays_deleted(client: AsyncClient, db_session, fakes):
    headers, user_id = await _register(client, db_session, "owner@acme-bikes.com")

    pending = await client.get("/api/v1/workspace-drafts/recommendations", headers=headers)
    assert pending.json() == {"status": "pending", "items": [], "sources": []}

    assert (await wr.refresh_workspace_recommendations(user_id))["status"] == "refreshed"
    assert fakes["search"] == ["acme-bikes.com"]
    # The search result is handed over as untrusted data, next to the person's records.
    assert fakes["model"][0]["public_search_results"] == "Acme Bikes builds bicycles."

    ready = (await client.get("/api/v1/workspace-drafts/recommendations", headers=headers)).json()
    assert ready["status"] == "ready"
    assert ready["items"] == [SUGGESTION]
    assert "public_web" in ready["sources"]

    db_session.expire_all()
    memory = await db_session.scalar(select(AgentMemory).where(AgentMemory.source == wr.PUBLIC_WEB_SOURCE))
    assert "Acme Bikes builds bicycles." in memory.content
    assert memory.expires_at is not None
    await db_session.delete(memory)
    await db_session.commit()

    await _age_cache(db_session, user_id)
    await wr.refresh_workspace_recommendations(user_id)
    assert fakes["search"] == ["acme-bikes.com"]
    db_session.expire_all()
    assert await db_session.scalar(select(AgentMemory).where(AgentMemory.source == wr.PUBLIC_WEB_SOURCE)) is None


@pytest.mark.asyncio
async def test_a_personal_mailbox_is_never_searched(client: AsyncClient, db_session, fakes):
    _, user_id = await _register(client, db_session, "owner@gmail.com")
    await wr.refresh_workspace_recommendations(user_id)
    assert fakes["search"] == []
    # With no records and nothing public there is nothing to reason from, so no model call either.
    assert fakes["model"] == []


@pytest.mark.asyncio
async def test_unchanged_signals_cost_nothing_and_a_fresh_result_is_not_recomputed(client: AsyncClient, db_session, fakes):
    _, user_id = await _register(client, db_session, "founder@acme-bikes.com")
    await wr.refresh_workspace_recommendations(user_id)
    assert len(fakes["model"]) == 1

    assert (await wr.refresh_workspace_recommendations(user_id))["status"] == "fresh"
    await _age_cache(db_session, user_id)
    assert (await wr.refresh_workspace_recommendations(user_id))["status"] == "unchanged"
    assert len(fakes["model"]) == 1


@pytest.mark.asyncio
async def test_opening_the_page_never_computes_and_a_stale_result_refreshes_behind_it(client: AsyncClient, db_session, fakes, monkeypatch):
    headers, user_id = await _register(client, db_session, "ops@acme-bikes.com")
    await wr.refresh_workspace_recommendations(user_id)
    await _age_cache(db_session, user_id)
    scheduled: list[str] = []
    monkeypatch.setattr(wr, "schedule_workspace_recommendations", scheduled.append)
    calls_before = len(fakes["model"])

    shown = (await client.get("/api/v1/workspace-drafts/recommendations", headers=headers)).json()
    assert shown["items"] == [SUGGESTION]
    assert scheduled == [user_id]
    assert len(fakes["model"]) == calls_before


@pytest.mark.asyncio
async def test_overlapping_refresh_does_not_start_a_second_model_run(fakes, monkeypatch):
    entered = asyncio.Event()
    finish = asyncio.Event()
    refreshed = []

    async def refresh(user_id):
        refreshed.append(user_id)
        entered.set()
        await finish.wait()
        return {"status": "refreshed"}

    monkeypatch.setattr(wr, "_refresh_workspace_recommendations", refresh)
    first = asyncio.create_task(wr.refresh_workspace_recommendations("user-1"))
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        assert (await wr.refresh_workspace_recommendations("user-1"))["status"] == "deferred"
    finally:
        finish.set()
        await first
    assert refreshed == ["user-1"]


@pytest.mark.asyncio
async def test_opted_out_user_gets_no_background_recommendations(client: AsyncClient, db_session, fakes, monkeypatch):
    headers, user_id = await _register(client, db_session, "opted-out@acme-bikes.com")
    user = await db_session.get(User, user_id)
    user.preferences = {**(user.preferences or {}), "workspace_recommendations_enabled": False}
    await db_session.commit()
    scheduled: list[str] = []
    monkeypatch.setattr(wr, "schedule_workspace_recommendations", scheduled.append)

    response = await client.get("/api/v1/workspace-drafts/recommendations", headers=headers)
    assert response.json() == {"status": "ready", "items": [], "sources": []}
    assert scheduled == []
    assert (await wr.refresh_workspace_recommendations(user_id))["status"] == "disabled"
    assert fakes == {"search": [], "model": []}


@pytest.mark.parametrize("enabled", [True, False])
@pytest.mark.asyncio
async def test_refresh_preserves_a_concurrent_preference_update(client: AsyncClient, db_session, fakes, monkeypatch, enabled):
    _, user_id = await _register(client, db_session, "concurrent@acme-bikes.com")
    load_actor = wr._load_actor
    loads = 0

    async def load_with_concurrent_update(db, actor_id):
        nonlocal loads
        actor = await load_actor(db, actor_id)
        loads += 1
        if loads == 2:
            user = await db_session.get(User, actor_id)
            user.preferences = {**user.preferences, "workspace_recommendations_enabled": enabled}
            await db_session.commit()
        return actor

    monkeypatch.setattr(wr, "_load_actor", load_with_concurrent_update)
    result = await wr.refresh_workspace_recommendations(user_id)

    db_session.expire_all()
    user = await db_session.get(User, user_id)
    assert user.preferences["workspace_recommendations_enabled"] is enabled
    assert (wr._PREFERENCE_KEY in user.preferences) is enabled
    assert result["status"] == ("refreshed" if enabled else "skipped")


@pytest.mark.asyncio
async def test_active_secondary_company_cannot_read_primary_company_suggestions(fakes, monkeypatch):
    class SecondaryCompanySession:
        async def scalar(self, statement):
            return "primary"

    scheduled: list[str] = []
    monkeypatch.setattr(wr, "schedule_workspace_recommendations", scheduled.append)
    user = SimpleNamespace(
        id="user-1",
        entity_id="secondary",
        preferences={wr._PREFERENCE_KEY: {"entity_id": "primary", "items": [SUGGESTION]}},
    )
    response = await wr.read_workspace_recommendations(SecondaryCompanySession(), user)
    assert response == {"status": "ready", "items": [], "sources": []}
    assert scheduled == []


@pytest.mark.asyncio
async def test_primary_company_change_invalidates_cached_suggestions(fakes, monkeypatch):
    class PrimaryCompanySession:
        async def scalar(self, statement):
            return "current"

    scheduled: list[str] = []
    monkeypatch.setattr(wr, "schedule_workspace_recommendations", scheduled.append)
    user = SimpleNamespace(
        id="user-1",
        entity_id="current",
        preferences={wr._PREFERENCE_KEY: {"entity_id": "previous", "items": [SUGGESTION]}},
    )
    response = await wr.read_workspace_recommendations(PrimaryCompanySession(), user)
    assert response == {"status": "pending", "items": [], "sources": []}
    assert scheduled == [user.id]
