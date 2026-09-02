import pytest

from packages.core.services import document_service
from packages.core.services.document_service import create_document


class _FakeDb:
    def __init__(self):
        self.added = []

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        return None


@pytest.mark.asyncio
async def test_user_owned_root_document_defaults_to_private(monkeypatch):
    async def _skip_storage_check(*_args, **_kwargs):
        return None

    async def _skip_cache_bump(*_args, **_kwargs):
        return None

    monkeypatch.setattr(document_service, "_enforce_storage_limit", _skip_storage_check)
    monkeypatch.setattr(document_service, "bump_tool_cache_version", _skip_cache_bump)

    doc = await create_document(
        _FakeDb(),
        "ent_1",
        name="private-note.md",
        owner_id="user_1",
        emit_created_event=False,
    )

    assert doc.visibility == "private"


@pytest.mark.asyncio
async def test_explicit_document_visibility_is_preserved(monkeypatch):
    async def _skip_storage_check(*_args, **_kwargs):
        return None

    async def _skip_cache_bump(*_args, **_kwargs):
        return None

    monkeypatch.setattr(document_service, "_enforce_storage_limit", _skip_storage_check)
    monkeypatch.setattr(document_service, "bump_tool_cache_version", _skip_cache_bump)

    doc = await create_document(
        _FakeDb(),
        "ent_1",
        name="shared-note.md",
        owner_id="user_1",
        visibility="entity",
        emit_created_event=False,
    )

    assert doc.visibility == "entity"


@pytest.mark.asyncio
async def test_create_document_queues_upload_event_in_owning_transaction(monkeypatch):
    from packages.core.services import event_emitter

    db = _FakeDb()
    emitted = []

    async def _skip_storage_check(*_args, **_kwargs):
        return None

    async def _skip_cache_bump(*_args, **_kwargs):
        return None

    async def _capture_event(event_db, entity_id, event_type, **kwargs):
        emitted.append((event_db, entity_id, event_type, kwargs))
        return 0

    monkeypatch.setattr(document_service, "_enforce_storage_limit", _skip_storage_check)
    monkeypatch.setattr(document_service, "bump_tool_cache_version", _skip_cache_bump)
    monkeypatch.setattr(event_emitter, "emit_in_session", _capture_event)

    document = await create_document(db, "ent_1", name="transactional-note.md")

    assert emitted == [
        (
            db,
            "ent_1",
            "document.uploaded",
            {
                "source": "document_service",
                "payload": {
                    "document_id": document.id,
                    "name": "transactional-note.md",
                },
                "deliver_after_commit": True,
            },
        )
    ]
