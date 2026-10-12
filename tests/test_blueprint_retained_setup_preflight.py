"""Retained setup is an account contract, not a hybrid portable Blueprint."""
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import HTTPException
import pytest

from apps.api.routers import workspaces as routes
from packages.core.blueprints import reconfigure
from packages.core.blueprints.payload import PayloadError, validate_payload
from packages.core.models.base import generate_ulid
from packages.core.services import reusable_resource_locks
from packages.core.services.provider_keys import canonical_provider_key


@pytest.fixture
def source_payload():
    root = Path(__file__).resolve().parents[1]
    return json.loads((root / "packages/core/blueprints/configs/creative/creative-handbook-v1.json").read_text())


def _retained_contract(source):
    contract = deepcopy(source["contract"])
    contract["requires"]["tools"].remove("compose_creative_publication")
    return contract


def _route_context(monkeypatch, source, retained):
    workspace = SimpleNamespace(id=generate_ulid(), entity_id=generate_ulid(), settings={})
    user = SimpleNamespace(id=generate_ulid(), entity_id=workspace.entity_id)
    monkeypatch.setattr(reusable_resource_locks, "lock_reusable_resource_lifecycle", AsyncMock())
    monkeypatch.setattr(routes, "_require_workspace_manage", AsyncMock(return_value=workspace))
    monkeypatch.setattr(routes, "_reconfiguration_payload", AsyncMock(return_value=(
        source, source, source, {}, {}, "1.0.6", "builtin:creative-handbook-v1",
    )))
    monkeypatch.setattr(reconfigure, "_state", AsyncMock(return_value=SimpleNamespace(
        baseline={"runtime": {"installed": {"contract": retained}}},
    )))
    monkeypatch.setattr(reconfigure, "plan", AsyncMock(return_value={
        "items": [{"key": reconfigure.RUNTIME_KEY, "action": "update"}],
    }))
    return workspace, user


async def test_preview_evaluates_old_setup_without_hybrid_payload_validation(
    db_session, monkeypatch, source_payload,
):
    retained = _retained_contract(source_payload)
    source_payload["contract"]["requires"]["mcp_servers"] = [{
        "slug": f"upstream-unconnected-{generate_ulid()}", "required": True,
    }]
    validate_payload(source_payload)
    hybrid = deepcopy(source_payload)
    hybrid["contract"] = retained
    with pytest.raises(PayloadError, match="compose_creative_publication"):
        validate_payload(hybrid)
    before = deepcopy(source_payload)
    workspace, user = _route_context(monkeypatch, source_payload, retained)

    result = await routes._preview_blueprint_reconfiguration(
        workspace_id=workspace.id, variable_values={}, channel_config_ids={}, user=user, db=db_session,
    )

    assert result["variables_ready"] is True
    assert result["setup_preflight"]["ready"] is False
    assert result["setup_preflight"]["blocking_count"] == 1
    assert result["kept_setup_preflight"] == {"ready": True, "blocking_count": 0, "requirements": []}
    assert source_payload == before
    assert "compose_creative_publication" not in retained["requires"]["tools"]


@pytest.mark.parametrize("keep_setup", [False, True])
async def test_invalid_new_source_is_rejected_even_when_setup_is_retained(
    db_session, source_payload, keep_setup,
):
    retained = _retained_contract(source_payload)
    source_payload["contract"] = deepcopy(retained)
    with pytest.raises(HTTPException) as error:
        await routes._blueprint_upgrade_setup_preflight(
            db_session, payload=source_payload,
            retained_contract=retained if keep_setup else None,
            user=SimpleNamespace(id=generate_ulid(), entity_id=generate_ulid()),
            workspace_id=generate_ulid(),
        )
    assert error.value.status_code == 400


@pytest.mark.parametrize("retained_account_missing", [False, True])
async def test_apply_keep_setup_uses_real_retained_account_preflight(
    db_session, monkeypatch, source_payload, retained_account_missing,
):
    retained = _retained_contract(source_payload)
    if retained_account_missing:
        retained["requires"]["mcp_servers"] = [{
            "slug": f"retained-unconnected-{generate_ulid()}", "required": True,
        }]
    source_payload["contract"]["requires"]["mcp_servers"] = [{
        "slug": f"new-unconnected-{generate_ulid()}", "required": True,
    }]
    workspace, user = _route_context(monkeypatch, source_payload, retained)
    apply = AsyncMock(return_value={"upgrade_protocol_version": 3, "synchronization_status": "customized"})
    monkeypatch.setattr(reconfigure, "apply", apply)
    commit = AsyncMock()
    monkeypatch.setattr(db_session, "commit", commit)
    request = routes.BlueprintReconfigurationApplyRequest(
        expected_blueprint_fingerprint="source", expected_plan_fingerprint="plan",
        configuration_resolutions=[{"key": reconfigure.RUNTIME_KEY, "resolution": "keep_yours"}],
    )

    if retained_account_missing:
        with pytest.raises(HTTPException) as error:
            await routes.apply_blueprint_reconfiguration(workspace.id, request, user, db_session)
        assert error.value.status_code == 409
        assert error.value.detail["code"] == "blueprint_setup_required"
        assert error.value.detail["preflight"]["blocking_count"] == 1
        assert error.value.detail["preflight"]["requirements"][0]["provider"] == canonical_provider_key(
            retained["requires"]["mcp_servers"][0]["slug"],
        )
        apply.assert_not_awaited()
        commit.assert_not_awaited()
    else:
        result = await routes.apply_blueprint_reconfiguration(workspace.id, request, user, db_session)
        assert result["synchronization_status"] == "customized"
        apply.assert_awaited_once()
        assert apply.call_args.kwargs["payload"] is source_payload
        commit.assert_awaited_once()
