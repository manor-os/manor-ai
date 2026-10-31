"""Knowledge documents are found by what they say while indexing is paused."""
from __future__ import annotations

import json

import pytest
from httpx import AsyncClient

from packages.core.services.knowledge_content_search import query_terms, score_text, snippet
from tests.test_document_permissions import _auth, _invite_and_accept_member
from tests.test_fs_document_visibility import fs_enabled  # noqa: F401


def test_chinese_questions_become_topic_bigrams():
    assert query_terms("根据我的知识库，青柚咖啡9月营收是多少？") == ["9", "青柚", "柚咖", "咖啡", "月营", "营收"]
    assert "补贴" in query_terms("员工晚上上班有什么额外补贴？")
    assert query_terms("What is the Q3 revenue?") == ["q3", "revenue"]


def test_ranking_prefers_more_terms_and_names():
    terms = query_terms("青柚咖啡9月营收")
    q3, _ = score_text(terms, "青柚咖啡_2026Q3_运营季报.md", "- 9 月营收：386.2 万元")
    unrelated, _ = score_text(terms, "IT账号安全规范.md", "所有员工账号必须开启双因素认证。")
    assert q3 > 0.5 and unrelated == 0


def test_snippet_is_the_window_holding_the_terms():
    text = "前言" * 400 + "夜班补贴：22:00 之后每小时 18 元" + "附录" * 400
    piece = snippet(text, ["夜班", "补贴"], max_chars=120)
    assert "夜班补贴" in piece and piece.startswith("…") and piece.endswith("…")


async def _upload(client: AsyncClient, headers: dict, name: str, body: str, visibility: str = "entity") -> dict:
    response = await client.post(
        f"/api/v1/documents/upload?visibility={visibility}", headers=headers,
        files={"file": (name, body.encode("utf-8"), "text/markdown")},
    )
    assert response.status_code == 201, response.text
    return response.json()


@pytest.mark.asyncio
async def test_search_documents_finds_unindexed_documents_by_their_text(
    client: AsyncClient, db_session, fs_enabled,
):
    from packages.core.ai.runtime.document_actions import runtime_search_documents_action

    owner = await _auth(client, "kcs_owner")
    me = (await client.get("/api/v1/auth/me", headers=owner)).json()
    await _upload(client, owner, "季报Q3.md", "# 季报\n- 9 月营收：386.2 万元\n- 爆款：桂花冷萃")
    await _upload(client, owner, "季报Q2.md", "# 季报\n- 6 月营收：301.5 万元")
    await _upload(client, owner, "员工手册.md", "22:00 之后的班次按每小时 18 元发放夜班补贴。")
    await _upload(client, owner, "私密.md", "董事会薪酬 9 月营收 机密", visibility="private")

    result = json.loads(await runtime_search_documents_action(
        entity_id=me["entity_id"], user_id=me["id"],
        params={"query": "9月营收是多少"}, db=db_session,
    ))
    names = [match["name"] for match in result["content_matches"]]
    assert names[0] in {"季报Q3.md", "私密.md"}
    assert "季报Q3.md" in names
    top = next(match for match in result["content_matches"] if match["name"] == "季报Q3.md")
    assert "386.2" in top["snippet"]
    assert top["markdown_link"].startswith("[季报Q3.md](/viewer/")
    assert result["content_search"]["scanned"] >= 3

    member, _ = await _invite_and_accept_member(client, owner, "kcs.member@test.com")
    member_me = (await client.get("/api/v1/auth/me", headers=member)).json()
    member_result = json.loads(await runtime_search_documents_action(
        entity_id=me["entity_id"], user_id=member_me["id"],
        params={"query": "9月营收"}, db=db_session,
    ))
    member_names = {match["name"] for match in member_result["content_matches"]}
    assert "季报Q3.md" in member_names
    assert "私密.md" not in member_names  # Document ACL holds for content search

    # The model searched in English for a Chinese document; the user's own
    # wording for the turn still finds it.
    english = {"query": "night shift allowance per hour"}
    missed = json.loads(await runtime_search_documents_action(
        entity_id=me["entity_id"], user_id=me["id"], params=english, db=db_session,
    ))
    assert "员工手册.md" not in {match["name"] for match in missed["content_matches"]}
    found = json.loads(await runtime_search_documents_action(
        entity_id=me["entity_id"], user_id=me["id"], params=english, db=db_session,
        user_wording="员工晚上上班有什么额外补贴？",
    ))
    assert found["content_matches"][0]["name"] == "员工手册.md"
    assert "18 元" in found["content_matches"][0]["snippet"]


def test_the_model_is_told_the_knowledge_base_exists():
    from packages.core.ai.runtime.prompt_guidance import runtime_tool_usage_guidance

    guidance = runtime_tool_usage_guidance(tool_names=["manor", "read_file"], has_tools=True)
    assert "manor(action='search_documents'" in guidance
    assert "Never say you have no access to the user's documents without searching" in guidance


@pytest.mark.asyncio
async def test_content_search_route_runs_as_the_user_under_the_document_acl(
    client: AsyncClient, fs_enabled,
):
    owner = await _auth(client, "kcs_route_owner")
    shared = await _upload(client, owner, "价目表.md", "# 价目\n- 年度套餐：12800 元")
    private = await _upload(client, owner, "私密价目.md", "年度套餐 内部底价 9800 元", visibility="private")
    in_workspace = await _upload(client, owner, "KCS报价.md", "KCS 年度套餐报价 15800 元")
    workspace = await client.post("/api/v1/workspaces", headers=owner, json={"name": "KCS"})
    assert workspace.status_code == 201, workspace.text
    group = await client.post(
        "/api/v1/documents/groups", headers=owner,
        json={"name": "KCS Knowledge", "workspace_id": workspace.json()["id"]},
    )
    assert group.status_code == 201, group.text
    linked = await client.post(
        f"/api/v1/documents/{in_workspace['id']}/groups/{group.json()['id']}", headers=owner,
    )
    assert linked.status_code == 200, linked.text

    async def found_ids(headers: dict, **params) -> list[str]:
        response = await client.get("/api/v1/documents/content-search", headers=headers, params=params)
        assert response.status_code == 200, response.text
        return [item["document"]["id"] for item in response.json()["items"]]

    owner_ids = await found_ids(owner, q="年度套餐多少元")
    assert {shared["id"], private["id"], in_workspace["id"]} <= set(owner_ids)
    response = await client.get("/api/v1/documents/content-search", headers=owner, params={"q": "12800"})
    top = response.json()["items"][0]
    assert top["document"]["id"] == shared["id"] and "12800" in top["snippet"] and top["score"] > 0

    member, _ = await _invite_and_accept_member(client, owner, "kcs.route.member@test.com")
    member_ids = await found_ids(member, q="年度套餐 底价")
    assert shared["id"] in member_ids
    assert private["id"] not in member_ids  # another user's private document never appears

    assert await found_ids(owner, q="年度套餐", workspace_id=workspace.json()["id"]) == [in_workspace["id"]]



@pytest.mark.asyncio
async def test_file_reads_run_after_the_listing_transaction_is_released(
    client: AsyncClient, db_session, fs_enabled, monkeypatch,
):
    """Reading text can take the whole budget; it must not pin a DB admission permit."""
    from packages.core.services import knowledge_content_search as kcs

    owner = await _auth(client, "kcs_txn_owner")
    me = (await client.get("/api/v1/auth/me", headers=owner)).json()
    await _upload(client, owner, "门店手册.md", "打烊前清点冷柜库存。")

    released: list[object] = []
    released_before_read: list[bool] = []

    async def fake_release(db):
        released.append(db)
        return True

    read = kcs._document_text

    async def spy(entity_root, doc):
        released_before_read.append(bool(released))
        return await read(entity_root, doc)

    monkeypatch.setattr("packages.core.database_runtime.release_read_transaction", fake_release)
    monkeypatch.setattr(kcs, "_document_text", spy)
    result = await kcs.search_knowledge_content(
        db_session, entity_id=me["entity_id"], user_id=me["id"], query="冷柜库存",
    )
    assert result["matches"] and result["matches"][0]["doc"].name == "门店手册.md"
    assert released == [db_session]
    assert released_before_read and all(released_before_read)
