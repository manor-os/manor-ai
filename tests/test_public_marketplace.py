from datetime import datetime, timezone

import pytest
from httpx import AsyncClient

from packages.core.models.blueprint import WorkspaceBlueprint


def _blueprint(
    *,
    blueprint_id: str,
    slug: str,
    status: str,
    entity_id: str | None = None,
) -> WorkspaceBlueprint:
    now = datetime.now(timezone.utc)
    return WorkspaceBlueprint(
        id=blueprint_id,
        entity_id=entity_id,
        slug=slug,
        title="Creator Revenue Workspace",
        summary="A long-running system for planning, publishing, and measuring creator products.",
        description="Run the complete creator loop. <script>private()</script>",
        cover_image_url="/assets/blueprints/oil-paintings/digital-store.webp",
        showcase_assets=[],
        tags=["creator", "workspace", "monetization"],
        author_user_id=None,
        author_handle="creator-lab" if entity_id else "manor",
        author_display_name="Creator Lab" if entity_id else "Manor AI",
        payload={
            "secret": "never-publish-this-value",
            "manifest": {"use_when": "Run a repeatable creator product."},
            "policy": {
                "expected_baseline": {
                    "first_week_outputs": ["A published product plan"],
                },
            },
        },
        payload_version="1.1",
        content_version="1.0.0",
        status=status,
        install_count=12,
        purchase_count=4,
        price_cents=4900,
        currency="usd",
        created_at=now,
        published_at=now if status == "published" else None,
    )


@pytest.mark.asyncio
async def test_public_marketplace_exposes_only_reviewed_metadata(
    client: AsyncClient,
    db_session,
):
    published = _blueprint(
        blueprint_id="builtin:creator-revenue",
        slug="creator-revenue",
        status="published",
    )
    draft = _blueprint(
        blueprint_id="01KPUBLICDRAFT000000000000",
        slug="private-draft",
        status="draft",
        entity_id="01KPUBLICENTITY00000000000",
    )
    db_session.add_all([published, draft])
    await db_session.commit()

    response = await client.get("/api/v1/public/marketplace/blueprints")
    assert response.status_code == 200
    data = response.json()
    assert [item["id"] for item in data] == [published.id]
    assert "payload" not in data[0]
    assert "source_workspace_id" not in data[0]
    assert "share_token" not in data[0]
    assert "secret" not in response.text

    draft_response = await client.get(
        f"/api/v1/public/marketplace/blueprints/{draft.id}",
    )
    assert draft_response.status_code == 404


@pytest.mark.asyncio
async def test_public_marketplace_pages_render_metadata_and_escape_creator_copy(
    client: AsyncClient,
    db_session,
):
    published = _blueprint(
        blueprint_id="01KPUBLICBLUEPRINT000000000",
        slug="creator-revenue",
        status="published",
        entity_id="01KPUBLICENTITY00000000000",
    )
    db_session.add(published)
    await db_session.commit()

    catalog = await client.get("/marketplace/")
    assert catalog.status_code == 200
    assert catalog.headers["x-robots-tag"].startswith("index")
    assert "Workspace Creator Marketplace" in catalog.text
    assert "AI workspaces that keep your business running" in catalog.text
    assert published.title in catalog.text
    assert "never-publish-this-value" not in catalog.text

    detail = await client.get(
        f"/marketplace/blueprints/{published.id}/{published.slug}",
    )
    assert detail.status_code == 200
    assert '<meta name="robots" content="index,follow,max-image-preview:large">' in detail.text
    assert "application/ld+json" in detail.text
    assert "SoftwareApplication" in detail.text
    assert "&lt;script&gt;private()&lt;/script&gt;" in detail.text
    assert "never-publish-this-value" not in detail.text

    creator = await client.get(f"/marketplace/creators/{published.entity_id}")
    assert creator.status_code == 200
    assert "ProfilePage" in creator.text
    assert f"{published.author_display_name}'s AI Workspace Blueprints" in creator.text

    sitemap = await client.get("/marketplace/sitemap.xml")
    assert sitemap.status_code == 200
    assert published.id in sitemap.text
    assert published.entity_id in sitemap.text
