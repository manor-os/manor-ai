"""The build spec a Workspace Architect commits before its part designers work.

The lead (the Architect) fixes the names and references every part shares --
service keys, the Agent each service gets, Workflow slugs, Stat keys,
Automation keys, Knowledge nets -- and a brief per part. Designers read it,
so parts designed in parallel agree on what they refer to. It is checked
here without a model: every reference resolves, keys are unique, a process
of three or more stages has a Workflow, and every routine the person named
has an Automation.

The spec also decides how the Draft is built (``mode``): a small Workspace
stays with the lead (``solo``), a larger one goes to the designers
(``team``), one wave of parts at a time in ``depends_on`` order.

It lives in the Draft field ``_build_spec``, which creation never reads.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from typing import Any

SPEC_FIELD = "_build_spec"
SPEC_VERSION = 1

# A spec with at most this many units of work (an Agent, a Workflow, an
# Automation or a Knowledge net each count one) and no Workflow of
# SOLO_MAX_STAGES or more stages is built by the lead itself.
SOLO_MAX_WORK_UNITS = 4
MULTI_STAGE_STAGES = 3
# A part sent back this many times with feedback goes to the lead.
MAX_SENDBACKS = 2

LIMITS = {"services": 5, "agents": 5, "workflows": 12, "stats": 12, "automations": 12,
          "routines": 12, "knowledge": 6, "integrations": 12}
DELIVERABLES = ("docx", "pptx", "xlsx", "pdf", "image", "video", "audio", "website")
STAGE_KINDS = ("agent", "approval")
# Parts a spec can give work to, in the order results are reported.
SPEC_PARTS = ("agents", "knowledge", "workflows", "automations", "app")
# A part in these states no longer holds up the parts that depend on it.
DONE = frozenset({"accepted", "skipped", "lead"})

_KEY = re.compile(r"^[a-z][a-z0-9_]*$")


def _rows(value: Any) -> list[dict[str, Any]]:
    return [dict(row) for row in value or [] if isinstance(row, dict)] if isinstance(value, list) else []


def _text(value: Any) -> str:
    return str(value or "").strip()


def _keys(value: Any) -> list[str]:
    return [_text(item) for item in value or [] if _text(item)] if isinstance(value, list) else []


def _fingerprint(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_spec(fields: dict[str, Any]) -> dict[str, Any] | None:
    spec = fields.get(SPEC_FIELD)
    return spec if isinstance(spec, dict) and spec.get("version") == SPEC_VERSION else None


# ── Validation ──────────────────────────────────────────────────────────────

def validate_build_spec(value: Any, fields: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
    """Check a proposed spec against the Draft; return (normalized spec, errors)."""
    from packages.core.schedule_cadence import normalize_timezone
    from packages.core.services.workspace_draft_sources import ready_source_ids

    if not isinstance(value, dict):
        return None, ["the build spec must be an object"]
    errors: list[str] = []
    saved = {_text(row.get("service_key")): row for row in _rows(fields.get("services")) if _text(row.get("service_key"))}

    def unique(section: str, rows: list[dict[str, Any]], key: str) -> list[str]:
        seen: list[str] = []
        for row in rows:
            name = _text(row.get(key))
            if not _KEY.match(name):
                errors.append(f"{section}: {key} {name!r} must be snake_case")
            elif name in seen:
                errors.append(f"{section}: {key} {name!r} is listed twice")
            seen.append(name)
        if len(rows) > LIMITS[section]:
            errors.append(f"{section}: at most {LIMITS[section]}, got {len(rows)}")
        return seen

    timezone = _text(value.get("timezone")) or "UTC"
    try:
        timezone = normalize_timezone(timezone)
    except ValueError:
        errors.append(f"timezone {timezone!r} is not an IANA time zone")

    services = _rows(value.get("services"))
    service_keys = unique("services", services, "key")
    for key in service_keys:
        if key and key not in saved:
            errors.append(f"services: {key!r} is not saved; save it with ws_propose_service first")
    for key in saved:
        if key not in service_keys:
            errors.append(f"services: the Draft's service {key!r} is missing from the spec")
    known_services = set(service_keys) | set(saved)
    stages_by_service = {_text(row.get("key")): _keys(row.get("stages")) for row in services}

    agents = _rows(value.get("agents"))
    agent_services = [_text(row.get("service_key")) for row in agents]
    if len(agents) > LIMITS["agents"]:
        errors.append(f"agents: at most {LIMITS['agents']}, got {len(agents)}")
    for row, key in zip(agents, agent_services):
        if key not in known_services:
            errors.append(f"agents: service_key {key!r} is not a service of the spec")
        elif agent_services.count(key) > 1:
            errors.append(f"agents: {key!r} has more than one Agent; each service has one")
        for deliverable in _keys(row.get("deliverables")):
            if deliverable not in DELIVERABLES:
                errors.append(f"agents.{key}: deliverable {deliverable!r} is not one of {list(DELIVERABLES)}")
        if not _text(row.get("role")):
            errors.append(f"agents.{key}: role says what this Agent does")
    for key in service_keys:
        if key and key not in agent_services:
            errors.append(f"agents: service {key!r} has no Agent; every service needs one")

    stats = _rows(value.get("stats"))
    stat_keys = unique("stats", stats, "key")
    workflows = _rows(value.get("workflows"))
    slugs = unique("workflows", workflows, "slug")
    for row in workflows:
        slug = _text(row.get("slug"))
        if _text(row.get("service_key")) not in known_services:
            errors.append(f"workflows.{slug}: service_key {row.get('service_key')!r} is not a service of the spec")
        for stage in _rows(row.get("stages")):
            kind = _text(stage.get("kind")) or "agent"
            if kind not in STAGE_KINDS:
                errors.append(f"workflows.{slug}: stage kind {kind!r} is not one of {list(STAGE_KINDS)}")
            elif kind == "agent" and _text(stage.get("service_key")) not in known_services:
                errors.append(f"workflows.{slug}: stage {stage.get('id')!r} runs unknown service "
                              f"{stage.get('service_key')!r}")
        for key in _keys(row.get("stat_keys")):
            if key not in stat_keys:
                errors.append(f"workflows.{slug}: stat_keys names {key!r}, which stats does not list")
    for row in stats:
        workflow = _text(row.get("workflow"))
        if workflow and workflow not in slugs:
            errors.append(f"stats.{row.get('key')}: workflow {workflow!r} is not a Workflow of the spec")

    # A process of several stages, a hand-off or a review is run by a Workflow.
    run_by = {_text(row.get("service_key")) for row in workflows} | {
        _text(stage.get("service_key")) for row in workflows for stage in _rows(row.get("stages"))}
    for key in known_services:
        saved_row = saved.get(key) or {}
        if key in run_by:
            continue
        if len(stages_by_service.get(key) or []) >= MULTI_STAGE_STAGES:
            errors.append(f"services.{key}: it has {len(stages_by_service[key])} stages but no Workflow runs "
                          "it; add one to workflows")
        elif saved_row.get("handoff_to") or saved_row.get("human_review"):
            errors.append(f"services.{key}: its hand-off or review is run by a Workflow; add one to workflows")

    automations = _rows(value.get("automations"))
    automation_keys = unique("automations", automations, "key")
    for row in automations:
        key = _text(row.get("key"))
        workflow, service = _text(row.get("workflow")), _text(row.get("service_key"))
        if workflow and workflow not in slugs:
            errors.append(f"automations.{key}: workflow {workflow!r} is not a Workflow of the spec")
        elif not workflow and service not in known_services:
            errors.append(f"automations.{key}: give the workflow it runs, or the service whose Agent runs it")
        if not _text(row.get("cadence")):
            errors.append(f"automations.{key}: cadence says when it runs in the person's words")
        if _text(row.get("timezone")):
            try:
                normalize_timezone(row["timezone"])
            except ValueError:
                errors.append(f"automations.{key}: timezone {row['timezone']!r} is not an IANA time zone")
    routines = _rows(value.get("routines"))
    if len(routines) > LIMITS["routines"]:
        errors.append(f"routines: at most {LIMITS['routines']}, got {len(routines)}")
    for row in routines:
        said, automation = _text(row.get("said")), _text(row.get("automation"))
        if not said:
            errors.append("routines: each routine quotes what the person said")
        elif not automation:
            errors.append(f"routines: {said!r} has no Automation; when its time is unclear ask the person with "
                          "ws_ask_user, then commit the spec again")
        elif automation not in automation_keys:
            errors.append(f"routines: {said!r} names automation {automation!r}, which automations does not list")

    knowledge = _rows(value.get("knowledge"))
    unique("knowledge", knowledge, "key")
    readable = ready_source_ids(fields)
    for row in knowledge:
        key = _text(row.get("key"))
        for service in _keys(row.get("for_services")):
            if service not in known_services:
                errors.append(f"knowledge.{key}: for_services names unknown service {service!r}")
        for source in _keys(row.get("source_ids")):
            if source not in readable:
                errors.append(f"knowledge.{key}: source {source!r} is not a ready source of this Draft")

    integrations = _rows(value.get("integrations"))
    if len(integrations) > LIMITS["integrations"]:
        errors.append(f"integrations: at most {LIMITS['integrations']}, got {len(integrations)}")

    briefs = {part: _text(text) for part, text in (value.get("briefs") or {}).items()
              if part in SPEC_PARTS and _text(text)} if isinstance(value.get("briefs"), dict) else {}
    spec = {
        "version": SPEC_VERSION, "language": _text(value.get("language")) or "en", "timezone": timezone,
        "business": {key: _text((value.get("business") or {}).get(key))
                     for key in ("summary", "audience", "success_signal")} if isinstance(value.get("business"), dict)
        else {"summary": "", "audience": "", "success_signal": ""},
        "services": [{"key": _text(row.get("key")), "stages": _keys(row.get("stages"))} for row in services],
        "agents": [{"service_key": _text(row.get("service_key")), "role": _text(row.get("role")),
                    "deliverables": _keys(row.get("deliverables")), "integrations": _keys(row.get("integrations")),
                    **({"reuse_agent_id": _text(row["reuse_agent_id"])} if _text(row.get("reuse_agent_id")) else {})}
                   for row in agents],
        "workflows": [{"slug": _text(row.get("slug")), "service_key": _text(row.get("service_key")),
                       "purpose": _text(row.get("purpose")),
                       "stages": [{"id": _text(stage.get("id")), "kind": _text(stage.get("kind")) or "agent",
                                   **({"service_key": _text(stage.get("service_key"))}
                                      if _text(stage.get("service_key")) else {})}
                                  for stage in _rows(row.get("stages"))],
                       "run_inputs": _rows(row.get("run_inputs")), "stat_keys": _keys(row.get("stat_keys")),
                       "dated": bool(row.get("dated")), "publishes_to": _keys(row.get("publishes_to"))}
                      for row in workflows],
        "stats": [{"key": _text(row.get("key")), "name": _text(row.get("name")),
                   **({"workflow": _text(row["workflow"])} if _text(row.get("workflow")) else {})} for row in stats],
        "automations": [{"key": _text(row.get("key")), "cadence": _text(row.get("cadence")),
                         **{name: _text(row[name]) for name in ("workflow", "service_key", "timezone", "why")
                            if _text(row.get(name))}}
                        for row in automations],
        "routines": [{"said": _text(row.get("said")), "automation": _text(row.get("automation"))} for row in routines],
        "knowledge": [{"key": _text(row.get("key")), "name": _text(row.get("name")) or _text(row.get("key")),
                       "purpose": _text(row.get("purpose")), "for_services": _keys(row.get("for_services")),
                       "source_ids": _keys(row.get("source_ids"))} for row in knowledge],
        "integrations": [{"provider": _text(row.get("provider")), "required_by": _keys(row.get("required_by"))}
                         for row in integrations if _text(row.get("provider"))],
        "briefs": briefs,
    }
    for part in planned_parts(spec, fields):
        if len(briefs.get(part, "")) < 20:
            errors.append(f"briefs.{part}: write the {part} designer a brief in the person's words")
    return (None if errors else spec), errors


# ── Parts, mode and waves ───────────────────────────────────────────────────

def _slices(spec: dict[str, Any]) -> dict[str, Any]:
    """What each part designs from: a change to it makes the part dirty."""
    briefs = spec.get("briefs") or {}
    return {
        "agents": [spec.get("services"), spec.get("agents"), spec.get("integrations"), briefs.get("agents")],
        "knowledge": [spec.get("knowledge"), briefs.get("knowledge")],
        "workflows": [spec.get("services"), spec.get("workflows"), spec.get("stats"), spec.get("timezone"),
                      briefs.get("workflows")],
        "automations": [spec.get("automations"), spec.get("routines"), spec.get("timezone"),
                        briefs.get("automations")],
        "app": [[row.get("slug") for row in spec.get("workflows") or []], spec.get("stats"), briefs.get("app")],
    }


def planned_parts(spec: dict[str, Any], fields: dict[str, Any] | None = None) -> list[str]:
    """The parts this spec gives work to."""
    planned = []
    if spec.get("agents"):
        planned.append("agents")
    if spec.get("knowledge"):
        planned.append("knowledge")
    if spec.get("workflows") or spec.get("stats"):
        planned.append("workflows")
    # Workflows may need schedules the spec does not list yet.
    if spec.get("automations") or spec.get("routines") or spec.get("workflows"):
        planned.append("automations")
    if spec.get("services") or (fields or {}).get("services"):
        planned.append("app")
    return planned


def work_units(spec: dict[str, Any]) -> int:
    return sum(len(spec.get(section) or []) for section in ("agents", "workflows", "automations", "knowledge"))


def build_mode(spec: dict[str, Any]) -> str:
    """``solo`` when the lead builds every part itself, else ``team``."""
    multi_stage = any(len(row.get("stages") or []) >= MULTI_STAGE_STAGES for row in spec.get("workflows") or [])
    return "team" if multi_stage or work_units(spec) > SOLO_MAX_WORK_UNITS else "solo"


def dirty_parts(old: dict[str, Any] | None, new: dict[str, Any], depends_on: dict[str, tuple[str, ...]]) -> list[str]:
    """Parts whose slice changed since ``old``, and every part downstream of one."""
    planned = planned_parts(new)
    if old is None:
        return planned
    before, after = _slices(old), _slices(new)
    dirty = {part for part in planned if _fingerprint(before.get(part)) != _fingerprint(after.get(part))}
    changed = True
    while changed:
        changed = False
        for part in planned:
            if part not in dirty and any(dep in dirty for dep in depends_on.get(part, ())):
                dirty.add(part)
                changed = True
    return [part for part in planned if part in dirty]


def commit_spec(fields: dict[str, Any], spec: dict[str, Any], depends_on: dict[str, tuple[str, ...]]) -> dict[str, Any]:
    """Store ``spec`` in ``fields``; dirty parts go back to pending. Returns the stored spec."""
    old = build_spec(fields)
    planned = planned_parts(spec, fields)
    dirty = dirty_parts(old, spec, depends_on)
    progress = {part: state for part, state in ((old or {}).get("progress") or {}).items() if part in planned}
    sendbacks = {part: count for part, count in ((old or {}).get("sendbacks") or {}).items() if part in planned}
    for part in dirty:
        progress[part] = "pending"
        sendbacks.pop(part, None)
    for part in planned:
        progress.setdefault(part, "pending")
    stored = {**copy.deepcopy(spec), "mode": build_mode(spec), "progress": progress, "sendbacks": sendbacks,
              "dirty": dirty}
    fields[SPEC_FIELD] = stored
    return stored


def set_progress(fields: dict[str, Any], part: str, state: str) -> None:
    """Record ``part``'s state; nothing without a spec or for a part it does not plan."""
    spec = build_spec(fields)
    if spec is None or part not in (spec.get("progress") or {}):
        return
    fields[SPEC_FIELD] = {**spec, "progress": {**spec["progress"], part: state}}


def count_sendback(fields: dict[str, Any], part: str) -> int:
    spec = build_spec(fields)
    if spec is None:
        return 0
    sendbacks = {**(spec.get("sendbacks") or {})}
    sendbacks[part] = int(sendbacks.get(part) or 0) + 1
    fields[SPEC_FIELD] = {**spec, "sendbacks": sendbacks}
    return sendbacks[part]


def next_wave(spec: dict[str, Any], depends_on: dict[str, tuple[str, ...]]) -> list[str]:
    """The parts ready to design now: not done, and everything they depend on is done."""
    progress = spec.get("progress") or {}
    return [part for part in SPEC_PARTS if part in progress and progress[part] not in DONE
            and all(progress.get(dep, "accepted") in DONE for dep in depends_on.get(part, ()))]


def waiting_on(spec: dict[str, Any], part: str, depends_on: dict[str, tuple[str, ...]]) -> list[str]:
    progress = spec.get("progress") or {}
    return [dep for dep in depends_on.get(part, ()) if progress.get(dep, "accepted") not in DONE]


def spec_slice(fields: dict[str, Any], part: str) -> dict[str, Any]:
    """What a designer reads of the spec: the shared names and its own section."""
    spec = build_spec(fields)
    if spec is None:
        return {}
    shared = {"language": spec.get("language"), "timezone": spec.get("timezone"),
              "business": spec.get("business"), "services": spec.get("services"),
              "agents": [{"service_key": row["service_key"], "role": row.get("role")} for row in spec.get("agents") or []],
              "workflows": [{"slug": row["slug"], "service_key": row.get("service_key"),
                             "purpose": row.get("purpose")} for row in spec.get("workflows") or []],
              "stats": spec.get("stats")}
    own = {
        "agents": {"agents": spec.get("agents"), "integrations": spec.get("integrations")},
        "knowledge": {"knowledge": spec.get("knowledge")},
        "workflows": {"workflows": spec.get("workflows"), "stats": spec.get("stats")},
        "automations": {"automations": spec.get("automations"), "routines": spec.get("routines")},
        "app": {},
    }.get(part, {})
    return {**shared, **own}


# ── Lint ────────────────────────────────────────────────────────────────────

def issues_by_part(issues: list[dict[str, Any]], lint_where: dict[str, tuple[str, ...]]) -> dict[str, list[dict[str, Any]]]:
    """Each lint issue under the part that owns its ``where``; the lead keeps the rest."""
    owned: dict[str, list[dict[str, Any]]] = {}
    for issue in issues:
        head = str(issue.get("where") or "").split(".", 1)[0]
        part = next((part for part, prefixes in lint_where.items() if head in prefixes), None)
        if part is not None and issue.get("severity") in {"P0", "P1"}:
            owned.setdefault(part, []).append(issue)
    return owned
