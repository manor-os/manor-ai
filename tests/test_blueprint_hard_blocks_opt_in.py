"""Portable author bans require operator opt-in; templates never carry grants.

Canonical action/category selectors retain exact meaning. Unrepresentable old
patterns may enter the conservative review-only import, but cannot be opted
into as exact bans or grants without a typed replacement.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import select

from packages.core.blueprints.installer import (
    demote_author_hard_blocks,
    install_blueprint,
)
from packages.core.governance.policy import WorkspacePolicy
from packages.core.governance.approval_policy import ApprovalPolicyOverride, ApprovalRule
from packages.core.governance.approval_projection import import_blueprint_governance, policy_override
from packages.core.governance.approval_policy_migration import LegacyApprovalConversionError
from packages.core.governance.defaults import normalize_default_workspace_governance
from packages.core.governance.presets import apply_preset

from packages.core.models.base import generate_ulid  # noqa: E402


@pytest.fixture
def entity_id() -> str:
    return generate_ulid()


CONFIG_DIR = Path("packages/core/blueprints/configs")
PLATFORM_CONFIGS = sorted(CONFIG_DIR.rglob("*.json"))


def _payload(governance: dict) -> dict:
    return {
        "manifest": {"blueprint_version": "1.1", "title": "T", "kind": "social_media", "description": "T"},
        "contract": {
            "variables": [], "channels": [], "sessions": [],
            "requires": {"manor_min_version": None, "tools": [], "mcp_servers": [], "skills": [], "agents": []},
        },
        "embedded": {"skills": [], "agents": [], "knowledge_packs": []},
        "recipe": {
            "operating_model": {}, "strategist": None, "prompts": [], "subscriptions": [],
            "scheduled_jobs": [], "workflows": [], "goals": [], "task_categories": [],
            "custom_fields": [], "sla_policies": [], "escalation_rules": [],
        },
        "policy": {"governance": governance, "post_install_checks": [], "expected_baseline": None},
    }


# ── pure helpers ─────────────────────────────────────────────────────────


def _governance(effect="deny", *, category="purchases"):
    return {"approval": {"mode": None, "rules": [{"id": "author-ban", "effect": effect,
        "selector": {"kind": "category", "category": category}}]}}


def test_demote_moves_denies_to_review_and_reports_them():
    policy = WorkspacePolicy(approval=ApprovalPolicyOverride(rules=[
        ApprovalRule(id="ban", effect="deny", selector={"kind": "category", "category": "purchases"}),
        ApprovalRule(id="review", effect="ask", selector={"kind": "action", "action_key": "external_message.send"}),
    ]))
    demoted, moved = demote_author_hard_blocks(policy)
    assert [rule.effect for rule in policy_override(demoted).rules] == ["ask", "ask"]
    assert moved == ["purchases"]
    assert policy_override(policy).rules[0].effect == "deny"


@pytest.mark.parametrize("preset", ["follow_default", "confirm_changes", "risk_based"])
def test_every_preset_keeps_author_blocks_reviewable_without_grants(preset):
    policy, _ = import_blueprint_governance(_governance(), [], preserve_hard_blocks=True)
    demoted, moved = demote_author_hard_blocks(apply_preset(policy, preset))
    assert [rule.effect for rule in policy_override(demoted).rules] == ["ask"]
    assert moved == ["purchases"]


@pytest.mark.parametrize("config", PLATFORM_CONFIGS, ids=[c.stem for c in PLATFORM_CONFIGS])
def test_every_platform_blueprint_defaults_to_review_without_grants(config: Path):
    payload = json.loads(config.read_text())
    governance = (payload.get("policy") or {}).get("governance") or {}
    sources = (payload.get("recipe") or {}).get("operating_model", {}).get("rules", [])
    base, sources = import_blueprint_governance(governance, sources)
    for preset in ("follow_default", "confirm_changes", "risk_based"):
        _, projected, _ = normalize_default_workspace_governance(sources, apply_preset(base, preset))
        assert all(rule.effect == "ask" for rule in policy_override(projected).rules), (config.stem, preset)
        assert policy_override(projected).mode != "automatic"


@pytest.mark.parametrize("legacy", [
    {"never_allow_actions": ["billing.*"]},
    {"never_allow_capabilities": ["external.social"]},
    {"auto_approve_capabilities": ["custom.export"]},
])
def test_unproven_legacy_selector_cannot_be_opted_into_as_exact_authority(legacy):
    with pytest.raises(LegacyApprovalConversionError, match="no proven equivalent"):
        import_blueprint_governance(legacy, [], preserve_hard_blocks=True)
    review, _ = import_blueprint_governance(legacy, [])
    assert policy_override(review).rules
    assert all(rule.effect == "ask" for rule in policy_override(review).rules)


# ── installer ────────────────────────────────────────────────────────────


async def _policy_row(db_session, workspace_id: str) -> dict:
    from packages.core.models.governance import GovernancePolicy

    row = (
        await db_session.execute(select(GovernancePolicy).where(GovernancePolicy.workspace_id == workspace_id))
    ).scalar_one()
    return dict(row.policy or {})


@pytest.mark.asyncio
async def test_install_demotes_author_hard_blocks_by_default(db_session, entity_id: str):
    result = await install_blueprint(
        db_session,
        entity_id=entity_id,
        payload=_payload(_governance()),
    )
    await db_session.commit()

    policy = await _policy_row(db_session, result.workspace_id)
    assert [rule.effect for rule in policy_override(policy).rules] == ["ask"]
    assert result.demoted_hard_blocks == ["purchases"]
    assert result.governance_applied is True


@pytest.mark.asyncio
async def test_install_keeps_author_hard_blocks_when_operator_opts_in(db_session, entity_id: str):
    result = await install_blueprint(
        db_session,
        entity_id=entity_id,
        payload=_payload(_governance()),
        install_author_hard_blocks=True,
    )
    await db_session.commit()

    policy = await _policy_row(db_session, result.workspace_id)
    assert [rule.effect for rule in policy_override(policy).rules] == ["deny"]
    assert result.demoted_hard_blocks == []


# ── API surface ──────────────────────────────────────────────────────────


def test_setup_preview_lists_author_hard_blocks():
    from apps.api.routers.blueprints import _payload_setup_preview

    preview = _payload_setup_preview(
        _payload(_governance())
    )
    assert preview.author_hard_blocks == ["purchases"]


def test_install_request_defaults_to_not_installing_hard_blocks():
    from apps.api.routers.blueprints import InstallBlueprintRequest

    assert InstallBlueprintRequest().install_author_hard_blocks is False
