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
             escalation_rules)
  policy     governance + post-install checks + expected baseline

Backward compat: v1.0 payloads (flat top-level title/workspace/
subscriptions/...) auto-migrate to v1.1 on load. The installer always
sees v1.1 shape.

What ``validate_payload`` enforces:

  * top-level shape (each of the 5 sections must be an object)
  * blueprint_version matches a version this module knows
  * no secret-shaped key names anywhere in the payload tree
    (credential_ref, *_token, *_secret, password, ...)
  * embedded agents/skills only bind tools declared in contract.requires
  * MCP allowlists don't declare secret-shaped field names
  * embedded agent starter_memory has no user_id (personal scope must
    not be templated)
  * knowledge_pack starter_documents are .md only
  * strategist.business_model.model_type is a known enum
  * strategist.evaluation_rubric.weights sum to 1.0
  * governance never_allow and auto_approve don't overlap

Versioning: bump ``BLUEPRINT_VERSION`` on a breaking change and add a
per-version migrator. Minor / additive changes don't bump the version.
"""
from __future__ import annotations

from typing import Any

BLUEPRINT_VERSION = "1.1"

# Versions this module knows how to read. Older versions are migrated
# up to BLUEPRINT_VERSION on load.
SUPPORTED_VERSIONS = frozenset({"1.0", "1.1"})


class PayloadError(ValueError):
    """Raised on malformed payloads."""


# ── Forbidden field-name patterns (secret-leak prevention) ────────────
#
# We scan KEY NAMES anywhere in the payload tree. Values are not
# inspected — a string value happening to be "api_key" is fine.

# Exact key names that always indicate a leak.
_FORBIDDEN_EXACT = frozenset({
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
})

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
_SUBSTRING_EXEMPTIONS = frozenset({
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
})


# ── Strategist business model enum ────────────────────────────────────

_MODEL_TYPES = frozenset({
    "social_growth",
    "saas",
    "marketplace",
    "content_publishing",
    "services_delivery",
    "community",
})


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
        "payload has no blueprint_version "
        "(checked manifest.blueprint_version and top-level blueprint_version)"
    )


def migrate_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Convert ``payload`` to the current shape (v1.1).

    Returns a NEW dict; does not mutate the input. v1.1 payloads are
    returned unchanged. v1.0 payloads are lifted into the 5-section
    shape; v1.1-new sections become empty/null since v1.0 doesn't
    carry them.
    """
    version = detect_version(payload)
    if version == BLUEPRINT_VERSION:
        return payload
    if version == "1.0":
        return _migrate_v10_to_v11(payload)
    raise PayloadError(
        f"unsupported blueprint_version {version!r}; "
        f"supported: {sorted(SUPPORTED_VERSIONS)}"
    )


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
        raise PayloadError(
            f"payload contains forbidden field names "
            f"(would leak credentials): {sorted(leaked)}"
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
    empty/null. The installer treats them as optional.
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
            starter_docs.append({
                "path": m["path"],
                "body_md": m.get("body", ""),
                "frontmatter": m.get("frontmatter"),
            })
        if starter_docs:
            knowledge_packs.append({
                "slug": "imported-memory",
                "title": "Imported memory files",
                "purpose": "Carried over from v1.0 memory_files",
                "mode": "inline_text",
                "folder_structure": [],
                "starter_documents": starter_docs,
                "external_source": None,
            })

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
    ("embedded", "skills"),
    ("embedded", "agents"),
    ("embedded", "knowledge_packs"),
    ("recipe", "prompts"),
    ("recipe", "subscriptions"),
    ("recipe", "scheduled_jobs"),
    ("recipe", "workflows"),
    ("recipe", "goals"),
    ("recipe", "task_categories"),
    ("recipe", "custom_fields"),
    ("recipe", "sla_policies"),
    ("recipe", "escalation_rules"),
    ("policy", "post_install_checks"),
)


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

    # 2) blueprint_version must match (we don't roundtrip "1.0" — it
    #    should have been migrated already).
    version = manifest.get("blueprint_version")
    if version != BLUEPRINT_VERSION:
        raise PayloadError(
            f"manifest.blueprint_version must be {BLUEPRINT_VERSION!r}, "
            f"got {version!r}"
        )

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
            raise PayloadError(
                f"payload.{'.'.join(path)} must be an array "
                f"(got {type(node).__name__})"
            )

    # 4) embedded.agents[].tool_bindings ⊆ contract.requires.tools
    declared_tools = set(
        (contract.get("requires") or {}).get("tools") or []
    )
    for a in embedded.get("agents") or []:
        if not isinstance(a, dict):
            continue
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

    # 8) knowledge_pack starter_documents are .md only.
    for kp in embedded.get("knowledge_packs") or []:
        if not isinstance(kp, dict):
            continue
        for d in kp.get("starter_documents") or []:
            if not isinstance(d, dict):
                continue
            path = d.get("path") or ""
            if not isinstance(path, str) or not path.endswith(".md"):
                raise PayloadError(
                    f"knowledge_pack {kp.get('slug')!r}: starter_documents "
                    f"must be .md files only (got {path!r})"
                )

    # 9) strategist.business_model.model_type enum.
    strategist = recipe.get("strategist")
    if isinstance(strategist, dict):
        bm = strategist.get("business_model")
        if isinstance(bm, dict):
            mt = bm.get("model_type")
            if mt is not None and mt not in _MODEL_TYPES:
                raise PayloadError(
                    f"strategist.business_model.model_type {mt!r} is not in "
                    f"the supported enum: {sorted(_MODEL_TYPES)}"
                )

        # 10) evaluation_rubric weights sum to ~1.0
        rubric = strategist.get("evaluation_rubric")
        if isinstance(rubric, dict):
            weights = rubric.get("weights")
            if isinstance(weights, dict) and weights:
                try:
                    total = sum(float(v) for v in weights.values())
                except (TypeError, ValueError) as exc:
                    raise PayloadError(
                        "strategist.evaluation_rubric.weights values must be "
                        f"numeric ({exc})"
                    ) from exc
                if not (0.99 <= total <= 1.01):
                    raise PayloadError(
                        "strategist.evaluation_rubric.weights must sum to 1.0 "
                        f"(got {total:.3f})"
                    )

    # 11) governance never_allow ∩ auto_approve = ∅
    gov = policy.get("governance") or {}
    if isinstance(gov, dict):
        never = set(gov.get("never_allow_actions") or [])
        auto = set(gov.get("auto_approve_actions") or [])
        overlap = never & auto
        if overlap:
            raise PayloadError(
                f"policy.governance.never_allow_actions and "
                f"auto_approve_actions overlap: {sorted(overlap)}"
            )

    # 12) Belt-and-suspenders forbidden-key scan on the migrated tree.
    #     The pre-migration scan in validate_payload catches v1.0 leaks;
    #     this one catches a hand-authored v1.1 payload with bad keys.
    leaked = _scan_forbidden_keys(p)
    if leaked:
        raise PayloadError(
            f"payload contains forbidden field names "
            f"(would leak credentials): {sorted(leaked)}"
        )


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
