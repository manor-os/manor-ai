from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from packages.core.ai.runtime import ledger_access
from packages.core.ai.runtime.surfaces import ChatSurface
from packages.core.ai.tools import recruiting_ledger_tools as module
from packages.core.models.base import generate_ulid
from packages.core.models.user import Entity
from packages.core.models.workspace import Workspace


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", ["_read_recruiting_ledger", "_record_recruiting_ledger"])
@pytest.mark.parametrize(
    "surface",
    [ChatSurface.PUBLIC_CUSTOMER_CHAT, ChatSurface.EXTERNAL_CHANNEL_CHAT],
)
async def test_recruiting_tools_reject_external_customer_before_storage(
    monkeypatch: pytest.MonkeyPatch,
    tool_name: str,
    surface: ChatSurface,
) -> None:
    async def storage_must_not_run(**_kwargs):
        raise AssertionError("storage should not run for external customers")

    monkeypatch.setattr(module, "read_recruiting_ledger", storage_must_not_run)
    monkeypatch.setattr(module, "record_recruiting_event", storage_must_not_run)
    tool = getattr(module, tool_name)
    result = json.loads(await tool(
        entity_id="entity-1",
        workspace_id="workspace-1",
        _runtime_envelope_from_context=SimpleNamespace(
            entity_id="entity-1",
            workspace_id="workspace-1",
            surface=surface,
        ),
    ))

    assert result["error"]["code"] == (
        "recruiting_ledger_not_available_on_external_surface"
    )


@pytest.mark.asyncio
async def test_recruiting_directory_resolves_only_from_installed_contract(
    db_session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entity_id = generate_ulid()
    workspace_id = generate_ulid()
    db_session.add(Entity(id=entity_id, name="Recruiting Tool Entity"))
    workspace = Workspace(
        id=workspace_id,
        entity_id=entity_id,
        name="Recruiting",
        settings={
            "ledger_contracts": [{
                "contract_id": "manor.recruiting_ledger/v1",
                "directory": "people/private",
            }],
        },
    )
    db_session.add(workspace)
    await db_session.flush()

    @asynccontextmanager
    async def current_session():
        yield db_session

    monkeypatch.setattr(ledger_access, "async_session", current_session)

    assert await module._configured_directory(
        entity_id=entity_id,
        workspace_id=workspace_id,
        requested_directory=None,
    ) == "people/private"
    with pytest.raises(module.RecruitingLedgerError) as mismatch:
        await module._configured_directory(
            entity_id=entity_id,
            workspace_id=workspace_id,
            requested_directory="recruiting-ledger",
        )
    assert mismatch.value.code == "ledger_directory_not_allowed"

    workspace.settings = {"ledger_contracts": []}
    await db_session.flush()
    with pytest.raises(module.RecruitingLedgerError) as uninstalled:
        await module._configured_directory(
            entity_id=entity_id,
            workspace_id=workspace_id,
            requested_directory=None,
        )
    assert uninstalled.value.code == "ledger_contract_not_installed"

    workspace.settings = {
        "ledger_contracts": [{
            "contract_id": "manor.recruiting_ledger/v1",
            "directory": "people/private",
        }],
    }
    workspace.deleted_at = datetime.now(UTC)
    await db_session.flush()
    with pytest.raises(module.RecruitingLedgerError) as deleted:
        await module._configured_directory(
            entity_id=entity_id,
            workspace_id=workspace_id,
            requested_directory=None,
        )
    assert deleted.value.code == "ledger_contract_not_installed"


@pytest.mark.asyncio
async def test_recruiting_read_uses_configured_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def configured(**_kwargs):
        return "people/private"

    async def read(**kwargs):
        assert kwargs["directory"] == "people/private"
        return {"rows": [], "entries": []}

    monkeypatch.setattr(module, "_configured_directory", configured)
    monkeypatch.setattr(module, "read_recruiting_ledger", read)

    result = json.loads(await module._read_recruiting_ledger(
        entity_id="entity-1",
        workspace_id="workspace-1",
    ))
    assert result["ok"] is True


@pytest.mark.asyncio
async def test_recruiting_write_uses_configured_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def configured(**_kwargs):
        return "people/private"

    async def record(**kwargs):
        assert kwargs["directory"] == "people/private"
        assert kwargs["subject_type"] is None
        assert kwargs["status"] is None
        assert kwargs["clear_fields"] == ["location"]
        return {"ok": True, "event_id": "event-1"}

    monkeypatch.setattr(module, "_configured_directory", configured)
    monkeypatch.setattr(module, "record_recruiting_event", record)

    result = json.loads(await module._record_recruiting_ledger(
        entity_id="entity-1",
        workspace_id="workspace-1",
        record_key="candidate-1",
        event="screen_completed",
        idempotency_key="candidate-1-screen",
        clear_fields=["location"],
    ))
    assert result["ok"] is True
    assert (
        module.RECORD_RECRUITING_LEDGER_SCHEMA["function"]["parameters"]
        ["properties"]["clear_fields"]["items"]["enum"]
        == list(module.RECRUITING_CLEARABLE_FIELDS)
    )
