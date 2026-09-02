from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from packages.core.models.base import generate_ulid
from packages.core.models.channel import TwilioVoiceCallSession
from packages.core.models.site import Site, SiteEvent
from packages.core.models.workspace import Workspace
from packages.core.services.entity_service import purge_workspace
from packages.core.services.voice.call_sessions import create_call_session


@pytest.mark.asyncio
async def test_purge_workspace_removes_workspace_owned_sites_and_events(db_session):
    entity_id = generate_ulid()
    workspace_id = generate_ulid()
    site_id = generate_ulid()
    event_id = generate_ulid()
    db_session.add(Workspace(
        id=workspace_id,
        entity_id=entity_id,
        name="site purge",
        deleted_at=datetime.now(timezone.utc) - timedelta(days=31),
    ))
    db_session.add(Site(
        id=site_id,
        entity_id=entity_id,
        workspace_id=workspace_id,
        name="site",
        slug=f"site-{site_id.lower()}",
        source_path="site",
    ))
    db_session.add(SiteEvent(
        id=event_id,
        site_id=site_id,
        entity_id=entity_id,
        workspace_id=workspace_id,
        event_type="page_view",
    ))
    await db_session.commit()

    assert await purge_workspace(db_session, workspace_id)
    assert await db_session.scalar(select(Site.id).where(Site.id == site_id)) is None
    assert await db_session.scalar(select(SiteEvent.id).where(SiteEvent.id == event_id)) is None


@pytest.mark.asyncio
async def test_purge_workspace_removes_only_its_twilio_voice_sessions(db_session):
    entity_id = generate_ulid()
    workspace_id = generate_ulid()
    preserved_workspace_id = generate_ulid()
    db_session.add_all([
        Workspace(
            id=workspace_id,
            entity_id=entity_id,
            name="voice purge",
            deleted_at=datetime.now(timezone.utc) - timedelta(days=31),
        ),
        Workspace(
            id=preserved_workspace_id,
            entity_id=entity_id,
            name="voice preserved",
        ),
    ])
    await db_session.flush()
    deleted_session, _ = await create_call_session(
        db_session,
        entity_id=entity_id,
        workspace_id=workspace_id,
        channel_config_id=generate_ulid(),
        direction="inbound",
        call_sid="CA-workspace-purge-deleted",
        from_number="+14155550111",
        to_number="+14155550110",
    )
    preserved_session, _ = await create_call_session(
        db_session,
        entity_id=entity_id,
        workspace_id=preserved_workspace_id,
        channel_config_id=generate_ulid(),
        direction="inbound",
        call_sid="CA-workspace-purge-preserved",
        from_number="+14155550112",
        to_number="+14155550110",
    )
    await db_session.commit()

    assert await purge_workspace(db_session, workspace_id)

    assert await db_session.get(TwilioVoiceCallSession, deleted_session.id) is None
    assert await db_session.get(TwilioVoiceCallSession, preserved_session.id) is not None


