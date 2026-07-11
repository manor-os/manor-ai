"""End-to-end smoke for the M12.1 blueprint flow.

Cases (against an in-memory sqlite DB):

  1. Build a workspace by hand → export → payload validates + has the
     expected sections + no forbidden keys
  2. Install (simulate) the payload as a fresh workspace → settings.sandbox
     is true, _blueprint metadata is stamped, [SIM] prefix on name
  3. Subscriptions resolve agents by slug; missing slug becomes a todo
  4. Goals + scheduled jobs + custom fields + governance all materialise
  5. Channel + session requirements appear in result.todos as blocking
  6. preflight_promote returns the expected unmet items
  7. Pair the missing channel + capture the missing session →
     preflight_promote returns []
  8. promote_workspace flips settings.sandbox=false + restores kind +
     strips [SIM] prefix
  9. Promote on a non-sandbox workspace raises PromoteError
 10. Forbidden-key payload (with credential_ref) is rejected by validate_payload

Run with: uv run python -m packages.core.blueprints._smoke
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

# Use the dev credential backend — sessions service still resolves
# get_credential_service even though we don't actually decrypt anything.
os.environ.setdefault("CREDENTIAL_BACKEND", "dev")

from sqlalchemy import JSON, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from packages.core.blueprints import (
    InstallMode,
    PayloadError,
    PromoteError,
    export_workspace,
    install_blueprint,
    preflight_promote,
    promote_workspace,
)
from packages.core.blueprints.payload import validate_payload
from packages.core.governance import WorkspacePolicy, update_policy
from packages.core.models.base import Base, generate_ulid
from packages.core.models.blueprint import WorkspaceBlueprint
from packages.core.models.channel import ChannelConfig
from packages.core.models.custom_field import CustomFieldDefinition
from packages.core.models.document import Channel
from packages.core.governance import WorkspacePolicy, get_policy
from packages.core.governance.presets import apply_preset, list_presets
from packages.core.blueprints.report import simulate_report
from packages.core.models.execution import ExecutionPlan, ExecutionStep
from packages.core.models.goal import Goal, GoalMeasurement
from packages.core.models.governance import GovernancePolicy, GovernanceRevision
from packages.core.models.document import Document, DocumentGroup
from packages.core.models.integration_session import IntegrationSession
from packages.core.models.mcp import AgentMCPBinding, MCPServer
from packages.core.models.memory import AgentMemory
from packages.core.models.runtime_learning import RuntimeEvidence
from packages.core.models.scheduler import ScheduledJob
from packages.core.models.skill import AgentSkillBinding, Skill
from packages.core.models.workflow import WorkflowDefinition
from packages.core.models.workspace import (
    Agent,
    AgentSubscription,
    AgentToolBinding,
    ToolDefinition,
    Workspace,
)


def _check(cond: bool, msg: str) -> None:
    print(f"  {'✓' if cond else '✗'} {msg}")
    if not cond:
        sys.exit(1)


# ── DB scaffolding (sqlite-compatible) ───────────────────────────────

# Tables touched by the test. Keep this list narrow so we don't create
# half the schema for an in-memory smoke.
_TABLES = [
    Workspace.__table__,
    ExecutionPlan.__table__,
    ExecutionStep.__table__,
    GoalMeasurement.__table__,
    Agent.__table__,
    AgentSubscription.__table__,
    Goal.__table__,
    ScheduledJob.__table__,
    CustomFieldDefinition.__table__,
    ChannelConfig.__table__,
    Channel.__table__,
    IntegrationSession.__table__,
    GovernancePolicy.__table__,
    GovernanceRevision.__table__,
    WorkspaceBlueprint.__table__,
    RuntimeEvidence.__table__,
    # v1.1 embedded.* exporter touches these.
    AgentToolBinding.__table__,
    ToolDefinition.__table__,
    AgentMCPBinding.__table__,
    MCPServer.__table__,
    AgentSkillBinding.__table__,
    Skill.__table__,
    AgentMemory.__table__,
    DocumentGroup.__table__,
    Document.__table__,
    WorkflowDefinition.__table__,
]


async def _build_engine():
    # JSONB → JSON, ARRAY(String) → JSON for sqlite portability.
    from sqlalchemy.dialects.postgresql import ARRAY
    for tbl in _TABLES:
        for col in tbl.columns:
            if isinstance(col.type, JSONB):
                col.type = JSON()
            elif isinstance(col.type, ARRAY):
                col.type = JSON()
                # Strip the postgres-only "{}" default + relax NOT NULL
                # so seeds don't have to populate every Workspace tag.
                if col.server_default is not None:
                    col.server_default = None
                col.nullable = True
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        for tbl in _TABLES:
            await conn.run_sync(tbl.create)
    return engine


async def _seed_workspace(db: AsyncSession) -> tuple[Workspace, Agent]:
    """Build a small but realistic workspace: 1 agent + 1 subscription
    + 1 goal + 1 scheduled job + 1 custom field + 1 governance policy +
    1 channel config + 1 integration session."""
    ws = Workspace(
        id=generate_ulid(),
        entity_id="ent_demo",
        name="X Growth (Calvin)",
        description="Daily posts + reply triage",
        kind="social_media",
        operating_context="@calvin handle",
        primary_work="Post + engage daily",
        operating_model={"services": [{"key": "social.x.poster"}]},
        settings={"timezone": "America/Los_Angeles", "sandbox": False},
    )
    db.add(ws)
    await db.flush()

    agent = Agent(
        id=generate_ulid(),
        entity_id="ent_demo",
        name="X Poster v2",
        slug="x-poster-v2",
        system_prompt="Draft tweets in the operator's voice.",
        is_template=False,
        # Marketplace-style: this agent should be exported as an EXTERNAL
        # requirement (contract.requires.agents) rather than embedded —
        # blueprints reference it by slug, not by inlining the brain.
        is_public=True,
        status="active",
    )
    db.add(agent)
    await db.flush()

    sub = AgentSubscription(
        id=generate_ulid(),
        entity_id="ent_demo",
        agent_id=agent.id,
        workspace_id=ws.id,
        service_key="social.x.poster",
        custom_prompt="Use first-person, never use hashtags.",
        config={"max_posts_per_day": 3},
        status="active",
    )
    db.add(sub)

    goal = Goal(
        id=generate_ulid(),
        entity_id="ent_demo",
        workspace_id=ws.id,
        title="Reach 10k X followers",
        metric_key="follower_count",
        target_value=Decimal("10000"),
        baseline_value=Decimal("1500"),
        current_value=Decimal("2100"),  # runtime — should NOT export
        deadline=date.today() + timedelta(days=180),
        measurement_source={"provider": "x", "action": "x.get_profile_stats"},
        measurement_cadence="daily",
        priority=2,
        status="active",
        pace_status="on_track",  # runtime
    )
    db.add(goal)

    sj = ScheduledJob(
        id=generate_ulid(),
        job_id="morning-draft",
        entity_id="ent_demo",
        workspace_id=ws.id,
        name="Morning post draft",
        job_type="cron",
        schedule_kind="cron",
        cron_expr="0 8 * * *",
        timezone="America/Los_Angeles",
        execution_type="agent_message",
        execution_target={"service_key": "social.x.poster"},
        payload_message="Draft tomorrow's posts.",
        enabled=True,
        last_run_at=datetime.now(timezone.utc),  # runtime — should NOT export
    )
    db.add(sj)

    cf = CustomFieldDefinition(
        id=generate_ulid(),
        entity_id="ent_demo",
        workspace_id=ws.id,
        name="campaign_tag",
        display_name="Campaign",
        field_type="select",
        target="task",
        options=["launch", "evergreen"],
        required=False,
        sort_order=10,
        status="active",
    )
    db.add(cf)

    cc = ChannelConfig(
        id=generate_ulid(),
        entity_id="ent_demo",
        workspace_id=ws.id,
        channel_type="telegram",
        provider="telegram_bot",
        name="alerts",
        config={"chat_id": "123"},
        credentials={},
        status="active",
    )
    db.add(cc)

    sess = IntegrationSession(
        id=generate_ulid(),
        entity_id="ent_demo",
        provider="x",
        label="main",
        status="active",
        health_check={"url": "https://x.com/home", "expected_text": "Home"},
        metadata_json={
            "expected_login_url": "https://x.com/login",
            "purpose": "post + read DMs",
        },
    )
    db.add(sess)

    await update_policy(
        db,
        entity_id="ent_demo",
        workspace_id=ws.id,
        policy=WorkspacePolicy(
            never_allow_actions=["billing.*"],
            hitl_required_actions=["x.delete_*"],
            max_risk_level="medium",
        ),
        changed_by="user_demo",
    )

    await db.flush()
    return ws, agent


# ── Cases ─────────────────────────────────────────────────────────────

async def main() -> None:
    engine = await _build_engine()
    SessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    # ─────────────────────────────────────────────────────────────
    print("[case] export workspace → valid payload, no leaks, no runtime")
    async with SessionLocal() as db:
        src_ws, src_agent = await _seed_workspace(db)
        await db.commit()

        payload = await export_workspace(
            db, src_ws.id,
            title="X Growth (Calvin's recipe)",
            summary="Daily posts + replies + engagement",
            tags=["social_media", "growth"],
            author_handle="calvin",
        )

    _check(payload["manifest"]["blueprint_version"] == "1.1", "blueprint_version present (v1.1)")
    _check(payload["manifest"]["kind"] == "social_media", "manifest.kind preserved")
    om = payload["recipe"]["operating_model"]
    _check(om.get("kind") == "social_media", "operating_model.kind preserved")
    _check(om.get("context") == "@calvin handle", "operating_model.context preserved")
    _check(om.get("primary_work") == "Post + engage daily", "operating_model.primary_work preserved")
    _check("sandbox" not in (om.get("settings") or {}), "runtime sandbox flag stripped from operating_model.settings")
    _check(len(payload["recipe"]["subscriptions"]) == 1, "1 subscription exported")
    _check(payload["recipe"]["subscriptions"][0]["agent_slug"] == "x-poster-v2", "agent_id → agent_slug")
    _check("agent_id" not in payload["recipe"]["subscriptions"][0], "raw agent_id NOT exported")
    _check(len(payload["recipe"]["goals"]) == 1, "1 goal exported")
    g = payload["recipe"]["goals"][0]
    _check("current_value" not in g, "goal.current_value (runtime) NOT exported")
    _check("pace_status" not in g, "goal.pace_status (runtime) NOT exported")
    _check(g["target_value"] == 10000.0, "goal.target_value preserved")
    _check(g["baseline_value"] == 1500.0, "goal.baseline_value preserved")
    _check(len(payload["recipe"]["scheduled_jobs"]) == 1, "1 scheduled_job exported")
    _check("last_run_at" not in payload["recipe"]["scheduled_jobs"][0], "scheduled_job.last_run_at (runtime) NOT exported")
    _check(len(payload["recipe"]["custom_fields"]) == 1, "1 custom_field exported")
    _check(payload["policy"]["governance"]["max_risk_level"] == "medium", "governance exported")
    _check(len(payload["contract"]["channels"]) == 1, "channel requirement surfaced")
    _check(payload["contract"]["channels"][0]["channel_type"] == "telegram", "telegram channel listed")
    _check(len(payload["contract"]["sessions"]) == 1, "session requirement surfaced")
    _check(payload["contract"]["sessions"][0]["provider"] == "x", "x session listed")
    # v1.1 envelope present even when empty.
    _check(isinstance(payload["embedded"]["agents"], list), "embedded.agents present")
    _check(isinstance(payload["embedded"]["skills"], list), "embedded.skills present")
    _check(isinstance(payload["recipe"]["workflows"], list), "recipe.workflows present")
    # x-poster-v2 is public → external requirement, not embedded.
    _check(len(payload["embedded"]["agents"]) == 0,
           "public agent stays external (not embedded)")
    req_agent_slugs = [a["slug"] for a in payload["contract"]["requires"]["agents"]]
    _check("x-poster-v2" in req_agent_slugs,
           "public agent declared in contract.requires.agents")

    # ─────────────────────────────────────────────────────────────
    print("\n[case] forbidden keys are rejected (v1.1)")
    bad = dict(payload)
    bad_recipe = dict(payload["recipe"])
    bad_om = dict(payload["recipe"]["operating_model"])
    bad_om["credential_ref"] = "vault:v1:LEAK"
    bad_recipe["operating_model"] = bad_om
    bad["recipe"] = bad_recipe
    try:
        validate_payload(bad)
        _check(False, "should have raised")
    except PayloadError as exc:
        _check("credential_ref" in str(exc), "credential_ref leak detected")

    # ─────────────────────────────────────────────────────────────
    print("\n[case] install (simulate) → sandbox + [SIM] prefix + _blueprint stamp")
    async with SessionLocal() as db:
        result = await install_blueprint(
            db, entity_id="ent_demo",
            payload=payload,
            mode=InstallMode.SIMULATE,
            user_id="user_demo",
            blueprint_id="blp_test", blueprint_slug="x-growth",
        )
        await db.commit()

        new_ws = (await db.execute(
            select(Workspace).where(Workspace.id == result.workspace_id)
        )).scalar_one()

    _check(new_ws.id != src_ws.id, "new workspace id (not the source)")
    _check(new_ws.name.startswith("[SIM] "), "[SIM] prefix added")
    _check(new_ws.settings["sandbox"] is True, "settings.sandbox=true")
    _check(new_ws.settings["_blueprint"]["install_mode"] == "simulate", "blueprint metadata stamped")
    _check(new_ws.settings["_blueprint"]["original_kind"] == "social_media", "original_kind preserved")
    _check(new_ws.kind == "social_media", "workspace.kind preserved during install")
    _check(len(result.subscription_ids) == 1, "1 subscription installed")
    _check(len(result.goal_ids) == 1, "1 goal installed")
    _check(len(result.scheduled_job_ids) == 1, "1 scheduled job installed")
    _check(len(result.custom_field_ids) == 1, "1 custom field installed")
    _check(result.governance_applied is True, "governance applied")

    # Channel + session todos surfaced.
    todo_kinds = sorted({t.kind for t in result.todos})
    _check("channel" in todo_kinds, "channel todo surfaced")
    _check("browser_session" in todo_kinds, "browser_session todo surfaced")
    _check(all(t.blocking for t in result.todos if t.kind in ("channel", "browser_session")),
           "channel + session todos are blocking")

    new_ws_id = result.workspace_id

    # ─────────────────────────────────────────────────────────────
    print("\n[case] missing agent → install todo, subscription skipped")
    async with SessionLocal() as db:
        bad_payload = dict(payload)
        bad_recipe2 = dict(payload["recipe"])
        bad_recipe2["subscriptions"] = [{
            "service_key": "x.unknown",
            "agent_slug": "no-such-agent",
            "custom_prompt": None,
            "config": {},
        }]
        bad_payload["recipe"] = bad_recipe2
        result_b = await install_blueprint(
            db, entity_id="ent_demo",
            payload=bad_payload,
            mode=InstallMode.SIMULATE,
        )
        await db.commit()
    _check(len(result_b.subscription_ids) == 0, "missing-agent subscription skipped")
    missing = [t for t in result_b.todos if t.kind == "missing_agent"]
    _check(len(missing) == 1, "missing_agent todo emitted")

    # ─────────────────────────────────────────────────────────────
    print("\n[case] preflight_promote lists unmet requirements")
    # Sessions are entity-scoped, and the seed already has an active
    # x/main session — so preflight only flags the channel requirement
    # (channels are workspace-scoped, no Channel row exists for new_ws yet).
    async with SessionLocal() as db:
        unmet = await preflight_promote(db, new_ws_id)
    _check(len(unmet) == 1, f"1 unmet (channel only), got {len(unmet)}")
    _check(unmet[0].kind == "channel", "the unmet item is the channel")

    # ─────────────────────────────────────────────────────────────
    print("\n[case] promote without preflight pass → not promoted")
    async with SessionLocal() as db:
        bad_promote = await promote_workspace(db, new_ws_id, force=False)
    _check(bad_promote.promoted is False, "promote refused")
    _check(len(bad_promote.unmet) == 1, "unmet returned in response")

    # ─────────────────────────────────────────────────────────────
    print("\n[case] satisfy requirements → preflight clean → promote succeeds")
    async with SessionLocal() as db:
        # Pair the channel (the only thing missing — session was already
        # active in the entity from the seed).
        db.add(Channel(
            id=generate_ulid(), entity_id="ent_demo",
            workspace_id=new_ws_id,
            type="telegram", name="alerts",
            config={"chat_id": "777"}, status="active",
        ))
        await db.commit()

        unmet2 = await preflight_promote(db, new_ws_id)
        _check(len(unmet2) == 0, "preflight now clean")

        promoted = await promote_workspace(db, new_ws_id, user_id="user_demo")
        await db.commit()

        new_ws_after = (await db.execute(
            select(Workspace).where(Workspace.id == new_ws_id)
        )).scalar_one()

    _check(promoted.promoted is True, "promote succeeded")
    _check(new_ws_after.settings["sandbox"] is False, "settings.sandbox flipped to false")
    _check(not new_ws_after.name.startswith("[SIM] "), "[SIM] prefix stripped")
    _check(new_ws_after.kind == "social_media", "kind restored from _blueprint metadata")
    _check(
        len(new_ws_after.settings["_blueprint"]["promotions"]) == 1,
        "promotion event recorded in audit",
    )

    # ─────────────────────────────────────────────────────────────
    print("\n[case] promote on already-live workspace raises PromoteError")
    async with SessionLocal() as db:
        try:
            await promote_workspace(db, new_ws_id)
            _check(False, "should have raised")
        except PromoteError as exc:
            _check("not in sandbox" in str(exc), "error names the cause")

    # ─────────────────────────────────────────────────────────────
    print("\n[case] install (live) → sandbox=false from the start, no [SIM] prefix")
    async with SessionLocal() as db:
        live_result = await install_blueprint(
            db, entity_id="ent_demo",
            payload=payload,
            mode=InstallMode.LIVE,
            workspace_name="Live X Growth",
        )
        await db.commit()
        live_ws = (await db.execute(
            select(Workspace).where(Workspace.id == live_result.workspace_id)
        )).scalar_one()
    _check(live_ws.settings.get("sandbox") in (False, None), "live install: sandbox not set")
    _check(not live_ws.name.startswith("[SIM] "), "no [SIM] prefix on live install")
    _check(live_ws.name == "Live X Growth", "custom workspace_name honoured")

    # ─────────────────────────────────────────────────────────────────
    # M12.4 — Governance presets
    # ─────────────────────────────────────────────────────────────────

    print("\n[case] governance preset list has 3 entries in canonical order")
    presets = list_presets()
    _check(len(presets) == 3, f"3 presets, got {len(presets)}")
    _check(
        [p.key for p in presets] == ["safe", "standard", "aggressive"],
        "canonical order Safe → Standard → Aggressive",
    )

    print("\n[case] safe preset tightens risk + adds * to HITL + halves caps")
    base = WorkspacePolicy(
        max_risk_level="high",
        hitl_required_actions=["x.delete_*"],
        budget_caps_per_kind={"action": 200, "llm": 100},
    )
    safe = apply_preset(base, "safe")
    _check(safe.max_risk_level == "low", "max_risk_level lowered to low")
    _check("*" in safe.hitl_required_actions, "wildcard HITL added")
    _check(safe.budget_caps_per_kind["action"] == 100, "action cap halved (200→100)")
    _check(safe.budget_caps_per_kind["llm"] == 50, "llm cap halved (100→50)")
    _check("code" in safe.budget_caps_per_kind, "missing kind got safe default")

    print("\n[case] standard preset is identity")
    std = apply_preset(base, "standard")
    _check(std == base, "standard returns base unchanged")

    print("\n[case] aggressive preset lifts risk, auto-approves HITLs, doubles caps")
    aggro = apply_preset(base, "aggressive")
    _check(aggro.max_risk_level == "high", "ceiling lifted to high")
    _check("x.delete_*" in aggro.auto_approve_actions, "HITL pattern moved to auto_approve")
    _check(aggro.hitl_required_actions == [], "HITL list cleared")
    _check(aggro.budget_caps_per_kind["action"] == 400, "action cap doubled")

    print("\n[case] install with governance_preset='safe' applies the overlay")
    async with SessionLocal() as db:
        # Re-export to a fresh payload so the previous mutations
        # don't bleed in.
        payload2 = await export_workspace(
            db, src_ws.id, title="X Growth v2",
        )
        sim_safe = await install_blueprint(
            db, entity_id="ent_demo",
            payload=payload2,
            mode=InstallMode.SIMULATE,
            user_id="user_demo",
            governance_preset="safe",
        )
        await db.commit()
        applied = await get_policy(db, sim_safe.workspace_id)
    _check(applied.max_risk_level == "low", "safe preset applied at install time")
    _check("*" in applied.hitl_required_actions, "wildcard HITL persisted")

    print("\n[case] install records preset in _blueprint metadata")
    async with SessionLocal() as db:
        ws_after = (await db.execute(
            select(Workspace).where(Workspace.id == sim_safe.workspace_id)
        )).scalar_one()
    _check(
        ws_after.settings["_blueprint"]["governance_preset"] == "safe",
        "governance_preset stamped in metadata",
    )

    print("\n[case] unknown preset → InstallError")
    async with SessionLocal() as db:
        try:
            await install_blueprint(
                db, entity_id="ent_demo",
                payload=payload2,
                governance_preset="reckless",
            )
            _check(False, "should have raised InstallError")
        except Exception as exc:
            _check("reckless" in str(exc), "error names the bad preset")

    # ─────────────────────────────────────────────────────────────────
    # M12.4 — Simulation report
    # ─────────────────────────────────────────────────────────────────

    print("\n[case] simulate_report on a fresh sandbox: zero activity + safe notes")
    async with SessionLocal() as db:
        report = await simulate_report(db, sim_safe.workspace_id)
    _check(report.workspace_id == sim_safe.workspace_id, "workspace_id round-trips")
    _check(report.in_simulation is True, "still in simulation")
    _check(report.governance_preset == "safe", "preset surfaced on report")
    _check(report.activity.total_steps == 0, "no steps yet")
    _check(report.cost.total_credits == 0, "no cost yet")
    _check(len(report.counterfactuals) == 3, "all 3 presets counterfactualised")
    _check(any("No steps ran" in n for n in report.notes), "empty-window note present")

    print("\n[case] simulate_report aggregates real steps + cost + governance events")
    sim_ws_id = sim_safe.workspace_id
    async with SessionLocal() as db:
        # Bracket the steps inside the simulation window. Anchor to the
        # blueprint's installed_at so the window check passes.
        ws_for_window = (await db.execute(
            select(Workspace).where(Workspace.id == sim_ws_id)
        )).scalar_one()
        installed_at = datetime.fromisoformat(
            ws_for_window.settings["_blueprint"]["installed_at"]
        )
        # Force a 1-day window so projected_monthly arithmetic is stable.
        bp_meta = dict(ws_for_window.settings["_blueprint"])
        bp_meta["installed_at"] = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        new_settings = dict(ws_for_window.settings)
        new_settings["_blueprint"] = bp_meta
        ws_for_window.settings = new_settings

        plan = ExecutionPlan(
            id=generate_ulid(), entity_id="ent_demo", workspace_id=sim_ws_id,
            status="running",
        )
        db.add(plan)
        await db.flush()

        now = datetime.now(timezone.utc)
        # 3 done steps with costs.
        for kind, action, cost_usd in [
            ("action", "x.post", 0.10),
            ("action", "x.like", 0.02),
            ("llm", None, 0.20),
        ]:
            db.add(ExecutionStep(
                id=generate_ulid(), plan_id=plan.id,
                entity_id="ent_demo", workspace_id=sim_ws_id,
                step_key=f"step-{kind}-{action or 'llm'}",
                kind=kind, action_key=action, risk_level="low",
                step_status="done", started_at=now, finished_at=now,
                cost={"usd": cost_usd}, params={},
            ))
        # 1 HITL-paused step.
        db.add(ExecutionStep(
            id=generate_ulid(), plan_id=plan.id,
            entity_id="ent_demo", workspace_id=sim_ws_id,
            step_key="step-hitl",
            kind="action", action_key="x.delete_post", risk_level="medium",
            step_status="paused", started_at=now,
            cost={"usd": 0},
            error={"type": "GovernancePolicyHITL",
                   "message": "x.delete_* requires approval",
                   "matched_rule": "x.delete_*"},
            params={},
        ))
        # 1 hard-denied step.
        db.add(ExecutionStep(
            id=generate_ulid(), plan_id=plan.id,
            entity_id="ent_demo", workspace_id=sim_ws_id,
            step_key="step-blocked",
            kind="action", action_key="billing.refund", risk_level="high",
            step_status="failed", started_at=now, finished_at=now,
            cost={"usd": 0},
            error={"type": "GovernancePolicy",
                   "message": "billing.* blocked",
                   "matched_rule": "billing.*"},
            params={},
        ))
        await db.commit()

        report2 = await simulate_report(db, sim_ws_id)

    _check(report2.activity.total_steps == 5, f"5 steps counted, got {report2.activity.total_steps}")
    _check(report2.activity.governance_paused == 1, "1 HITL pause counted")
    _check(report2.activity.governance_denied == 1, "1 hard-deny counted")
    _check(report2.activity.by_kind.get("action") == 4, "4 action steps")
    _check(report2.activity.by_kind.get("llm") == 1, "1 llm step")
    _check(report2.activity.by_action_key.get("x.post") == 1, "x.post recorded")

    _check(report2.cost.total_credits > 0, "credits aggregated")
    # 0.10 + 0.02 + 0.20 = 0.32 USD * 5 credits/USD = 1.6, ceil = 2
    _check(report2.cost.total_credits == 2, f"expected 2 credits, got {report2.cost.total_credits}")
    _check(report2.cost.by_kind_credits.get("action", 0) > 0, "action cost present")
    _check(report2.cost.simulation_days >= 0.99, "simulation_days reflects ~1 day window")
    _check(report2.cost.projected_monthly_credits > 0, "projection populated")

    print("\n[case] counterfactuals show preset deltas")
    cf_safe = next(c for c in report2.counterfactuals if c.preset_key == "safe")
    cf_std = next(c for c in report2.counterfactuals if c.preset_key == "standard")
    cf_aggro = next(c for c in report2.counterfactuals if c.preset_key == "aggressive")

    # Operator-relevant signal: how much each preset lets through.
    # Under safe, only the LLM step (no action_key, low risk) escapes
    # both the wildcard HITL gate and the low risk ceiling.
    _check(cf_safe.allowed <= 1, f"safe allows ≤1, got {cf_safe.allowed}")
    blocked_safe = cf_safe.paused_for_hitl + cf_safe.denied
    _check(blocked_safe >= 4, f"safe blocks ≥4 of 5, got {blocked_safe}")

    # Aggressive should let the most through.
    _check(cf_aggro.allowed >= cf_safe.allowed, "aggressive allows ≥ safe")
    # billing.* is in the blueprint's never_allow — denied even by aggressive.
    _check(cf_aggro.denied >= 1, "aggressive still hard-denies billing.*")

    print("\n[case] notes flag HITL pauses + projected burn")
    _check(any("paused" in n for n in report2.notes), "HITL pause note surfaced")
    _check(any("credits/month" in n for n in report2.notes), "monthly projection note")

    print("\nSMOKE OK")


if __name__ == "__main__":
    asyncio.run(main())
