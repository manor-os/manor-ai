"""Unit tests for the v1.1 blueprint payload schema.

Pure-Python — no DB, no fixtures from conftest. These tests live next to
the schema because the schema is the contract between exporter and
installer; if it breaks, both sides break.

Covered:
  * detect_version handles v1.0 (top-level) and v1.1 (manifest-nested)
  * migrate_payload lifts v1.0 → v1.1 correctly
  * round-trip: migrate(v1.1) == v1.1
  * each structural validation rule fires on bad input
  * forbidden-key scanner: pre-migration scan + post-migration scan +
    exemption list
  * MCP allowlist + starter_memory + knowledge_pack rules
  * strategist enum + weights-sum-to-1 rule
  * governance never_allow ∩ auto_approve rule
"""

from __future__ import annotations

import copy

import pytest

from packages.core.blueprints.payload import (
    BLUEPRINT_VERSION,
    SUPPORTED_VERSIONS,
    PayloadError,
    detect_version,
    migrate_payload,
    validate_payload,
)
from packages.core.constants.blueprints import (
    BLUEPRINT_KNOWLEDGE_MAX_DOCUMENT_BYTES,
    BLUEPRINT_KNOWLEDGE_MAX_DOCUMENTS,
    BLUEPRINT_KNOWLEDGE_MAX_TOTAL_BYTES,
)


# ── Sample payloads ───────────────────────────────────────────────────


def _v10_payload() -> dict:
    """Minimal valid v1.0 payload."""
    return {
        "blueprint_version": "1.0",
        "title": "X Growth",
        "summary": "Daily posts",
        "description": "Long description.",
        "tags": ["social"],
        "author": {"handle": "calvin", "display_name": "Calvin"},
        "workspace": {
            "kind": "social_media",
            "operating_context": "@calvin handle",
            "primary_work": "Post + engage daily",
            "operating_model": {"services": [{"key": "social.x.poster"}]},
            "settings": {"timezone": "America/Los_Angeles"},
        },
        "subscriptions": [
            {"service_key": "social.x.poster", "agent_slug": "x-poster-v2", "custom_prompt": None, "config": {}}
        ],
        "goals": [
            {
                "title": "10k followers",
                "metric_key": "follower_count",
                "target_value": 10000,
                "deadline": "2026-12-31",
                "measurement_source": {"action": "x.get_profile_stats"},
                "measurement_cadence": "daily",
                "priority": 2,
            }
        ],
        "scheduled_jobs": [
            {
                "job_id": "morning-draft",
                "name": "Morning post",
                "schedule_kind": "cron",
                "cron_expr": "0 8 * * *",
                "timezone": "America/Los_Angeles",
                "execution_type": "agent_message",
                "execution_target": {"service_key": "social.x.poster"},
                "payload_message": "Draft today.",
            }
        ],
        "custom_fields": [
            {"name": "campaign_tag", "field_type": "select", "target": "task", "options": ["launch", "evergreen"]}
        ],
        "governance_policy": {
            "never_allow_actions": ["billing.*"],
            "hitl_required_actions": ["x.delete_*"],
            "max_risk_level": "medium",
        },
        "channel_requirements": [{"channel_type": "telegram", "purpose": "alerts", "required": True}],
        "session_requirements": [{"provider": "x", "label": "main", "required": True}],
        "memory_files": [{"path": "voice.md", "frontmatter": {"tags": ["brand"]}, "body": "Voice: founder-led."}],
    }


def _v11_payload() -> dict:
    """Minimal valid v1.1 payload."""
    return {
        "manifest": {
            "blueprint_version": BLUEPRINT_VERSION,
            "slug": "x-growth",
            "title": "X Growth",
            "summary": "Daily posts",
            "use_when": "consistent X presence",
            "description": "Long description.",
            "tags": ["social"],
            "kind": "social_media",
            "category": "marketing.social",
            "author": {"handle": "calvin"},
            "cover_image_url": None,
            "forked_from_id": None,
            "changelog": None,
        },
        "contract": {
            "variables": [{"key": "brand_name", "required": True}],
            "channels": [{"channel_type": "telegram", "required": True}],
            "sessions": [{"provider": "x", "label": "main"}],
            "requires": {
                "manor_min_version": "1.0",
                "tools": ["tool.x.post", "tool.x.reply"],
                "mcp_servers": [],
                "skills": [],
                "agents": [{"slug": "x-poster-v2"}],
            },
        },
        "embedded": {
            "skills": [],
            "agents": [],
            "knowledge_packs": [],
        },
        "recipe": {
            "operating_model": {
                "kind": "social_media",
                "context": "Running for {{brand_name}}.",
                "primary_work": "Draft 1-3 posts/day.",
                "services": [{"key": "social.x.poster"}],
            },
            "strategist": None,
            "prompts": [],
            "subscriptions": [{"service_key": "social.x.poster", "agent_slug": "x-poster-v2", "config": {}}],
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
                "never_allow_actions": ["billing.*"],
                "hitl_required_actions": ["x.delete_*"],
                "auto_approve_actions": ["x.like"],
                "max_risk_level": "medium",
            },
            "post_install_checks": [],
            "expected_baseline": None,
        },
    }


# ── Version detection ─────────────────────────────────────────────────


def test_detect_version_v10_top_level():
    assert detect_version(_v10_payload()) == "1.0"


def test_detect_version_v11_nested():
    assert detect_version(_v11_payload()) == BLUEPRINT_VERSION


def test_detect_version_missing_raises():
    with pytest.raises(PayloadError, match="blueprint_version"):
        detect_version({"foo": "bar"})


def test_detect_version_non_dict_raises():
    with pytest.raises(PayloadError, match="JSON object"):
        detect_version("not a dict")  # type: ignore[arg-type]


def test_contract_variables_require_unique_non_secret_keys():
    payload = _v11_payload()
    payload["contract"]["variables"] = [
        {"key": "brand_name"},
        {"key": "brand_name"},
    ]
    with pytest.raises(PayloadError, match="duplicates"):
        validate_payload(payload)

    payload["contract"]["variables"] = [{"key": "api_token"}]
    with pytest.raises(PayloadError, match="credential-shaped"):
        validate_payload(payload)


def test_mcp_requirements_reject_duplicate_canonical_provider_aliases():
    payload = _v11_payload()
    payload["contract"]["requires"]["mcp_servers"] = [
        {"slug": "x", "required": False},
        {"slug": "twitter_x", "required": True},
    ]

    with pytest.raises(PayloadError, match="duplicates canonical provider 'twitter_x'"):
        validate_payload(payload)


@pytest.mark.parametrize("field", ["required", "install_blocking"])
def test_mcp_requirement_boolean_flags_are_strict(field: str):
    payload = _v11_payload()
    payload["contract"]["requires"]["mcp_servers"] = [{
        "slug": "chrome",
        field: "false",
    }]

    with pytest.raises(PayloadError, match=rf"{field} must be a boolean"):
        validate_payload(payload)


def test_runtime_derived_scheduled_jobs_are_not_portable():
    payload = _v11_payload()
    payload["recipe"]["scheduled_jobs"] = [{
        "job_id": "gm:source-goal-id",
        "execution_type": "goal_measurement",
    }]
    with pytest.raises(PayloadError, match="runtime-derived"):
        validate_payload(payload)


def test_scheduled_skill_requires_portable_target():
    payload = _v11_payload()
    payload["recipe"]["scheduled_jobs"] = [{
        "job_id": "pending-skill",
        "execution_type": "skill",
        "execution_target": {},
    }]

    with pytest.raises(PayloadError, match="portable Skill target"):
        validate_payload(payload)


def test_scheduled_workflow_requires_declared_portable_target():
    payload = _v11_payload()
    payload["recipe"]["scheduled_jobs"] = [{
        "job_id": "pending-workflow",
        "execution_type": "workflow",
        "execution_target": {"workflow_slug": "missing-flow"},
    }]

    with pytest.raises(PayloadError, match="portable Workflow target"):
        validate_payload(payload)


def _startup_agent_job(job_id: str, *, job_type: str = "manual") -> dict:
    return {
        "job_id": job_id,
        "job_type": job_type,
        "schedule_kind": None,
        "execution_type": "agent",
        "execution_target": {"service_key": "social.x.poster"},
        "payload_message": "Prepare the Workspace.",
    }


def test_blueprint_startup_job_references_must_resolve():
    payload = _v11_payload()
    payload["recipe"]["operating_model"]["settings"] = {
        "blocking_setup": {
            "checks": [{"key": "identity", "setup_job_id": "missing-setup"}],
            "on_ready_job_id": "missing-ready",
        }
    }

    with pytest.raises(PayloadError, match="setup_job_id.*missing-setup"):
        validate_payload(payload)


def test_blueprint_startup_setup_job_must_be_manual_and_enabled():
    payload = _v11_payload()
    payload["recipe"]["scheduled_jobs"] = [
        {
            **_startup_agent_job("prepare-identity", job_type="cron"),
            "schedule_kind": "cron",
            "cron_expr": "0 7 * * *",
        },
    ]
    payload["recipe"]["operating_model"]["settings"] = {
        "blocking_setup": {
            "checks": [
                {"key": "identity", "setup_job_id": "prepare-identity"},
            ],
        }
    }

    with pytest.raises(PayloadError, match="setup_job_id.*manual"):
        validate_payload(payload)

    payload["recipe"]["scheduled_jobs"][0] = {
        **_startup_agent_job("prepare-identity"),
        "enabled": False,
    }
    with pytest.raises(PayloadError, match="setup_job_id.*disabled"):
        validate_payload(payload)


def test_blueprint_startup_accepts_opt_in_manual_setup_and_ready_job():
    payload = _v11_payload()
    payload["recipe"]["scheduled_jobs"] = [
        _startup_agent_job("prepare-identity"),
        {
            "job_id": "first-review",
            "job_type": "cron",
            "schedule_kind": "cron",
            "cron_expr": "0 7 * * *",
            "execution_type": "strategist_review",
            "execution_target": {},
            "payload_message": "Review the Workspace.",
        },
    ]
    payload["recipe"]["operating_model"]["settings"] = {
        "blocking_setup": {
            "checks": [
                {"key": "identity", "setup_job_id": "prepare-identity"},
                {"key": "browser", "kind": "integration_provider"},
            ],
            "on_ready_job_id": "first-review",
        }
    }

    validate_payload(payload)


def _youtube_publication_workflow() -> dict:
    return {
        "slug": "publish-video",
        "run_inputs": [{
            "key": "youtube_visibility",
            "type": "string",
            "schema": {"type": "string", "enum": ["public", "private"]},
        }],
        "proposal_authorization": {
            "kind": "youtube_publication_v1",
            "action_key": "workspace.proposal.workflow_run.external",
            "when": {"input_key": "youtube_visibility", "equals": "public"},
            "destination": "studio.youtube.com",
            "upload_step_id": "upload_video",
            "publish_step_id": "publish_video",
            "ttl_seconds": 86400,
        },
        "steps": [
            {"id": "upload_video", "type": "agent", "next": ["publish_video"]},
            {"id": "publish_video", "type": "agent", "next": ["done"]},
            {"id": "done", "type": "end", "next": []},
        ],
    }


def test_workflow_proposal_authorization_accepts_allowlisted_publication_contract():
    payload = _v11_payload()
    payload["recipe"]["workflows"] = [_youtube_publication_workflow()]

    validate_payload(payload)


@pytest.mark.parametrize(
    ("patch", "match"),
    [
        ({"kind": "arbitrary_action_v1"}, "kind"),
        ({"action_key": "chrome.anything"}, "action_key"),
        ({"destination": "example.com"}, "destination"),
        ({"publish_step_id": "missing"}, "publish_step_id"),
        ({"when": {"input_key": "missing", "equals": "public"}}, "input_key"),
        ({"when": {"input_key": "youtube_visibility", "equals": "private"}}, "equals"),
    ],
)
def test_workflow_proposal_authorization_rejects_untrusted_scope(patch, match):
    payload = _v11_payload()
    workflow = _youtube_publication_workflow()
    workflow["proposal_authorization"].update(patch)
    payload["recipe"]["workflows"] = [workflow]

    with pytest.raises(PayloadError, match=match):
        validate_payload(payload)


@pytest.mark.parametrize("execution_type", ["agent", "agent_message"])
def test_scheduled_agent_requires_portable_service_target(execution_type):
    payload = _v11_payload()
    payload["recipe"]["scheduled_jobs"] = [{
        "job_id": "targetless-agent",
        "execution_type": execution_type,
        "execution_target": {},
    }]

    with pytest.raises(PayloadError, match="portable Agent service_key target"):
        validate_payload(payload)


# ── Migration: v1.0 → v1.1 ────────────────────────────────────────────


def test_migrate_v10_to_v11_basic_shape():
    p10 = _v10_payload()
    p11 = migrate_payload(p10)
    # 5 sections present
    for section in ("manifest", "contract", "embedded", "recipe", "policy"):
        assert isinstance(p11[section], dict), f"missing section: {section}"
    # version bumped
    assert p11["manifest"]["blueprint_version"] == BLUEPRINT_VERSION


def test_migrate_v10_to_v11_preserves_manifest_fields():
    p11 = migrate_payload(_v10_payload())
    m = p11["manifest"]
    assert m["title"] == "X Growth"
    assert m["summary"] == "Daily posts"
    assert m["description"] == "Long description."
    assert m["tags"] == ["social"]
    assert m["kind"] == "social_media"
    assert m["author"] == {"handle": "calvin", "display_name": "Calvin"}


def test_migrate_v10_operating_model_absorbs_shell_fields():
    p11 = migrate_payload(_v10_payload())
    om = p11["recipe"]["operating_model"]
    assert om["context"] == "@calvin handle"
    assert om["primary_work"] == "Post + engage daily"
    assert om["kind"] == "social_media"
    assert om["settings"] == {"timezone": "America/Los_Angeles"}
    # original operating_model contents preserved
    assert om["services"] == [{"key": "social.x.poster"}]


def test_migrate_v10_lists_lift_to_recipe():
    p11 = migrate_payload(_v10_payload())
    r = p11["recipe"]
    assert len(r["subscriptions"]) == 1
    assert r["subscriptions"][0]["agent_slug"] == "x-poster-v2"
    assert len(r["goals"]) == 1
    assert len(r["scheduled_jobs"]) == 1
    assert len(r["custom_fields"]) == 1


def test_migrate_v10_governance_to_policy():
    p11 = migrate_payload(_v10_payload())
    assert p11["policy"]["governance"]["max_risk_level"] == "medium"
    assert p11["policy"]["governance"]["never_allow_actions"] == ["billing.*"]


def test_migrate_v10_requirements_to_contract():
    p11 = migrate_payload(_v10_payload())
    assert len(p11["contract"]["channels"]) == 1
    assert p11["contract"]["channels"][0]["channel_type"] == "telegram"
    assert len(p11["contract"]["sessions"]) == 1
    assert p11["contract"]["sessions"][0]["provider"] == "x"


def test_migrate_v10_memory_files_to_knowledge_pack():
    p11 = migrate_payload(_v10_payload())
    packs = p11["embedded"]["knowledge_packs"]
    assert len(packs) == 1
    pack = packs[0]
    assert pack["slug"] == "imported-memory"
    assert pack["mode"] == "inline_text"
    assert len(pack["starter_documents"]) == 1
    assert pack["starter_documents"][0]["path"] == "voice.md"


def test_migrate_v10_new_sections_are_empty():
    p11 = migrate_payload(_v10_payload())
    assert p11["contract"]["variables"] == []
    assert p11["contract"]["requires"]["tools"] == []
    assert p11["embedded"]["skills"] == []
    assert p11["embedded"]["agents"] == []
    assert p11["recipe"]["strategist"] is None
    assert p11["recipe"]["workflows"] == []
    assert p11["recipe"]["task_categories"] == []
    assert p11["recipe"]["sla_policies"] == []
    assert p11["policy"]["post_install_checks"] == []
    assert p11["policy"]["expected_baseline"] is None


def test_migrate_does_not_mutate_input():
    p10 = _v10_payload()
    snapshot = copy.deepcopy(p10)
    migrate_payload(p10)
    assert p10 == snapshot


def test_migrate_v11_is_idempotent():
    p11 = _v11_payload()
    out = migrate_payload(p11)
    assert out == p11


def test_migrate_v11_assigns_unique_goal_keys_without_splitting_shared_metric():
    p11 = _v11_payload()
    p11["recipe"]["goals"] = [
        {"title": "Trial signups", "metric_key": "signup_count"},
        {"title": "Paid signups", "metric_key": "signup_count"},
        {
            "title": "Qualified signups",
            "goal_key": "goal_trial_signups",
            "metric_key": "signup_count",
        },
    ]
    snapshot = copy.deepcopy(p11)

    migrated = migrate_payload(p11)

    assert p11 == snapshot
    assert [goal["goal_key"] for goal in migrated["recipe"]["goals"]] == [
        "goal_trial_signups_2",
        "goal_paid_signups",
        "goal_trial_signups",
    ]
    assert {goal["metric_key"] for goal in migrated["recipe"]["goals"]} == {"signup_count"}
    assert migrate_payload(migrated) == migrated


def test_migrate_v11_rejects_duplicate_explicit_goal_keys():
    p11 = _v11_payload()
    p11["recipe"]["goals"] = [
        {
            "title": "Trial signups",
            "goal_key": "signup_target",
            "metric_key": "signup_count",
            "target_value": 100,
        },
        {
            "title": "Paid signups",
            "goal_key": "signup_target",
            "metric_key": "signup_count",
            "target_value": 25,
        },
    ]

    with pytest.raises(PayloadError, match="goal_key duplicates"):
        migrate_payload(p11)


def test_migrate_v11_normalizes_legacy_operating_model_goals():
    p11 = _v11_payload()
    del p11["recipe"]["goals"]
    p11["recipe"]["operating_model"]["goals"] = [
        {
            "title": "Trial signups",
            "goal_key": "signup_count",
            "metric_key": "signup_count",
            "target_value": 10,
        },
        {
            "title": "Paid signups",
            "goal_key": "signup_count",
            "metric_key": "signup_count",
            "target_value": 5,
        },
    ]

    migrated = migrate_payload(p11)

    expected_keys = ["signup_count", "signup_count_2"]
    assert [goal["goal_key"] for goal in migrated["recipe"]["goals"]] == expected_keys
    assert "goals" not in migrated["recipe"]["operating_model"]


def test_migrate_v11_explicit_empty_recipe_goals_override_legacy_goals():
    p11 = _v11_payload()
    p11["recipe"]["goals"] = []
    p11["recipe"]["operating_model"]["goals"] = [
        {
            "title": "Legacy signups",
            "goal_key": "legacy_signups",
            "metric_key": "signup_count",
            "target_value": 10,
        }
    ]

    migrated = migrate_payload(p11)

    assert migrated["recipe"]["goals"] == []
    assert "goals" not in migrated["recipe"]["operating_model"]


def test_migrate_v11_uses_recipe_goals_as_the_canonical_identity_source():
    p11 = _v11_payload()
    p11["recipe"]["goals"] = [
        {
            "title": "Paid signups",
            "goal_key": "paid_signups",
            "metric_key": "signup_count",
        }
    ]
    p11["recipe"]["operating_model"]["goals"] = [
        {
            "title": "Phantom signups",
            "goal_key": "phantom_signups",
            "metric_key": "signup_count",
        }
    ]

    migrated = migrate_payload(p11)

    assert migrated["recipe"]["goals"][0]["goal_key"] == "paid_signups"
    assert "goals" not in migrated["recipe"]["operating_model"]


def test_explicit_null_recipe_goals_fails_before_legacy_goals_are_dropped():
    p11 = _v11_payload()
    p11["recipe"]["goals"] = None
    p11["recipe"]["operating_model"]["goals"] = [
        {
            "title": "Legacy signups",
            "goal_key": "legacy_signups",
            "metric_key": "signup_count",
            "target_value": 10,
        }
    ]

    with pytest.raises(PayloadError, match=r"payload\.recipe\.goals must be an array"):
        validate_payload(p11)


def test_migrate_unknown_version_raises():
    bad = {"blueprint_version": "9.9", "workspace": {}}
    with pytest.raises(PayloadError, match="unsupported"):
        migrate_payload(bad)


# ── Round-trip: validate(v1.0) and validate(v1.1) both succeed ────────


def test_validate_accepts_v10():
    validate_payload(_v10_payload())  # should not raise


def test_validate_accepts_v11():
    validate_payload(_v11_payload())  # should not raise


def test_v11_task_policy_sections_are_not_portable():
    p = _v11_payload()
    p["recipe"]["task_categories"] = [{"key": "production", "label": "Production"}]
    with pytest.raises(PayloadError, match="task_categories.*not portable"):
        validate_payload(p)


def test_v11_task_policy_rejects_source_entity_user_ids():
    p = _v11_payload()
    p["recipe"]["sla_policies"] = [{"key": "review", "threshold_hours": 24}]
    p["recipe"]["escalation_rules"] = [
        {
            "key": "late",
            "sla_policy_key": "review",
            "action": "notify",
            "notify_user_ids": ["source-user"],
        }
    ]
    with pytest.raises(PayloadError, match="sla_policies.*not portable"):
        validate_payload(p)


def test_v11_task_policy_rejects_unknown_sla_reference():
    p = _v11_payload()
    p["recipe"]["escalation_rules"] = [
        {
            "key": "late",
            "sla_policy_key": "missing",
            "action": "notify",
        }
    ]
    with pytest.raises(PayloadError, match="escalation_rules.*not portable"):
        validate_payload(p)


def test_v11_prompts_reject_non_object_items_instead_of_dropping_them():
    p = _v11_payload()
    p["recipe"]["prompts"] = [{"key": "daily", "body": "Run daily."}, "not-an-object"]
    with pytest.raises(PayloadError, match=r"prompts\[1\].*object"):
        validate_payload(p)


# ── Rule 1: top-level sections must be objects ────────────────────────


def test_v11_missing_manifest_raises():
    p = _v11_payload()
    del p["manifest"]
    with pytest.raises(PayloadError, match="manifest"):
        validate_payload(p)


def test_v11_recipe_must_be_object():
    p = _v11_payload()
    p["recipe"] = []
    with pytest.raises(PayloadError, match="recipe"):
        validate_payload(p)


# ── Rule 2: blueprint_version match ───────────────────────────────────


def test_v11_wrong_blueprint_version_raises():
    p = _v11_payload()
    p["manifest"]["blueprint_version"] = "2.0"
    with pytest.raises(PayloadError, match="unsupported|blueprint_version"):
        validate_payload(p)


# ── Rule 3: list-shaped sections must be lists ────────────────────────


def test_v11_subscriptions_must_be_list():
    p = _v11_payload()
    p["recipe"]["subscriptions"] = {"not": "a list"}
    with pytest.raises(PayloadError, match="recipe.subscriptions"):
        validate_payload(p)


# ── Rule 4: embedded agent tool_bindings ⊆ declared tools ─────────────


def test_v11_embedded_agent_undeclared_tool_raises():
    p = _v11_payload()
    p["embedded"]["agents"] = [
        {
            "slug": "calvin-reply",
            "tool_bindings": ["tool.x.unknown"],  # not in requires.tools
        }
    ]
    with pytest.raises(PayloadError, match="undeclared|requires.tools"):
        validate_payload(p)


def test_v11_embedded_agent_declared_tool_passes():
    p = _v11_payload()
    p["embedded"]["agents"] = [
        {
            "slug": "calvin-reply",
            "tool_bindings": ["tool.x.reply"],  # IS in requires.tools
        }
    ]
    validate_payload(p)  # should not raise


def test_v11_exact_skill_binding_requires_nonempty_slug():
    p = _v11_payload()
    marketplace_id = "01EXACTMARKETPLACESKILL00"
    p["contract"]["requires"]["skills"] = [
        {
            "slug": "manor/triage",
            "marketplace_source": "platform",
            "marketplace_id": marketplace_id,
        }
    ]
    p["embedded"]["agents"] = [
        {
            "slug": "calvin-reply",
            "tool_bindings": [],
            "skill_bindings": ["manor/triage"],
            "skill_binding_refs": [
                {
                    "marketplace_source": "platform",
                    "marketplace_id": marketplace_id,
                }
            ],
        }
    ]

    with pytest.raises(PayloadError, match=r"skill_binding_refs\[0\].*slug"):
        validate_payload(p)


# ── Rule 5: embedded skill tools ⊆ declared tools ─────────────────────


def test_v11_embedded_skill_undeclared_tool_raises():
    p = _v11_payload()
    p["embedded"]["skills"] = [
        {
            "slug": "handle-mention",
            "tools": ["tool.unknown"],
        }
    ]
    with pytest.raises(PayloadError, match="undeclared|requires.tools"):
        validate_payload(p)


# ── Rule 6: MCP allowlist must not name secret-shaped fields ──────────


def test_v11_mcp_allowlist_secret_field_raises():
    p = _v11_payload()
    p["embedded"]["agents"] = [
        {
            "slug": "calvin-reply",
            "tool_bindings": [],
            "mcp_bindings": [
                {
                    "server_slug": "linear-mcp",
                    "config_override_allowlist": ["api_token"],
                }
            ],
        }
    ]
    with pytest.raises(PayloadError, match="api_token|credential"):
        validate_payload(p)


def test_v11_mcp_allowlist_safe_field_passes():
    p = _v11_payload()
    p["embedded"]["agents"] = [
        {
            "slug": "calvin-reply",
            "tool_bindings": [],
            "mcp_bindings": [
                {
                    "server_slug": "linear-mcp",
                    "config_override_allowlist": ["team_id", "project_id"],
                }
            ],
        }
    ]
    validate_payload(p)  # should not raise


# ── Rule 7: starter_memory must not have user_id ──────────────────────


def test_v11_starter_memory_with_user_id_raises():
    p = _v11_payload()
    p["embedded"]["agents"] = [
        {
            "slug": "calvin-reply",
            "tool_bindings": [],
            "starter_memory": [{"content": "hi", "user_id": "usr_abc"}],
        }
    ]
    with pytest.raises(PayloadError, match="user_id"):
        validate_payload(p)


def test_v11_starter_memory_user_id_none_passes():
    p = _v11_payload()
    p["embedded"]["agents"] = [
        {
            "slug": "calvin-reply",
            "tool_bindings": [],
            "starter_memory": [{"content": "hi", "user_id": None}],
        }
    ]
    validate_payload(p)  # explicit None is treated as absence


# ── Rule 8: knowledge_pack starter_documents are .md only ─────────────


def test_v11_knowledge_pack_non_md_raises():
    p = _v11_payload()
    p["embedded"]["knowledge_packs"] = [
        {
            "slug": "intel",
            "mode": "inline_text",
            "starter_documents": [{"path": "secrets.json", "body_md": "..."}],
        }
    ]
    with pytest.raises(PayloadError, match="\\.md"):
        validate_payload(p)


def test_v11_knowledge_pack_duplicate_path_raises():
    p = _v11_payload()
    p["embedded"]["knowledge_packs"] = [{
        "slug": "intel",
        "mode": "inline_text",
        "starter_documents": [
            {"key": "first", "path": "shared.md", "body_md": "first"},
            {"key": "second", "path": "shared.md", "body_md": "second"},
        ],
    }]

    with pytest.raises(PayloadError, match="duplicate starter document path"):
        validate_payload(p)


def test_v11_shared_knowledge_key_requires_identical_content():
    p = _v11_payload()
    p["embedded"]["knowledge_packs"] = [
        {
            "slug": "one",
            "mode": "inline_text",
            "starter_documents": [
                {"key": "shared", "path": "shared.md", "body_md": "same"},
            ],
        },
        {
            "slug": "two",
            "mode": "inline_text",
            "starter_documents": [
                {"key": "shared", "path": "shared.md", "body_md": "changed"},
            ],
        },
    ]

    with pytest.raises(PayloadError, match="describes conflicting content"):
        validate_payload(p)

    p["embedded"]["knowledge_packs"][1]["starter_documents"][0]["body_md"] = "same"
    validate_payload(p)


def test_v11_knowledge_starter_content_is_bounded():
    p = _v11_payload()
    p["embedded"]["knowledge_packs"] = [{
        "slug": "large",
        "mode": "inline_text",
        "starter_documents": [{
            "path": "large.md",
            "body_md": "x" * (BLUEPRINT_KNOWLEDGE_MAX_DOCUMENT_BYTES + 1),
        }],
    }]
    with pytest.raises(PayloadError, match="exceeds .* bytes"):
        validate_payload(p)

    p["embedded"]["knowledge_packs"][0]["starter_documents"] = [
        {"path": f"{index}.md", "body_md": "x"}
        for index in range(BLUEPRINT_KNOWLEDGE_MAX_DOCUMENTS + 1)
    ]
    with pytest.raises(PayloadError, match="maximum of .* starter documents"):
        validate_payload(p)

    chunk_size = BLUEPRINT_KNOWLEDGE_MAX_DOCUMENT_BYTES
    p["embedded"]["knowledge_packs"][0]["starter_documents"] = [
        {"path": f"{index}.md", "body_md": "x" * chunk_size}
        for index in range((BLUEPRINT_KNOWLEDGE_MAX_TOTAL_BYTES // chunk_size) + 1)
    ]
    with pytest.raises(PayloadError, match="total bytes"):
        validate_payload(p)


# ── Rule 9: strategist business_model.model_type enum ─────────────────


def test_v11_strategist_unknown_model_type_raises():
    p = _v11_payload()
    p["recipe"]["strategist"] = {
        "business_model": {"model_type": "blockchain_growth"},
    }
    with pytest.raises(PayloadError, match="model_type"):
        validate_payload(p)


def test_v11_strategist_known_model_type_passes():
    p = _v11_payload()
    p["recipe"]["strategist"] = {
        "business_model": {"model_type": "social_growth"},
    }
    validate_payload(p)


# ── Rule 10: strategist evaluation_rubric.weights sum to 1.0 ──────────


def test_v11_strategist_weights_must_sum_to_one():
    p = _v11_payload()
    p["recipe"]["strategist"] = {
        "evaluation_rubric": {
            "weights": {"a": 0.5, "b": 0.2},  # sums to 0.7
        },
    }
    with pytest.raises(PayloadError, match="sum to 1"):
        validate_payload(p)


def test_v11_strategist_weights_sum_one_passes():
    p = _v11_payload()
    p["recipe"]["strategist"] = {
        "evaluation_rubric": {
            "weights": {"a": 0.4, "b": 0.6},
        },
    }
    validate_payload(p)


def test_v11_strategist_weights_within_tolerance_passes():
    # Floating point: 0.4 + 0.6 may be 1.0000000001 — tolerance allows.
    p = _v11_payload()
    p["recipe"]["strategist"] = {
        "evaluation_rubric": {
            "weights": {"a": 0.33, "b": 0.33, "c": 0.34},  # = 1.00
        },
    }
    validate_payload(p)


# ── Rule 11: governance never_allow ∩ auto_approve = ∅ ────────────────


def test_v11_governance_overlap_raises():
    p = _v11_payload()
    p["policy"]["governance"]["auto_approve_actions"] = ["x.like", "billing.*"]
    # billing.* is in never_allow already
    with pytest.raises(PayloadError, match="overlap"):
        validate_payload(p)


# ── Rule 12: forbidden-key scanner ────────────────────────────────────


def test_forbidden_credential_ref_anywhere_caught():
    p = _v11_payload()
    p["recipe"]["operating_model"]["credential_ref"] = "vault:LEAK"
    with pytest.raises(PayloadError, match="credential_ref"):
        validate_payload(p)


def test_forbidden_api_token_caught_pre_migration():
    p = _v10_payload()
    p["workspace"]["api_token"] = "sk_live_LEAK"
    with pytest.raises(PayloadError, match="api_token|credential"):
        validate_payload(p)


def test_forbidden_token_substring_caught():
    p = _v11_payload()
    p["recipe"]["operating_model"]["github_token"] = "ghp_LEAK"
    with pytest.raises(PayloadError, match="github_token|credential"):
        validate_payload(p)


def test_forbidden_password_field_caught():
    p = _v11_payload()
    p["contract"]["channels"][0]["password"] = "hunter2"
    with pytest.raises(PayloadError, match="password|credential"):
        validate_payload(p)


def test_exempted_service_key_passes():
    # service_key contains "_key" substring but is exempted.
    p = _v11_payload()
    # already has service_key in subscriptions; validate passes
    validate_payload(p)


def test_exempted_metric_key_passes():
    p = _v11_payload()
    p["recipe"]["goals"] = [
        {
            "title": "T",
            "metric_key": "follower_count",
            "target_value": 100,
        }
    ]
    validate_payload(p)


def test_workspace_stats_and_goal_stat_key_pass():
    p = _v11_payload()
    p["recipe"]["stats"] = [
        {"library_key": "workspace.tasks.completed"},
    ]
    p["recipe"]["goals"] = [
        {
            "title": "T",
            "metric_key": "workspace.tasks.completed",
            "stat_key": "workspace.tasks.completed",
            "target_value": 100,
        }
    ]
    validate_payload(p)


@pytest.mark.parametrize(
    ("goal", "message"),
    [
        ("not-an-object", r"recipe\.goals\[0\] must be an object"),
        ({"target_value": 1}, r"recipe\.goals\[0\] requires title"),
        ({"title": "Missing target"}, r"recipe\.goals\[0\] requires target_value"),
        (
            {"title": "Bad target", "target_value": "NaN"},
            r"recipe\.goals\[0\]\.target_value must be a finite number",
        ),
        (
            {"title": "Huge target", "target_value": "1e20"},
            r"recipe\.goals\[0\]\.target_value exceeds the supported numeric range",
        ),
        (
            {"title": "Over-precise target", "target_value": "0.00001"},
            r"recipe\.goals\[0\]\.target_value must have at most 4 decimal places",
        ),
        (
            {
                "title": "Over-precise baseline",
                "target_value": 1,
                "baseline_value": "0.00009",
            },
            r"recipe\.goals\[0\]\.baseline_value must have at most 4 decimal places",
        ),
        (
            {
                "title": "Long identity",
                "goal_key": "x" * 101,
                "target_value": 1,
            },
            r"recipe\.goals\[0\]\.goal_key must be a string of at most 100 characters",
        ),
        (
            {"title": "Bad description", "target_value": 1, "description": 42},
            r"recipe\.goals\[0\]\.description must be a string",
        ),
        (
            {"title": "Bad deadline", "target_value": 1, "deadline": "tomorrow"},
            r"recipe\.goals\[0\]\.deadline must be an ISO date",
        ),
        (
            {"title": "Bad source", "target_value": 1, "measurement_source": "api"},
            r"recipe\.goals\[0\]\.measurement_source must be an object",
        ),
        (
            {
                "title": "Bad cadence",
                "target_value": 1,
                "measurement_source": {"provider": "workspace_internal"},
                "measurement_cadence": "fortnightly",
            },
            r"recipe\.goals\[0\]\.measurement_cadence unsupported",
        ),
        (
            {
                "title": "Impossible cadence",
                "target_value": 1,
                "measurement_source": {"provider": "workspace_internal"},
                "measurement_cadence": "0 0 31 2 *",
            },
            r"recipe\.goals\[0\]\.measurement_cadence unsupported",
        ),
    ],
)
def test_goal_records_fail_closed_before_install(goal, message):
    p = _v11_payload()
    p["recipe"]["goals"] = [goal]

    with pytest.raises(PayloadError, match=message):
        validate_payload(p)


def test_goal_numbers_allow_insignificant_trailing_zeroes_at_database_scale():
    p = _v11_payload()
    p["recipe"]["goals"] = [
        {
            "title": "Exact rate",
            "target_value": "9999999999999999.9999",
            "baseline_value": "1.23000",
        }
    ]

    validate_payload(p)


def test_exempted_config_fields_to_set_passes():
    # config_fields_to_set is the allowlist mechanism itself; the
    # field name LISTS other fields but is itself safe.
    p = _v11_payload()
    p["contract"]["requires"]["mcp_servers"] = [
        {
            "slug": "linear-mcp",
            "purpose": "task sync",
            "config_fields_to_set": ["team_id", "project_id"],
        }
    ]
    validate_payload(p)


# ── Belt-and-suspenders: pre-migration scan catches v1.0 leaks ────────


def test_pre_migration_scan_catches_v10_credential_ref():
    """A v1.0 payload with credential_ref in workspace must be rejected
    even though migration drops unknown workspace fields."""
    p = _v10_payload()
    p["workspace"]["credential_ref"] = "vault:LEAK"
    with pytest.raises(PayloadError, match="credential_ref"):
        validate_payload(p)


@pytest.mark.parametrize(
    ("section", "value", "message"),
    [
        ("custom_fields", [{}], r"custom_fields\[0\]\.name is required"),
        ("custom_fields", ["bad"], r"custom_fields\[0\] must be an object"),
        ("stats", [{}], r"stats\[0\] requires library_key or key and name"),
        ("workflows", [{}], r"workflows\[0\]\.slug is required"),
    ],
)
def test_installable_record_sections_fail_before_workspace_creation(
    section,
    value,
    message,
):
    p = _v11_payload()
    p["recipe"][section] = value

    with pytest.raises(PayloadError, match=message):
        validate_payload(p)


def test_scheduled_job_rejects_local_runtime_ids():
    p = _v11_payload()
    p["recipe"]["scheduled_jobs"] = [{
        "job_id": "private-agent-job",
        "execution_type": "agent_message",
        "agent_id": "01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "execution_target": {
            "workflow_id": "01ARZ3NDEKTSV4RRFFQ69G5FAW",
        },
    }]

    with pytest.raises(PayloadError, match="local runtime reference"):
        validate_payload(p)


def test_subworkflow_requires_declared_portable_component_key():
    p = _v11_payload()
    p["recipe"]["workflows"] = [{
        "slug": "parent",
        "steps": [{
            "id": "child",
            "type": "subworkflow",
            "config": {"source_workflow_key": "missing-child"},
        }],
    }]

    with pytest.raises(PayloadError, match="missing Blueprint Flow"):
        validate_payload(p)


# ── Supported versions list ──────────────────────────────────────────


def test_supported_versions_contains_current_and_predecessor():
    assert BLUEPRINT_VERSION in SUPPORTED_VERSIONS
    assert "1.0" in SUPPORTED_VERSIONS
