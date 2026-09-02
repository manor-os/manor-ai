from __future__ import annotations

from contextlib import asynccontextmanager
from importlib import import_module
import json
from types import SimpleNamespace

import pytest


def test_ledger_write_tools_receive_the_runtime_envelope() -> None:
    from packages.core.ai.runtime.tool_execution import RUNTIME_ENVELOPE_AWARE_TOOLS

    assert {
        "record_content_ledger",
        "record_finance_ledger",
        "record_recruiting_ledger",
        "record_relationship_ledger",
    }.issubset(RUNTIME_ENVELOPE_AWARE_TOOLS)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("module_name", "handler_name"),
    [
        ("content_ledger_tools", "_record_content_ledger"),
        ("finance_ledger_tools", "_record_finance_ledger"),
        ("recruiting_ledger_tools", "_record_recruiting_ledger"),
        ("relationship_ledger_tools", "_record_relationship_ledger"),
    ],
)
async def test_ledger_write_tools_stop_before_storage_when_permission_is_denied(
    monkeypatch: pytest.MonkeyPatch,
    module_name: str,
    handler_name: str,
) -> None:
    module = import_module(f"packages.core.ai.tools.{module_name}")

    async def denied(_context):
        return False

    monkeypatch.setattr(module, "runtime_workspace_ledger_write_allowed", denied)
    result = json.loads(await getattr(module, handler_name)(
        entity_id="entity-1",
        workspace_id="workspace-1",
    ))

    assert result["error"]["code"] == "workspace_ledger_write_forbidden"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("module_name", "handler_name", "storage_name", "contract_id", "arguments"),
    [
        (
            "content_ledger_tools",
            "_record_content_ledger",
            "reserve_content",
            "manor.content_ledger/v1",
            {"action": "reserve", "identity_key": "content-1", "run_key": "run-1"},
        ),
        (
            "finance_ledger_tools",
            "_record_finance_ledger",
            "record_finance_entry",
            "manor.finance_ledger/v1",
            {
                "entry_type": "income",
                "amount_minor": 100,
                "currency": "USD",
                "direction": "inflow",
                "account_ref": "sales",
                "idempotency_key": "finance-1",
            },
        ),
        (
            "relationship_ledger_tools",
            "_record_relationship_ledger",
            "record_relationship_event",
            "manor.relationship_ledger/v1",
            {
                "identity_key": "lead-1",
                "event": "contacted",
                "idempotency_key": "relationship-1",
            },
        ),
    ],
)
async def test_ledger_write_tools_use_the_installed_directory(
    monkeypatch: pytest.MonkeyPatch,
    module_name: str,
    handler_name: str,
    storage_name: str,
    contract_id: str,
    arguments: dict[str, object],
) -> None:
    module = import_module(f"packages.core.ai.tools.{module_name}")
    captured: dict[str, object] = {}

    async def allowed(_context):
        return True

    async def configured(**kwargs):
        assert kwargs["contract_id"] == contract_id
        return {
            "contract_id": contract_id,
            "directory": "custom/private",
            "legacy_directories": ("legacy/private",),
            "legacy_identity_fields": ("legacy_key",),
        }

    async def write(**kwargs):
        captured.update(kwargs)
        return {"ok": True}

    monkeypatch.setattr(module, "runtime_workspace_ledger_write_allowed", allowed)
    monkeypatch.setattr(module, "runtime_workspace_ledger_config", configured)
    monkeypatch.setattr(module, storage_name, write)

    result = json.loads(await getattr(module, handler_name)(
        entity_id="entity-1",
        workspace_id="workspace-1",
        **arguments,
    ))

    assert result["ok"] is True
    assert captured["directory"] == "custom/private"
    if module_name == "content_ledger_tools":
        assert captured["legacy_directories"] == ("legacy/private",)
        assert captured["legacy_identity_fields"] == ("legacy_key",)


@pytest.mark.asyncio
async def test_relationship_ledger_tool_rejects_non_array_contact_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = import_module("packages.core.ai.tools.relationship_ledger_tools")
    storage_called = False

    async def allowed(_context):
        return True

    async def configured(**_kwargs):
        return {
            "contract_id": "manor.relationship_ledger/v1",
            "directory": "relationship-ledger",
        }

    async def write(**_kwargs):
        nonlocal storage_called
        storage_called = True
        return {"ok": True}

    monkeypatch.setattr(module, "runtime_workspace_ledger_write_allowed", allowed)
    monkeypatch.setattr(module, "runtime_workspace_ledger_config", configured)
    monkeypatch.setattr(module, "record_relationship_event", write)

    result = json.loads(await module._record_relationship_ledger(
        entity_id="entity-1",
        workspace_id="workspace-1",
        identity_key="lead-1",
        event="contacted",
        idempotency_key="relationship-invalid-contact",
        contact_points={"kind": "email", "value": "person@example.com"},
    ))

    assert result["error"]["code"] == "invalid_input"
    assert storage_called is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("module_name", "handler_name", "storage_name", "contract_id"),
    [
        (
            "content_ledger_tools",
            "_read_content_ledger",
            "read_content_ledger",
            "manor.content_ledger/v1",
        ),
        (
            "finance_ledger_tools",
            "_read_finance_ledger",
            "read_finance_ledger",
            "manor.finance_ledger/v1",
        ),
        (
            "relationship_ledger_tools",
            "_read_relationship_ledger",
            "read_relationship_ledger",
            "manor.relationship_ledger/v1",
        ),
    ],
)
async def test_ledger_read_tools_use_the_installed_directory(
    monkeypatch: pytest.MonkeyPatch,
    module_name: str,
    handler_name: str,
    storage_name: str,
    contract_id: str,
) -> None:
    module = import_module(f"packages.core.ai.tools.{module_name}")
    captured: dict[str, object] = {}

    async def configured(**kwargs):
        assert kwargs["contract_id"] == contract_id
        return {
            "contract_id": contract_id,
            "directory": "custom/private",
            "legacy_directories": ("legacy/private",),
            "legacy_identity_fields": ("legacy_key",),
        }

    async def read(**kwargs):
        captured.update(kwargs)
        return {"rows": [], "entries": []}

    monkeypatch.setattr(module, "runtime_workspace_ledger_config", configured)
    monkeypatch.setattr(module, storage_name, read)

    result = json.loads(await getattr(module, handler_name)(
        entity_id="entity-1",
        workspace_id="workspace-1",
    ))

    assert result["ok"] is True
    assert captured["directory"] == "custom/private"


@pytest.mark.asyncio
async def test_ledger_write_guard_preserves_background_runtime_semantics() -> None:
    from packages.core.ai.runtime.surfaces import ChatSurface
    from packages.core.ai.runtime.tool_context import RuntimeToolCallContext
    from packages.core.ai.tools.ledger_access import (
        runtime_workspace_ledger_write_allowed,
    )

    context = RuntimeToolCallContext(
        user_id="read-only-initiator",
        workspace_id="workspace-1",
        runtime_envelope=SimpleNamespace(
            surface=ChatSurface.WORKFLOW_AGENT_STEP,
        ),
    )

    assert await runtime_workspace_ledger_write_allowed(context) is True


@pytest.mark.asyncio
async def test_ledger_write_guard_checks_user_permission_for_workspace_chat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from packages.core.ai.runtime.surfaces import ChatSurface
    from packages.core.ai.runtime.tool_context import RuntimeToolCallContext
    from packages.core.ai.runtime import ledger_access

    session = object()
    checked: dict[str, object] = {}

    @asynccontextmanager
    async def fake_session():
        yield session

    async def denied(db, *, workspace_id, user_id, entity_role=None):
        checked.update({
            "db": db,
            "workspace_id": workspace_id,
            "user_id": user_id,
            "entity_role": entity_role,
        })
        return False

    monkeypatch.setattr(ledger_access, "async_session", fake_session)
    monkeypatch.setattr(
        ledger_access,
        "user_can_write_workspace_artifacts",
        denied,
    )
    context = RuntimeToolCallContext(
        user_id="viewer-1",
        workspace_id="workspace-1",
        runtime_envelope=SimpleNamespace(surface=ChatSurface.WORKSPACE_CHAT),
    )

    assert await ledger_access.runtime_workspace_ledger_write_allowed(context) is False
    assert checked == {
        "db": session,
        "workspace_id": "workspace-1",
        "user_id": "viewer-1",
        "entity_role": None,
    }
