"""Site model round-trip."""
import pytest

from packages.core.models import Site


@pytest.mark.asyncio
async def test_site_model_roundtrip(db_session):
    site = Site(
        entity_id="01ENTITY0000000000000000AA",
        name="旅行社落地页",
        slug="travel-landing",
        source_path="Workspaces/demo/code/travel-landing",
        entry="index.html",
    )
    db_session.add(site)
    await db_session.commit()
    await db_session.refresh(site)
    assert site.id and len(site.id) == 26
    assert site.status == "active"
    assert site.revision == 0
    assert site.custom_domain is None
