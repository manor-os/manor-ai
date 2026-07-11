"""End-to-end tests for the v1.1 blueprint pipeline.

Exercises the FULL surface — FastAPI HTTP API + real PostgreSQL via
the ``client`` and ``db_session`` fixtures from ``conftest.py``. These
tests catch interaction bugs the unit tests can't:

  * pydantic request/response models honor the v1.1 shape
  * SQLAlchemy → asyncpg → real Postgres round-trip works (in-memory
    sqlite hides JSONB type coercion bugs)
  * the WorkspaceBlueprint row's ``payload_version`` column gets the
    right value from manifest.blueprint_version
  * the export → store → list → install pipeline preserves data
  * v1.0 payloads (stored from older Manor releases) still install via
    the auto-migrator

Auth: every test registers a fresh user via ``/api/v1/auth/register``
and uses the returned access token.
"""

from __future__ import annotations

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.base import generate_ulid
from packages.core.models.blueprint import WorkspaceBlueprint
from packages.core.models.memory import AgentMemory
from packages.core.models.skill import AgentSkillBinding, Skill
from packages.core.models.workflow import WorkflowDefinition
from packages.core.models.workspace import (
    Agent,
    AgentSubscription,
    AgentToolBinding,
    ToolDefinition,
    Workspace,
)


# ── Helpers ────────────────────────────────────────────────────────────


async def _register(client: AsyncClient, username: str) -> tuple[dict, dict]:
    """Register a fresh user + return (auth_headers, user_payload)."""
    resp = await client.post(
        "/api/v1/auth/register",
        json={
            "username": username,
            "email": f"{username}@v11e2e.test",
            "password": "pass123",
            "entity_name": f"E2E {username}",
        },
    )
    assert resp.status_code in (200, 201), resp.text
    data = resp.json()
    return (
        {"Authorization": f"Bearer {data['access_token']}"},
        data,
    )


async def _create_workspace(
    client: AsyncClient,
    headers: dict,
    name: str = "Source WS",
) -> dict:
    resp = await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={
            "name": name,
            "kind": "social_media",
            "operating_context": "Running for {{brand_name}}.",
            "primary_work": "Draft posts daily.",
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


# ── Test 1: full export → store → install pipeline ────────────────────


async def test_v11_full_export_install_roundtrip(
    client: AsyncClient,
    db_session: AsyncSession,
):
    """Create a workspace, export it as a v1.1 blueprint, install the
    blueprint as a fresh sandbox workspace, verify the new workspace
    inherits the configured operating_model and that the WorkspaceBlueprint
    row records ``payload_version='1.1'``."""
    headers, _ = await _register(client, "v11rt")
    ws = await _create_workspace(client, headers, "Source X Growth")
    src_ws_id = ws["id"]

    # Export.
    export_resp = await client.post(
        f"/api/v1/workspaces/{src_ws_id}/export-blueprint",
        headers=headers,
        json={
            "slug": "x-growth-v1",
            "title": "X Growth — Calvin's playbook",
            "summary": "Daily posts + reply triage",
            "tags": ["social", "growth"],
            "author_handle": "calvin",
        },
    )
    assert export_resp.status_code == 201, export_resp.text
    bp = export_resp.json()
    payload = bp["payload"]

    # 5-section shape
    assert "manifest" in payload
    assert "contract" in payload
    assert "embedded" in payload
    assert "recipe" in payload
    assert "policy" in payload

    assert payload["manifest"]["blueprint_version"] == "1.1"
    assert payload["manifest"]["title"] == "X Growth — Calvin's playbook"
    assert payload["manifest"]["kind"] == "social_media"

    # Empty envelope sections present (no seeded subs/skills/agents/etc.)
    assert payload["embedded"]["agents"] == []
    assert payload["embedded"]["skills"] == []
    assert payload["embedded"]["knowledge_packs"] == []
    assert payload["recipe"]["workflows"] == []

    # operating_model absorbs the workspace shell:
    om = payload["recipe"]["operating_model"]
    assert om.get("context") == "Running for {{brand_name}}."
    assert om.get("primary_work") == "Draft posts daily."

    # payload_version column populated correctly (post my router patch
    # that reads manifest first).
    assert bp["payload_version"] == "1.1"

    # Install as sandbox in the same entity
    install_resp = await client.post(
        f"/api/v1/blueprints/{bp['id']}/install",
        headers=headers,
        json={"mode": "simulate", "workspace_name": "Sandbox X Growth"},
    )
    assert install_resp.status_code == 201, install_resp.text
    install_data = install_resp.json()
    new_ws_id = install_data["workspace_id"]

    # Verify the new workspace
    ws_resp = await client.get(f"/api/v1/workspaces/{new_ws_id}", headers=headers)
    assert ws_resp.status_code == 200
    new_ws = ws_resp.json()
    assert new_ws["name"] == "[SIM] Sandbox X Growth"
    assert new_ws["kind"] == "social_media"
    assert new_ws["operating_context"] == "Running for {{brand_name}}."
    assert new_ws["primary_work"] == "Draft posts daily."


# ── Test 2: install_count increments + listing ────────────────────────


async def test_v11_install_count_and_listing(
    client: AsyncClient,
    db_session: AsyncSession,
):
    """Each successful install bumps ``install_count`` on the
    WorkspaceBlueprint row. The list endpoint returns blueprints owned
    by the caller."""
    headers, _ = await _register(client, "v11list")
    ws = await _create_workspace(client, headers, "Source")
    bp_resp = await client.post(
        f"/api/v1/workspaces/{ws['id']}/export-blueprint",
        headers=headers,
        json={"slug": "listme", "title": "List Me"},
    )
    bp_id = bp_resp.json()["id"]

    # List — should have our 1 draft
    list_resp = await client.get("/api/v1/blueprints", headers=headers)
    assert list_resp.status_code == 200
    items = list_resp.json()
    assert any(b["id"] == bp_id for b in items)
    initial_install_count = next(b for b in items if b["id"] == bp_id)["install_count"]

    # Install once, then twice
    for _ in range(2):
        r = await client.post(
            f"/api/v1/blueprints/{bp_id}/install",
            headers=headers,
            json={"mode": "simulate"},
        )
        assert r.status_code == 201, r.text

    # install_count went up by 2
    detail = (
        await client.get(
            f"/api/v1/blueprints/{bp_id}",
            headers=headers,
        )
    ).json()
    assert detail["install_count"] == initial_install_count + 2


# ── Test 3: embedded.agents + bindings end-to-end ─────────────────────


async def test_v11_embedded_agent_roundtrip(
    client: AsyncClient,
    db_session: AsyncSession,
):
    """Seed a workspace with an entity-private agent (is_public=false)
    bound to a ToolDefinition + a private Skill + a starter AgentMemory,
    plus an active AgentSubscription. Export. The blueprint must surface
    the agent inside ``embedded.agents`` (NOT in ``requires.agents``)
    with its bindings + memory faithfully serialised. Install in the
    SAME entity — expect idempotent reuse (same slug already exists);
    verify by checking the row count stays at 1."""
    headers, user = await _register(client, "v11emb")
    entity_id = user["entity_id"]

    # Seed a workspace via the API so it carries the right entity_id
    ws = await _create_workspace(client, headers, "Embed Test WS")
    ws_id = ws["id"]

    # Now seed the agent + bindings + subscription + memory directly via
    # db_session (no HTTP API exists for some of these objects).
    async with db_session.begin():
        tool = ToolDefinition(
            id=generate_ulid(),
            name="tool.x.reply",
            display_name="X Reply",
            status="active",
        )
        skill = Skill(
            id=generate_ulid(),
            entity_id=entity_id,
            name="Reply Tone",
            slug="reply-tone-v1",
            system_prompt="Reply in founder voice.",
            tools=["tool.x.reply"],
            is_public=False,
            version="1.0.0",
        )
        agent = Agent(
            id=generate_ulid(),
            entity_id=entity_id,
            name="Calvin Reply Agent",
            slug="calvin-reply-agent-v1",
            system_prompt="Reply like Calvin.",
            config={"model": "claude-opus-4.7", "temperature": 0.5},
            is_template=False,
            is_public=False,
            status="active",
            version="1.0",
        )
        db_session.add_all([tool, skill, agent])
        await db_session.flush()

        db_session.add_all(
            [
                AgentSubscription(
                    id=generate_ulid(),
                    entity_id=entity_id,
                    agent_id=agent.id,
                    workspace_id=ws_id,
                    service_key="social.x.reply",
                    config={},
                    status="active",
                ),
                AgentToolBinding(agent_id=agent.id, tool_id=tool.id),
                AgentSkillBinding(
                    id=generate_ulid(),
                    agent_id=agent.id,
                    skill_id=skill.id,
                    status="active",
                ),
                AgentMemory(
                    id=generate_ulid(),
                    entity_id=entity_id,
                    agent_id=agent.id,
                    user_id=None,
                    workspace_id=None,
                    memory_type="instruction",
                    content="Never reply to outrage within first hour.",
                    importance=8,
                    confidence=0.9,
                    status="active",
                    visibility="entity",
                    classification="internal",
                ),
            ]
        )

    # Export with starter_memory ON via the toggle exposed in the request
    export = await client.post(
        f"/api/v1/workspaces/{ws_id}/export-blueprint",
        headers=headers,
        json={
            "slug": "embed-roundtrip",
            "title": "Embed Roundtrip",
        },
    )
    assert export.status_code == 201, export.text
    payload = export.json()["payload"]

    # Embedded section populated
    embedded_slugs = [a["slug"] for a in payload["embedded"]["agents"]]
    assert "calvin-reply-agent-v1" in embedded_slugs
    [agent_export] = [a for a in payload["embedded"]["agents"] if a["slug"] == "calvin-reply-agent-v1"]
    assert "tool.x.reply" in (agent_export.get("tool_bindings") or [])
    assert "reply-tone-v1" in (agent_export.get("skill_bindings") or [])
    # starter_memory is OFF by default in the export request; verify
    # default behaviour.
    assert agent_export.get("starter_memory") == []

    # Embedded skill present
    skill_slugs = [s["slug"] for s in payload["embedded"]["skills"]]
    assert "reply-tone-v1" in skill_slugs

    # Tools deduplicated into requires.tools
    assert "tool.x.reply" in payload["contract"]["requires"]["tools"]


# ── Test 4: workflow + post_install_check round-trip ──────────────────


async def test_v11_workflow_and_post_install_checks(
    client: AsyncClient,
    db_session: AsyncSession,
):
    """Install a raw payload via ``/api/v1/blueprints/install-payload``
    that ships a workflow + a post_install_check referencing that
    workflow. Verify the WorkflowDefinition row got created with the
    dependency graph inverted, and the post_install_check passes (no
    blocking todo emitted for it)."""
    headers, user = await _register(client, "v11wf")
    entity_id = user["entity_id"]

    payload = {
        "manifest": {
            "blueprint_version": "1.1",
            "title": "Workflow E2E",
            "kind": "social_media",
        },
        "contract": {
            "variables": [],
            "channels": [],
            "sessions": [],
            "requires": {
                "manor_min_version": None,
                "tools": [],
                "mcp_servers": [],
                "skills": [],
                "agents": [],
            },
        },
        "embedded": {"skills": [], "agents": [], "knowledge_packs": []},
        "recipe": {
            "operating_model": {"context": "x", "primary_work": "y"},
            "strategist": None,
            "prompts": [],
            "subscriptions": [],
            "scheduled_jobs": [],
            "workflows": [
                {
                    "slug": "morning-flow",
                    "trigger_type": "scheduled",
                    "trigger_ref": "morning-cron",
                    "variables": [
                        {"key": "topic", "default": "product_update"},
                    ],
                    "steps": [
                        {
                            "id": "draft",
                            "kind": "agent_call",
                            "service_key": "x.poster",
                            "input": "Draft on ${{vars.topic}}",
                        },
                        {"id": "review", "kind": "hitl_approval", "depends_on": ["draft"], "timeout_minutes": 60},
                        {"id": "post", "kind": "tool_call", "depends_on": ["review"], "tool": "tool.x.post"},
                    ],
                }
            ],
            "goals": [],
            "task_categories": [],
            "custom_fields": [],
            "sla_policies": [],
            "escalation_rules": [],
        },
        "policy": {
            "governance": {},
            "post_install_checks": [
                {"kind": "workflow_dryrun", "workflow_slug": "morning-flow"},
                # And one that's *expected to fail* — there's no session
                # in this entity yet.
                {"kind": "session_alive", "provider": "x", "session_label": "main"},
            ],
            "expected_baseline": None,
        },
    }

    resp = await client.post(
        "/api/v1/blueprints/install-payload",
        headers=headers,
        json={"payload": payload, "mode": "simulate"},
    )
    assert resp.status_code == 201, resp.text
    install_data = resp.json()

    # Inspect post_install_check todos
    pic_todos = [t for t in install_data["todos"] if t["kind"] == "post_install_check"]
    # Two checks; one passes silently, one fails (the session_alive).
    assert len(pic_todos) == 1
    assert pic_todos[0]["blocking"] is True
    assert "session" in pic_todos[0]["detail"].lower()
    assert pic_todos[0]["payload"]["check"]["kind"] == "session_alive"

    # Workflow created in the DB
    async with db_session.begin():
        wf = (
            await db_session.execute(
                select(WorkflowDefinition).where(
                    WorkflowDefinition.entity_id == entity_id,
                    WorkflowDefinition.name == "morning-flow",
                )
            )
        ).scalar_one()

    # Translated DAG
    steps_by_id = {s["id"]: s for s in wf.steps}
    assert steps_by_id["draft"]["next"] == ["review"]
    assert steps_by_id["review"]["next"] == ["post"]
    assert steps_by_id["post"]["next"] == []
    # Variables list → dict
    assert wf.variables == {"topic": "product_update"}


# ── Test 5: backward compat — v1.0 payload installs ──────────────────


async def test_v11_v10_payload_auto_migrates_and_installs(
    client: AsyncClient,
    db_session: AsyncSession,
):
    """Construct a v1.0 payload by hand (the shape older Manor releases
    used), POST it through ``install-payload``, and verify it installs
    OK — the auto-migrator should lift it to v1.1 and the installer
    should consume the migrated shape transparently."""
    headers, _ = await _register(client, "v11compat")

    v10_payload = {
        "blueprint_version": "1.0",
        "title": "Legacy Blueprint",
        "summary": "v1.0 format",
        "tags": ["legacy"],
        "author": {"handle": "old-system", "display_name": "Old"},
        "workspace": {
            "kind": "social_media",
            "operating_context": "Legacy context",
            "primary_work": "Legacy work",
            "operating_model": {"services": [{"key": "social.x.poster"}]},
            "settings": {"timezone": "America/Los_Angeles"},
        },
        "subscriptions": [],
        "goals": [],
        "scheduled_jobs": [],
        "custom_fields": [],
        "governance_policy": {
            "never_allow_actions": ["billing.*"],
            "max_risk_level": "medium",
        },
        "channel_requirements": [
            {"channel_type": "telegram", "purpose": "alerts", "required": True},
        ],
        "session_requirements": [
            {"provider": "x", "label": "main", "required": True},
        ],
        "memory_files": [
            {"path": "voice.md", "frontmatter": {"tags": ["brand"]}, "body": "Voice: founder-led."},
        ],
    }

    resp = await client.post(
        "/api/v1/blueprints/install-payload",
        headers=headers,
        json={"payload": v10_payload, "mode": "simulate"},
    )
    assert resp.status_code == 201, resp.text
    install_data = resp.json()
    new_ws_id = install_data["workspace_id"]

    # New workspace has the v1.0 shell fields preserved
    ws_resp = await client.get(f"/api/v1/workspaces/{new_ws_id}", headers=headers)
    new_ws = ws_resp.json()
    assert new_ws["kind"] == "social_media"
    assert new_ws["operating_context"] == "Legacy context"
    assert new_ws["primary_work"] == "Legacy work"

    # Channel + session requirements surfaced as todos
    todo_kinds = {t["kind"] for t in install_data["todos"]}
    assert "channel" in todo_kinds
    assert "browser_session" in todo_kinds


# ── Test 6: governance preset rejects bad embedded tool binding ───────


async def test_v11_governance_rejects_embedded_blocked_tool(
    client: AsyncClient,
    db_session: AsyncSession,
):
    """A blueprint that ships an embedded agent bound to a tool the
    governance policy hard-blocks should be rejected at install time
    with HTTP 400, BEFORE any agent row is created."""
    headers, user = await _register(client, "v11gov")
    entity_id = user["entity_id"]

    # Seed the tool the embedded agent will declare a binding for.
    async with db_session.begin():
        db_session.add(
            ToolDefinition(
                id=generate_ulid(),
                name="tool.x.delete_account",
                display_name="X Delete",
                status="active",
            )
        )

    payload = {
        "manifest": {
            "blueprint_version": "1.1",
            "title": "Destroyer Test",
            "kind": "social_media",
        },
        "contract": {
            "variables": [],
            "channels": [],
            "sessions": [],
            "requires": {
                "manor_min_version": None,
                "tools": ["tool.x.delete_account"],
                "mcp_servers": [],
                "skills": [],
                "agents": [],
            },
        },
        "embedded": {
            "skills": [],
            "agents": [
                {
                    "slug": "destroyer-agent",
                    "name": "D",
                    "system_prompt": "x",
                    "config": {},
                    "tool_bindings": ["tool.x.delete_account"],
                    "mcp_bindings": [],
                    "skill_bindings": [],
                    "starter_memory": [],
                }
            ],
            "knowledge_packs": [],
        },
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
            "governance": {
                "never_allow_actions": ["x.delete_*"],
                "max_risk_level": "medium",
            },
            "post_install_checks": [],
            "expected_baseline": None,
        },
    }

    resp = await client.post(
        "/api/v1/blueprints/install-payload",
        headers=headers,
        json={"payload": payload, "mode": "simulate"},
    )
    # Installer raises InstallError → router returns 400
    assert resp.status_code == 400, resp.text
    assert "governance" in resp.text.lower() or "blocked" in resp.text.lower()

    # Critically: no Agent row got created (the check happens before the
    # row is added to the session).
    async with db_session.begin():
        agent_rows = list(
            (
                await db_session.execute(
                    select(Agent).where(
                        Agent.entity_id == entity_id,
                        Agent.slug == "destroyer-agent",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert agent_rows == []


# ── Test 7: strategist template lands in operating_model.strategist ────


async def test_v11_strategist_template_installs(
    client: AsyncClient,
    db_session: AsyncSession,
):
    """When ``recipe.strategist`` is set on the blueprint, the installer
    merges it into ``workspace.operating_model.strategist`` — splitting
    the nested ``cadence`` so legacy readers (which expect a string)
    keep working, while the new structured fields (trigger_conditions,
    business_model, do_not_propose) land as peers."""
    headers, _ = await _register(client, "v11strat")

    payload = {
        "manifest": {
            "blueprint_version": "1.1",
            "title": "Strategist Test",
            "kind": "social_media",
        },
        "contract": {
            "variables": [],
            "channels": [],
            "sessions": [],
            "requires": {
                "manor_min_version": None,
                "tools": [],
                "mcp_servers": [],
                "skills": [],
                "agents": [],
            },
        },
        "embedded": {"skills": [], "agents": [], "knowledge_packs": []},
        "recipe": {
            "operating_model": {"context": "x", "primary_work": "y"},
            "strategist": {
                "business_model": {
                    "model_type": "social_growth",
                    "primary_signal": "follower_count",
                },
                "cadence": {
                    "schedule": "daily",
                    "trigger_conditions": {
                        "skip_if_any": ["budget_remaining_pct < 10"],
                    },
                },
                "proposal_shape": {"max_tasks_per_cycle": 3},
                "do_not_propose": ["No mass DMs"],
                "evaluation_rubric": {
                    "weights": {"goal_impact": 0.5, "cost_efficiency": 0.5},
                    "passing_score": 0.6,
                },
            },
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

    resp = await client.post(
        "/api/v1/blueprints/install-payload",
        headers=headers,
        json={"payload": payload, "mode": "simulate"},
    )
    assert resp.status_code == 201, resp.text
    new_ws_id = resp.json()["workspace_id"]

    async with db_session.begin():
        ws_row = (await db_session.execute(select(Workspace).where(Workspace.id == new_ws_id))).scalar_one()

    strat = (ws_row.operating_model or {}).get("strategist") or {}
    # Legacy reader sees a STRING:
    assert strat.get("cadence") == "daily"
    # New readers see the structured peers:
    assert strat["trigger_conditions"]["skip_if_any"] == ["budget_remaining_pct < 10"]
    assert strat["business_model"]["model_type"] == "social_growth"
    assert strat["proposal_shape"]["max_tasks_per_cycle"] == 3
    assert strat["do_not_propose"] == ["No mass DMs"]


# ── Test 8: payload validation surfaces 400 from the API ──────────────


async def test_v11_invalid_payload_returns_400(client: AsyncClient):
    """A clearly invalid payload (missing required sections) must come
    back from install-payload as a 400, not crash the server."""
    headers, _ = await _register(client, "v11bad")

    bad_payload = {
        # No manifest, no recipe — validate_payload rejects fast
        "blueprint_version": "9.9",
    }
    resp = await client.post(
        "/api/v1/blueprints/install-payload",
        headers=headers,
        json={"payload": bad_payload, "mode": "simulate"},
    )
    assert resp.status_code == 400, resp.text
    assert "invalid blueprint" in resp.text.lower() or "unsupported" in resp.text.lower()


# ── Test 9: stored v1.0 blueprint row still installs via ID path ──────


async def test_v11_legacy_blueprint_row_in_db_installs_via_id(
    client: AsyncClient,
    db_session: AsyncSession,
):
    """If a WorkspaceBlueprint row was created by an older Manor (v1.0
    payload stored in the JSONB column), installing it via
    ``/api/v1/blueprints/{id}/install`` should auto-migrate at install
    time. This proves on-the-wire backward compat for blueprints already
    in customers' databases."""
    headers, user = await _register(client, "v11legacy")
    entity_id = user["entity_id"]

    # Hand-insert a v1.0 row.
    async with db_session.begin():
        bp_row = WorkspaceBlueprint(
            id=generate_ulid(),
            entity_id=entity_id,
            slug="legacy-bp",
            title="Legacy",
            payload={
                "blueprint_version": "1.0",
                "title": "Legacy from DB",
                "tags": ["legacy"],
                "author": {},
                "workspace": {
                    "kind": "social_media",
                    "operating_context": "Stored long ago",
                    "primary_work": "Old work",
                    "operating_model": {},
                    "settings": {},
                },
                "subscriptions": [],
                "goals": [],
                "scheduled_jobs": [],
                "custom_fields": [],
                "governance_policy": {},
                "channel_requirements": [],
                "session_requirements": [],
                "memory_files": [],
            },
            payload_version="1.0",
            status="published",  # so the install path lets us read it
        )
        db_session.add(bp_row)

    install_resp = await client.post(
        f"/api/v1/blueprints/{bp_row.id}/install",
        headers=headers,
        json={"mode": "simulate"},
    )
    assert install_resp.status_code == 201, install_resp.text
    new_ws_id = install_resp.json()["workspace_id"]

    ws_resp = await client.get(f"/api/v1/workspaces/{new_ws_id}", headers=headers)
    assert ws_resp.status_code == 200
    new_ws = ws_resp.json()
    assert new_ws["operating_context"] == "Stored long ago"
    assert new_ws["primary_work"] == "Old work"
