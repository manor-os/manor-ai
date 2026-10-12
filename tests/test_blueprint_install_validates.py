"""Every Blueprint installs into a Workspace that passes operation validation.

An install that leaves validation errors behind hands them to every later
Workspace operation (R2-4: ``video-studio-v1`` subscribed five
``product_video.*`` roles but declared only its three ``video_studio.*``
services). The enumeration is repo-wide: a new Blueprint file is covered
without registering it here.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.blueprints.installer import InstallMode, install_blueprint
from packages.core.blueprints.payload import migrate_payload, validate_payload
from packages.core.governance.approval_policy_service import account_defaults
from packages.core.models.base import generate_ulid
from packages.core.models.user import Entity
from packages.core.models.workspace import Workspace, WorkspaceOperationDraft
from packages.core.services import workspace_operation_service as operations

ROOT = Path(__file__).resolve().parents[1]
CONFIG_ROOT = ROOT / "packages/core/blueprints/configs"
BLUEPRINT_FILES = sorted(CONFIG_ROOT.rglob("*.json"))
VIDEO_STUDIO = CONFIG_ROOT / "video_studio/video-studio-v1.json"


def _load(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    validate_payload(payload)
    return payload


def _service_keys(payload: dict) -> set[str]:
    services = (migrate_payload(payload)["recipe"].get("operating_model") or {}).get("services") or []
    return {str(row.get("key") or row.get("service_key")) for row in services if isinstance(row, dict)}


async def _entity(db: AsyncSession) -> str:
    entity_id = generate_ulid()
    db.add(Entity(id=entity_id, name="Install validation", plan_id="plan_enterprise",
                  settings={"approval_defaults": account_defaults()}))
    await db.flush()
    return entity_id


async def _installed_state_validation(db: AsyncSession, workspace_id: str) -> dict:
    """Validate the installed state itself: a draft that changes nothing."""
    workspace = await db.get(Workspace, workspace_id)
    current = await operations._snapshot_workspace_state(db, workspace)
    draft = WorkspaceOperationDraft(
        id=generate_ulid(), workspace_id=workspace.id, entity_id=workspace.entity_id,
        created_by_user_id=None, status="open", base_revision=int(workspace.operation_revision or 0),
        current_state=copy.deepcopy(current), patches=[],
    )
    return await operations.validate_operation_draft(db, draft, current_state=current)


def test_the_enumeration_sees_every_blueprint_family():
    families = {path.parent.name for path in BLUEPRINT_FILES}
    assert {"practical", "creative", "recording"} <= families
    assert len(BLUEPRINT_FILES) >= 17


@pytest.mark.parametrize("path", BLUEPRINT_FILES, ids=lambda path: path.stem)
def test_every_subscription_names_a_declared_service(path):
    payload = _load(path)
    declared = _service_keys(payload)
    if not declared:
        return
    undeclared = sorted(
        sub["service_key"] for sub in migrate_payload(payload)["recipe"].get("subscriptions") or []
        if sub["service_key"] not in declared
    )
    assert undeclared == [], f"{path.name} subscribes undeclared services {undeclared}"


@pytest.mark.asyncio
@pytest.mark.parametrize("path", BLUEPRINT_FILES, ids=lambda path: path.stem)
async def test_every_blueprint_installs_into_a_valid_workspace(db_session: AsyncSession, path):
    payload = _load(path)
    installed = await install_blueprint(
        db_session, entity_id=await _entity(db_session), payload=payload, mode=InstallMode.LIVE,
    )
    validation = await _installed_state_validation(db_session, installed.workspace_id)
    assert validation["pre_existing_issues"] == []
    # The only thing wrong with a draft that changes nothing is that it changes nothing.
    assert [error.get("code") for error in validation["errors"]] == ["no_changes"]


@pytest.mark.asyncio
async def test_installer_declares_subscribed_services_a_payload_left_out(db_session: AsyncSession):
    """The pre-fix R2-4 payload shape still installs into a valid Workspace."""
    if not VIDEO_STUDIO.exists():
        pytest.skip("Video Studio is not part of this distribution")
    payload = _load(VIDEO_STUDIO)
    services = payload["recipe"]["operating_model"]["services"]
    payload["recipe"]["operating_model"]["services"] = [
        service for service in services if not service["key"].startswith("product_video.")
    ]
    assert len(payload["recipe"]["operating_model"]["services"]) == 3
    installed = await install_blueprint(
        db_session, entity_id=await _entity(db_session), payload=payload, mode=InstallMode.LIVE,
    )
    workspace = await db_session.get(Workspace, installed.workspace_id)
    declared = {row["key"]: row["name"] for row in workspace.operating_model["services"]}
    assert declared["product_video.exploration"] == "Product Explorer"
    assert declared["product_video.quality"] == "Product Quality Reviewer"
    assert len(declared) == 8
    validation = await _installed_state_validation(db_session, installed.workspace_id)
    assert validation["pre_existing_issues"] == []
