"""The Architect's build spec and its part designers working in parallel waves."""
from __future__ import annotations

import asyncio
import copy
import json
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.core.ai.runtime.capabilities import CORE_CAPABILITIES
from packages.core.models.base import generate_ulid
from packages.core.models.workspace_draft import WorkspaceDraft
from packages.core.services import workspace_agents_designer as agents_designer
from packages.core.services import workspace_build_spec as build
from packages.core.services import workspace_part_designers as parts
from packages.core.services.workspace_part_designers import DesignJob, PartDesignResult, design_wave, part_designers

SERVICES = [
    {"service_key": "writing", "name": "Writing", "description": "Write the weekly articles.", "handoff_to": ["review"]},
    {"service_key": "review", "name": "Review", "description": "Edit and approve articles.", "human_review": True},
]
REPORT = {
    "slug": "weekly_report", "name": "Weekly report", "service_key": "review",
    "prompt": "Report what was published this week.", "cron_expr": "0 9 * * 1", "timezone": "Asia/Shanghai",
    "stats": [{"key": "articles_published", "name": "Articles published", "rubric": "Articles published this week."}],
}


def _spec(**changes):
    spec = {
        "language": "zh-CN", "timezone": "Asia/Shanghai",
        "services": [{"key": "writing", "stages": ["research", "draft"]}, {"key": "review", "stages": ["edit"]}],
        "agents": [{"service_key": "writing", "role": "Writes articles"},
                   {"service_key": "review", "role": "Edits and reports", "deliverables": ["docx"]}],
        "workflows": [{"slug": "weekly_report", "service_key": "review", "purpose": "Weekly numbers",
                       "stages": [{"id": "write", "service_key": "writing"}, {"id": "approve", "kind": "approval"}],
                       "stat_keys": ["articles_published"]}],
        "stats": [{"key": "articles_published", "workflow": "weekly_report"}],
        "automations": [{"key": "weekly_report", "workflow": "weekly_report", "cadence": "每周一上午9点"}],
        "routines": [{"said": "每周一出周报", "automation": "weekly_report"}],
        "knowledge": [{"key": "brand", "name": "Brand voice", "for_services": ["writing"]}],
        "briefs": {part: f"The {part} the person asked for, in their words." for part in build.SPEC_PARTS},
    }
    spec.update(changes)
    return spec


# ── The build spec ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("change, error", [
    ({"services": [{"key": "writing"}]}, "'review' is missing from the spec"),
    ({"agents": [{"service_key": "writing", "role": "Writes"}]}, "service 'review' has no Agent"),
    ({"stats": []}, "stat_keys names 'articles_published'"),
    ({"routines": [{"said": "每月1号复盘"}]}, "ask the person with ws_ask_user"),
    ({"automations": [{"key": "weekly_report", "workflow": "nope", "cadence": "weekly"}]}, "'nope' is not a Workflow"),
    ({"workflows": [], "stats": []}, "hand-off or review is run by a Workflow"),
    ({"briefs": {}}, "briefs.agents"),
    ({"timezone": "Mars/Base"}, "not an IANA time zone"),
])
def test_a_spec_whose_references_do_not_hold_is_refused(change, error):
    spec, errors = build.validate_build_spec(_spec(**change), {"services": SERVICES})
    assert spec is None and any(error in message for message in errors), errors


def test_a_service_of_three_stages_needs_a_workflow():
    services = [{"key": "writing", "stages": ["research", "draft", "polish"]}, {"key": "review"}]
    _, errors = build.validate_build_spec(_spec(services=services, workflows=[], stats=[], automations=[], routines=[]),
                                          {"services": [{"service_key": "writing"}, {"service_key": "review"}]})
    assert any("3 stages but no Workflow" in message for message in errors), errors


def test_small_specs_stay_with_the_lead_and_larger_ones_go_to_the_team():
    spec, errors = build.validate_build_spec(_spec(), {"services": SERVICES})
    assert errors == [] and build.work_units(spec) == 5 and build.build_mode(spec) == "team"
    small = {**spec, "knowledge": [], "automations": [], "routines": []}
    assert build.work_units(small) == 3 and build.build_mode(small) == "solo"
    # One Workflow of three stages is enough for the team, however few parts it has.
    staged = {**small, "workflows": [{**spec["workflows"][0], "stages": [{"id": stage} for stage in "abc"]}]}
    assert build.build_mode(staged) == "team"


def test_committing_a_changed_spec_designs_again_only_what_it_touches():
    deps = parts.designer_dependencies()
    fields = {"services": SERVICES}
    first = build.commit_spec(fields, build.validate_build_spec(_spec(), fields)[0], deps)
    assert first["dirty"] == ["agents", "knowledge", "workflows", "automations", "app"]
    assert build.next_wave(first, deps) == ["agents", "knowledge", "workflows"]
    for part in ("agents", "knowledge", "workflows"):
        build.set_progress(fields, part, "accepted")
    assert build.next_wave(build.build_spec(fields), deps) == ["automations", "app"]
    changed = _spec(stats=[{"key": "articles_published", "name": "Published", "workflow": "weekly_report"}])
    second = build.commit_spec(fields, build.validate_build_spec(changed, fields)[0], deps)
    # The Workflows changed, so what depends on them is designed again; the Agents are kept.
    assert second["dirty"] == ["workflows", "automations", "app"]
    assert second["progress"]["agents"] == "accepted" and second["progress"]["workflows"] == "pending"


def test_lint_issues_go_back_to_the_part_that_owns_them():
    issues = [{"severity": "P0", "where": "agent_mappings.review.tool_bindings"},
              {"severity": "P1", "where": "workflows.weekly_report"},
              {"severity": "P2", "where": "sources.S1"}, {"severity": "P0", "where": "goals.reach"}]
    owned = build.issues_by_part(issues, parts.designer_lint_where())
    assert {part: [row["where"] for row in rows] for part, rows in owned.items()} == {
        "agents": ["agent_mappings.review.tool_bindings"], "workflows": ["workflows.weekly_report"]}


# ── Designer checks ─────────────────────────────────────────────────────────

async def _no_lint(*_args, **_kwargs):
    return True


def _fields_with_spec():
    fields = {"services": copy.deepcopy(SERVICES), "primary_work": "Weekly articles"}
    build.commit_spec(fields, build.validate_build_spec(_spec(), fields)[0], parts.designer_dependencies())
    return fields


def _agent(context, service_key, labels=("tool:write_article Write article",), **extra):
    return {"service_key": service_key, "agent_name": service_key.title(), "system_prompt": "A capable worker. " * 10,
            "plan": {"capability_ids": ["tool:x"], "intent": "", "labels": list(labels),
                     "context_fingerprint": context.capability_fingerprint(service_key, "")}, **extra}


def test_an_agent_must_be_able_to_make_the_files_the_spec_names():
    fields = _fields_with_spec()
    fields["sources"] = [{"id": "S1", "name": "Brand method", "kind": "document", "status": "ready",
                          "extracted_text": "Private source body"}]
    designer = part_designers()["agents"]
    context = designer.context_from_draft(fields)
    assert context.summary()["sources"] == [{"id": "S1", "name": "Brand method"}]
    assert "extracted_text" not in context.fields["sources"][0]
    candidate, errors, _ = designer.check(context, {"agents": [_agent(context, "writing"), _agent(context, "review")]})
    assert candidate is None and any("must make docx files" in message for message in errors), errors
    docx = _agent(context, "review", labels=("skill:01J docx Create and edit Word documents",))
    writer = _agent(context, "writing", missing_skill_specs=[{
        "name": "Brand method", "system_prompt": "Follow the brand method. " * 10, "source_ids": ["S1"]}])
    candidate, errors, _ = designer.check(context, {"agents": [writer, docx]})
    assert errors == [] and len(candidate["agents"]) == 2
    # The service changed after its capabilities were matched.
    fields["services"][1]["description"] = "Edit, approve and publish articles."
    _, errors, _ = designer.check(designer.context_from_draft(fields), candidate)
    assert any("match them again" in message for message in errors), errors


async def test_a_sent_back_agents_design_keeps_its_matches_until_the_service_changes(monkeypatch):
    from packages.core.ai.tools.workspace_arch_tools import _agent_capability_catalog_fingerprint
    from packages.core.services import agent_generator, workspace_agents_designer as agents

    calls: list[str] = []

    async def match(*, prompt, **_kwargs):
        calls.append(prompt)
        return SimpleNamespace(selected_catalog_ids=("capability:x",), status=SimpleNamespace(value="ready"),
                               setup_required=())

    monkeypatch.setattr(agent_generator, "match_agent_capabilities", match)
    fields = _fields_with_spec()
    context = part_designers()["agents"].context_from_draft(fields)
    context.catalog = SimpleNamespace(prompt_payload=lambda: [], candidates=[])
    sent_back = {"value": {"agents": [
        {**_agent(context, "writing"), "plan": {**_agent(context, "writing")["plan"],
                                                "catalog_fingerprint": _agent_capability_catalog_fingerprint(context.catalog)}},
        {**_agent(context, "review"), "plan": {**_agent(context, "review")["plan"], "catalog_fingerprint": "older"}}]}}
    agents.reuse_matches(context, sent_back)
    await agents.prematch_services(context, 3)
    # Only the service whose plan no longer fits the catalog is matched again.
    assert len(calls) == 1 and "Service: Review" in calls[0]


def _platform_context(**agent):
    """A service the spec says works on Gmail and Instagram, and a catalog where Facebook is how Instagram is reached."""
    from packages.core.services.agent_capability_catalog import (
        AgentCapabilityCandidate,
        AgentCapabilityCatalog,
        AgentCapabilityKind,
    )

    def skill(ref, name, description, *tools):
        return AgentCapabilityCandidate(catalog_id=f"skill:{ref}", kind=AgentCapabilityKind.SKILL, ref=ref, name=name,
                                        description=description, metadata={"declared_tools": tools, "server_key": name[4:]})

    def action(server, name):
        return AgentCapabilityCandidate(
            catalog_id=f"mcp:{server}:{name}", kind=AgentCapabilityKind.MCP_ACTION, ref=server, name=f"{server} {name}",
            actions=({"name": name, "tool_name": f"mcp__{server}__{name}"},), metadata={"server_key": server, "action": name})

    fields = _fields_with_spec()
    fields["_build_spec"]["agents"][1]["integrations"] = ["gmail", "instagram"]
    context = part_designers()["agents"].context_from_draft(fields)
    context.catalog = AgentCapabilityCatalog(candidates=(
        skill("G", "mcp_gmail", "Operate Gmail", "mcp__gmail__read"),
        skill("F", "mcp_facebook", "Operate Pages and linked Instagram accounts", "mcp__facebook__post"),
        # Outlook's provider is still coming soon: its Skill is listed, but its tools are offered by no server.
        skill("O", "mcp_outlook", "Operate Outlook mail", "mcp__outlook__list_messages"),
        AgentCapabilityCandidate(catalog_id="skill:R", kind=AgentCapabilityKind.SKILL, ref="R", name="Recap",
                                 description="Summarize customer email and Instagram engagement"),
        action("gmail", "read"), action("facebook", "post"),
        AgentCapabilityCandidate(catalog_id="tool:invoke_skill", kind=AgentCapabilityKind.TOOL, ref="invoke_skill",
                                 name="Invoke Skill")))
    context.entity_agents = [{"id": "01J0000000000000000000AGNT", "name": "Analyst", **agent}]
    return context


@pytest.mark.parametrize("bindings, missing", [
    # A Skill that only mentions a platform is not a way into the account (the reported case).
    ({"skill_bindings": [{"id": "R"}]}, "gmail, instagram"),
    ({"mcp_bindings": ["gmail"]}, "instagram"),
    ({"mcp_bindings": ["gmail", "facebook"]}, None),
    ({"skill_bindings": [{"id": "G"}, {"id": "F"}]}, None),
])
def test_a_reused_agent_must_reach_the_platforms_the_spec_names(bindings, missing):
    context = _platform_context(**bindings)
    rows = [_agent(context, "writing"), {"service_key": "review", "existing_agent_id": "01J0000000000000000000AGNT"}]
    _, errors, _ = context.check({"agents": rows})
    found = [message for message in errors if "cannot reach" in message]
    assert (f"cannot reach {missing}," in found[0]) if missing else not found


def test_a_custom_agent_must_reach_them_too_until_matching_has_nothing_more_to_give():
    context = _platform_context()
    labels = [agents_designer.candidate_label(row) for row in context.catalog.candidates]
    row = lambda reach: {**_agent(context, "review", labels=("docx Word document",)), "plan": {
        **_agent(context, "review", labels=("docx Word document",))["plan"], "reach": reach}}
    rows = lambda reach: {"agents": [_agent(context, "writing"), row(reach)]}

    _, errors, _ = context.check(rows(labels[:1]))
    assert any("do not reach instagram, which it works on; match again" in message for message in errors), errors
    _, errors, _ = context.check(rows(labels[:2]))
    assert not any("reach" in message for message in errors), errors

    # Matched as often as allowed: the lead decides, the design is not stuck on it.
    context.matched = {("review", str(n)): {} for n in range(agents_designer.MAX_MATCHES_PER_SERVICE)}
    _, errors, warnings = context.check(rows(labels[:1]))
    assert not any("reach" in message for message in errors) and any("do not reach instagram" in w for w in warnings)


def test_a_platform_is_left_to_the_browser_or_to_another_way_when_the_catalog_cannot_reach_it():
    from dataclasses import replace

    from packages.core.services.agent_capability_catalog import AgentCapabilityCandidate, AgentCapabilityKind

    catalog = _platform_context().catalog
    assert agents_designer.platforms_out_of_reach(["instagram"], ["skill:c mcp_chrome operate a browser"], catalog) == []
    assert agents_designer.platforms_out_of_reach(["instagram"], ["skill:c mcp_chrome_knowledge_local upload"],
                                                  catalog) == ["instagram"]
    # No capability of its own and no browser to work it through: nothing the check could ask for.
    assert agents_designer.platforms_out_of_reach(["xiaohongshu"], [], catalog) == []
    assert agents_designer.platforms_out_of_reach(["outlook"], [], catalog) == []

    # With a browser, a platform that has no capability of its own needs it, even with a Skill it cannot save.
    with_browser = replace(catalog, candidates=(*catalog.candidates, AgentCapabilityCandidate(
        catalog_id="mcp:chrome", kind=AgentCapabilityKind.MCP_ACTION, ref="chrome", name="Chrome",
        metadata={"server_key": "chrome"})))
    assert agents_designer.platforms_out_of_reach(["outlook", "xiaohongshu"], [], with_browser) == ["outlook", "xiaohongshu"]
    assert agents_designer.platforms_out_of_reach(["outlook"], ["mcp:chrome chrome"], with_browser) == []


def test_a_skill_whose_dependencies_are_not_offered_here_is_not_for_a_matcher_to_choose():
    catalog = _platform_context().catalog
    scanned = {row.get("id") for row in catalog.scan_payload()}
    assert {"skill:G", "skill:F", "skill:R"} <= scanned and "skill:O" not in scanned
    # An exact binding still resolves against it, and says what is missing.
    assert catalog.resolve(["skill:O"]).setup_required[0]["setup_kind"] == "skill_dependency_unavailable"


def test_matching_is_told_to_keep_each_named_platform_and_no_other():
    fields = _fields_with_spec()
    fields["_build_spec"]["agents"][1]["integrations"] = ["gmail"]
    intent = agents_designer.default_intent(part_designers()["agents"].context_from_draft(fields), "review")
    assert "Works on gmail: keep for each" in intent and "Select no other platform's" in intent
    assert "worked through mcp:chrome" in intent


async def test_a_sent_back_match_stays_with_its_service_whatever_design_it_had(monkeypatch):
    from packages.core.ai.tools import workspace_arch_tools as tools
    from packages.core.ai.tools.workspace_arch_tools import _agent_capability_catalog_fingerprint
    from packages.core.services import agent_generator, workspace_agents_designer as agents

    calls: list[str] = []

    async def match(*, prompt, **_kwargs):
        calls.append(prompt)
        return SimpleNamespace(selected_catalog_ids=("capability:x",), status=SimpleNamespace(value="ready"),
                               setup_required=())

    monkeypatch.setattr(agent_generator, "match_agent_capabilities", match)
    existing = "01J0000000000000000000AGNT"
    fields = _fields_with_spec()
    designer = part_designers()["agents"]
    context = designer.context_from_draft(fields)
    context.catalog = SimpleNamespace(prompt_payload=lambda: [], candidates=[], selectable_candidates=lambda: [])
    context.entity_agents = [{"id": existing, "name": "Reviewer"}]
    catalog = _agent_capability_catalog_fingerprint(context.catalog)

    def plan(key, intent, labels):
        return {**_agent(context, key, labels=labels)["plan"], "service_key": key, "intent": intent, "catalog_fingerprint": catalog,
                "context_fingerprint": context.capability_fingerprint(key, intent)}

    # A sharper intent than the default, and a service that reused an Agent: both keep their match.
    writing = plan("writing", "writes in the brand voice", ("tool:write Write",))
    review = plan("review", "", ("tool:read Read files",))
    agents.reuse_matches(context, {"value": {"agents": [
        {**_agent(context, "writing"), "plan": writing},
        {"service_key": "review", "existing_agent_id": existing, "plan": review}]}})
    await agents.prematch_services(context, 3)
    assert calls == []

    state: dict = {}
    executor = agents.agents_designer_executor(context, state)
    read = {row["service_key"]: row for row in json.loads(await executor("ag_read_context", {}))["services"]}
    assert read["writing"]["matched_capabilities"]["intent"] == "writes in the brand voice"
    assert read["review"]["missing_deliverables"] == ["docx"]
    submitted = json.loads(await executor("ag_submit_candidate", {"agents": [
        {key: value for key, value in _agent(context, "writing").items() if key != "plan"},
        {"service_key": "review", "existing_agent_id": existing}]}))
    assert submitted["ok"] is True, submitted
    rows = {row["service_key"]: row for row in state["candidate"]["agents"]}
    assert rows["writing"]["plan"]["intent"] == "writes in the brand voice"
    assert rows["review"]["plan"]["capability_ids"] == ["tool:x"]

    # A reused Agent's match is kept for the next revision, never stored as a plan to build from.
    draft = SimpleNamespace(fields={**fields, "_candidates": {}})
    parts.keep_candidate(draft.fields, "agents", {"value": state["candidate"]})

    async def load(*_args):
        return draft

    async def persist(*_args):
        pass

    async def saved(*_args, **_kwargs):
        return json.dumps({"ok": True})

    @asynccontextmanager
    async def phase():
        yield

    monkeypatch.setattr(tools, "_load_draft", load)
    monkeypatch.setattr(tools, "_persist", persist)
    monkeypatch.setattr(tools, "_request_custom_agent", saved)
    monkeypatch.setattr(tools, "_propose_agent_mapping", saved)
    _, failure = await agents._apply_agents(None, {"value": state["candidate"]}, entity_id="ent-1", user_id="user-1",
                                            draft_id="draft-1", draft_phase=phase)
    assert failure is None
    stored = draft.fields[tools._AGENT_CAPABILITY_PLANS_FIELD]
    assert [row["service_key"] for row in stored] == ["writing"]


@pytest.mark.parametrize("service_keys", [("writing", "review"), ("catalog", "images")])
async def test_accepting_agents_retries_only_the_failed_service_after_partial_success(monkeypatch, service_keys):
    from packages.core.ai.tools import workspace_arch_tools as tools

    first_key, retry_key = service_keys
    fields = {"services": [{"service_key": key, "name": key.title()} for key in service_keys],
              "_build_spec": {"version": build.SPEC_VERSION, "progress": {"agents": "candidate"}}}
    context = part_designers()["agents"].context_from_draft(fields)
    candidate, errors, _ = context.check({"agents": [
        *(_agent(context, key) for key in service_keys),
    ]})
    assert errors == []
    parts.keep_candidate(fields, "agents", {"value": candidate})
    draft = SimpleNamespace(fields=fields)
    calls = []

    async def load(*_args):
        return draft

    async def persist(*_args):
        pass

    async def request(*_args, **kwargs):
        key = kwargs["service_key"]
        calls.append(key)
        if key == retry_key and calls.count(key) == 1:
            return json.dumps({"ok": False, "error": "temporary provider failure"})
        return json.dumps({"ok": True, "agent_name": key})

    @asynccontextmanager
    async def phase():
        yield

    monkeypatch.setattr(tools, "_load_draft", load)
    monkeypatch.setattr(tools, "_persist", persist)
    monkeypatch.setattr(tools, "_request_custom_agent", request)
    first = json.loads(await tools._accept_parts(
        None, entity_id="ent-1", user_id="user-1", draft_id="draft-1", draft_phase=phase, parts=["agents"],
    ))
    assert first["ok"] is False
    waiting = parts.pending_candidates(draft.fields)["agents"]
    assert waiting["applied_services"] == [first_key] and len(waiting["value"]["agents"]) == 2
    second = json.loads(await tools._accept_parts(
        None, entity_id="ent-1", user_id="user-1", draft_id="draft-1", draft_phase=phase, parts=["agents"],
    ))
    assert second["ok"] is True and calls == [first_key, retry_key, retry_key]
    assert "agents" not in parts.pending_candidates(draft.fields)
    assert draft.fields["_build_spec"]["progress"]["agents"] == "accepted"


@pytest.mark.parametrize("net, error", [
    ({"name": "Brand voice", "purpose": "How we write.", "linked_service_keys": ["writing"]}, "would start empty"),
    ({"name": "Brand voice", "purpose": "How we write.", "linked_service_keys": ["nope"],
      "starter_outline": ["Tone", "Words we avoid", "Examples"]}, "unknown services"),
    ({"name": "Other", "purpose": "Something else.", "linked_service_keys": ["writing"],
      "starter_outline": ["A", "B", "C"]}, "'brand' of the spec has no net"),
])
def test_every_knowledge_net_starts_with_documents(net, error):
    designer = part_designers()["knowledge"]
    _, errors, _ = designer.check(designer.context_from_draft(_fields_with_spec()), {"nets": [net]})
    assert any(error in message for message in errors), errors


def _report_fields():
    from packages.core.services.workspace_workflow_designer import WorkflowDesignContext, run_pattern

    fields = _fields_with_spec()
    library: dict = {}
    _, result = run_pattern(WorkflowDesignContext.from_draft_fields(fields), library, "wf_pattern_report_stat", REPORT)
    fields["workflows"] = result["workflows"]
    fields["_automations_needed"] = result["automations"]
    return fields


@pytest.mark.parametrize("cadence, expected", [
    ({"kind": "weekly", "days": [1], "at": "09:00"}, {"cron_expr": "0 9 * * 1"}),
    ({"kind": "monthly", "day_of_month": 1, "at": "08:30"}, {"cron_expr": "30 8 1 * *"}),
    ({"kind": "every_minutes", "minutes": 15}, {"every_seconds": 900.0}),
    ({"kind": "as_needed"}, {"cron_expr": "0 9 * * 1"}),
])
def test_automations_are_scheduled_from_checked_cadences(cadence, expected):
    designer = part_designers()["automations"]
    context = designer.context_from_draft(_report_fields())
    candidate, errors, _ = designer.check(context, {"automations": [{
        "key": "weekly_report", "workflow": "weekly_report", "description": "Report the week's numbers.",
        "cadence": cadence}]})
    assert errors == [], errors
    row = candidate["entries"][0]
    assert designer.check(context, candidate)[1] == []  # checked again as submitted when accepted
    assert {key: row.get(key) for key in expected} == expected and row["timezone"] == "Asia/Shanghai"
    assert row["scheduled_job"]["execution_target"] == {"workflow_slug": "weekly_report"}


def test_a_job_cannot_run_a_workflow_creation_never_binds():
    from packages.core.blueprints.installer import validate_portable_recipe_resources

    fields = _report_fields()
    workflow = {**fields["workflows"][0], "trigger_type": "schedule"}
    job = {"job_id": "weekly", "execution_type": "workflow", "schedule_kind": "cron", "cron_expr": "0 9 * * 1", "timezone": "UTC",
           "execution_target": {"workflow_slug": workflow["slug"]}}
    with pytest.raises(Exception, match="never bound"):
        validate_portable_recipe_resources({"workflows": [workflow], "scheduled_jobs": [job]})


def test_a_needed_schedule_or_named_routine_cannot_be_left_out():
    designer = part_designers()["automations"]
    context = designer.context_from_draft(_report_fields())
    _, errors, _ = designer.check(context, {"automations": [{
        "key": "monthly_review", "service_key": "review", "description": "Review the month with the person.",
        "cadence": {"kind": "cron", "cron_expr": "0 9 1 * *"}}]})
    assert any("cadence.kind is one of" in message for message in errors), errors
    _, errors, _ = designer.check(context, {"automations": [{
        "key": "monthly_review", "service_key": "review", "description": "Review the month with the person.",
        "cadence": {"kind": "monthly", "at": "09:00"}}]})
    assert any("'weekly_report' must run on a schedule" in message for message in errors), errors
    assert any("'每周一出周报'" in message for message in errors), errors
    # A Workflow's own schedule may do a planned Automation's work, named once instead of scheduled twice.
    _, errors, _ = designer.check(context, {"automations": [{
        "key": "report_worker", "workflow": "weekly_report", "description": "Report the week's numbers.",
        "cadence": {"kind": "as_needed"}, "covers": ["weekly_report"]}]})
    assert errors == [], errors


# ── Waves ───────────────────────────────────────────────────────────────────

def _fake(part, behave):
    return replace(part_designers()[part], run=behave)


async def _wave(*jobs, **kwargs):
    return await design_wave(list(jobs), entity_id="ent-1", user_id=None, workspace_id=None, surface=None, **kwargs)


def _done(notes):
    return PartDesignResult(candidate={"done": notes}, warnings=[], notes=notes, attempts=1)


async def test_a_wave_runs_its_designers_at_once_and_one_failure_stops_no_other(monkeypatch):
    monkeypatch.setattr(parts, "DESIGN_RETRY_DELAY_SECONDS", 0)
    monkeypatch.setattr(parts, "DESIGN_STAGGER_SECONDS", 0)
    running, peak, calls = 0, 0, {"knowledge": 0, "workflows": 0}

    async def slow(context, **_kwargs):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.05)
        running -= 1
        return _done(context)

    async def flaky(context, **_kwargs):
        calls["knowledge"] += 1
        if calls["knowledge"] == 1:
            raise RuntimeError("provider 503")
        return await slow(context)

    async def broken(_context, **_kwargs):
        calls["workflows"] += 1
        raise RuntimeError("provider 503")

    outcomes = await _wave(DesignJob(_fake("agents", slow), "agents", "b"), DesignJob(_fake("knowledge", flaky), "knowledge", "b"),
                           DesignJob(_fake("workflows", broken), "workflows", "b"))
    assert [outcome.job.context for outcome in outcomes] == ["agents", "knowledge", "workflows"]
    assert outcomes[0].result.notes == "agents" and outcomes[1].result.notes == "knowledge"
    # A technical failure is tried once more, then reported as the outcome, not raised.
    assert (outcomes[1].attempts, outcomes[2].attempts, calls["workflows"]) == (2, 2, 2)
    assert outcomes[2].result is None and "503" in outcomes[2].error
    assert peak >= 2


async def test_a_stuck_provider_cannot_hold_the_wave_and_a_rate_limit_makes_the_rest_wait(monkeypatch):
    monkeypatch.setattr(parts, "DESIGN_RETRY_DELAY_SECONDS", 0)
    monkeypatch.setattr(parts, "DESIGN_STAGGER_SECONDS", 0)

    async def stuck(_context, **_kwargs):
        await asyncio.sleep(60)

    started = asyncio.get_running_loop().time()
    outcomes = await asyncio.wait_for(_wave(
        DesignJob(_fake("agents", stuck), "agents", "b"),
        DesignJob(_fake("knowledge", stuck), "knowledge", "b"), timeout=2, concurrency=1,
    ), timeout=3)
    assert all(outcome.error for outcome in outcomes) and asyncio.get_running_loop().time() - started < 3

    running, peak, limited = 0, 0, {"done": False}

    async def limit_once(context, **_kwargs):
        nonlocal running, peak
        running += 1
        peak = max(peak, running) if limited["done"] else peak
        try:
            if not limited["done"]:
                limited["done"] = True
                raise RuntimeError("429 Too Many Requests")
            await asyncio.sleep(0.05)
            return _done(context)
        finally:
            running -= 1

    jobs = [DesignJob(_fake(part, limit_once), part, "b") for part in ("agents", "knowledge", "workflows")]
    outcomes = await _wave(*jobs)
    assert all(outcome.result is not None for outcome in outcomes) and peak == 1


async def test_a_designer_that_cannot_run_hands_its_part_to_the_lead_silently(monkeypatch):
    from packages.core.ai.tools import workspace_arch_tools as tools

    fields = _fields_with_spec()
    draft = SimpleNamespace(fields=fields)

    async def load(*_args):
        return draft

    async def persist(_db, _draft):
        return None

    async def broken(_context, **_kwargs):
        raise RuntimeError("upstream connect error 503")

    monkeypatch.setattr(tools, "_load_draft", load)
    monkeypatch.setattr(tools, "_persist", persist)
    monkeypatch.setattr(parts, "DESIGN_RETRY_DELAY_SECONDS", 0)
    designers = dict(part_designers())
    monkeypatch.setattr(parts, "part_designers", lambda: {**designers, "knowledge": replace(designers["knowledge"], run=broken)})
    reply = json.loads(await tools._design_parts(None, entity_id="ent-1", draft_id="draft-1",
                                                 parts=[{"part": "knowledge"}]))
    assert reply["ok"] is True and reply["parts"]["knowledge"]["status"] == "lead"
    assert "ws_attach_knowledge" in reply["parts"]["knowledge"]["next"] and "503" not in json.dumps(reply)
    assert draft.fields["_build_spec"]["progress"]["knowledge"] == "lead"
    # A part that depends on one not done yet waits.
    reply = json.loads(await tools._design_parts(None, entity_id="ent-1", draft_id="draft-1",
                                                 parts=[{"part": "automations"}]))
    assert reply["parts"]["automations"]["status"] == "waiting"


async def test_a_long_design_keeps_telling_the_stream_it_is_alive(monkeypatch):
    """Regression (jr acceptance 2026-11-04): one designer model call took 297 s
    and the wave sent nothing, so the stall watchdog killed a working turn after
    7 minutes. Its own timeout bounds a wave; while it runs, the stream hears."""
    from packages.core.ai.runtime.workspace_drafts import runtime_workspace_architect_tool_executor
    from packages.core.ai.tools import workspace_arch_tools as tools

    monkeypatch.setattr(parts, "DESIGN_KEEPALIVE_SECONDS", 0.01)
    events: list[str] = []

    async def stream(event: str, _payload: dict) -> None:
        events.append(event)

    class Turn:
        async def lock(self):
            return None

        async def release(self):
            return None

    async def slow(context, **_kwargs):
        await asyncio.sleep(0.1)
        return _done(context)

    async def command(_db, *, draft_phase, **_kwargs):
        await _wave(DesignJob(_fake("app", slow), "app", "b"), keepalive=tools._draft_keepalive(draft_phase))
        return json.dumps({"ok": True})

    monkeypatch.setitem(tools.HANDLERS, "ws_design_app_view", command)
    executor = runtime_workspace_architect_tool_executor(
        None, draft_id="draft-1", entity_id="ent-1", user_id="user-1", turn=Turn(), stream_handler=stream,
    )
    assert json.loads(await executor("ws_design_app_view", {"brief": "b"}))["ok"] is True
    assert events.count("tool_progress") >= 2


@pytest.mark.asyncio
@pytest.mark.parametrize("via", ["ws_design_app_view", "ws_design_parts"])
async def test_a_workspace_needing_an_app_is_not_ready_until_it_has_one_or_the_person_is_told(db_session, monkeypatch, via):
    """Regression (jr acceptance 2026-11-04): the App design was lost with its
    turn; lint passed, the draft became ready and nobody heard about it."""
    from packages.core.ai.tools import workspace_arch_tools as tools
    from packages.core.services import workspace_draft_service as drafts

    flow = {"slug": "plan_campaign", "name": "Plan", "steps": [{"id": "start", "type": "trigger", "next": []}]}
    fields = {**_fields_with_spec(), "workflows": [flow]}
    draft = WorkspaceDraft(entity_id=generate_ulid(), user_id=generate_ulid(), status="active",
                           fields=fields, messages=[{"role": "user", "content": "把推广做好"}], missing=[], ready=False)
    db_session.add(draft)
    await db_session.commit()
    owner = {"entity_id": draft.entity_id, "user_id": draft.user_id, "draft_id": draft.id}

    async def app_issues() -> list[dict]:
        lint = json.loads(await tools._lint_draft(db_session, **owner))
        return [issue for issue in lint["issues"] if issue["where"] == "app_view"]

    async def blocks_ready() -> bool:
        return "app_view" in (await tools._mark_ready(db_session, **owner))

    async def broken(_context, **_kwargs):
        raise RuntimeError("upstream connect error 503")

    # The lost design left nothing: the next lint sends the Architect back to it.
    assert [issue["severity"] for issue in await app_issues()] == ["P0"] and await blocks_ready()

    designers = dict(part_designers())
    monkeypatch.setattr(parts, "part_designers", lambda: {**designers, "app": replace(designers["app"], run=broken)})
    monkeypatch.setattr(parts, "DESIGN_RETRY_DELAY_SECONDS", 0)

    async def design_fails() -> None:
        if via == "ws_design_app_view":
            await tools._design_app_view(db_session, **owner, brief="b")
            return
        stored = dict(draft.fields)
        stored["_build_spec"] = {**stored["_build_spec"], "progress": {
            part: "accepted" for part in stored["_build_spec"]["progress"]}}
        stored["_build_spec"]["progress"]["app"] = "pending"
        draft.fields = stored
        reply = json.loads(await tools._design_parts(db_session, **owner, parts=[{"part": "app", "brief": "b"}]))
        assert reply["parts"]["app"]["status"] == "lead"

    await design_fails()
    assert [issue["severity"] for issue in await app_issues()] == ["P0"]  # retried first
    await design_fails()
    assert [issue["severity"] for issue in await app_issues()] == ["P1"]  # then not blocking ...

    # ... and the person hears it plainly, once, in their language, whatever the Architect wrote.
    assert parts.app_design_gave_up(draft.fields)
    monkeypatch.setattr(drafts, "_refresh_missing_from_lint", _no_lint)

    async def replied(*_args, **_kwargs):
        return "草稿好了。"

    monkeypatch.setattr(drafts, "_architect_turn", replied)
    await db_session.commit()
    reply, _ = await drafts.process_draft_message(db_session, **owner, user_message="继续")
    assert reply.startswith("草稿好了。") and "标准页面" in reply
    reply, _ = await drafts.process_draft_message(db_session, **owner, user_message="好的")
    assert reply == "草稿好了。"

    # A page already there or nothing to run needs no design, and a design that lands clears the count.
    assert parts.missing_app_issue({"workflows": [flow], "app_view": {"hero": {}}}) is None
    assert parts.missing_app_issue({"workflows": [{**flow, "internal": True}]}) is None
    waiting = {"workflows": [flow], parts.DESIGN_FAILURES_FIELD: {"app": 2}}
    parts.keep_candidate(waiting, "app", {"value": {}})
    assert not parts.app_design_gave_up(waiting) and parts.missing_app_issue(waiting)["severity"] == "P0"
    assert waiting[parts.DESIGN_FAILURES_FIELD] == {}


async def test_a_blueprints_page_is_its_authors_choice_even_when_it_has_none(db_session, monkeypatch):
    from packages.core.ai.tools import workspace_arch_tools as tools
    from packages.core.services import workspace_draft_service as drafts

    flow = {"slug": "plan_campaign", "name": "Plan", "steps": [{"id": "start", "type": "trigger", "next": []}]}
    fields = {"workflows": [flow], parts.DESIGN_FAILURES_FIELD: {"app": parts.APP_DESIGN_TRIES}}
    draft = SimpleNamespace(fields=fields, applied_blueprint_id="blueprint-1", messages=[])

    async def load(*_args):
        return draft

    monkeypatch.setattr(tools, "_load_draft", load)
    lint = json.loads(await tools._lint_draft(db_session, entity_id=generate_ulid(), user_id=generate_ulid(), draft_id="d"))
    assert not [issue for issue in lint["issues"] if issue["where"] == "app_view"]
    assert drafts._app_gap_note(draft) is None


async def test_a_weekly_routine_nothing_schedules_is_reported(db_session, monkeypatch):
    """Staging 2026-11-09: a restaurant draft had weekly_content and weekly_review_replies, and no schedule."""
    from packages.core.ai.tools import workspace_arch_tools as tools

    def flow(slug, name):
        return {"slug": slug, "name": name, "steps": [{"id": "start", "type": "trigger", "next": []}]}

    fields = {"workflows": [flow("weekly_content", "Plan the week"), flow("dish_add", "Add a dish"),
                            flow("monthly_report", "每月营销报告"), flow("reviews", "每周点评回复")],
              "scheduled_jobs": [{"job_id": "monthly", "execution_target": {"type": "workflow",
                                                                            "workflow_slug": "monthly_report"}}]}
    draft = SimpleNamespace(fields=fields, applied_blueprint_id=None, messages=[])

    async def load(*_args):
        return draft

    monkeypatch.setattr(tools, "_load_draft", load)
    lint = json.loads(await tools._lint_draft(db_session, entity_id=generate_ulid(), user_id=generate_ulid(), draft_id="d"))
    unscheduled = sorted(issue["where"] for issue in lint["issues"] if "no schedule runs it" in issue["message"])
    assert unscheduled == ["workflows.reviews", "workflows.weekly_content"]


# ── End to end ──────────────────────────────────────────────────────────────

def _scripted(scripts, before=None):
    """Stand in for each designer's model: run its real tools with the given calls."""
    async def run(designer, context, **_kwargs):
        if before is not None:
            await before(designer.part)
        state: dict = {}
        tools = designer.executor(context, state)
        replies = []
        for name, args in scripts[designer.part]:
            replies.append(json.loads(await tools(name, args(replies) if callable(args) else args)))
            assert replies[-1].get("ok", True) is not False, (designer.part, name, replies[-1])
        return PartDesignResult(candidate=state.get("candidate"), warnings=state.get("warnings") or [],
                                notes=f"scripted {designer.part}", attempts=state.get("attempts", 0),
                                details=state.get("details") or {}, state=state)
    return run


async def test_a_spec_is_designed_in_parallel_waves_accepted_and_created(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spec, wave 1 (Agents, Knowledge, Workflows) with no Draft lock held, accept, wave 2, accept, create."""
    from packages.core.ai.runtime.workspace_drafts import runtime_workspace_architect_tool_executor
    from packages.core.models.document import DocumentGroup
    from packages.core.models.scheduler import ScheduledJob
    from packages.core.models.user import User, UserMembership
    from packages.core.models.workflow import WorkflowDefinition
    from packages.core.services import (
        agent_generator,
        workspace_agents_designer,
        workspace_app_designer,
        workspace_workflow_designer,
    )
    from packages.core.services.workspace_draft_service import DraftTurn, finalize_draft
    from packages.core.services.workspace_setup_service import DEFAULT_FIELDS

    entity_id, user_id = generate_ulid(), generate_ulid()
    db_session.add_all([
        User(id=user_id, entity_id=entity_id, email=f"{user_id}@example.test", password_hash="x", role="owner",
             status="active"),
        UserMembership(user_id=user_id, entity_id=entity_id, role="owner", status="active"),
    ])
    draft = WorkspaceDraft(id=generate_ulid(), entity_id=entity_id, user_id=user_id, messages=[], missing=[],
                           ready=False, status="active", fields={
                               **dict(DEFAULT_FIELDS), "name": "Article studio", "kind": "content",
                               "operating_context": "A small publisher.", "primary_work": "Weekly articles.",
                               "services": [{key: row[key] for key in ("service_key", "name", "description")}
                                            for row in SERVICES], "agent_mappings": [], "goals": [], "stats": []})
    db_session.add(draft)
    await db_session.commit()
    sessions = async_sessionmaker(db_session.bind, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr("packages.core.database.async_session", sessions)

    async def match(*, capability_catalog, **_kwargs):
        # The provider boundary: the matcher selects one capability of the real catalog a service Agent may hold.
        return capability_catalog.resolve([next(candidate.catalog_id for candidate in capability_catalog.candidates
                                                if candidate.catalog_id.startswith("capability:")
                                                and CORE_CAPABILITIES[candidate.ref].risk_level != "admin")])

    monkeypatch.setattr(agent_generator, "match_agent_capabilities", match)
    seen: dict = {}

    async def meanwhile(part):
        if part != "knowledge":
            return
        # While designers run, another connection locks the Draft at once and the person edits it.
        async with sessions() as other:
            row = (await other.execute(select(WorkspaceDraft).where(WorkspaceDraft.id == draft.id)
                                       .with_for_update(nowait=True))).scalar_one()
            row.fields = {**row.fields, "name": "Weekly article studio"}
            await other.commit()
            seen["locked"] = True

    def agent(service_key):
        return {"service_key": service_key, "agent_name": f"{service_key.title()} Agent",
                "system_prompt": f"An expert {service_key} worker who plans, drafts and checks articles carefully. " * 3}

    scripts = {
        "agents": [("ag_read_context", {}), ("ag_match_capabilities", {"service_key": "writing"}),
                   ("ag_match_capabilities", {"service_key": "review", "intent": "saves Word docx files"}),
                   ("ag_submit_candidate", {"agents": [agent("writing"), agent("review")]})],
        "knowledge": [("kn_read_context", {}), ("kn_submit_candidate", {"nets": [{
            "key": "brand", "name": "Brand voice", "purpose": "How the publisher writes.",
            "linked_service_keys": ["writing"], "starter_outline": ["语气", "禁用词", "范例"]}]})],
        "workflows": [("wf_read_context", {}), ("wf_pattern_report_stat", REPORT),
                      ("wf_submit_candidate", {"use_patterns": ["weekly_report"]})],
        "automations": [("au_read_context", {}), ("au_submit_candidate", {"automations": [{
            "key": "weekly_report", "workflow": "weekly_report", "description": "Report the week's numbers.",
            "said": "每周一出周报", "cadence": {"kind": "weekly", "days": [1], "at": "09:00"}}]})],
        "app": [("app_read_context", {}), ("app_submit_candidate", {"app_view": {
            "title": "Article studio", "hero": "workflow:weekly_report",
            "icons": {"service:review": "checklist", "service:writing": "document", "workflow:weekly_report": "report"}},
            "recipes": []})],
    }
    scripted = _scripted(scripts, before=meanwhile)
    monkeypatch.setattr(parts, "run_part_designer", scripted)
    for module in (workspace_agents_designer, workspace_workflow_designer, workspace_app_designer):
        monkeypatch.setattr(module, "run_part_designer", scripted)
    # The spec says review makes docx; the stand-in matcher selects no such capability.
    spec = _spec(agents=[{"service_key": "writing", "role": "Writes"}, {"service_key": "review", "role": "Edits"}])

    turn = DraftTurn(db_session, draft_id=draft.id, entity_id=entity_id, user_id=user_id)
    await turn.claim(wait_seconds=0)
    await db_session.commit()
    run = runtime_workspace_architect_tool_executor(db_session, draft_id=draft.id, entity_id=entity_id,
                                                    user_id=user_id, turn=turn)

    async def call(name, **args):
        return json.loads(await run(name, {"draft_id": draft.id, **args}))

    committed = await call("ws_commit_build_spec", **spec)
    assert committed["ok"] is True and committed["mode"] == "team", committed
    assert committed["next_wave"] == ["agents", "knowledge", "workflows"]
    wave = await call("ws_design_parts")
    assert {part: row["status"] for part, row in wave["parts"].items()} == {
        "agents": "candidate", "knowledge": "candidate", "workflows": "candidate"}, wave
    assert seen == {"locked": True}
    accepted = await call("ws_accept_parts", parts=["agents", "knowledge", "workflows"])
    assert accepted["ok"] is True, accepted
    assert set(accepted["accepted"]["agents"]["agents"]) == {"writing", "review"}

    wave = await call("ws_design_parts")
    assert {part: row["status"] for part, row in wave["parts"].items()} == {
        "automations": "candidate", "app": "candidate"}, wave
    accepted = await call("ws_accept_parts", parts=["automations", "app"])
    assert accepted["ok"] is True, accepted
    lint = await call("ws_lint_draft")
    assert "by_part" not in lint or not lint["by_part"].get("automations"), lint

    await db_session.commit()
    stored = await db_session.get(WorkspaceDraft, draft.id, populate_existing=True)
    assert stored.fields["name"] == "Weekly article studio"  # the person's edit during the wave is kept
    assert stored.fields["_build_spec"]["progress"] == {part: "accepted" for part in build.SPEC_PARTS}
    assert "_automations_needed" not in stored.fields and "_candidates" not in stored.fields
    turn.finish(stored)

    async def ready(*_args, **_kwargs): return True
    monkeypatch.setattr("packages.core.services.workspace_draft_service._refresh_missing_from_lint", ready)
    stored.ready, stored.status = True, "ready"
    workspace_id, _ = await finalize_draft(db_session, draft_id=draft.id, entity_id=entity_id, user_id=user_id)
    installed = (await db_session.execute(select(WorkflowDefinition).where(
        WorkflowDefinition.workspace_id == workspace_id))).scalars().all()
    assert [row.name for row in installed] == ["weekly_report"]
    jobs = (await db_session.execute(select(ScheduledJob).where(ScheduledJob.workspace_id == workspace_id))).scalars().all()
    assert [(job.cron_expr, job.timezone) for job in jobs if job.cron_expr == "0 9 * * 1"] == [("0 9 * * 1", "Asia/Shanghai")]
    group = (await db_session.execute(select(DocumentGroup).where(
        DocumentGroup.workspace_id == workspace_id, DocumentGroup.name == "Brand voice"))).scalar_one()
    assert group.settings["starter_document"]["outline"] == ["语气", "禁用词", "范例"]
