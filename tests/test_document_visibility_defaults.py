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
    )

    assert doc.visibility == "entity"
