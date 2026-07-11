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
    # Same dict reference is fine — migrate_payload returns input
    # untouched when version already matches.
    assert out is p11 or out == p11


def test_migrate_unknown_version_raises():
    bad = {"blueprint_version": "9.9", "workspace": {}}
    with pytest.raises(PayloadError, match="unsupported"):
        migrate_payload(bad)


# ── Round-trip: validate(v1.0) and validate(v1.1) both succeed ────────


def test_validate_accepts_v10():
    validate_payload(_v10_payload())  # should not raise


def test_validate_accepts_v11():
    validate_payload(_v11_payload())  # should not raise


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


# ── Supported versions list ──────────────────────────────────────────


def test_supported_versions_contains_current_and_predecessor():
    assert BLUEPRINT_VERSION in SUPPORTED_VERSIONS
    assert "1.0" in SUPPORTED_VERSIONS
