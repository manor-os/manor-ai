"""Unit tests for the installer's recipe.workflows + post_install_checks.

Uses postgres via the conftest ``db_session`` fixture (see
test_blueprint_exporter_embedded.py for why we moved off in-memory
sqlite — shared MetaData mutation broke downstream tests in the same
pytest process).

Exercises:
  * blueprint workflow shape (kind/depends_on) → WorkflowDefinition
    shape (type/next) — dependency graph inversion
  * variables list → dict translation
  * idempotent re-install reuses (entity_id, name) row
  * post_install_checks: session_alive / agent_callable / cron_scheduled /
    workflow_present each emit blocking todos on failure and stay silent
    on success
  * unknown check kind emits non-blocking note
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.blueprints.installer import InstallMode, install_blueprint
from packages.core.models.base import generate_ulid
from packages.core.models.integration_session import IntegrationSession
from packages.core.models.workflow import WorkflowDefinition
from packages.core.models.workspace import Agent


@pytest.fixture
def entity_id() -> str:
    return generate_ulid()


def _base_payload(**overrides) -> dict:
    payload = {
        "manifest": {"blueprint_version": "1.1", "title": "T", "kind": "k"},
        "contract": {
            "variables": [],
            "channels": [],
            "sessions": [],
            "requires": {"manor_min_version": None, "tools": [], "mcp_servers": [], "skills": [], "agents": []},
        },
        "embedded": {"skills": [], "agents": [], "knowledge_packs": []},
        "recipe": {
            "operating_model": {},
            "strategist": None,
            "prompts": [],
            "subscriptions": [],
            "scheduled_jobs": [],
            "workflows": [],
            "goals": [],
            "task_categories": [],
            "custom_fields": [],
            "sla_policies": [],
            "escalation_rules": [],
        },
        "policy": {
            "governance": {},
            "post_install_checks": [],
            "expected_baseline": None,
        },
    }
    for k, v in overrides.items():
        path = k.split(".")
        cur = payload
        for s in path[:-1]:
            cur = cur.setdefault(s, {})
        cur[path[-1]] = v
    return payload


# ── Workflow translation ─────────────────────────────────────────────


async def test_workflow_dependency_inversion(
    db_session: AsyncSession,
    entity_id: str,
):
    slug = f"morning-post-with-review-{entity_id}"
    payload = _base_payload(
        **{
            "recipe.workflows": [
                {
                    "slug": slug,
                    "trigger_type": "scheduled",
                    "trigger_ref": "morning-draft",
                    "variables": [
                        {"key": "post_topic", "default": "product_update"},
                    ],
                    "steps": [
                        {"id": "draft", "kind": "agent_call", "service_key": "social.x.poster", "input": "Draft post"},
                        {
                            "id": "review",
                            "kind": "hitl_approval",
                            "depends_on": ["draft"],
                            "channel": "telegram",
                            "timeout_minutes": 60,
                        },
                        {"id": "post", "kind": "tool_call", "depends_on": ["review"], "tool": "tool.x.post"},
                    ],
                }
            ],
        }
    )
    await install_blueprint(db_session, entity_id=entity_id, payload=payload)
    await db_session.commit()

    wf = (
        await db_session.execute(
            select(WorkflowDefinition).where(
                WorkflowDefinition.entity_id == entity_id,
                WorkflowDefinition.name == slug,
            )
        )
    ).scalar_one()

    assert wf.variables == {"post_topic": "product_update"}
    assert wf.trigger_type == "scheduled"
    assert wf.trigger_config == {"trigger_ref": "morning-draft"}
    steps_by_id = {s["id"]: s for s in wf.steps}
    assert steps_by_id["draft"]["next"] == ["review"]
    assert steps_by_id["review"]["next"] == ["post"]
    assert steps_by_id["post"]["next"] == []
    assert steps_by_id["draft"]["type"] == "agent_call"
    assert steps_by_id["review"]["type"] == "hitl_approval"
    assert steps_by_id["draft"]["config"]["service_key"] == "social.x.poster"
    assert steps_by_id["review"]["config"]["timeout_minutes"] == 60


async def test_workflow_idempotent_reinstall(
    db_session: AsyncSession,
    entity_id: str,
):
    slug = f"wf-one-{entity_id}"
    payload = _base_payload(
        **{
            "recipe.workflows": [
                {
                    "slug": slug,
                    "trigger_type": "manual",
                    "variables": [],
                    "steps": [
                        {"id": "s1", "kind": "agent_call"},
                    ],
                }
            ],
        }
    )
    await install_blueprint(db_session, entity_id=entity_id, payload=payload)
    await db_session.commit()
    await install_blueprint(db_session, entity_id=entity_id, payload=payload)
    await db_session.commit()
    rows = list(
        (
            await db_session.execute(
                select(WorkflowDefinition).where(
                    WorkflowDefinition.entity_id == entity_id,
                    WorkflowDefinition.name == slug,
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1


async def test_workflow_diamond_dependencies(
    db_session: AsyncSession,
    entity_id: str,
):
    """A step depended on by multiple downstreams populates `next` with
    all of them."""
    slug = f"diamond-{entity_id}"
    payload = _base_payload(
        **{
            "recipe.workflows": [
                {
                    "slug": slug,
                    "trigger_type": "manual",
                    "variables": [],
                    "steps": [
                        {"id": "root", "kind": "agent_call"},
                        {"id": "branch_a", "kind": "agent_call", "depends_on": ["root"]},
                        {"id": "branch_b", "kind": "agent_call", "depends_on": ["root"]},
                        {"id": "join", "kind": "agent_call", "depends_on": ["branch_a", "branch_b"]},
                    ],
                }
            ],
        }
    )
    await install_blueprint(db_session, entity_id=entity_id, payload=payload)
    await db_session.commit()
    wf = (await db_session.execute(select(WorkflowDefinition).where(WorkflowDefinition.name == slug))).scalar_one()
    steps_by_id = {s["id"]: s for s in wf.steps}
    assert sorted(steps_by_id["root"]["next"]) == ["branch_a", "branch_b"]
    assert steps_by_id["branch_a"]["next"] == ["join"]
    assert steps_by_id["branch_b"]["next"] == ["join"]
    assert steps_by_id["join"]["next"] == []


# ── post_install_checks ───────────────────────────────────────────────


async def test_check_session_alive_pass(
    db_session: AsyncSession,
    entity_id: str,
):
    db_session.add(
        IntegrationSession(
            id=generate_ulid(),
            entity_id=entity_id,
            provider="x",
            label="main",
            status="active",
        )
    )
    await db_session.commit()
    payload = _base_payload(
        **{
            "policy.post_install_checks": [
                {"kind": "session_alive", "provider": "x", "session_label": "main"},
            ],
        }
    )
    result = await install_blueprint(
        db_session,
        entity_id=entity_id,
        payload=payload,
        mode=InstallMode.SIMULATE,
    )
    await db_session.commit()
    pic_todos = [t for t in result.todos if t.kind == "post_install_check"]
    assert pic_todos == []


async def test_check_session_alive_fail(
    db_session: AsyncSession,
    entity_id: str,
):
    payload = _base_payload(
        **{
            "policy.post_install_checks": [
                {"kind": "session_alive", "provider": "x", "session_label": "main"},
            ],
        }
    )
    result = await install_blueprint(
        db_session,
        entity_id=entity_id,
        payload=payload,
        mode=InstallMode.SIMULATE,
    )
    await db_session.commit()
    pic_todos = [t for t in result.todos if t.kind == "post_install_check"]
    assert len(pic_todos) == 1
    assert pic_todos[0].blocking is True
    assert "no active session" in pic_todos[0].detail


async def test_check_agent_callable_pass(
    db_session: AsyncSession,
    entity_id: str,
):
    agent_slug = f"ax-{entity_id}"
    db_session.add(
        Agent(
            id=generate_ulid(),
            entity_id=entity_id,
            name="A",
            slug=agent_slug,
            is_public=True,
            status="active",
        )
    )
    await db_session.commit()
    payload = _base_payload(
        **{
            "recipe.subscriptions": [
                {
                    "service_key": "x.svc",
                    "agent_slug": agent_slug,
                    "custom_prompt": None,
                    "config": {},
                }
            ],
            "policy.post_install_checks": [
                {"kind": "agent_callable", "service_key": "x.svc"},
            ],
        }
    )
    result = await install_blueprint(db_session, entity_id=entity_id, payload=payload)
    await db_session.commit()
    pic_todos = [t for t in result.todos if t.kind == "post_install_check"]
    assert pic_todos == []


async def test_check_agent_callable_fail(
    db_session: AsyncSession,
    entity_id: str,
):
    payload = _base_payload(
        **{
            "policy.post_install_checks": [
                {"kind": "agent_callable", "service_key": "x.svc"},
            ],
        }
    )
    result = await install_blueprint(db_session, entity_id=entity_id, payload=payload)
    await db_session.commit()
    pic_todos = [t for t in result.todos if t.kind == "post_install_check"]
    assert len(pic_todos) == 1
    assert "no active subscription" in pic_todos[0].detail


async def test_check_cron_scheduled_pass(
    db_session: AsyncSession,
    entity_id: str,
):
    payload = _base_payload(
        **{
            "recipe.scheduled_jobs": [
                {
                    "job_id": "morning-draft",
                    "name": "Morning",
                    "schedule_kind": "cron",
                    "cron_expr": "0 8 * * *",
                    "execution_type": "agent_message",
                    "execution_target": {"service_key": "x.svc"},
                    "payload_message": "draft",
                }
            ],
            "policy.post_install_checks": [
                {"kind": "cron_scheduled", "job_id": "morning-draft"},
            ],
        }
    )
    result = await install_blueprint(db_session, entity_id=entity_id, payload=payload)
    await db_session.commit()
    pic_todos = [t for t in result.todos if t.kind == "post_install_check"]
    assert pic_todos == []


async def test_check_cron_scheduled_fail(
    db_session: AsyncSession,
    entity_id: str,
):
    payload = _base_payload(
        **{
            "policy.post_install_checks": [
                {"kind": "cron_scheduled", "job_id": "morning-draft"},
            ],
        }
    )
    result = await install_blueprint(db_session, entity_id=entity_id, payload=payload)
    await db_session.commit()
    pic_todos = [t for t in result.todos if t.kind == "post_install_check"]
    assert len(pic_todos) == 1
    assert pic_todos[0].blocking is True


async def test_check_workflow_present_pass(
    db_session: AsyncSession,
    entity_id: str,
):
    slug = f"wf-test-{entity_id}"
    payload = _base_payload(
        **{
            "recipe.workflows": [
                {
                    "slug": slug,
                    "trigger_type": "manual",
                    "variables": [],
                    "steps": [],
                }
            ],
            "policy.post_install_checks": [
                {"kind": "workflow_dryrun", "workflow_slug": slug},
            ],
        }
    )
    result = await install_blueprint(db_session, entity_id=entity_id, payload=payload)
    await db_session.commit()
    pic_todos = [t for t in result.todos if t.kind == "post_install_check"]
    assert pic_todos == []


async def test_check_workflow_present_fail(
    db_session: AsyncSession,
    entity_id: str,
):
    payload = _base_payload(
        **{
            "policy.post_install_checks": [
                {"kind": "workflow_dryrun", "workflow_slug": f"wf-missing-{entity_id}"},
            ],
        }
    )
    result = await install_blueprint(db_session, entity_id=entity_id, payload=payload)
    await db_session.commit()
    pic_todos = [t for t in result.todos if t.kind == "post_install_check"]
    assert len(pic_todos) == 1
    assert f"wf-missing-{entity_id}" in pic_todos[0].detail


async def test_check_unknown_kind_non_blocking_note(
    db_session: AsyncSession,
    entity_id: str,
):
    payload = _base_payload(
        **{
            "policy.post_install_checks": [
                {"kind": "ping_satellite", "freq_mhz": 2400},
            ],
        }
    )
    result = await install_blueprint(db_session, entity_id=entity_id, payload=payload)
    await db_session.commit()
    pic_todos = [t for t in result.todos if t.kind == "post_install_check"]
    assert len(pic_todos) == 1
    assert pic_todos[0].blocking is False
    assert "ping_satellite" in pic_todos[0].detail
