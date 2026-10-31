"""MCP public payload shapes for Workspace configuration operations.

These supplement the MCP descriptors without changing the in-product tool
contracts or guide context. They describe data, not a second operation dispatcher. Runtime
validation and authorization remain in workspace_operation_service and its
downstream services. Open configuration objects retain extension fields used
by existing Blueprints; executable operation names are always explicit.
"""
from __future__ import annotations

from typing import Any

from packages.core.services.workspace_app_view import _ACTION_KEY, _FIELD_KEY, APP_ENTRY_ICONS


def _text(description: str) -> dict[str, Any]:
    return {"type": "string", "description": description}


def _object(description: str, properties: dict | None = None, *, required=(), extra=True) -> dict:
    result = {"type": "object", "description": description,
              "properties": properties or {}, "additionalProperties": extra}
    if required:
        result["required"] = list(required)
    return result


def _array(description: str, items: dict) -> dict:
    return {"type": "array", "description": description, "items": items}


def _wrapped(schema: dict, name: str) -> dict:
    # The service supports either direct fields or one named object wrapper.
    return {**schema, "properties": {**schema["properties"], name: schema},
            **({"anyOf": [{"required": schema["required"]}, {"required": [name]}],
                "required": []} if schema.get("required") else {})}


_STRINGS = _array("String values.", {"type": "string"})
_BOOL = {"type": "boolean"}
_NUMBER = {"type": "number"}
_CONFIG = _object("Configuration values; fields are interpreted by the selected component.")
_RULE = _object("One operating rule. Enforcement is derived from its type and selectors.", {
    "rule_key": _text("Stable rule identity."), "description": _text("Rule instructions."),
    "rule_type": _text("Rule type, for example guidance, draft_only or approval_required. "
                       "Omit it for a limit or condition (e.g. at most 6 per person): that is guidance."),
    "action_patterns": {**_STRINGS, "description": "Action keys Manor runs (social_post.publish, "
                        "external_message.send, workspace.file.create, <integration>.<action>); "
                        "a rule naming anything else is rejected."},
    "capability_patterns": _STRINGS,
    "approval_selectors": _array('Structured approval selectors, e.g. [{"kind": "category", '
                                 '"category": "publishing"}] or [{"kind": "action", '
                                 '"action_key": "social_post.publish"}].', _CONFIG),
    "enforcement_scope": _text("Scope of the rule's enforcement."),
})
_SOURCE = _object("Goal measurement source configuration, never account credentials.", {
    "provider": _text("Connected provider key, or manual."),
    "action": _text("Measurement action supported by that provider."), "params": _CONFIG,
})
_GOAL = _object("Goal definition or fields to merge into an existing goal.", {
    **{key: {"type": ["string", "null"], "description": description} for key, description in {
        "goal_key": "Stable goal identity.", "key": "Legacy goal identity.",
        "goal_id": "Existing goal id.", "id": "Legacy existing goal id.",
        "title": "Goal title.", "goal": "Legacy goal title.",
        "description": "Goal description.", "metric_key": "Metric to measure.",
        "measurement_cadence": "Named cadence or cron expression.",
        "status": "Goal status.", "deadline": "Goal deadline in YYYY-MM-DD form.",
    }.items()},
    "target_value": {"type": ["number", "string", "null"]},
    "baseline_value": {"type": ["number", "string", "null"]},
    "current_value": {"type": ["number", "string", "null"]},
    "priority": {"type": ["integer", "string", "null"]},
    "measurement_source": {**_SOURCE, "type": ["object", "null"]},
})
_SERVICE = _object("Service role metadata. Agent provisioning belongs in agent_binding.upsert.", {
    "key": _text("Stable service key."), "service_key": _text("Alias for key."),
    "name": _text("Service name."), "description": _text("What the service delivers."),
})
_BINDING = _object("Workspace-local capability or Skill binding; does not connect accounts.", {
    **{key: _text(description) for key, description in {
        "binding_key": "Stable binding identity.", "capability_type": "Binding kind: tool, mcp, action, capability or skill.",
        "owner_scope": "workspace_agent, service, or agent; task bindings cannot be configured here.",
        "owner_service_key": "Service to receive this binding.", "service_key": "Alias for owner_service_key.",
        "agent_id": "Existing Agent id.",
        "agent_subscription_id": "Workspace Agent subscription id.",
        "capability_id": "Capability identifier.", "tool_name": "Native tool name.",
        "integration_key": "Connected integration provider key.", "mcp_server_key": "MCP server key.",
        "skill_id": "Existing actor-visible Skill id.",
        "skill_key": "Skill key.", "name": "Binding display name.",
    }.items()},
    "enabled": _BOOL, "required": _BOOL,
    "allowed_tools": _array("Allowed MCP action names for this server binding.", {"type": "string"}),
})
_MAPPING = _object("Map an existing or custom Agent to a service; preserve unrelated deployments.", {
    "service_key": _text("Existing service key."), "agent_id": _text("Existing actor-visible Agent id."),
    "recommended_agent_id": _text("Alternative selected Agent id."),
    "agent_subscription_id": _text("Existing Workspace subscription id."),
    "strategy": _text("Agent selection strategy; create_custom provisions create_agent_draft."),
    # Field names mirror agent_provisioning_service.spec_from_create_agent_draft.
    "create_agent_draft": _object("Custom Agent provisioning definition; bindings reference existing actor-visible resources.", {
        "agent_name": _text("Reusable Agent name."), "agent_description": _text("Agent capability description."),
        "agent_slug": {"type": ["string", "null"]},
        "system_prompt": _text("Reusable Agent instructions; Workspace framing belongs in mapping.custom_prompt."),
        "system_prompt_seed": _text("Fallback seed when system_prompt is omitted."),
        "tool_bindings": _array("Native tool names.", {"type": "string"}),
        "business_capabilities": _array("Runtime capability identifiers.", {"type": "string"}),
        "skill_bindings": _array("Existing Skill ids or slugs.", {"type": "string"}),
        "skill_binding_refs": _array("Existing Skill references with provenance metadata.", _CONFIG),
        "mcp_bindings": _array("Existing MCP server ids or keys.", {"type": "string"}),
        "mcp_allowed_tools": {"type": "object", "description": "Action allowlists keyed by MCP server id/key; null keeps server defaults.",
                              "additionalProperties": {"type": ["array", "null"], "items": {"type": "string"}}},
        "missing_skill_specs": _array("Skill definitions resolved through the existing provisioning and access checks.", _CONFIG),
    }),
    "custom_prompt": {"type": ["string", "null"], "description": "Workspace-specific subscription instructions; null preserves existing/custom fallback behavior."},
    "config": _CONFIG,
}, required=("service_key",))
_PROCESS = _object("Descriptive process metadata; this does not create a runnable Workflow.", {
    "process_key": _text("Stable process identity."), "key": _text("Alias for process_key."),
    "name": _text("Process name."), "description": _text("Process purpose."),
    "steps": _array("Descriptive process steps.", {"type": ["string", "object"]}),
})
_KNOWLEDGE = _object("Knowledge retrieval policy; document content is managed separately.", {
    "auto_search": _BOOL, "citation_required": _BOOL, "strict_mode": _BOOL,
    "retrieval_mode": {"type": "string", "enum": ["auto", "manual", "strict"]},
    "default_group_ids": _STRINGS,
    "group_purposes": {"type": "object", "additionalProperties": {"type": "string"}},
})
_STRATEGIST = _object("Strategist planning policy and scenario configuration.", {
    "priors": {"type": ["array", "object", "null"], "items": {"type": ["string", "object"]}},
    "do_not_propose": _STRINGS,
    "business_model": {"type": ["string", "object", "null"]},
    "proposal_shape": {"type": ["string", "object", "null"]},
    "voice": {"type": ["string", "object", "null"]},
    "evaluation_rubric": _CONFIG, "use_goals": _BOOL,
})
# Mirrors the stored evaluation shape written by workspace_arch_tools._set_evaluation.
_EVALUATION = _object("Evaluation scorecard configuration.", {
    "enabled": _BOOL, "cadence": {"type": ["string", "null"]},
    "scorecard": _array("Weighted goal metrics.", _object("One scorecard metric.", {
        "metric_key": _text("Metric identity."), "goal_key": _text("Related goal identity."),
        "weight": _NUMBER,
    })),
    "target_score": {"type": ["number", "null"]}, "warning_score": {"type": ["number", "null"]},
    "notes": {"type": ["string", "null"]},
})
_GOVERNANCE = _object("Risk ceilings and budgets only; approval policy uses Execution & approvals settings.", {
    "max_risk_level": {"type": "string", "enum": ["low", "medium", "high"]},
    "budget_caps_per_kind": {"type": "object", "description": "Credit limits keyed by action kind.",
                             "additionalProperties": {"type": "integer", "minimum": 0}},
}, extra=False)
_CHANNEL = _object("Bind channel configuration to a Workspace service. Never include credentials.", {
    "channel_id": _text("Channel identity for an existing binding."),
    "channel_type": _text("Channel type, such as webchat or telegram."),
    "type": _text("Alias for channel_type."), "provider": _text("Channel provider."),
    "channel_config_id": _text("Existing active, user-owned account configuration; required for external channels."),
    "linked_service_key": _text("Workspace service with an Agent mapping."),
    "service_key": _text("Alias for linked_service_key."), "login_required": _BOOL,
    "config": _CONFIG,
})
_CHANNELS = _object("Workspace channel bindings.", {
    "channels": _array("Channel bindings.", _CHANNEL),
    "primary_external_channel": _CHANNEL, "internal_channel": _CHANNEL,
    "secondary_external_channels": _array("Additional external channels.", _CHANNEL),
})
_ASKS = {
    "type": "object", "description": "Hidden Flow input keys to ask for, keyed by App action id.",
    "propertyNames": {"pattern": _ACTION_KEY.pattern},
    "additionalProperties": {"type": "array", "items": {"type": "string", "pattern": _FIELD_KEY.pattern},
                             "minItems": 1, "maxItems": 10},
}
_APP_I18N = {
    "type": "object",
    "description": "Page text overlays by language, such as zh or zh-CN. Mirror existing App fields and their value types; "
                   "only existing display text is retained, never structural identifiers or targets. "
                   "A null language value removes its overlay. Stored language codes are normalized to their base language.",
    "additionalProperties": {"type": ["object", "null"], "additionalProperties": True},
}
_APP = _object("Workspace App presentation and action references. Null map values remove entries.", {
    **{key: {"type": ["string", "null"]} for key in (
        "eyebrow", "title", "description", "hero", "motion_cover_url", "motion_cover_source_url")},
    "cover_image_url": {"type": ["string", "null"],
                        "description": "The App's own cover (site path or https URL), shown when the Workspace has none "
                                       "of its own; it travels with the Blueprint, unlike Marketplace artwork. A motion "
                                       "cover names it as motion_cover_source_url. Null clears it."},
    "theme": {"type": ["object", "null"], "additionalProperties": False,
              "description": "The App's own look within the one platform layout: accent (#rrggbb seed colour; hover "
                             "and soft shades and readable text are derived), surface (neutral, tinted with the "
                             "accent, or deep: a dark App in either theme) and radius (sharp, soft, round). Null "
                             "restores the platform look.",
              "properties": {"accent": {"type": "string", "pattern": "^#[0-9a-fA-F]{6}$"},
                             "surface": {"enum": ["neutral", "tinted", "deep"]},
                             "radius": {"enum": ["sharp", "soft", "round"]}}},
    "layout": {"type": "string", "enum": ["feature", "cover", "list"]},
    "visual": {"type": "string", "enum": ["auto", "image", "motion", "plain"]},
    "open_as": {"type": ["string", "null"], "enum": ["split", "app", "chat", None],
                "description": "What opens first for someone who has not chosen a view of this Workspace yet: the App "
                               "beside the chat (split), the App alone (app) or the chat alone (chat). Null clears it."},
    "order": _STRINGS, "hidden": _STRINGS,
    "asks": {**_ASKS, "description": "Hidden Flow input keys to ask for by App action id; null removes an override.",
             "additionalProperties": {"anyOf": [_ASKS["additionalProperties"], {"type": "null"}]}},
    "i18n": _APP_I18N,
    **{key: _object(description) for key, description in {
        "labels": "Labels keyed by action id.", "descriptions": "Descriptions keyed by action id.",
        "forms": "Input form schemas keyed by action id.", "targets": "Existing Agent, Workflow or Automation references keyed by action id.",
        "entries": "Independent entries keyed by action:<entry id>; null deletes the entry's display configuration.",
        "prompts": "Agent instructions keyed by action id.", "presets": "Input presets keyed by action id.",
        "choices": "The Workspace's own templates for an entry's free-text inputs, keyed by action id, then input key: "
                   "1-60 of {value, label, description, group (category to browse by), image and sample (an audio "
                   "clip to hear; site path or https URL), presets (other input keys to the text each sets)}. They "
                   "are offered first among the input's suggestions, browsed by group and search when there are "
                   "many; picking one also sets its presets. Null removes an entry's choices.",
        "data_sources": "App data source declarations, keyed by name. Project sources (workflow_projects, related_projects, "
                        "project_records, project_documents, project_files, project_references) read project records; "
                        "workflow_results reads what a Flow's runs made (producer_workflows, fields, label_fields and up "
                        "to two media document-id paths, all starting at a step's output_var, a run input or __result) "
                        "and is shown by a gallery block: one card per result, its latest version.",
        "icons": f"Entry glyph names keyed by action id ({', '.join(APP_ENTRY_ICONS)}); null removes one, and "
                 "an entry without one gets the glyph its name suggests.",
    }.items()},
    "components": _array("App component declarations.", _CONFIG),
    "sections": {**_array("App sections; null restores default sections (connections, queue, actions, outputs; "
                          "no metrics). A metrics section shows only the tracked numbers its items name "
                          "(stat:<key> or goal:<key>, in that order), or all of them without items. A section's "
                          "optional title (1-60 characters) is its heading instead of its source's name.", _CONFIG),
                 "type": ["array", "null"]},
}, extra=False)
_MODEL = _object("Workspace operating model. Supplied collections replace those collections; omitted keys are preserved only by update.", {
    "services": _array("Service roles.", _SERVICE), "goals": _array("Goal definitions.", _GOAL),
    "rules": _array("Operating rules.", _RULE), "automations": _array("Automation configuration.", _CONFIG),
    "capability_bindings": _array("Capability bindings.", _BINDING),
    "skill_bindings": _array("Skill bindings.", _BINDING),
    "agent_mappings": _array("Agent/service mappings.", _MAPPING),
    "processes": _array("Descriptive processes.", _PROCESS), "evaluation": _EVALUATION,
    "knowledge": _KNOWLEDGE, "knowledge_policy": _KNOWLEDGE,
    "integration_scope": _object("Workspace-disabled integration providers.", {"disabled_providers": _STRINGS}, extra=False),
    "channel_config": _CHANNELS, "strategist": _STRATEGIST,
    "governance": _GOVERNANCE, "governance_policy": _GOVERNANCE,
    "app_view": {**_APP, "properties": {**_APP["properties"], "version": {"type": "integer", "const": 1},
                                       "asks": {**_ASKS, "maxProperties": 50},
                                       "i18n": {**_APP_I18N, "type": ["object", "null"]}}},
})
_TASK = _object("Immediate Task to materialize on apply through the normal task/governance path.", {
    "title": _text("Task title."), "description": _text("Task instructions."),
    "priority": {"type": "integer", "minimum": 1, "maximum": 5},
    "details": _CONFIG, "runtime_context": _CONFIG, "delegate_service_keys": _STRINGS,
    "owner_service_key": _text("Service responsible for the Task."),
    "owner_subscription_id": _text("Existing Agent subscription responsible for the Task."),
    "task_type": _text("Task type, default general."),
    "start": {"type": "boolean", "description": "Start the Task when the draft is applied; otherwise it stays pending until started."},
    "assignee_id": _text("Existing staff assignee id."), "agent_id": _text("Existing Agent assignee id."),
    "agent_type": _text("Agent assignee type."),
}, required=("title",))
_GOAL_UPDATE = {**_GOAL, "properties": {**_GOAL["properties"],
    "source": {**_SOURCE, "type": ["object", "null"]}, "source_type": _text("Provider key or manual."),
    "provider": _text("Provider key."), "action": _text("Measurement action."),
    "cadence": {"type": ["string", "null"]}, "refresh_cadence": {"type": ["string", "null"]},
    **{key: {"type": ["number", "string", "null"]} for key in ("baseline", "current", "target")},
}}

# The four integration/workflow schemas live beside their strict normalizers.
# All other canonical operations obtain their public shape here.
OPERATION_PAYLOAD_SCHEMAS = {
    "operating_model.replace": _wrapped(_MODEL, "operating_model"),
    "operating_model.update": _wrapped(_MODEL, "operating_model"),
    "app_view.update": _wrapped(_APP, "app_view"),
    "evaluation.update": _wrapped(_EVALUATION, "evaluation"),
    "budget_policy.update": _object("Workspace spending limits; changing these does not buy credits.", {
        "monthly_budget_credits": {"type": ["number", "null"], "minimum": 0},
        "monthly_budget_usd": {"type": ["number", "null"], "minimum": 0},
        "auto_pause_on_budget": _BOOL, "reset_alert_state": _BOOL,
    }),
    "heartbeat_policy.update": _object("Enable/disable or reschedule Strategist reviews; disabling pauses an active live Workspace.", {
        "enabled": _BOOL, "cadence": {"type": ["string", "null"],
                                     "description": "hourly, daily, weekly, biweekly, cron, or null for the existing/default cadence."},
    }),
    "governance_policy.update": {**_GOVERNANCE, "properties": {
        **_GOVERNANCE["properties"], "governance": _GOVERNANCE, "governance_policy": _GOVERNANCE,
    }},
    "rule.add": _wrapped(_RULE, "rule"),
    "rule.remove": _object("Identify the rule to remove.", {
        "rule_key": _text("Existing rule key."), "key": _text("Alias for rule_key."),
        "description": _text("Exact rule description, if no key is available."),
        "match_description": _text("Exact existing description."),
    }),
    "rule.update": _object("Identify an existing rule, then merge its rule fields.", {
        **_RULE["properties"], "key": _text("Alias for rule_key."),
        "match_description": _text("Exact existing description when identifying without a key."), "rule": _RULE,
    }),
    "rules.replace": _object("Replace the complete rule collection; [] removes all rules.", {
        "rules": _array("Complete replacement rule list.", _RULE),
    }, required=("rules",)),
    "goals.replace": _object("Replace the complete goal collection; missing goals are deactivated.", {
        "goals": _array("Complete replacement goal list.", _GOAL),
    }, required=("goals",)),
    "goal.upsert": _wrapped(_GOAL, "goal"),
    "goal.measurement_sources.update": {**_GOAL_UPDATE, "properties": {**_GOAL_UPDATE["properties"],
        **{key: {"description": "Goal updates as a list or map keyed by goal identity.",
                 "anyOf": [_array("Goal measurement updates.", _GOAL_UPDATE),
                           {"type": "object", "additionalProperties": {"type": ["object", "string", "null"]}}]}
           for key in ("updates", "measurement_sources", "sources", "goals")},
    }},
    "service_role.upsert": _wrapped(_SERVICE, "service"),
    "service_role.remove": _object("Remove one service and its Agent mappings.", {
        "key": _text("Existing service key."), "service_key": _text("Alias for key."),
    }),
    "knowledge_policy.update": _KNOWLEDGE,
    "capability_binding.upsert": {**_wrapped(_BINDING, "binding"), "properties": {
        **_wrapped(_BINDING, "binding")["properties"], "bindings": _array("Bindings to insert or merge.", _BINDING),
    }},
    "capability_binding.remove": _object("Remove one Workspace-local binding, leaving account credentials and shared Agents intact.", {
        "binding_key": _text("Existing binding key."), "key": _text("Alias for binding_key."),
    }),
    "skill_binding.upsert": _wrapped(_BINDING, "binding"),
    "channel.upsert": _wrapped(_CHANNEL, "channel"),
    "agent_binding.upsert": _wrapped(_MAPPING, "mapping"),
    "agent_binding.remove": _object("Remove Agent mappings for one service.", {
        "service_key": _text("Existing service key."), "key": _text("Alias for service_key."),
    }),
    "agent_mappings.replace": _object("Replace every Agent/service mapping; omitted mappings are deactivated.", {
        key: _array("Complete replacement mapping list.", _MAPPING)
        for key in ("agent_mappings", "mappings", "agent_bindings")
    }),
    "process.upsert": _wrapped(_PROCESS, "process"),
    "strategist.update": _STRATEGIST,
    "task.prepare": _wrapped(_TASK, "task"),
}
