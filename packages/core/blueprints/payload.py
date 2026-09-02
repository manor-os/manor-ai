"""Blueprint payload schema — the portable JSON document.

The payload deliberately leaves a few "sharp edges" — it's a forward-
compatible JSON, not a typed Pydantic model. Reasons:

  * Manor's internal models grow new columns; if every blueprint had
    to re-validate against the latest type signatures, every export
    would break on schema drift. The payload locks in only the fields
    a blueprint needs to *re-create* a workspace, not every column.

  * Operators (and downstream tools) may want to author payloads by
    hand or fork existing ones — JSON is friendlier than dataclasses.

v1.1 shape (5 sections — see the reference comment block at the bottom):

  manifest   identity + discovery + version + declared dependencies
  contract   what the installer's environment must bring (variables,
             channels, sessions, external tools/agents/skills/MCP)
  embedded   what the blueprint itself carries (private skills, private
             agents with their bindings, knowledge-pack scaffolds)
  recipe     how the workspace runs (operating_model, strategist,
             prompts, subscriptions, scheduled_jobs, workflows, goals,
             task_categories, custom_fields, sla_policies,
             escalation_rules, simulation_experience)
  policy     governance + post-install checks + expected baseline

Backward compat: v1.0 payloads (flat top-level title/workspace/
subscriptions/...) auto-migrate to v1.1 on load. The installer always
sees v1.1 shape.

What ``validate_payload`` enforces:

  * top-level shape (each of the 5 sections must be an object)
  * blueprint_version matches a version this module knows
  * no secret-shaped key names anywhere in the payload tree
    (credential_ref, *_token, *_secret, password, ...)
  * no local runtime ULIDs under Workspace/Agent/Skill/Workflow reference keys
  * every installable list section contains shaped object records
  * embedded agents/skills only bind tools declared in contract.requires
  * MCP allowlists don't declare secret-shaped field names
  * embedded agent starter_memory has no user_id (personal scope must
    not be templated)
  * knowledge_pack starter_documents are .md only
  * strategist.business_model.model_type is a known enum
  * strategist.evaluation_rubric.weights sum to 1.0
  * portable Goal records have installable identity, target, and measurement fields
  * governance never_allow and auto_approve don't overlap
  * simulation_experience artifacts are safe, portable, and explicitly typed

Versioning: bump ``BLUEPRINT_VERSION`` on a breaking change and add a
per-version migrator. Minor / additive changes don't bump the version.
"""

from __future__ import annotations

import copy
import re
from datetime import date
from pathlib import PurePosixPath
from typing import Any

from packages.core.blueprints.simulation import (
    SIMULATION_ACTION_KINDS,
    SIMULATION_ARTIFACT_KINDS,
    SIMULATION_EXPERIENCE_VERSION,
    SIMULATION_STAGE_AUTHORS,
    SIMULATION_STAGE_KINDS,
)
from packages.core.goals.factory import GoalIdentityFactory
from packages.core.constants.blueprints import (
    BLUEPRINT_KNOWLEDGE_DOCUMENT_KEY_MAX_LENGTH,
    BLUEPRINT_KNOWLEDGE_MAX_DOCUMENT_BYTES,
    BLUEPRINT_KNOWLEDGE_MAX_DOCUMENTS,
    BLUEPRINT_KNOWLEDGE_MAX_TOTAL_BYTES,
    is_runtime_derived_scheduled_job,
)
from packages.core.goals.numbers import validate_goal_number
from packages.core.goals.scheduling import validate_measurement_cadence
from packages.core.proposals.constants import WORKFLOW_RUN_EXTERNAL_ACTION_KEY
from packages.core.services.provider_keys import canonical_provider_key

BLUEPRINT_VERSION = "1.1"

# Versions this module knows how to read. Older versions are migrated
# up to BLUEPRINT_VERSION on load.
SUPPORTED_VERSIONS = frozenset({"1.0", "1.1"})

_LOCAL_ULID_RE = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$", re.IGNORECASE)
_KNOWLEDGE_DOCUMENT_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
LOCAL_RUNTIME_REFERENCE_KEYS = frozenset({
    "entity_id",
    "workspace_id",
    "user_id",
    "agent_id",
    "agent_subscription_id",
    "subscription_id",
    "conversation_id",
    "channel_config_id",
    "document_id",
    "group_id",
    "task_id",
    "goal_id",
    "workflow_id",
    "workflow_binding_id",
    "binding_id",
    "integration_session_id",
    "browser_session_id",
    "session_id",
    "mcp_server_id",
    "tool_id",
    "skill_id",
    "sla_policy_id",
    "category_id",
    "connection_id",
    "created_by_user_id",
    "source_existing_group_id",
    "source_template_group_id",
})


class PayloadError(ValueError):
    """Raised on malformed payloads."""


# ── Forbidden field-name patterns (secret-leak prevention) ────────────
#
# We scan KEY NAMES anywhere in the payload tree. Values are not
# inspected — a string value happening to be "api_key" is fine.

# Exact key names that always indicate a leak.
_FORBIDDEN_EXACT = frozenset(
    {
        "credential_ref",
        "credentials",
        "session_state_ref",
        "secret",
        "vault_token",
        "worker_secret",
        "password",
        "passphrase",
        "api_key",
        "apikey",
        "access_token",
        "refresh_token",
        "bearer_token",
        "private_key",
    }
)

# Substring patterns. A key containing any of these (case-insensitive)
# is flagged — unless it's in _SUBSTRING_EXEMPTIONS.
_FORBIDDEN_SUBSTRINGS = (
    "_token",
    "_secret",
    "_apikey",
    "_api_key",
    "password",
    "passphrase",
)

# Known-safe names that happen to substring-match. Add new entries here
# when introducing a payload field that trips the scanner.
_SUBSTRING_EXEMPTIONS = frozenset(
    {
        # Bare generic — used as variable.key, prompt.key, etc.
        "key",
        # Domain-key suffixes (semantic identifiers, not credentials).
        "service_key",
        "metric_key",
        "memory_key",
        "tool_key",
        "skill_key",
        "agent_key",
        "field_key",
        "job_key",
        "step_key",
        "action_key",
        "event_key",
        "config_key",
        "uses_prompt",  # not a key match, but listed for documentation
        # Allowlist fields that NAME other fields (the values are what
        # actually gets configured; the field itself is a declaration).
        "config_fields_to_set",
        "config_override_allowlist",
    }
)


# ── Strategist business model enum ────────────────────────────────────

_MODEL_TYPES = frozenset(
    {
        "social_growth",
        "saas",
        "marketplace",
        "content_publishing",
        "services_delivery",
        "community",
    }
)


# ── Public API ────────────────────────────────────────────────────────


def detect_version(payload: dict[str, Any]) -> str:
    """Return the blueprint_version of ``payload``.

    v1.1 puts it at ``manifest.blueprint_version``; v1.0 had it at the
    top level. Raises ``PayloadError`` if neither is present.
    """
    if not isinstance(payload, dict):
        raise PayloadError("payload must be a JSON object")
    m = payload.get("manifest")
    if isinstance(m, dict) and m.get("blueprint_version"):
        return str(m["blueprint_version"])
    v = payload.get("blueprint_version")
    if v is not None:
        return str(v)
    raise PayloadError(
        "payload has no blueprint_version (checked manifest.blueprint_version and top-level blueprint_version)"
    )


def migrate_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Convert ``payload`` to the current shape (v1.1).

    Returns a NEW dict and does not mutate the input. v1.0 payloads are
    lifted into the 5-section shape; v1.1-new sections become empty/null
    since v1.0 doesn't carry them. Goal identities are normalized for both
    versions so legacy shared metrics remain portable.
    """
    version = detect_version(payload)
    if version == BLUEPRINT_VERSION:
        migrated = copy.deepcopy(payload)
    elif version == "1.0":
        migrated = _migrate_v10_to_v11(payload)
    else:
        raise PayloadError(f"unsupported blueprint_version {version!r}; supported: {sorted(SUPPORTED_VERSIONS)}")
    return _normalize_goal_keys(migrated)


def _normalize_goal_keys(payload: dict[str, Any]) -> dict[str, Any]:
    recipe = payload.get("recipe")
    if not isinstance(recipe, dict):
        return payload

    operating_model = recipe.get("operating_model")
    operating_goals = operating_model.get("goals") if isinstance(operating_model, dict) else None
    recipe_goals = recipe.get("goals")

    # recipe.goals is the portable source of truth. Legacy payloads sometimes
    # declared Goals only inside operating_model, so promote that list only when
    # the canonical field is absent. An explicit empty recipe.goals is the
    # portable declaration that this Workspace has no Goals.
    # operating_model.goals is migration input only; installers project the
    # canonical definitions into Workspace runtime state.
    source_goals = None
    canonical_goals = False
    if isinstance(recipe_goals, list):
        source_goals = recipe_goals
        canonical_goals = True
    elif "goals" not in recipe and isinstance(operating_goals, list):
        source_goals = operating_goals

    if source_goals is not None:
        # Validate an explicitly authored identity before normalization can
        # trim or stringify it. Generated keys may be shortened, but a caller's
        # stable logical key must never change silently.
        explicit_goal_keys: dict[str, int] = {}
        for index, goal in enumerate(source_goals):
            if not isinstance(goal, dict):
                continue
            goal_key = goal.get("goal_key")
            if goal_key is not None and (
                not isinstance(goal_key, str) or len(goal_key.strip()) > 100
            ):
                raise PayloadError(
                    f"recipe.goals[{index}].goal_key must be a string of at most 100 characters"
                )
            normalized_goal_key = str(goal_key or "").strip()
            if canonical_goals and normalized_goal_key:
                previous_index = explicit_goal_keys.get(normalized_goal_key)
                if previous_index is not None:
                    raise PayloadError(
                        f"recipe.goals[{index}].goal_key duplicates "
                        f"recipe.goals[{previous_index}].goal_key"
                    )
                explicit_goal_keys[normalized_goal_key] = index
        recipe["goals"] = GoalIdentityFactory.normalize_records(source_goals)
    if isinstance(operating_model, dict):
        operating_model.pop("goals", None)
    return payload


def validate_payload(payload: dict[str, Any]) -> None:
    """Cheap structural + safety check.

    Accepts v1.0 or v1.1 input. For v1.0, the payload is migrated to
    v1.1 internally before structural checks run; v1.1 invariants are
    enforced either way. Does not mutate the caller's payload.
    """
    if not isinstance(payload, dict):
        raise PayloadError("payload must be a JSON object")

    # 1) Scan the INPUT for forbidden keys before any migration.
    #    Migration drops unknown fields, so a v1.0 payload carrying a
    #    rogue ``credential_ref`` somewhere unconventional would survive
    #    silently if we scanned the migrated form only.
    leaked = _scan_forbidden_keys(payload)
    if leaked:
        raise PayloadError(f"payload contains forbidden field names (would leak credentials): {sorted(leaked)}")
    local_references = _scan_local_reference_values(payload)
    if local_references:
        raise PayloadError(
            "payload contains local runtime references: "
            f"{sorted(local_references)}"
        )

    # 2) Migrate to v1.1 and check structural invariants there.
    p = migrate_payload(payload)
    _validate_v11(p)


# ── v1.0 → v1.1 migration ─────────────────────────────────────────────


def _migrate_v10_to_v11(p: dict[str, Any]) -> dict[str, Any]:
    """Lift the flat v1.0 shape into the 5-section v1.1 shape.

    Mapping:
      payload.title/summary/description/tags/author    → manifest.*
      payload.workspace.kind                           → manifest.kind +
                                                          recipe.operating_model.kind
      payload.workspace.operating_context              → recipe.operating_model.context
      payload.workspace.primary_work                   → recipe.operating_model.primary_work
      payload.workspace.settings                       → recipe.operating_model.settings
      payload.workspace.operating_model.*              → recipe.operating_model.* (merged)
      payload.subscriptions/goals/scheduled_jobs/      → recipe.*
        custom_fields
      payload.channel_requirements                     → contract.channels
      payload.session_requirements                     → contract.sessions
      payload.governance_policy                        → policy.governance
      payload.memory_files                             → embedded.knowledge_packs[0]
                                                          (single inline pack)

    All v1.1-new sections (variables, requires, embedded.skills/agents,
    strategist, workflows, task_categories, sla_policies,
    escalation_rules, post_install_checks, expected_baseline) become
    empty/null. Proposal-generated Task instances and task templates are
    runtime concerns and are never migrated into a Blueprint.
    """
    ws = p.get("workspace") if isinstance(p.get("workspace"), dict) else {}

    # operating_model absorbs the workspace shell fields so the
    # installer has one consistent place to read from.
    om: dict[str, Any] = dict(ws.get("operating_model") or {})
    if ws.get("operating_context") and "context" not in om:
        om["context"] = ws.get("operating_context")
    if ws.get("primary_work") and "primary_work" not in om:
        om["primary_work"] = ws.get("primary_work")
    if ws.get("kind") and "kind" not in om:
        om["kind"] = ws.get("kind")
    settings = ws.get("settings") or {}
    if settings and "settings" not in om:
        om["settings"] = dict(settings)

    # v1.0 memory_files → a single inline knowledge_pack so the content
    # survives migration. Operators can split it into proper packs later.
    memory_files = p.get("memory_files") or []
    knowledge_packs: list[dict[str, Any]] = []
    if isinstance(memory_files, list) and memory_files:
        starter_docs: list[dict[str, Any]] = []
        for m in memory_files:
            if not isinstance(m, dict) or not m.get("path"):
                continue
            starter_docs.append(
                {
                    "path": m["path"],
                    "body_md": m.get("body", ""),
                    "frontmatter": m.get("frontmatter"),
                }
            )
        if starter_docs:
            knowledge_packs.append(
                {
                    "slug": "imported-memory",
                    "title": "Imported memory files",
                    "purpose": "Carried over from v1.0 memory_files",
                    "mode": "inline_text",
                    "folder_structure": [],
                    "starter_documents": starter_docs,
                    "external_source": None,
                }
            )

    return {
        "manifest": {
            "blueprint_version": BLUEPRINT_VERSION,
            "slug": None,
            "title": p.get("title"),
            "summary": p.get("summary"),
            "use_when": None,
            "description": p.get("description"),
            "tags": list(p.get("tags") or []),
            "kind": ws.get("kind"),
            "category": None,
            "author": p.get("author") or {},
            "cover_image_url": None,
            "forked_from_id": None,
            "changelog": None,
        },
        "contract": {
            "variables": [],
            "channels": list(p.get("channel_requirements") or []),
            "sessions": list(p.get("session_requirements") or []),
            "requires": {
                "manor_min_version": None,
                "tools": [],
                "mcp_servers": [],
                "skills": [],
                "agents": [],
            },
        },
        "embedded": {
            "skills": [],
            "agents": [],
            "knowledge_packs": knowledge_packs,
        },
        "recipe": {
            "operating_model": om,
            "strategist": None,
            "prompts": [],
            "subscriptions": list(p.get("subscriptions") or []),
            "scheduled_jobs": list(p.get("scheduled_jobs") or []),
            "workflows": [],
            "stats": list(p.get("stats") or []),
            "goals": list(p.get("goals") or []),
            "task_categories": [],
            "custom_fields": list(p.get("custom_fields") or []),
            "sla_policies": [],
            "escalation_rules": [],
        },
        "policy": {
            "governance": p.get("governance_policy") or {},
            "post_install_checks": [],
            "expected_baseline": None,
        },
    }


# ── v1.1 structural validation ────────────────────────────────────────

_LIST_PATHS = (
    ("contract", "variables"),
    ("contract", "channels"),
    ("contract", "sessions"),
    ("contract", "requires", "mcp_servers"),
    ("embedded", "skills"),
    ("embedded", "agents"),
    ("embedded", "knowledge_packs"),
    ("recipe", "prompts"),
    ("recipe", "subscriptions"),
    ("recipe", "scheduled_jobs"),
    ("recipe", "workflows"),
    ("recipe", "stats"),
    ("recipe", "goals"),
    ("recipe", "task_categories"),
    ("recipe", "custom_fields"),
    ("recipe", "sla_policies"),
    ("recipe", "escalation_rules"),
    ("policy", "post_install_checks"),
)


def _validate_task_policy_sections(recipe: dict[str, Any]) -> None:
    """Reject entity-shared task policy and validate descriptive rules.

    Categories and SLA rows have entity scope rather than Workspace ownership,
    so Blueprint installation cannot safely create or update them. A rule with
    an SLA reference is likewise executable entity policy and is rejected. A
    condition-only rule is retained as operating guidance and never materialized
    as a ``TaskEscalationRule`` row.
    """
    for section in ("task_categories", "sla_policies"):
        values = recipe.get(section) or []
        if values:
            raise PayloadError(f"recipe.{section} is not portable; task policy is entity-scoped")

    for index, rule in enumerate(recipe.get("escalation_rules") or []):
        if not isinstance(rule, dict):
            raise PayloadError(f"recipe.escalation_rules[{index}] must be an object")
        if str(rule.get("sla_policy_key") or rule.get("sla_key") or "").strip():
            raise PayloadError(f"recipe.escalation_rules[{index}] is not portable; SLA-linked rules are entity-scoped")
        if not str(rule.get("key") or rule.get("slug") or "").strip():
            raise PayloadError(f"recipe.escalation_rules[{index}] requires key")
        if not str(rule.get("action") or rule.get("action_type") or "").strip():
            raise PayloadError(f"recipe.escalation_rules[{index}] requires action")
        if not str(rule.get("condition") or "").strip():
            raise PayloadError(f"recipe.escalation_rules[{index}] requires condition")
        if rule.get("notify_user_ids"):
            raise PayloadError(
                f"recipe.escalation_rules[{index}].notify_user_ids is not portable; "
                "resolve target users in the runtime entity"
            )


def _validate_prompts(recipe: dict[str, Any]) -> None:
    for index, prompt in enumerate(recipe.get("prompts") or []):
        if not isinstance(prompt, dict):
            raise PayloadError(f"recipe.prompts[{index}] must be an object")


def _validate_contract_variables(contract: dict[str, Any]) -> None:
    seen: set[str] = set()
    for index, variable in enumerate(contract.get("variables") or []):
        path = f"contract.variables[{index}]"
        if not isinstance(variable, dict):
            raise PayloadError(f"{path} must be an object")
        key = variable.get("key")
        if (
            not isinstance(key, str)
            or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]{0,99}", key)
        ):
            raise PayloadError(
                f"{path}.key must be a portable variable name of at most 100 characters"
            )
        if _is_secret_shape(key):
            raise PayloadError(
                f"{path}.key {key!r} is credential-shaped; Blueprint install "
                "variables must not carry secrets"
            )
        if key in seen:
            raise PayloadError(f"{path}.key duplicates {key!r}")
        seen.add(key)
        if "required" in variable and not isinstance(variable["required"], bool):
            raise PayloadError(f"{path}.required must be a boolean")
        if "materialize" in variable and not isinstance(variable["materialize"], bool):
            raise PayloadError(f"{path}.materialize must be a boolean")


def _validate_scheduled_jobs(recipe: dict[str, Any]) -> None:
    seen: set[str] = set()
    workflow_slugs = {
        str(workflow.get("slug") or "").strip()
        for workflow in recipe.get("workflows") or []
        if isinstance(workflow, dict) and str(workflow.get("slug") or "").strip()
    }
    for index, job in enumerate(recipe.get("scheduled_jobs") or []):
        path = f"recipe.scheduled_jobs[{index}]"
        if not isinstance(job, dict):
            raise PayloadError(f"{path} must be an object")
        job_id = str(job.get("job_id") or "").strip()
        if not job_id:
            raise PayloadError(f"{path}.job_id is required")
        if job_id in seen:
            raise PayloadError(f"{path}.job_id duplicates {job_id!r}")
        seen.add(job_id)
        if is_runtime_derived_scheduled_job(
            job_id=job_id,
            execution_type=job.get("execution_type"),
        ):
            raise PayloadError(
                f"{path} is runtime-derived; declare its Workspace, Goal, or "
                "Stat configuration instead"
            )
        if job.get("execution_type") == "skill":
            target = job.get("execution_target")
            if not isinstance(target, dict) or not (
                str(target.get("skill_component_key") or "").strip()
                or str(target.get("skill_marketplace_id") or "").strip()
            ):
                raise PayloadError(
                    f"{path} requires a portable Skill target"
                )
        if job.get("execution_type") == "workflow":
            target = job.get("execution_target")
            workflow_slug = str(
                target.get("workflow_slug")
                if isinstance(target, dict) else ""
            ).strip()
            if not workflow_slug or workflow_slug not in workflow_slugs:
                raise PayloadError(
                    f"{path} requires a declared portable Workflow target"
                )
        if job.get("execution_type") in {"agent", "agent_message"}:
            target = job.get("execution_target")
            service_key = str(
                (target.get("service_key") or "")
                if isinstance(target, dict)
                else ""
            ).strip()
            if not service_key:
                raise PayloadError(
                    f"{path} requires a portable Agent service_key target"
                )
        if job.get("agent_id"):
            raise PayloadError(
                f"{path}.agent_id is a local runtime reference; use service_key"
            )
        target = job.get("execution_target")
        if target is not None and not isinstance(target, dict):
            raise PayloadError(f"{path}.execution_target must be an object")
        local_target_fields = sorted(
            field
            for field in (
                "agent_id",
                "binding_id",
                "skill_id",
                "workflow_id",
                "workspace_id",
            )
            if isinstance(target, dict) and target.get(field)
        )
        if local_target_fields:
            raise PayloadError(
                f"{path}.execution_target contains local runtime references: "
                f"{local_target_fields}"
            )
        for field in ("enabled", "delete_after_run"):
            if field in job and not isinstance(job[field], bool):
                raise PayloadError(f"{path}.{field} must be a boolean")


_BLUEPRINT_SETUP_EXECUTION_TYPES = frozenset({
    "agent",
    "agent_message",
    "skill",
    "workflow",
})
_BLUEPRINT_READY_EXECUTION_TYPES = _BLUEPRINT_SETUP_EXECUTION_TYPES | frozenset({
    "briefing",
    "strategist_review",
})


def _validate_blueprint_startup(recipe: dict[str, Any]) -> None:
    operating_model = recipe.get("operating_model") or {}
    if not isinstance(operating_model, dict):
        return
    settings = operating_model.get("settings") or {}
    if not isinstance(settings, dict):
        return
    blocking_setup = settings.get("blocking_setup")
    if blocking_setup is None:
        return
    if not isinstance(blocking_setup, dict):
        raise PayloadError(
            "recipe.operating_model.settings.blocking_setup must be an object"
        )

    jobs_by_id = {
        str(job.get("job_id") or "").strip(): job
        for job in recipe.get("scheduled_jobs") or []
        if isinstance(job, dict) and str(job.get("job_id") or "").strip()
    }
    checks = blocking_setup.get("checks") or []
    if not isinstance(checks, list):
        raise PayloadError(
            "recipe.operating_model.settings.blocking_setup.checks must be an array"
        )
    for index, check in enumerate(checks):
        if not isinstance(check, dict):
            raise PayloadError(
                "recipe.operating_model.settings.blocking_setup"
                f".checks[{index}] must be an object"
            )
        setup_job_id = str(check.get("setup_job_id") or "").strip()
        if not setup_job_id:
            continue
        path = (
            "recipe.operating_model.settings.blocking_setup"
            f".checks[{index}].setup_job_id"
        )
        job = jobs_by_id.get(setup_job_id)
        if job is None:
            raise PayloadError(f"{path} references missing job {setup_job_id!r}")
        if job.get("enabled") is False:
            raise PayloadError(f"{path} references disabled job {setup_job_id!r}")
        if job.get("job_type") != "manual" or job.get("schedule_kind") is not None:
            raise PayloadError(f"{path} must reference a manual unscheduled job")
        execution_type = str(job.get("execution_type") or "agent").strip()
        if execution_type not in _BLUEPRINT_SETUP_EXECUTION_TYPES:
            raise PayloadError(
                f"{path} references unsupported setup execution_type "
                f"{execution_type!r}"
            )

    on_ready_job_id = str(blocking_setup.get("on_ready_job_id") or "").strip()
    if not on_ready_job_id:
        return
    path = "recipe.operating_model.settings.blocking_setup.on_ready_job_id"
    job = jobs_by_id.get(on_ready_job_id)
    if job is None:
        raise PayloadError(f"{path} references missing job {on_ready_job_id!r}")
    if job.get("enabled") is False:
        raise PayloadError(f"{path} references disabled job {on_ready_job_id!r}")
    execution_type = str(job.get("execution_type") or "agent").strip()
    if execution_type not in _BLUEPRINT_READY_EXECUTION_TYPES:
        raise PayloadError(
            f"{path} references unsupported ready execution_type {execution_type!r}"
        )


def _validate_record_sections(p: dict[str, Any]) -> None:
    """Require every list section to contain installable object records."""
    for path in _LIST_PATHS:
        node: Any = p
        for key in path:
            node = node.get(key) if isinstance(node, dict) else None
        for index, item in enumerate(node or []):
            if not isinstance(item, dict):
                raise PayloadError(
                    f"payload.{'.'.join(path)}[{index}] must be an object"
                )

    required_fields = {
        ("contract", "channels"): ("channel_type",),
        ("contract", "sessions"): ("provider",),
        ("contract", "requires", "mcp_servers"): ("slug",),
        ("embedded", "skills"): ("slug",),
        ("embedded", "agents"): ("slug",),
        ("embedded", "knowledge_packs"): ("slug",),
        ("recipe", "subscriptions"): ("service_key",),
        ("recipe", "workflows"): ("slug",),
        ("recipe", "custom_fields"): ("name",),
        ("policy", "post_install_checks"): ("kind",),
    }
    for path, fields in required_fields.items():
        node: Any = p
        for key in path:
            node = node.get(key) if isinstance(node, dict) else None
        for index, item in enumerate(node or []):
            for field in fields:
                if not str(item.get(field) or "").strip():
                    raise PayloadError(
                        f"payload.{'.'.join(path)}[{index}].{field} is required"
                    )

    for index, stat in enumerate(p["recipe"].get("stats") or []):
        if stat.get("library_key"):
            continue
        if not str(stat.get("key") or "").strip() or not str(
            stat.get("name") or ""
        ).strip():
            raise PayloadError(
                f"payload.recipe.stats[{index}] requires library_key or key and name"
            )


def _validate_workflows(recipe: dict[str, Any]) -> None:
    workflows = list(recipe.get("workflows") or [])
    slugs: set[str] = set()
    for index, workflow in enumerate(workflows):
        slug = str(workflow.get("slug") or "").strip()
        if slug in slugs:
            raise PayloadError(
                f"recipe.workflows[{index}].slug duplicates {slug!r}"
            )
        slugs.add(slug)
        if "trigger_config" in workflow and not isinstance(
            workflow.get("trigger_config"), dict
        ):
            raise PayloadError(
                f"recipe.workflows[{index}].trigger_config must be an object"
            )
        if "binding_config" in workflow and not isinstance(
            workflow.get("binding_config"), dict
        ):
            raise PayloadError(
                f"recipe.workflows[{index}].binding_config must be an object"
            )
        for field in ("enabled", "definition_enabled"):
            if field in workflow and not isinstance(workflow[field], bool):
                raise PayloadError(
                    f"recipe.workflows[{index}].{field} must be a boolean"
                )
        for field in ("status", "definition_status"):
            if field in workflow and (
                not isinstance(workflow.get(field), str)
                or not str(workflow.get(field) or "").strip()
            ):
                raise PayloadError(
                    f"recipe.workflows[{index}].{field} must be a non-empty string"
                )
        if "steps" in workflow and not isinstance(workflow.get("steps"), list):
            raise PayloadError(f"recipe.workflows[{index}].steps must be an array")
        _validate_workflow_proposal_authorization(workflow, index=index)

    for workflow_index, workflow in enumerate(workflows):
        for step_index, step in enumerate(workflow.get("steps") or []):
            if not isinstance(step, dict):
                raise PayloadError(
                    f"recipe.workflows[{workflow_index}].steps[{step_index}] "
                    "must be an object"
                )
            step_type = step.get("type") or step.get("kind")
            if step_type not in {"subworkflow", "foreach_subworkflow"}:
                continue
            config = step.get("config")
            if not isinstance(config, dict):
                raise PayloadError(
                    f"recipe.workflows[{workflow_index}].steps[{step_index}].config "
                    "must be an object"
                )
            source_key = str(
                config.get("source_workflow_key")
                or config.get("workflow_id")
                or ""
            ).strip()
            if _LOCAL_ULID_RE.fullmatch(source_key):
                raise PayloadError(
                    f"recipe.workflows[{workflow_index}].steps[{step_index}] "
                    "contains a local workflow_id"
                )
            if not source_key or source_key not in slugs:
                raise PayloadError(
                    f"recipe.workflows[{workflow_index}].steps[{step_index}] "
                    f"references missing Blueprint Flow {source_key!r}"
                )


def _validate_workflow_proposal_authorization(
    workflow: dict[str, Any],
    *,
    index: int,
) -> None:
    declaration = workflow.get("proposal_authorization")
    if declaration is None:
        return
    path = f"recipe.workflows[{index}].proposal_authorization"
    if not isinstance(declaration, dict):
        raise PayloadError(f"{path} must be an object")

    allowed_fields = {
        "kind",
        "action_key",
        "when",
        "destination",
        "upload_step_id",
        "publish_step_id",
        "ttl_seconds",
    }
    unknown_fields = sorted(set(declaration) - allowed_fields)
    if unknown_fields:
        raise PayloadError(f"{path} contains unsupported fields: {unknown_fields}")
    if declaration.get("kind") != "youtube_publication_v1":
        raise PayloadError(f"{path}.kind must be 'youtube_publication_v1'")
    if declaration.get("action_key") != WORKFLOW_RUN_EXTERNAL_ACTION_KEY:
        raise PayloadError(
            f"{path}.action_key must be {WORKFLOW_RUN_EXTERNAL_ACTION_KEY!r}"
        )
    if declaration.get("destination") != "studio.youtube.com":
        raise PayloadError(f"{path}.destination must be 'studio.youtube.com'")

    condition = declaration.get("when")
    if not isinstance(condition, dict):
        raise PayloadError(f"{path}.when must be an object")
    if set(condition) != {"input_key", "equals"}:
        raise PayloadError(f"{path}.when supports only input_key and equals")
    input_key = str(condition.get("input_key") or "").strip()
    input_keys = {
        str(item.get("key") or "").strip()
        for item in workflow.get("run_inputs") or []
        if isinstance(item, dict)
    }
    if not input_key or input_key not in input_keys:
        raise PayloadError(f"{path}.when.input_key must reference a declared run input")
    if condition.get("equals") != "public":
        raise PayloadError(f"{path}.when.equals must be 'public'")

    steps_by_id = {
        str(step.get("id") or "").strip(): step
        for step in workflow.get("steps") or []
        if isinstance(step, dict) and str(step.get("id") or "").strip()
    }
    step_ids: list[str] = []
    for field in ("upload_step_id", "publish_step_id"):
        step_id = str(declaration.get(field) or "").strip()
        step = steps_by_id.get(step_id)
        if step is None:
            raise PayloadError(f"{path}.{field} must reference a Workflow step")
        if (step.get("type") or step.get("kind")) != "agent":
            raise PayloadError(f"{path}.{field} must reference an agent step")
        step_ids.append(step_id)
    if len(set(step_ids)) != len(step_ids):
        raise PayloadError(f"{path} upload and publish steps must be distinct")

    ttl_seconds = declaration.get("ttl_seconds", 86400)
    if (
        isinstance(ttl_seconds, bool)
        or not isinstance(ttl_seconds, int)
        or not 1 <= ttl_seconds <= 86400
    ):
        raise PayloadError(f"{path}.ttl_seconds must be an integer from 1 to 86400")


def _validate_goal_number(goal: dict[str, Any], *, index: int, field: str) -> None:
    value = goal.get(field)
    if value is None:
        if field == "target_value":
            raise PayloadError(f"recipe.goals[{index}] requires target_value")
        return
    try:
        validate_goal_number(value)
    except ValueError as exc:
        raise PayloadError(f"recipe.goals[{index}].{field} {exc}") from None


def _validate_goals(recipe: dict[str, Any]) -> None:
    goals = recipe.get("goals")
    if goals is None:
        if "goals" in recipe:
            raise PayloadError("payload.recipe.goals must be an array (got NoneType)")
        return

    for index, goal in enumerate(goals):
        if not isinstance(goal, dict):
            raise PayloadError(f"recipe.goals[{index}] must be an object")

        title = goal.get("title")
        if not isinstance(title, str) or not title.strip():
            raise PayloadError(f"recipe.goals[{index}] requires title")
        if len(title) > 255:
            raise PayloadError(f"recipe.goals[{index}].title must be at most 255 characters")

        description = goal.get("description")
        if description is not None and not isinstance(description, str):
            raise PayloadError(f"recipe.goals[{index}].description must be a string")

        for field in ("goal_key", "metric_key"):
            value = goal.get(field)
            if value is not None and (not isinstance(value, str) or len(value.strip()) > 100):
                raise PayloadError(f"recipe.goals[{index}].{field} must be a string of at most 100 characters")

        _validate_goal_number(goal, index=index, field="target_value")
        _validate_goal_number(goal, index=index, field="baseline_value")

        deadline = goal.get("deadline")
        if deadline is not None:
            if not isinstance(deadline, str):
                raise PayloadError(f"recipe.goals[{index}].deadline must be an ISO date")
            try:
                date.fromisoformat(deadline)
            except ValueError:
                raise PayloadError(f"recipe.goals[{index}].deadline must be an ISO date") from None

        measurement_source = goal.get("measurement_source")
        if measurement_source is not None and not isinstance(measurement_source, dict):
            raise PayloadError(f"recipe.goals[{index}].measurement_source must be an object")

        cadence = goal.get("measurement_cadence")
        if cadence is not None and (not isinstance(cadence, str) or not cadence.strip() or len(cadence) > 64):
            raise PayloadError(
                f"recipe.goals[{index}].measurement_cadence must be a non-empty string of at most 64 characters"
            )
        if cadence is not None:
            try:
                validate_measurement_cadence(cadence)
            except ValueError as exc:
                raise PayloadError(
                    f"recipe.goals[{index}].measurement_cadence {exc}"
                ) from None

        priority = goal.get("priority", 3)
        if isinstance(priority, bool) or not isinstance(priority, int) or not 1 <= priority <= 5:
            raise PayloadError(f"recipe.goals[{index}].priority must be an integer from 1 to 5")


def _validate_mcp_requirements(contract: dict[str, Any]) -> None:
    """Reject order-dependent aliases for the same runtime Integration."""
    requires = contract.get("requires") or {}
    if not isinstance(requires, dict):
        raise PayloadError("payload.contract.requires must be an object")
    seen: dict[str, int] = {}
    for index, spec in enumerate(requires.get("mcp_servers") or []):
        if not isinstance(spec, dict):
            continue
        path = f"contract.requires.mcp_servers[{index}]"
        for field in ("required", "install_blocking"):
            if field in spec and not isinstance(spec[field], bool):
                raise PayloadError(f"{path}.{field} must be a boolean")
        provider = canonical_provider_key(spec.get("slug"))
        if not provider:
            continue
        previous_index = seen.get(provider)
        if previous_index is not None:
            raise PayloadError(
                "contract.requires.mcp_servers"
                f"[{index}].slug duplicates canonical provider {provider!r} "
                f"from index {previous_index}"
            )
        seen[provider] = index


def _validate_v11(p: dict[str, Any]) -> None:
    """Run all v1.1 structural + safety rules. Assumes ``p`` is already
    in v1.1 shape (migrate first if loading older format)."""
    # 1) Top-level 5 sections must be objects.
    for section in ("manifest", "contract", "embedded", "recipe", "policy"):
        if not isinstance(p.get(section), dict):
            raise PayloadError(f"payload.{section} must be an object")

    manifest = p["manifest"]
    contract = p["contract"]
    embedded = p["embedded"]
    recipe = p["recipe"]
    policy = p["policy"]

    # Proposal/Strategist creates concrete Task rows at runtime. A template
    # payload would introduce a competing task-generation path and cannot
    # reproduce the business-context-dependent proposal, so fail closed.
    if recipe.get("task_templates"):
        raise PayloadError("recipe.task_templates is not portable; Proposal generates Task instances at runtime")

    # 2) blueprint_version must match (we don't roundtrip "1.0" — it
    #    should have been migrated already).
    version = manifest.get("blueprint_version")
    if version != BLUEPRINT_VERSION:
        raise PayloadError(f"manifest.blueprint_version must be {BLUEPRINT_VERSION!r}, got {version!r}")

    # 3) List-shaped sections must be arrays where present.
    for path in _LIST_PATHS:
        node: Any = p
        for key in path:
            if not isinstance(node, dict):
                node = None
                break
            node = node.get(key)
        if node is None:
            continue  # optional list — absent is fine
        if not isinstance(node, list):
            raise PayloadError(f"payload.{'.'.join(path)} must be an array (got {type(node).__name__})")

    _validate_record_sections(p)
    _validate_prompts(recipe)
    _validate_contract_variables(contract)
    _validate_mcp_requirements(contract)
    _validate_scheduled_jobs(recipe)
    _validate_blueprint_startup(recipe)
    _validate_goals(recipe)
    _validate_task_policy_sections(recipe)
    _validate_workflows(recipe)

    # 4) embedded.agents[].tool_bindings ⊆ contract.requires.tools
    declared_tools = set((contract.get("requires") or {}).get("tools") or [])
    for a in embedded.get("agents") or []:
        if not isinstance(a, dict):
            continue
        skill_bindings = a.get("skill_bindings") or []
        if not isinstance(skill_bindings, list) or any(
            not isinstance(ref, str) or not ref.strip() for ref in skill_bindings
        ):
            raise PayloadError(f"embedded agent {a.get('slug')!r} skill_bindings must be non-empty string references")
        exact_skill_refs = a.get("skill_binding_refs") or []
        if not isinstance(exact_skill_refs, list):
            raise PayloadError(f"embedded agent {a.get('slug')!r} skill_binding_refs must be an array")
        declared_skill_sources = {
            (
                str(item.get("marketplace_source") or "platform").strip(),
                str(item.get("marketplace_id") or "").strip(),
            )
            for item in (contract.get("requires") or {}).get("skills") or []
            if isinstance(item, dict) and str(item.get("marketplace_id") or "").strip()
        }
        for index, ref in enumerate(exact_skill_refs):
            if not isinstance(ref, dict):
                raise PayloadError(f"embedded agent {a.get('slug')!r} skill_binding_refs[{index}] must be an object")
            source = str(ref.get("marketplace_source") or "platform").strip()
            marketplace_id = str(ref.get("marketplace_id") or "").strip()
            slug = str(ref.get("slug") or "").strip()
            if not slug or source not in {"manor", "platform"} or not marketplace_id:
                raise PayloadError(
                    f"embedded agent {a.get('slug')!r} "
                    f"skill_binding_refs[{index}] requires a non-empty slug, "
                    "supported marketplace_source, and marketplace_id"
                )
            if (source, marketplace_id) not in declared_skill_sources:
                raise PayloadError(
                    f"embedded agent {a.get('slug')!r} binds Marketplace Skill "
                    f"{source}:{marketplace_id} without declaring it in "
                    "contract.requires.skills"
                )
        bound = set(a.get("tool_bindings") or [])
        missing = bound - declared_tools
        if missing:
            raise PayloadError(
                f"embedded agent {a.get('slug')!r} binds tools not declared "
                f"in contract.requires.tools: {sorted(missing)}"
            )

    # 5) embedded.skills[].tools ⊆ contract.requires.tools
    for s in embedded.get("skills") or []:
        if not isinstance(s, dict):
            continue
        used = set(s.get("tools") or [])
        missing = used - declared_tools
        if missing:
            raise PayloadError(
                f"embedded skill {s.get('slug')!r} uses tools not declared "
                f"in contract.requires.tools: {sorted(missing)}"
            )

    # 6) MCP config_override_allowlist must not name secret-shaped fields.
    #    These ARE the names the agent will read at runtime — if an
    #    allowlist says "api_token" is fine, anyone configuring the MCP
    #    on the install side could put a real token in there.
    for a in embedded.get("agents") or []:
        if not isinstance(a, dict):
            continue
        for b in a.get("mcp_bindings") or []:
            if not isinstance(b, dict):
                continue
            for field in b.get("config_override_allowlist") or []:
                if not isinstance(field, str):
                    continue
                # Skip the exemption check here — allowlist VALUES must
                # not look secret-shaped even when the FIELD they appear
                # under is exempted by name.
                if _is_secret_shape(field):
                    raise PayloadError(
                        f"embedded agent {a.get('slug')!r} MCP allowlist "
                        f"declares suspected-secret field name {field!r}; "
                        f"blueprints must never allowlist credential-shaped fields"
                    )

    # 7) starter_memory must not carry user_id (personal scope).
    for a in embedded.get("agents") or []:
        if not isinstance(a, dict):
            continue
        for m in a.get("starter_memory") or []:
            if isinstance(m, dict) and "user_id" in m and m["user_id"] is not None:
                raise PayloadError(
                    f"embedded agent {a.get('slug')!r} starter_memory must not "
                    f"include user_id (per-user memory cannot be templated)"
                )

    # 8) Knowledge starter documents have bounded, unambiguous portable
    # identities. A key may appear in several packs to model one Document with
    # several group memberships, but every occurrence must describe the same
    # content. Legacy payloads without keys remain installable when paths are
    # unique inside their pack.
    knowledge_document_count = 0
    knowledge_total_bytes = 0
    knowledge_document_by_key: dict[str, dict[str, Any]] = {}
    for kp in embedded.get("knowledge_packs") or []:
        if not isinstance(kp, dict):
            continue
        pack_slug = kp.get("slug")
        pack_paths: set[str] = set()
        pack_keys: set[str] = set()
        for d in kp.get("starter_documents") or []:
            if not isinstance(d, dict):
                continue
            knowledge_document_count += 1
            if knowledge_document_count > BLUEPRINT_KNOWLEDGE_MAX_DOCUMENTS:
                raise PayloadError(
                    "Blueprint Knowledge exceeds the maximum of "
                    f"{BLUEPRINT_KNOWLEDGE_MAX_DOCUMENTS} starter documents"
                )
            path = d.get("path") or ""
            if not isinstance(path, str) or not path.endswith(".md"):
                raise PayloadError(
                    f"knowledge_pack {pack_slug!r}: starter_documents must be .md files only (got {path!r})"
                )
            if path in pack_paths:
                raise PayloadError(
                    f"knowledge_pack {pack_slug!r}: duplicate starter document path {path!r}"
                )
            pack_paths.add(path)

            body = d.get("body_md")
            if not isinstance(body, str) or not body.strip():
                raise PayloadError(
                    f"knowledge_pack {pack_slug!r}: starter document {path!r} requires non-empty body_md"
                )
            body_bytes = len(body.encode("utf-8"))
            if body_bytes > BLUEPRINT_KNOWLEDGE_MAX_DOCUMENT_BYTES:
                raise PayloadError(
                    f"knowledge_pack {pack_slug!r}: starter document {path!r} exceeds "
                    f"{BLUEPRINT_KNOWLEDGE_MAX_DOCUMENT_BYTES} bytes"
                )
            knowledge_total_bytes += body_bytes
            if knowledge_total_bytes > BLUEPRINT_KNOWLEDGE_MAX_TOTAL_BYTES:
                raise PayloadError(
                    "Blueprint Knowledge starter content exceeds "
                    f"{BLUEPRINT_KNOWLEDGE_MAX_TOTAL_BYTES} total bytes"
                )

            template = d.get("template")
            if template is not None:
                if not isinstance(template, dict):
                    raise PayloadError(
                        f"knowledge_pack {pack_slug!r}: starter document "
                        f"template must be an object (got {type(template).__name__})"
                    )
                template_id = str(template.get("id") or "").strip()
                mode = str(template.get("mode") or "").strip()
                renderer = str(template.get("renderer") or "").strip()
                version = template.get("version")
                if not template_id or mode != "live_projection" or not renderer:
                    raise PayloadError(
                        f"knowledge_pack {pack_slug!r}: live Knowledge "
                        "template requires id, mode='live_projection', and renderer"
                    )
                if not isinstance(version, int) or isinstance(version, bool) or version < 1:
                    raise PayloadError(
                        f"knowledge_pack {pack_slug!r}: live Knowledge template version must be a positive integer"
                    )

            document_key = d.get("key")
            if document_key is None:
                continue
            if (
                not isinstance(document_key, str)
                or len(document_key) > BLUEPRINT_KNOWLEDGE_DOCUMENT_KEY_MAX_LENGTH
                or not _KNOWLEDGE_DOCUMENT_KEY_RE.fullmatch(document_key)
            ):
                raise PayloadError(
                    f"knowledge_pack {pack_slug!r}: invalid starter document key {document_key!r}"
                )
            if document_key in pack_keys:
                raise PayloadError(
                    f"knowledge_pack {pack_slug!r}: duplicate starter document key {document_key!r}"
                )
            pack_keys.add(document_key)
            portable_document = {
                "path": path,
                "body_md": body,
                "template": template,
            }
            previous = knowledge_document_by_key.setdefault(
                document_key,
                portable_document,
            )
            if previous != portable_document:
                raise PayloadError(
                    f"starter document key {document_key!r} describes conflicting content"
                )

    # 9) strategist.business_model.model_type enum.
    strategist = recipe.get("strategist")
    if isinstance(strategist, dict):
        bm = strategist.get("business_model")
        if isinstance(bm, dict):
            mt = bm.get("model_type")
            if mt is not None and mt not in _MODEL_TYPES:
                raise PayloadError(
                    f"strategist.business_model.model_type {mt!r} is not in the supported enum: {sorted(_MODEL_TYPES)}"
                )

        # 10) evaluation_rubric weights sum to ~1.0
        rubric = strategist.get("evaluation_rubric")
        if isinstance(rubric, dict):
            weights = rubric.get("weights")
            if isinstance(weights, dict) and weights:
                try:
                    total = sum(float(v) for v in weights.values())
                except (TypeError, ValueError) as exc:
                    raise PayloadError(f"strategist.evaluation_rubric.weights values must be numeric ({exc})") from exc
                if not (0.99 <= total <= 1.01):
                    raise PayloadError(f"strategist.evaluation_rubric.weights must sum to 1.0 (got {total:.3f})")

    # 11) governance never_allow ∩ auto_approve = ∅
    gov = policy.get("governance") or {}
    if isinstance(gov, dict):
        never = set(gov.get("never_allow_actions") or [])
        auto = set(gov.get("auto_approve_actions") or [])
        overlap = never & auto
        if overlap:
            raise PayloadError(
                f"policy.governance.never_allow_actions and auto_approve_actions overlap: {sorted(overlap)}"
            )

    # 12) Optional Blueprint-owned simulation experience. Older payloads may
    #     omit it; exporter and installer materialise a deterministic fallback.
    experience = recipe.get("simulation_experience")
    if experience is not None:
        _validate_simulation_experience(experience)

    # 13) Belt-and-suspenders forbidden-key scan on the migrated tree.
    #     The pre-migration scan in validate_payload catches v1.0 leaks;
    #     this one catches a hand-authored v1.1 payload with bad keys.
    leaked = _scan_forbidden_keys(p)
    if leaked:
        raise PayloadError(f"payload contains forbidden field names (would leak credentials): {sorted(leaked)}")
    local_references = _scan_local_reference_values(p)
    if local_references:
        raise PayloadError(
            "payload contains local runtime references: "
            f"{sorted(local_references)}"
        )


def _validate_simulation_experience(experience: Any) -> None:
    if not isinstance(experience, dict):
        raise PayloadError("recipe.simulation_experience must be an object")
    if experience.get("schema_version") != SIMULATION_EXPERIENCE_VERSION:
        raise PayloadError(f"recipe.simulation_experience.schema_version must be {SIMULATION_EXPERIENCE_VERSION!r}")
    for field in ("title", "sample_prompt", "completion_summary"):
        value = experience.get(field)
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise PayloadError(f"recipe.simulation_experience.{field} must be a non-empty string")

    artifacts = experience.get("artifacts")
    if not isinstance(artifacts, list) or not (1 <= len(artifacts) <= 8):
        raise PayloadError("recipe.simulation_experience.artifacts must contain 1 to 8 artifacts")
    seen_ids: set[str] = set()
    for index, artifact in enumerate(artifacts):
        path = f"recipe.simulation_experience.artifacts[{index}]"
        if not isinstance(artifact, dict):
            raise PayloadError(f"{path} must be an object")
        for field in ("id", "kind", "title", "filename", "mime_type"):
            value = artifact.get(field)
            if not isinstance(value, str) or not value.strip():
                raise PayloadError(f"{path}.{field} must be a non-empty string")
        artifact_id = artifact["id"].strip()
        if artifact_id in seen_ids:
            raise PayloadError(f"{path}.id must be unique (got {artifact_id!r})")
        seen_ids.add(artifact_id)
        if artifact["kind"] not in SIMULATION_ARTIFACT_KINDS:
            raise PayloadError(f"{path}.kind must be one of {sorted(SIMULATION_ARTIFACT_KINDS)}")

        filename = artifact["filename"].strip()
        if PurePosixPath(filename).name != filename or filename in {".", ".."} or "\\" in filename:
            raise PayloadError(f"{path}.filename must be a safe filename, not a path")
        mime_type = artifact["mime_type"].strip().lower()
        if artifact["kind"] == "video" and not mime_type.startswith("video/"):
            raise PayloadError(f"{path}.mime_type must be video/* for video artifacts")
        if artifact["kind"] == "image" and not mime_type.startswith("image/"):
            raise PayloadError(f"{path}.mime_type must be image/* for image artifacts")
        preview_url = artifact.get("preview_url")
        if preview_url is not None and (
            not isinstance(preview_url, str) or not preview_url.startswith("/assets/") or ".." in preview_url
        ):
            raise PayloadError(f"{path}.preview_url must be a local /assets/ path")
        duration = artifact.get("duration_seconds")
        if duration is not None and (
            isinstance(duration, bool) or not isinstance(duration, (int, float)) or not (0 < duration <= 3600)
        ):
            raise PayloadError(f"{path}.duration_seconds must be between 0 and 3600")

    stages = experience.get("stages")
    if stages is None:
        # Additive backwards compatibility: authored v1.0 experiences created
        # before the persisted runtime shipped are enriched by
        # resolve_simulation_experience() during export/install/start.
        return
    if not isinstance(stages, list) or not (1 <= len(stages) <= 30):
        raise PayloadError("recipe.simulation_experience.stages must contain 1 to 30 stages")
    seen_stage_ids: set[str] = set()
    for index, stage in enumerate(stages):
        path = f"recipe.simulation_experience.stages[{index}]"
        if not isinstance(stage, dict):
            raise PayloadError(f"{path} must be an object")
        for field in ("id", "kind", "title", "author", "body"):
            value = stage.get(field)
            if not isinstance(value, str) or not value.strip():
                raise PayloadError(f"{path}.{field} must be a non-empty string")
        stage_id = stage["id"].strip()
        if stage_id in seen_stage_ids:
            raise PayloadError(f"{path}.id must be unique (got {stage_id!r})")
        seen_stage_ids.add(stage_id)
        if stage["kind"] not in SIMULATION_STAGE_KINDS:
            raise PayloadError(f"{path}.kind must be one of {sorted(SIMULATION_STAGE_KINDS)}")
        if stage["author"] not in SIMULATION_STAGE_AUTHORS:
            raise PayloadError(f"{path}.author must be one of {sorted(SIMULATION_STAGE_AUTHORS)}")
        action = stage.get("pending_action")
        if action is not None:
            if not isinstance(action, dict):
                raise PayloadError(f"{path}.pending_action must be an object")
            action_kind = action.get("kind")
            if action_kind not in SIMULATION_ACTION_KINDS:
                raise PayloadError(f"{path}.pending_action.kind must be one of {sorted(SIMULATION_ACTION_KINDS)}")
        artifact_ids = stage.get("artifact_ids")
        if artifact_ids is not None:
            if (
                not isinstance(artifact_ids, list)
                or not artifact_ids
                or any(not isinstance(item, str) or item not in seen_ids for item in artifact_ids)
            ):
                raise PayloadError(f"{path}.artifact_ids must reference declared artifacts")


# ── Forbidden key scanner ─────────────────────────────────────────────


def _is_secret_shape(name: str) -> bool:
    """True if ``name`` (a field name) looks like a credential carrier.

    Pure shape check — no exemption logic. Use ``_looks_like_secret_key``
    for the exemption-aware version applied to payload tree keys.
    """
    f = name.lower()
    if f in _FORBIDDEN_EXACT:
        return True
    return any(sub in f for sub in _FORBIDDEN_SUBSTRINGS)


def _looks_like_secret_key(name: object) -> bool:
    """Exemption-aware variant for scanning dict keys in the payload tree."""
    if not isinstance(name, str):
        return False
    f = name.lower()
    if f in _SUBSTRING_EXEMPTIONS:
        return False
    return _is_secret_shape(f)


def _scan_forbidden_keys(node: Any) -> set[str]:
    """Recursively walk dict/list and collect KEYS that look like
    secret-bearing field names. Values are not inspected."""
    found: set[str] = set()
    if isinstance(node, dict):
        for k, v in node.items():
            if _looks_like_secret_key(k):
                found.add(str(k))
            found |= _scan_forbidden_keys(v)
    elif isinstance(node, list):
        for item in node:
            found |= _scan_forbidden_keys(item)
    return found


def _scan_local_reference_values(node: Any) -> set[str]:
    """Collect local ULIDs stored under runtime-reference field names."""
    found: set[str] = set()
    if isinstance(node, dict):
        for key, value in node.items():
            if (
                key in LOCAL_RUNTIME_REFERENCE_KEYS
                and isinstance(value, str)
                and _LOCAL_ULID_RE.fullmatch(value.strip())
            ):
                found.add(key)
            found |= _scan_local_reference_values(value)
    elif isinstance(node, list):
        for item in node:
            found |= _scan_local_reference_values(item)
    return found


# ── Reference shape (for documentation only) ──────────────────────────
#
# {
#   "manifest": {
#     "blueprint_version": "1.1",
#     "slug": "twitter-growth-calvin-v1",
#     "title": "X Growth — Calvin's playbook",
#     "summary": "Daily posts + reply triage + engagement",
#     "use_when": "you want consistent X presence with HITL on risky actions",
#     "description": "Long-form markdown — story, design choices, gotchas.",
#     "tags": ["social", "growth"],
#     "kind": "social_media",
#     "category": "marketing.social",
#     "author": {"handle": "calvin", "display_name": "Calvin"},
#     "cover_image_url": "https://...",
#     "forked_from_id": null,
#     "changelog": "1.1: tightened reply tone."
#   },
#
#   "contract": {
#     "variables": [
#       {"key": "brand_name", "required": true, "label": "Your brand"},
#       {"key": "voice_hint", "default": "founder-led, direct"}
#     ],
#     "channels": [
#       {"channel_type": "telegram", "purpose": "alerts", "required": true}
#     ],
#     "sessions": [
#       {"provider": "x", "label": "main",
#        "expected_login_url": "https://x.com/login",
#        "health_check": {"url": "https://x.com/home", "expected_text": "Home"},
#        "required": true, "purpose": "post + read DMs"}
#     ],
#     "requires": {
#       "manor_min_version": "1.0",
#       "tools": ["tool.x.post", "tool.x.reply"],
#       "mcp_servers": [
#         {"slug": "linear-mcp", "purpose": "task sync",
#          "config_fields_to_set": ["api_token", "team_id"]}
#       ],
#       "skills": [{"slug": "manor/triage-incoming", "min_version": "1.0.0"}],
#       "agents": [{"slug": "x-poster-v2", "min_version": "2.0"}]
#     }
#   },
#
#   "embedded": {
#     "skills": [
#       {"slug": "handle-competitor-mention", "version": "1.0.0",
#        "system_prompt": "...{{voice_hint}}...",
#        "tools": ["tool.x.reply"],
#        "input_schema": {"trigger": "string"},
#        "output_format": "text",
#        "is_public": false}
#     ],
#     "agents": [
#       {"slug": "calvin-reply-tone",
#        "version": "1.0",
#        "name": "Calvin Reply Tone",
#        "system_prompt": "...",
#        "config": {"model": "claude-opus-4.7", "temperature": 0.5},
#        "category": "social_replies",
#        "tags": ["replies"],
#        "tool_bindings": ["tool.x.reply"],
#        "mcp_bindings": [
#          {"server_slug": "linear-mcp",
#           "allowed_tools": ["linear.create_issue"],
#           "config_override_allowlist": ["team_id"]}
#        ],
#        "skill_bindings": ["handle-competitor-mention"],
#        "skill_binding_refs": [
#          {"slug": "manor/triage-incoming",
#           "marketplace_source": "platform",
#           "marketplace_id": "01..."}
#        ],
#        "starter_memory": [
#          {"memory_type": "instruction", "scope": "guidance",
#           "content": "Never reply to outrage tweets within first hour.",
#           "importance": 8, "confidence": 0.9}
#        ]}
#     ],
#     "knowledge_packs": [
#       {"slug": "competitor-intel", "title": "Competitor Intelligence",
#        "purpose": "background on top 5 competitors",
#        "mode": "skeleton",
#        "folder_structure": [{"path": "competitors/", "description": "..."}],
#        "starter_documents": [
#          {"path": "competitors/README.md",
#           "body_md": "Add one folder per competitor."}
#        ],
#        "external_source": null}
#     ]
#   },
#
#   "recipe": {
#     "operating_model": {
#       "kind": "social_media",
#       "context": "Running social presence for {{brand_name}}.",
#       "primary_work": "Draft 1–3 X posts/day, triage replies.",
#       "settings": {"timezone": "America/Los_Angeles"},
#       "services": [{"key": "social.x.poster"}],
#       "rules": [{"id": "no_negativity",
#                  "rule": "Never reply combatively.",
#                  "note": "Learned the hard way in 2026-03"}],
#       "evaluation": {"metric": "weekly_engagement_lift", "target": "+10%"}
#     },
#     "strategist": {
#       "business_model": {
#         "model_type": "social_growth",
#         "primary_signal": "follower_count",
#         "secondary_signals": ["engagement_rate"],
#         "anti_signals": ["follower_via_promo"],
#         "decision_window": "weekly"
#       },
#       "cadence": {
#         "schedule": "daily",
#         "trigger_conditions": {
#           "skip_if_any": ["budget_remaining_pct < 10"]
#         }
#       },
#       "proposal_shape": {
#         "max_tasks_per_cycle": 3,
#         "preferred_owner_mix": {"agent_driven": 0.7, "human_driven": 0.3},
#         "preferred_categories": ["content", "engagement"],
#         "task_horizon_hours": [4, 48]
#       },
#       "priors": {
#         "expected_approval_rate": 0.75,
#         "expected_credits_per_cycle": 80
#       },
#       "evaluation_rubric": {
#         "weights": {"goal_impact": 0.4, "cost_efficiency": 0.2,
#                     "voice_quality": 0.2, "governance_compliance": 0.2},
#         "passing_score": 0.6
#       },
#       "do_not_propose": [
#         "Mass-DM tasks (>10 recipients)"
#       ],
#       "voice": {
#         "style": "concise, founder-direct, no marketing-speak",
#         "examples": ["Draft 3 X posts about onboarding pain points."]
#       },
#       "system_prompt_override": null
#     },
#     "prompts": [
#       {"key": "post_drafter", "body": "Draft a {{brand_name}} post about...",
#        "used_by": ["social.x.poster"]}
#     ],
#     "subscriptions": [
#       {"service_key": "social.x.poster", "agent_slug": "x-poster-v2",
#        "uses_prompt": "post_drafter", "custom_prompt": null,
#        "config": {"max_posts_per_day": 3}}
#     ],
#     "scheduled_jobs": [
#       {"job_id": "morning-draft", "name": "Morning post draft",
#        "schedule_kind": "cron", "cron_expr": "0 8 * * *",
#        "timezone": "{{tz}}",
#        "execution_type": "agent_message",
#        "execution_target": {"service_key": "social.x.poster"},
#        "payload_message": "Draft today's posts.",
#        "note": "8am because audience peaks then"}
#     ],
#     "workflows": [
#       {"slug": "morning-post-with-review",
#        "trigger_type": "scheduled",
#        "trigger_ref": "morning-draft",
#        "variables": [{"key": "post_topic", "default": "product_update"}],
#        "steps": [
#          {"id": "draft", "kind": "agent_call",
#           "service_key": "social.x.poster",
#           "input": "Draft post on ${{vars.post_topic}}"},
#          {"id": "review", "kind": "hitl_approval",
#           "depends_on": ["draft"], "channel": "telegram",
#           "timeout_minutes": 60}
#        ]}
#     ],
#     "goals": [
#       {"title": "Reach 10k X followers",
#        "metric_key": "follower_count", "target_value": 10000,
#        "deadline": "2026-12-31",
#        "measurement_source": {"action": "x.get_profile_stats"},
#        "measurement_cadence": "daily",
#        "note": "Followers not engagement: engagement is gameable early"}
#     ],
#     "task_categories": [
#       {"name": "content", "color": "#4A90E2"},
#       {"name": "experiment", "color": "#F5A623"}
#     ],
#     "custom_fields": [
#       {"name": "campaign_tag", "target": "task", "field_type": "select",
#        "options": ["launch", "evergreen"]}
#     ],
#     "sla_policies": [
#       {"category": "engagement", "response_time_minutes": 30,
#        "resolution_time_hours": 4}
#     ],
#     "escalation_rules": [
#       {"trigger": "sla_breach", "target_role": "owner",
#        "action_type": "notify", "channel_type": "telegram"}
#     ]
#   },
#
#   "policy": {
#     "governance": {
#       "never_allow_actions": ["billing.*"],
#       "hitl_required_actions": ["x.delete_*", "x.dm_send"],
#       "auto_approve_actions": ["x.like", "x.repost"],
#       "max_risk_level": "medium",
#       "budget_caps_per_kind": {"action": 200},
#       "rationale": {"x.delete_*": "Once deleted a viral post in 2026-03"}
#     },
#     "post_install_checks": [
#       {"kind": "session_alive", "session_label": "main"},
#       {"kind": "agent_callable", "service_key": "social.x.poster"},
#       {"kind": "cron_scheduled", "job_id": "morning-draft"}
#     ],
#     "expected_baseline": {
#       "simulation_days": 7,
#       "daily_credits_p50": 120,
#       "daily_credits_p90": 200,
#       "actions_per_day": {"x.post": 2, "x.like": 15}
#     }
#   }
# }
