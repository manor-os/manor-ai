"""Regression: capability_binding.upsert must keep payload-level scope/capability
and recover integrations the model mislabels as capabilities.

Prod card 01KY0XQG… ("所有 agent 可以用 chrome extension"): the model emitted four
well-formed patches with ``owner_scope`` / ``capability_id`` / ``capability_type``
at the payload top level (siblings of ``binding``, which held only
``description``), binding the Chrome extension as
``capability_type='capability', capability_id='browser.chrome_extension'``.

Two stacked bugs:
  A. ``_apply_single_patch`` read only ``payload['binding']`` → dropped scope +
     capability, collapsing all four patches into one ``unscoped`` keyless
     binding → "binding requires capability key/name" + bad owner_scope.
  B. Even merged, ``browser.chrome_extension`` is not a real capability; the
     correct shape is the MCP integration ``capability_type='mcp',
     integration_key='chrome'``.

Fixes: merge payload-level fields into the binding, and coerce a capability bound
to a nonexistent id that resolves to a known MCP server into the mcp shape —
without masking genuinely-unknown capabilities.
"""

from __future__ import annotations

from packages.core.ai.runtime.capability_bindings import (
    normalize_runtime_capability_binding,
    validate_runtime_capability_binding,
)
from packages.core.services.workspace_operation_service import _apply_single_patch


def _chrome_patch(owner_scope: str, service_key: str | None = None) -> dict:
    payload = {
        "binding": {"description": "Use the Chrome extension integration."},
        "owner_scope": owner_scope,
        "capability_id": "browser.chrome_extension",
        "capability_type": "capability",
    }
    if service_key:
        payload["service_key"] = service_key
    return {"op": "capability_binding.upsert", "payload": payload}


def test_prod_chrome_card_yields_four_valid_mcp_bindings():
    patches = [
        _chrome_patch("workspace_agent"),
        _chrome_patch("service", "project_management"),
        _chrome_patch("service", "customer_service"),
        _chrome_patch("service", "marketing"),
    ]
    state: dict = {}
    for patch in patches:
        state = _apply_single_patch(state, patch)

    bindings = state.get("capability_bindings") or []
    # Bug A: all four collapsed to one before the payload-merge fix.
    assert len(bindings) == 4

    errors = []
    keys = set()
    for i, b in enumerate(bindings):
        assert b["capability_type"] == "mcp"          # Bug B: coerced from capability
        assert b["integration_key"] == "chrome"
        keys.add(b["binding_key"])
        errors += validate_runtime_capability_binding(
            b,
            path=f"capability_bindings[{i}]",
            service_keys={"project_management", "customer_service", "marketing"},
        )
    assert errors == []
    assert len(keys) == 4  # distinct scopes, not one "unscoped" key


def test_payload_top_level_fields_are_not_dropped():
    state = _apply_single_patch({}, _chrome_patch("workspace_agent"))
    b = (state.get("capability_bindings") or [])[0]
    assert b["owner_scope"] == "workspace_agent"
    assert "unscoped" not in b["binding_key"]


def test_capability_correctly_mislabeled_integration_is_coerced():
    b = normalize_runtime_capability_binding({
        "owner_scope": "workspace_agent",
        "capability_type": "capability",
        "capability_id": "browser.chrome_extension",
        "description": "x",
    })
    assert b["capability_type"] == "mcp"
    assert b["integration_key"] == "chrome"
    assert "capability_id" not in b
    assert validate_runtime_capability_binding(b, path="cb[0]") == []


def test_various_integration_mislabels_resolve():
    cases = {
        "chrome_extension": "chrome",
        "twitter": "twitter_x",
        "gmail": "gmail",
        "shopify.store": "shopify",
    }
    for cap_id, expected_key in cases.items():
        b = normalize_runtime_capability_binding({
            "owner_scope": "workspace_agent",
            "capability_type": "capability",
            "capability_id": cap_id,
        })
        assert b["capability_type"] == "mcp", cap_id
        assert b["integration_key"] == expected_key, cap_id


def test_genuinely_unknown_capability_is_not_masked():
    # A capability id that does NOT resolve to any integration must still fail
    # validation, not get silently coerced into a bogus binding.
    b = normalize_runtime_capability_binding({
        "owner_scope": "workspace_agent",
        "capability_type": "capability",
        "capability_id": "totally_made_up_thing",
        "description": "x",
    })
    assert b["capability_type"] == "capability"
    assert "integration_key" not in b
    errors = validate_runtime_capability_binding(b, path="cb[0]")
    assert any("unknown runtime capability" in e["message"] for e in errors)


def test_real_capability_is_left_untouched():
    # An id that IS a real runtime capability must not be touched even if a token
    # coincidentally looks integration-ish.
    b = normalize_runtime_capability_binding({
        "owner_scope": "workspace_agent",
        "capability_type": "capability",
        "capability_id": "workspace.task",
    })
    assert b["capability_type"] == "capability"
    assert b.get("capability_id") == "workspace.task"
    assert validate_runtime_capability_binding(b, path="cb[0]") == []


# ── Variant 3: patch-top-level fields + skill_key=chrome (prod card 01KY1GERJK) ──
# The model placed scope/skill_key/service_key/capability_type at the PATCH top
# level (siblings of payload, which held only {purpose}), and mislabeled the
# Chrome integration as capability_type='skill', skill_key='chrome'.
# _normalise_patch keeps only `payload`, so those fields were dropped entirely.


def _skill_chrome_patch(scope: str, service_key: str | None = None) -> dict:
    patch = {
        "op": "capability_binding.upsert",
        "scope": scope,
        "payload": {"purpose": "Use the Chrome extension for browser-side ops."},
        "skill_key": "chrome",
        "capability_type": "skill",
    }
    if service_key:
        patch["service_key"] = service_key
    return patch


def test_patch_top_level_skill_chrome_card_yields_valid_mcp_bindings():
    patches = [
        _skill_chrome_patch("workspace_agent"),
        _skill_chrome_patch("service", "geo_content_strategy"),
        _skill_chrome_patch("service", "social_media"),
    ]
    state: dict = {}
    for patch in patches:
        state = _apply_single_patch(state, patch)

    bindings = state.get("capability_bindings") or []
    assert len(bindings) == 3  # were dropped/collapsed before the fix
    errors, keys = [], set()
    for i, b in enumerate(bindings):
        assert b["capability_type"] == "mcp"
        assert b["integration_key"] == "chrome"
        assert "skill_key" not in b
        keys.add(b["binding_key"])
        errors += validate_runtime_capability_binding(
            b, path=f"capability_bindings[{i}]",
            service_keys={"geo_content_strategy", "social_media"},
        )
    assert errors == []
    assert len(keys) == 3


def test_scope_alias_and_purpose_alias_are_applied():
    state = _apply_single_patch({}, _skill_chrome_patch("workspace_agent"))
    b = (state.get("capability_bindings") or [])[0]
    assert b["owner_scope"] == "workspace_agent"   # `scope` -> `owner_scope`


def test_genuine_skill_binding_is_not_coerced_to_mcp():
    # A skill whose key is a real skill (not an integration server) stays a skill.
    b = normalize_runtime_capability_binding({
        "owner_scope": "workspace_agent",
        "capability_type": "skill",
        "skill_key": "weekly_report_writer",
    })
    assert b["capability_type"] == "skill"
    assert b.get("skill_key") == "weekly_report_writer"
    assert "integration_key" not in b
