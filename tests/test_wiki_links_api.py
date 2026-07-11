import pytest
from httpx import AsyncClient


async def _register(client: AsyncClient, username: str = "wikiuser") -> dict:
    resp = await client.post(
        "/api/v1/auth/register",
        json={
            "username": username,
            "email": f"{username}@test.com",
            "password": "pass123",
            "entity_name": "Wiki Corp",
        },
    )
    assert resp.status_code == 200
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.mark.asyncio
async def test_wiki_links_endpoint_returns_document_metadata(client: AsyncClient, tmp_path):
    import apps.api.routers.documents as documents_router
    from packages.core.config import get_settings

    settings = get_settings()
    old_documents_settings = documents_router.settings
    old_root = settings.MANOR_FS_ROOT
    old_enabled = settings.MANOR_FS_ENABLED
    settings.MANOR_FS_ROOT = str(tmp_path)
    settings.MANOR_FS_ENABLED = True
    documents_router.settings = settings
    try:
        headers = await _register(client)
        target_resp = await client.post(
            "/api/v1/documents/create-blank",
            headers=headers,
            json={"name": "Client FAQ", "file_type": "md"},
        )
        assert target_resp.status_code == 201
        target = target_resp.json()

        source_resp = await client.post(
            "/api/v1/documents/create-blank",
            headers=headers,
            json={"name": "Lease Playbook", "file_type": "md"},
        )
        assert source_resp.status_code == 201
        source = source_resp.json()

        save_resp = await client.put(
            f"/api/v1/documents/{source['id']}/content",
            headers=headers,
            json={"content": "Use [[Client FAQ|FAQ]] before [[Missing Page]]."},
        )
        assert save_resp.status_code == 200

        links_resp = await client.get(
            "/api/v1/fs/wiki-links",
            headers=headers,
            params={"path": source["fs_path"]},
        )
        assert links_resp.status_code == 200
        by_target = {link["target"]: link for link in links_resp.json()["links"]}

        assert by_target["Client FAQ"]["exists"] is True
        assert by_target["Client FAQ"]["document_id"] == target["id"]
        assert by_target["Client FAQ"]["document_name"] == target["name"]
        assert by_target["Missing Page"]["exists"] is False
        assert by_target["Missing Page"]["document_id"] is None

        index_resp = await client.get("/api/v1/fs/wiki-index", headers=headers)
        assert index_resp.status_code == 200
        index = index_resp.json()
        assert index["page_count"] == 2
        assert index["missing_count"] == 1
        page_by_title = {page["title"]: page for page in index["pages"]}
        assert page_by_title["Client FAQ"]["document_id"] == target["id"]
        assert page_by_title["Lease Playbook"]["links"][0]["document_id"] == target["id"]
        assert index["missing_links"][0]["target"] == "Missing Page"
    finally:
        settings.MANOR_FS_ROOT = old_root
        settings.MANOR_FS_ENABLED = old_enabled
        documents_router.settings = old_documents_settings


@pytest.mark.asyncio
async def test_wiki_index_includes_db_backed_markdown_documents(client: AsyncClient, db_session, tmp_path):
    import apps.api.routers.documents as documents_router
    from packages.core.config import get_settings
    from packages.core.models.base import generate_ulid
    from packages.core.models.document import Document, DocumentGroup, DocumentGroupMember

    settings = get_settings()
    old_documents_settings = documents_router.settings
    old_root = settings.MANOR_FS_ROOT
    old_enabled = settings.MANOR_FS_ENABLED
    settings.MANOR_FS_ROOT = str(tmp_path)
    settings.MANOR_FS_ENABLED = True
    documents_router.settings = settings
    try:
        register = await client.post(
            "/api/v1/auth/register",
            json={
                "username": "wikidbuser",
                "email": "wikidbuser@test.com",
                "password": "pass123",
                "entity_name": "Wiki DB Corp",
            },
        )
        assert register.status_code == 200
        body = register.json()
        headers = {"Authorization": f"Bearer {body['access_token']}"}
        entity_id = body["entity_id"]
        (tmp_path / entity_id).mkdir(parents=True, exist_ok=True)

        group_id = generate_ulid()
        alpha_id = generate_ulid()
        beta_id = generate_ulid()
        ungrouped_id = generate_ulid()
        db_session.add_all(
            [
                DocumentGroup(id=group_id, entity_id=entity_id, name="Workspace Knowledge"),
                Document(
                    id=alpha_id,
                    entity_id=entity_id,
                    name="Alpha.md",
                    file_type="md",
                    mime_type="text/markdown",
                    vector_status="ready",
                    metadata_={"content_text": "See [[Beta]] before [[Missing Page]]."},
                ),
                Document(
                    id=beta_id,
                    entity_id=entity_id,
                    name="Beta.md",
                    file_type="md",
                    mime_type="text/markdown",
                    vector_status="ready",
                    metadata_={"content_text": "Beta details."},
                ),
                Document(
                    id=ungrouped_id,
                    entity_id=entity_id,
                    name="Ungrouped.md",
                    file_type="md",
                    mime_type="text/markdown",
                    vector_status="ready",
                    metadata_={"content_text": "Outside scoped net."},
                ),
                DocumentGroupMember(group_id=group_id, document_id=alpha_id),
                DocumentGroupMember(group_id=group_id, document_id=beta_id),
            ]
        )
        await db_session.commit()

        index_resp = await client.get("/api/v1/fs/wiki-index", headers=headers)
        assert index_resp.status_code == 200
        index = index_resp.json()
        page_by_title = {page["title"]: page for page in index["pages"]}
        assert {"Alpha", "Beta", "Ungrouped"}.issubset(page_by_title)
        assert page_by_title["Alpha"]["document_id"] == alpha_id
        assert page_by_title["Alpha"]["links"][0]["resolved_path"] == page_by_title["Beta"]["path"]
        assert page_by_title["Alpha"]["links"][0]["document_id"] == beta_id
        assert page_by_title["Beta"]["backlinks"] == [
            {"source_path": page_by_title["Alpha"]["path"], "source_title": "Alpha"}
        ]
        assert index["missing_links"][0]["target"] == "Missing Page"

        scoped_resp = await client.get("/api/v1/fs/wiki-index", headers=headers, params={"group_id": group_id})
        assert scoped_resp.status_code == 200
        scoped = scoped_resp.json()
        assert {page["title"] for page in scoped["pages"]} == {"Alpha", "Beta"}
        assert scoped["scope"]["kind"] == "knowledge_net"
        assert scoped["scope"]["net_ids"] == [group_id]
    finally:
        settings.MANOR_FS_ROOT = old_root
        settings.MANOR_FS_ENABLED = old_enabled
        documents_router.settings = old_documents_settings


@pytest.mark.asyncio
async def test_wiki_links_endpoint_uses_db_content_when_fs_file_missing(client: AsyncClient, db_session, tmp_path):
    import apps.api.routers.documents as documents_router
    from packages.core.config import get_settings
    from packages.core.models.base import generate_ulid
    from packages.core.models.document import Document

    settings = get_settings()
    old_documents_settings = documents_router.settings
    old_root = settings.MANOR_FS_ROOT
    old_enabled = settings.MANOR_FS_ENABLED
    settings.MANOR_FS_ROOT = str(tmp_path)
    settings.MANOR_FS_ENABLED = True
    documents_router.settings = settings
    try:
        register = await client.post(
            "/api/v1/auth/register",
            json={
                "username": "wikifallbackuser",
                "email": "wikifallbackuser@test.com",
                "password": "pass123",
                "entity_name": "Wiki Fallback Corp",
            },
        )
        assert register.status_code == 200
        body = register.json()
        headers = {"Authorization": f"Bearer {body['access_token']}"}
        entity_id = body["entity_id"]
        (tmp_path / entity_id).mkdir(parents=True, exist_ok=True)

        doc_id = generate_ulid()
        fs_path = "Workspaces/Demo/artifacts/generated-summary.md"
        db_session.add(
            Document(
                id=doc_id,
                entity_id=entity_id,
                name="Generated Summary.md",
                fs_path=fs_path,
                file_type="md",
                mime_type="text/markdown",
                vector_status="ready",
                metadata_={"content_text": "Generated artifact body without wiki links."},
            )
        )
        await db_session.commit()

        links_resp = await client.get(
            "/api/v1/fs/wiki-links",
            headers=headers,
            params={"path": fs_path},
        )
        assert links_resp.status_code == 200
        assert links_resp.json()["count"] == 0
    finally:
        settings.MANOR_FS_ROOT = old_root
        settings.MANOR_FS_ENABLED = old_enabled
        documents_router.settings = old_documents_settings
