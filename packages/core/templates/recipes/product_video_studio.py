"""Generic Product Video Studio workspace recipe and workflow contracts."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.constants.plans import is_cloud
from packages.core.templates.registry import TemplateInput, TemplateResult, register


PROJECT_TYPE = "product_video"
PROJECT_SCHEMA_VERSION = 1
PROJECT_STAGES = [
    "draft",
    "discovering",
    "awaiting_plan_approval",
    "capturing",
    "producing",
    "quality_gate",
    "awaiting_acceptance",
    "accepted",
    "completed",
    "blocked",
    "revision_required",
]
DEFAULT_OUTPUT_PROFILE = {
    "aspect_ratio": "16:9",
    "width": 1920,
    "height": 1080,
    "target_duration_seconds": {"min": 60, "max": 120},
    "language": "request_language",
    "voice_profile": "natural_explainer",
    "subtitle_profile": "clean_bottom",
}


ARTIFACT_REF_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {
        "artifact_id": {"type": "string", "minLength": 1},
        "kind": {
            "type": "string",
            "enum": [
                "video",
                "image",
                "audio",
                "subtitle",
                "document",
                "timeline",
                "qa_report",
                "cover",
            ],
        },
        "scene_id": {"type": ["string", "null"], "minLength": 1},
        "document_id": {"type": ["string", "null"], "minLength": 1},
        "fs_path": {"type": ["string", "null"], "minLength": 1},
        "knowledge_path": {"type": ["string", "null"], "minLength": 1},
        "source": {
            "type": "string",
            "enum": ["browser_capture", "generated", "supplied", "derived"],
        },
        "mime_type": {"type": "string", "minLength": 1},
        "status": {
            "type": "string",
            "enum": ["pending", "processing", "ready", "failed", "blocked"],
        },
        "duration_seconds": {"type": ["number", "null"], "minimum": 0},
        "checksum": {"type": ["string", "null"]},
        "provenance": {"type": "object"},
    },
    "required": [
        "artifact_id",
        "kind",
        "source",
        "mime_type",
        "status",
        "provenance",
    ],
    "allOf": [
        {
            "if": {"properties": {"status": {"const": "ready"}}, "required": ["status"]},
            "then": {
                "anyOf": [
                    {"required": ["document_id"]},
                    {"required": ["fs_path"]},
                    {"required": ["knowledge_path"]},
                ]
            },
        },
        {
            "if": {
                "properties": {
                    "source": {"const": "browser_capture"},
                    "status": {"const": "ready"},
                },
                "required": ["source", "status"],
            },
            "then": {
                "properties": {
                    "document_id": {"type": "string", "minLength": 1},
                },
                "required": ["document_id"],
            },
        },
    ],
    "additionalProperties": False,
}


SCENE_ARTIFACT_REF_SCHEMA = deepcopy(ARTIFACT_REF_SCHEMA)
SCENE_ARTIFACT_REF_SCHEMA["properties"]["scene_id"] = {
    "type": "string",
    "minLength": 1,
}
SCENE_ARTIFACT_REF_SCHEMA["required"] = [
    *SCENE_ARTIFACT_REF_SCHEMA["required"],
    "scene_id",
]


BROWSER_EFFECT_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {
        "effect_id": {"type": "string", "minLength": 1},
        "scene_id": {"type": "string", "minLength": 1},
        "action": {"type": "string", "minLength": 1},
        "precondition": {"type": "object"},
        "expected_postcondition": {"type": "object"},
        "status": {
            "type": "string",
            "enum": [
                "not_started",
                "in_progress",
                "observed_complete",
                "failed",
                "unknown",
            ],
        },
        "evidence": {"type": "array", "items": {"type": "object"}},
        "attempt_count": {"type": "integer", "minimum": 0, "maximum": 2},
    },
    "required": [
        "effect_id",
        "scene_id",
        "action",
        "precondition",
        "expected_postcondition",
        "status",
        "evidence",
        "attempt_count",
    ],
    "additionalProperties": False,
}


OUTPUT_PROFILE_SCHEMA = {
    "type": "object",
    "title": "Output settings",
    "x-ui": {
        "collapsible": True,
        "collapsed": True,
        "order": [
            "aspect_ratio",
            "width",
            "height",
            "target_duration_seconds",
            "language",
            "voice_profile",
            "subtitle_profile",
        ],
    },
    "properties": {
        "aspect_ratio": {
            "type": "string",
            "title": "Aspect ratio",
            "enum": ["16:9", "9:16", "1:1", "4:3", "3:4"],
        },
        "width": {"type": "integer", "title": "Width", "minimum": 320, "maximum": 7680},
        "height": {"type": "integer", "title": "Height", "minimum": 320, "maximum": 7680},
        "target_duration_seconds": {
            "type": "object",
            "title": "Target duration (seconds)",
            "x-ui": {"order": ["min", "max"]},
            "properties": {
                "min": {"type": "number", "title": "Minimum", "minimum": 1, "maximum": 3600},
                "max": {"type": "number", "title": "Maximum", "minimum": 1, "maximum": 3600},
            },
            "required": ["min", "max"],
            "additionalProperties": False,
        },
        "language": {
            "type": "string",
            "title": "Language",
            "description": "Use request_language to follow the language of the brief.",
            "minLength": 1,
        },
        "voice_profile": {"type": "string", "title": "Voice profile", "minLength": 1},
        "subtitle_profile": {"type": "string", "title": "Subtitle profile", "minLength": 1},
    },
    "required": [
        "aspect_ratio",
        "width",
        "height",
        "target_duration_seconds",
        "language",
        "voice_profile",
        "subtitle_profile",
    ],
    "additionalProperties": False,
}


PRODUCT_VIDEO_REQUEST_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "title": "Product video request",
    "x-ui": {
        "order": [
            "product_name",
            "start_url",
            "audience",
            "video_type",
            "promotion_goal",
            "must_show",
            "must_not_show",
            "final_cta",
            "narration_instructions",
            "subtitle_instructions",
            "production_constraints",
            "failure_policy",
            "output_profile",
            "reference_documents",
            "reference_assets",
            "browser_session",
        ]
    },
    "properties": {
        "product_name": {"type": "string", "title": "Product name", "minLength": 1},
        "start_url": {
            "type": "string",
            "title": "Product start URL",
            "format": "uri",
            "pattern": r"^[Hh][Tt][Tt][Pp][Ss]?://",
            "minLength": 1,
        },
        "audience": {"type": "string", "title": "Audience", "minLength": 1},
        "promotion_goal": {
            "type": "string",
            "title": "Promotion goal",
            "description": "The product promise and outcome this video must prove.",
            "minLength": 1,
            "x-ui": {"control": "textarea", "rows": 3},
        },
        "video_type": {
            "type": "string",
            "title": "Video type",
            "enum": ["walkthrough", "feature_promotion", "launch", "onboarding", "support"],
        },
        "must_show": {
            "type": "array",
            "title": "Must show",
            "description": "One required product moment per line.",
            "items": {"type": "string", "minLength": 1},
            "minItems": 1,
            "x-ui": {"control": "line_list", "rows": 5},
        },
        "must_not_show": {
            "type": "array",
            "title": "Must not show",
            "description": "One privacy or content exclusion per line.",
            "items": {"type": "string", "minLength": 1},
            "x-ui": {"control": "line_list", "rows": 3},
        },
        "final_cta": {"type": "string", "title": "Final CTA", "minLength": 1},
        "narration_instructions": {
            "type": "string",
            "title": "Narration instructions",
            "x-ui": {"control": "textarea", "rows": 3},
        },
        "subtitle_instructions": {
            "type": "string",
            "title": "Subtitle instructions",
            "x-ui": {"control": "textarea", "rows": 3},
        },
        "production_constraints": {
            "type": "array",
            "title": "Production constraints",
            "items": {"type": "string", "minLength": 1},
            "x-ui": {"control": "line_list", "rows": 4},
        },
        "failure_policy": {
            "type": "string",
            "title": "Failure and retry policy",
            "x-ui": {"control": "textarea", "rows": 3},
        },
        "reference_documents": {
            "type": "array",
            "title": "Reference documents",
            "items": ARTIFACT_REF_SCHEMA,
            "x-ui": {"hidden": True},
        },
        "reference_assets": {
            "type": "array",
            "title": "Reference assets",
            "items": ARTIFACT_REF_SCHEMA,
            "x-ui": {"hidden": True},
        },
        "output_profile": OUTPUT_PROFILE_SCHEMA,
        "browser_session": {
            "type": "string",
            "title": "Browser session",
            "const": "current_paired_chrome_session",
            "x-ui": {"hidden": True},
        },
        "source_brief": {
            "type": "string",
            "title": "Source brief",
            "x-ui": {"hidden": True},
        },
    },
    "required": [
        "product_name",
        "start_url",
        "must_show",
    ],
    "additionalProperties": False,
}


PRODUCT_VIDEO_REQUEST_RETRY_SCHEMA = {
    "type": "object",
    "x-ui": {"order": ["request", "revision_notes"]},
    "properties": {
        "request": deepcopy(PRODUCT_VIDEO_REQUEST_SCHEMA),
        "revision_notes": {
            "type": "string",
            "title": "Revision notes",
        },
    },
    "required": ["request"],
    "additionalProperties": False,
}


DEFAULT_PRODUCT_VIDEO_REQUEST = {
    "audience": "Prospective and first-time product users",
    "video_type": "feature_promotion",
    "promotion_goal": "Demonstrate the product's key workflow and practical value.",
    "must_show": [],
    "must_not_show": [
        "Credentials, secrets, personal data, and unrelated browser content",
    ],
    "final_cta": (
        "End on the final required product moment; do not add an external publishing CTA."
    ),
    "reference_documents": [],
    "reference_assets": [],
    "narration_instructions": (
        "Follow the brief's language and tone and use one continuous narration track."
    ),
    "subtitle_instructions": (
        "Use 52px subtitles on a 1920x1080 canvas, scale proportionally for other "
        "resolutions, keep at most two lines, a light outline, and the bottom safe area."
    ),
    "production_constraints": [
        "Use the current paired browser session",
        "Use only local or user-authorized data",
        "Store every project asset in one project directory in this Workspace Knowledge",
    ],
    "failure_policy": (
        "Pause at the failed node, report the observed problem and editable input, "
        "then resume from that node after correction."
    ),
    "output_profile": deepcopy(DEFAULT_OUTPUT_PROFILE),
    "browser_session": "current_paired_chrome_session",
}


PRODUCT_VIDEO_REQUEST_PREFILL = {
    "source": "chat_message",
    "mode": "structured",
    "instructions": (
        "Extract the product, clean start URL, audience, duration, required "
        "product moments, exclusions, language, narration, subtitle, browser "
        "session, storage, and interruption constraints from the user's message. "
        "Keep the user's numbered top-level must-show items as the Must show entries; "
        "nested tabs or modules stay inside their parent item instead of becoming extra scenes. "
        "When the brief explicitly limits the video to named content and gives no "
        "CTA, use a neutral no-additional-CTA instruction ending on the last required "
        "scene rather than inventing a marketing action."
    ),
}


def _product_video_request_run_inputs() -> list[dict[str, Any]]:
    properties = PRODUCT_VIDEO_REQUEST_SCHEMA["properties"]
    required = set(PRODUCT_VIDEO_REQUEST_SCHEMA["required"])
    ordered_keys = [
        *PRODUCT_VIDEO_REQUEST_SCHEMA["x-ui"]["order"],
        "source_brief",
    ]
    rows: list[dict[str, Any]] = []
    for key in ordered_keys:
        schema = deepcopy(properties[key])
        schema_type = schema.get("type")
        input_type = (
            "number"
            if schema_type in {"number", "integer"}
            else "boolean"
            if schema_type == "boolean"
            else "json"
            if schema_type in {"array", "object"}
            else "string"
        )
        row = {
            "key": key,
            "label": str(schema.get("title") or key),
            "type": input_type,
            "required": key in required,
            "hidden": bool((schema.get("x-ui") or {}).get("hidden")),
            "schema": schema,
            "target": f"request.{key}",
        }
        if key in DEFAULT_PRODUCT_VIDEO_REQUEST:
            row["default"] = deepcopy(DEFAULT_PRODUCT_VIDEO_REQUEST[key])
        if key == "source_brief":
            row["prefill"] = {
                "source": "chat_message",
                "mode": "raw",
            }
        rows.append(row)
    return rows


PRODUCT_VIDEO_REQUEST_RUN_INPUTS = _product_video_request_run_inputs()


SCENE_PLAN_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {
        "scene_id": {"type": "string", "minLength": 1},
        "target_page": {"type": "string", "minLength": 1},
        "precondition": {"type": "object", "minProperties": 1},
        "ordered_actions": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "minLength": 1},
                    "arguments": {"type": "object"},
                    "side_effect": {"type": "boolean"},
                },
                "required": ["action", "arguments", "side_effect"],
                "additionalProperties": False,
            },
        },
        "expected_visual_state": {"type": "object", "minProperties": 1},
        "canonical_narration": {"type": "string", "minLength": 1},
        "required_asset_types": {
            "type": "array",
            "minItems": 1,
            "uniqueItems": True,
            "items": {
                "type": "string",
                "enum": ["recording", "screenshot"],
            },
        },
        "acceptance_evidence": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "properties": {
                    "must_show": {"type": "string", "minLength": 1},
                    "observable": {"type": "string", "minLength": 1},
                    "required_asset_type": {
                        "type": "string",
                        "enum": ["recording", "screenshot"],
                    },
                },
                "required": ["must_show", "observable", "required_asset_type"],
                "additionalProperties": False,
            },
        },
        "target_duration_seconds": {"type": "number", "exclusiveMinimum": 0, "maximum": 120},
        "dependencies": {"type": "array", "items": {"type": "string", "minLength": 1}},
        "recovery": {"type": "string", "minLength": 1},
        "privacy": {
            "type": "array",
            "minItems": 1,
            "items": {"type": "string", "minLength": 1},
        },
    },
    "required": [
        "scene_id",
        "target_page",
        "precondition",
        "ordered_actions",
        "expected_visual_state",
        "canonical_narration",
        "required_asset_types",
        "acceptance_evidence",
        "target_duration_seconds",
        "dependencies",
        "recovery",
        "privacy",
    ],
    "additionalProperties": False,
}


SCENE_BLOCKER_SCHEMA = {
    "type": "object",
    "properties": {
        "scene_id": {"type": "string", "minLength": 1},
        "observed_state": {"type": "object", "minProperties": 1},
        "required_change": {"type": "string", "minLength": 1},
        "preserved_receipts": {"type": "array", "items": ARTIFACT_REF_SCHEMA},
    },
    "required": [
        "scene_id",
        "observed_state",
        "required_change",
        "preserved_receipts",
    ],
    "additionalProperties": False,
}


PROJECT_SCENE_SCHEMA = deepcopy(SCENE_PLAN_SCHEMA)
PROJECT_SCENE_SCHEMA["properties"].update(
    {
        "status": {
            "type": "string",
            "enum": ["planned", "capturing", "waiting", "completed", "failed", "blocked"],
        },
        "artifact_ids": {"type": "array", "items": {"type": "string", "minLength": 1}},
        "blocker": {"anyOf": [SCENE_BLOCKER_SCHEMA, {"type": "null"}]},
    }
)


MUST_SHOW_COVERAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "requirement": {"type": "string", "minLength": 1},
        "scene_ids": {
            "type": "array",
            "minItems": 1,
            "uniqueItems": True,
            "items": {"type": "string", "minLength": 1},
        },
        "acceptance_evidence": deepcopy(
            SCENE_PLAN_SCHEMA["properties"]["acceptance_evidence"]
        ),
    },
    "required": ["requirement", "scene_ids", "acceptance_evidence"],
    "additionalProperties": False,
}


PRODUCT_VIDEO_PLAN_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {
        "plan_version": {"type": "integer", "minimum": 1},
        "product_promise": {"type": "string", "minLength": 1},
        "canonical_narration": {"type": "string", "minLength": 1},
        "scene_ids": {
            "type": "array",
            "minItems": 1,
            "uniqueItems": True,
            "items": {"type": "string", "minLength": 1},
        },
        "scenes": {"type": "array", "minItems": 1, "items": SCENE_PLAN_SCHEMA},
        "must_show_coverage": {
            "type": "array",
            "minItems": 1,
            "items": MUST_SHOW_COVERAGE_SCHEMA,
        },
        "must_show_coverage_complete": {"type": "boolean"},
        "covered_must_show": {
            "type": "array",
            "minItems": 1,
            "uniqueItems": True,
            "items": {"type": "string", "minLength": 1},
        },
        "listed_side_effects": {"type": "array", "items": {"type": "string", "minLength": 1}},
        "estimated_duration_seconds": {"type": "number", "exclusiveMinimum": 0},
        "output_profile": OUTPUT_PROFILE_SCHEMA,
        "privacy_notes": {"type": "array", "items": {"type": "string", "minLength": 1}},
    },
    "required": [
        "plan_version",
        "product_promise",
        "canonical_narration",
        "scene_ids",
        "scenes",
        "must_show_coverage",
        "must_show_coverage_complete",
        "covered_must_show",
        "listed_side_effects",
        "estimated_duration_seconds",
        "output_profile",
        "privacy_notes",
    ],
    "additionalProperties": False,
}


QUALITY_RESULT_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {
        "verdict": {
            "type": "string",
            "enum": ["machine_pass", "repairable_technical", "revision_required"],
        },
        "coverage_complete": {"type": "boolean"},
        "evidence_complete": {"type": "boolean"},
        "measured_sync_passed": {"type": "boolean"},
        "covered_must_show": {
            "type": "array",
            "minItems": 1,
            "uniqueItems": True,
            "items": {"type": "string", "minLength": 1},
        },
        "technical_checks": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "properties": {
                    "check_id": {"type": "string", "minLength": 1},
                    "status": {"type": "string", "enum": ["pass", "fail"]},
                    "evidence": {"type": "object", "minProperties": 1},
                },
                "required": ["check_id", "status", "evidence"],
                "additionalProperties": False,
            },
        },
        "visual_findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "finding_id": {"type": "string", "minLength": 1},
                    "severity": {"type": "string", "enum": ["info", "warning", "blocking"]},
                    "scene_id": {"type": ["string", "null"]},
                    "description": {"type": "string", "minLength": 1},
                    "evidence_artifact_ids": {"type": "array", "items": {"type": "string"}},
                },
                "required": [
                    "finding_id",
                    "severity",
                    "scene_id",
                    "description",
                    "evidence_artifact_ids",
                ],
                "additionalProperties": False,
            },
        },
        "coverage": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "properties": {
                    "requirement": {"type": "string", "minLength": 1},
                    "status": {"type": "string", "enum": ["covered", "missing"]},
                    "scene_ids": {"type": "array", "items": {"type": "string"}},
                    "evidence_artifact_ids": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["requirement", "status", "scene_ids", "evidence_artifact_ids"],
                "additionalProperties": False,
            },
        },
        "scene_revisions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "scene_id": {"type": "string", "minLength": 1},
                    "finding_ids": {"type": "array", "items": {"type": "string"}},
                    "instruction": {"type": "string", "minLength": 1},
                },
                "required": ["scene_id", "finding_ids", "instruction"],
                "additionalProperties": False,
            },
        },
        "operator_checks": {"type": "array", "items": {"type": "string", "minLength": 1}},
    },
    "required": [
        "verdict",
        "coverage_complete",
        "evidence_complete",
        "measured_sync_passed",
        "covered_must_show",
        "technical_checks",
        "visual_findings",
        "coverage",
        "scene_revisions",
        "operator_checks",
    ],
    "allOf": [
        {
            "if": {
                "properties": {"verdict": {"const": "machine_pass"}},
                "required": ["verdict"],
            },
            "then": {
                "properties": {
                    "coverage_complete": {"const": True},
                    "evidence_complete": {"const": True},
                    "measured_sync_passed": {"const": True},
                    "technical_checks": {
                        "minItems": 1,
                        "items": {
                            "properties": {"status": {"const": "pass"}},
                            "required": ["status"],
                        },
                    },
                    "coverage": {
                        "minItems": 1,
                        "items": {
                            "properties": {
                                "status": {"const": "covered"},
                                "scene_ids": {"minItems": 1},
                                "evidence_artifact_ids": {"minItems": 1},
                            },
                            "required": [
                                "status",
                                "scene_ids",
                                "evidence_artifact_ids",
                            ],
                        },
                    },
                    "scene_revisions": {"maxItems": 0},
                }
            },
        }
    ],
    "additionalProperties": False,
}


DISCOVERY_SCHEMA = {
    "type": "object",
    "properties": {
        "observations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "minLength": 1},
                    "labels": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["url", "labels"],
                "additionalProperties": False,
            },
        },
        "recommended_steps": {"type": "array", "items": {"type": "string", "minLength": 1}},
        "assumptions": {"type": "array", "items": {"type": "string", "minLength": 1}},
        "gaps": {"type": "array", "items": {"type": "string", "minLength": 1}},
        "privacy_risks": {"type": "array", "items": {"type": "string", "minLength": 1}},
        "ready_for_planning": {"type": "boolean"},
    },
    "required": [
        "observations",
        "recommended_steps",
        "assumptions",
        "gaps",
        "privacy_risks",
        "ready_for_planning",
    ],
    "additionalProperties": False,
}


PRODUCT_VIDEO_PROJECT_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {
        "request": PRODUCT_VIDEO_REQUEST_SCHEMA,
        "project_root": {"type": ["string", "null"]},
        "ledger_path": {"type": ["string", "null"]},
        "artifact_manifest_path": {"type": ["string", "null"]},
        "current_phase": {"type": "string", "minLength": 1},
        "business_outcome": {
            "type": "string",
            "enum": [
                "in_progress",
                "needs_input",
                "revision_required",
                "ready_for_acceptance",
                "accepted",
                "completed",
                "cancelled",
            ],
        },
        "product_source": {
            "type": "object",
            "properties": {
                "start_url": {"type": "string", "minLength": 1},
                "browser_session": {"type": "string", "minLength": 1},
            },
            "required": ["start_url", "browser_session"],
            "additionalProperties": False,
        },
        "output_profile": OUTPUT_PROFILE_SCHEMA,
        "discovered_journey": {"anyOf": [DISCOVERY_SCHEMA, {"type": "null"}]},
        "plan": {"anyOf": [PRODUCT_VIDEO_PLAN_SCHEMA, {"type": "null"}]},
        "approved_plan_version": {"type": ["integer", "null"], "minimum": 1},
        "capture_grant_id": {"type": ["string", "null"], "minLength": 1},
        "scenes": {"type": "array", "items": PROJECT_SCENE_SCHEMA},
        "segments": {"type": "array", "items": PROJECT_SCENE_SCHEMA},
        "artifacts": {"type": "array", "items": ARTIFACT_REF_SCHEMA},
        "checkpoints": {"type": "object"},
        "retry_state": {"type": ["object", "null"]},
        "final_artifacts": {"type": "array", "items": ARTIFACT_REF_SCHEMA},
        "history": {"type": "array", "items": {"type": "object"}},
        "timeline": {"type": ["object", "null"]},
        "quality_result": {"anyOf": [QUALITY_RESULT_SCHEMA, {"type": "null"}]},
        "operator_acceptance": {"type": ["object", "null"]},
        "revision_history": {"type": "array", "items": {"type": "object"}},
        "blockers": {"type": "array", "items": {"type": "object"}},
    },
    "required": [
        "request",
        "project_root",
        "ledger_path",
        "artifact_manifest_path",
        "current_phase",
        "business_outcome",
        "product_source",
        "output_profile",
        "discovered_journey",
        "plan",
        "approved_plan_version",
        "capture_grant_id",
        "scenes",
        "segments",
        "artifacts",
        "checkpoints",
        "retry_state",
        "final_artifacts",
        "history",
        "timeline",
        "quality_result",
        "operator_acceptance",
        "revision_history",
        "blockers",
    ],
    "additionalProperties": False,
}


BROWSER_PREFLIGHT_SCHEMA = {
    "type": "object",
    "properties": {
        "available": {"type": "boolean"},
        "requires_login": {"type": "boolean"},
        "blocker": {"type": ["string", "null"]},
    },
    "required": ["available", "requires_login", "blocker"],
    "allOf": [
        {
            "if": {
                "properties": {"available": {"const": False}},
                "required": ["available"],
            },
            "then": {
                "properties": {
                    "blocker": {"type": "string", "minLength": 1, "pattern": r"\S"},
                },
            },
        },
    ],
    "additionalProperties": False,
}


CAPTURE_RESULT_SCHEMA = {
    "type": "object",
    "properties": {
        "scene": PROJECT_SCENE_SCHEMA,
        "artifacts": {"type": "array", "items": SCENE_ARTIFACT_REF_SCHEMA},
        "wait_required": {"type": "boolean"},
        "wait_seconds": {"type": "number", "minimum": 0, "maximum": 86400},
    },
    "required": ["scene", "artifacts", "wait_required", "wait_seconds"],
    "allOf": [
        {
            "if": {
                "properties": {
                    "scene": {
                        "properties": {"status": {"const": "completed"}},
                        "required": ["status"],
                    }
                },
                "required": ["scene"],
            },
            "then": {
                "properties": {
                    "scene": {
                        "properties": {"artifact_ids": {"minItems": 1}},
                    },
                    "artifacts": {
                        "minItems": 1,
                        "items": {
                            "properties": {"status": {"const": "ready"}},
                            "required": ["status"],
                        },
                    },
                }
            },
        },
        {
            "if": {
                "properties": {
                    "scene": {
                        "properties": {
                            "status": {"const": "completed"},
                            "required_asset_types": {
                                "contains": {"const": "screenshot"},
                            },
                        },
                        "required": ["status", "required_asset_types"],
                    }
                },
                "required": ["scene"],
            },
            "then": {
                "properties": {
                    "artifacts": {
                        "contains": {
                            "properties": {
                                "kind": {"const": "image"},
                                "status": {"const": "ready"},
                            },
                            "required": ["kind", "status"],
                        },
                        "minContains": 1,
                    }
                }
            },
        },
        {
            "if": {
                "properties": {
                    "scene": {
                        "properties": {
                            "status": {"const": "completed"},
                            "required_asset_types": {
                                "contains": {"const": "recording"},
                            },
                        },
                        "required": ["status", "required_asset_types"],
                    }
                },
                "required": ["scene"],
            },
            "then": {
                "properties": {
                    "artifacts": {
                        "contains": {
                            "properties": {
                                "kind": {"const": "video"},
                                "status": {"const": "ready"},
                            },
                            "required": ["kind", "status"],
                        },
                        "minContains": 1,
                    }
                }
            },
        },
    ],
    "additionalProperties": False,
}


COLLECTION_RESULT_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {
        "collection_complete": {"type": "boolean"},
        "segments": {"type": "array", "items": PROJECT_SCENE_SCHEMA},
        "artifacts": {"type": "array", "items": SCENE_ARTIFACT_REF_SCHEMA},
        "retry_segment_ids": {
            "type": "array",
            "uniqueItems": True,
            "items": {"type": "string", "minLength": 1},
        },
        "manifest_path": {"type": "string", "minLength": 1},
        "blocker": {"anyOf": [SCENE_BLOCKER_SCHEMA, {"type": "null"}]},
    },
    "required": [
        "collection_complete",
        "segments",
        "artifacts",
        "retry_segment_ids",
        "manifest_path",
        "blocker",
    ],
    "allOf": [
        {
            "if": {
                "properties": {"collection_complete": {"const": True}},
                "required": ["collection_complete"],
            },
            "then": {
                "properties": {
                    "retry_segment_ids": {"maxItems": 0},
                    "blocker": {"type": "null"},
                    "segments": {
                        "items": {
                            "properties": {"status": {"const": "completed"}},
                            "required": ["status"],
                        }
                    },
                }
            },
            "else": {
                "properties": {
                    "retry_segment_ids": {"minItems": 1},
                    "blocker": SCENE_BLOCKER_SCHEMA,
                }
            },
        }
    ],
    "additionalProperties": False,
}


def _artifact_ref_schema_for(kind: str) -> dict[str, Any]:
    schema = deepcopy(ARTIFACT_REF_SCHEMA)
    schema["properties"]["kind"] = {"const": kind}
    return schema


VISUAL_SCENE_INTERVAL_SCHEMA = {
    "type": "object",
    "properties": {
        "scene_id": {"type": "string", "minLength": 1},
        "visual_start_seconds": {"type": "number", "minimum": 0},
        "visual_end_seconds": {"type": "number", "exclusiveMinimum": 0},
        "narration_start_seconds": {"type": "number", "minimum": 0},
        "narration_end_seconds": {"type": "number", "exclusiveMinimum": 0},
    },
    "required": [
        "scene_id",
        "visual_start_seconds",
        "visual_end_seconds",
        "narration_start_seconds",
        "narration_end_seconds",
    ],
    "additionalProperties": False,
}


ALIGNMENT_METRICS_SCHEMA = {
    "type": "object",
    "properties": {
        "similarity": {"type": "number", "minimum": 0.90, "maximum": 1},
        "coverage": {"type": "number", "minimum": 0.95, "maximum": 1},
        "measured_timestamps": {"const": True},
        "transcription_model": {"type": "string", "minLength": 1},
        "aligned_sentence_indexes": {
            "type": "array",
            "minItems": 1,
            "uniqueItems": True,
            "items": {"type": "integer", "minimum": 1},
        },
        "missing_sentence_indexes": {
            "type": "array",
            "uniqueItems": True,
            "items": {"type": "integer", "minimum": 1},
        },
        "timing_sources": {
            "type": "array",
            "minItems": 1,
            "uniqueItems": True,
            "items": {"type": "string", "pattern": "measured"},
        },
        "sentence_timestamps": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "properties": {
                    "sentence_index": {"type": "integer", "minimum": 1},
                    "start": {"type": "number", "minimum": 0},
                    "end": {"type": "number", "exclusiveMinimum": 0},
                    "timing_source": {"type": "string", "pattern": "measured"},
                },
                "required": ["sentence_index", "start", "end", "timing_source"],
                "additionalProperties": False,
            },
        },
        "scene_coverage": {
            "type": "object",
            "properties": {
                "coverage": {"type": "number", "minimum": 0.95, "maximum": 1},
                "covered_scene_ids": {
                    "type": "array",
                    "minItems": 1,
                    "uniqueItems": True,
                    "items": {"type": "string", "minLength": 1},
                },
                "missing_scene_ids": {
                    "type": "array",
                    "maxItems": 0,
                    "items": {"type": "string", "minLength": 1},
                },
                "missing_interval_scene_ids": {
                    "type": "array",
                    "maxItems": 0,
                    "items": {"type": "string", "minLength": 1},
                },
                "unmapped_scene_ids": {
                    "type": "array",
                    "maxItems": 0,
                    "items": {"type": "string", "minLength": 1},
                },
            },
            "required": [
                "coverage",
                "covered_scene_ids",
                "missing_scene_ids",
                "missing_interval_scene_ids",
                "unmapped_scene_ids",
            ],
            "additionalProperties": False,
        },
    },
    "required": [
        "similarity",
        "coverage",
        "measured_timestamps",
        "transcription_model",
        "aligned_sentence_indexes",
        "missing_sentence_indexes",
        "timing_sources",
        "sentence_timestamps",
        "scene_coverage",
    ],
    "additionalProperties": False,
}


DEFICIT_EVIDENCE_SCHEMA = {
    "type": "object",
    "properties": {
        "scene_id": {"type": "string", "minLength": 1},
        "required_narration_span_seconds": {"type": "number", "exclusiveMinimum": 0},
        "available_visual_duration_seconds": {"type": "number", "minimum": 0},
        "deficit_seconds": {"type": "number", "exclusiveMinimum": 0},
        "allowed_deficit_seconds": {"type": "number", "minimum": 0, "maximum": 0.75},
        "threshold_formula": {
            "const": "min(0.75s, 10% of required_narration_span_seconds)",
        },
        "exceeds_threshold": {"type": "boolean"},
    },
    "required": [
        "scene_id",
        "required_narration_span_seconds",
        "available_visual_duration_seconds",
        "deficit_seconds",
        "allowed_deficit_seconds",
        "threshold_formula",
        "exceeds_threshold",
    ],
    "additionalProperties": False,
}


PRODUCTION_TIMELINE_SCHEMA = {
    "type": "object",
    "properties": {
        "final_video": _artifact_ref_schema_for("video"),
        "narration": _artifact_ref_schema_for("audio"),
        "subtitles": _artifact_ref_schema_for("subtitle"),
        "scene_boundaries": {
            "type": "array",
            "minItems": 1,
            "items": {"type": "number", "minimum": 0},
        },
        "visual_scene_intervals": {
            "type": "array",
            "minItems": 1,
            "items": VISUAL_SCENE_INTERVAL_SCHEMA,
        },
        "alignment_metrics": ALIGNMENT_METRICS_SCHEMA,
    },
    "required": [
        "final_video",
        "narration",
        "subtitles",
        "scene_boundaries",
        "visual_scene_intervals",
        "alignment_metrics",
    ],
    "additionalProperties": False,
}


PRODUCTION_BLOCKER_SCHEMA = {
    "type": "object",
    "properties": {
        "blocker_type": {"type": "string", "minLength": 1},
        "observed_problem": {"type": "object", "minProperties": 1},
        "required_change": {"type": "string", "minLength": 1},
        "preserved_receipts": {"type": "array", "items": ARTIFACT_REF_SCHEMA},
    },
    "required": [
        "blocker_type",
        "observed_problem",
        "required_change",
        "preserved_receipts",
    ],
    "additionalProperties": False,
}


PRODUCTION_RESULT_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": ["ready", "retake_required", "blocked"],
        },
        "timeline": {"anyOf": [PRODUCTION_TIMELINE_SCHEMA, {"type": "null"}]},
        "artifacts": {"type": "array", "items": ARTIFACT_REF_SCHEMA},
        "deficient_scene_ids": {
            "type": "array",
            "uniqueItems": True,
            "items": {"type": "string", "minLength": 1},
        },
        "retry_segment_ids": {
            "type": "array",
            "uniqueItems": True,
            "items": {"type": "string", "minLength": 1},
        },
        "deficit_evidence": {"type": "array", "items": DEFICIT_EVIDENCE_SCHEMA},
        "required_change": {
            "anyOf": [
                {"type": "string", "minLength": 1},
                {"type": "null"},
            ]
        },
        "blocker": {"anyOf": [PRODUCTION_BLOCKER_SCHEMA, {"type": "null"}]},
    },
    "required": [
        "status",
        "timeline",
        "artifacts",
        "deficient_scene_ids",
        "retry_segment_ids",
        "deficit_evidence",
        "required_change",
        "blocker",
    ],
    "allOf": [
        {
            "if": {
                "properties": {"status": {"const": "ready"}},
                "required": ["status"],
            },
            "then": {
                "properties": {
                    "timeline": {
                        "type": "object",
                        "properties": {
                            "final_video": {
                                "properties": {"status": {"const": "ready"}},
                                "required": ["status"],
                            },
                            "narration": {
                                "properties": {"status": {"const": "ready"}},
                                "required": ["status"],
                            },
                            "subtitles": {
                                "properties": {"status": {"const": "ready"}},
                                "required": ["status"],
                            },
                        },
                        "required": [
                            "final_video",
                            "narration",
                            "subtitles",
                            "scene_boundaries",
                            "visual_scene_intervals",
                            "alignment_metrics",
                        ],
                    },
                    "artifacts": {"minItems": 3},
                    "deficient_scene_ids": {"maxItems": 0},
                    "retry_segment_ids": {"maxItems": 0},
                    "deficit_evidence": {"maxItems": 0},
                    "required_change": {"type": "null"},
                    "blocker": {"type": "null"},
                }
            }
        },
        {
            "if": {
                "properties": {"status": {"const": "retake_required"}},
                "required": ["status"],
            },
            "then": {
                "properties": {
                    "deficient_scene_ids": {"minItems": 1},
                    "retry_segment_ids": {"minItems": 1},
                    "deficit_evidence": {"minItems": 1},
                    "required_change": {"type": "string", "minLength": 1},
                    "blocker": {"type": "null"},
                }
            },
        },
        {
            "if": {
                "properties": {"status": {"const": "blocked"}},
                "required": ["status"],
            },
            "then": {
                "properties": {
                    "deficient_scene_ids": {"maxItems": 0},
                    "retry_segment_ids": {"maxItems": 0},
                    "deficit_evidence": {"maxItems": 0},
                    "required_change": {"type": "null"},
                    "blocker": PRODUCTION_BLOCKER_SCHEMA,
                }
            },
        }
    ],
    "additionalProperties": False,
}


PLAN_CONTRACT_VALIDATOR_CODE = r'''import json
import re

request = inputs.get("request") if isinstance(inputs.get("request"), dict) else {}
plan = inputs.get("plan") if isinstance(inputs.get("plan"), dict) else {}
errors = []
invalid_scene_ids = set()


def fail(code, **details):
    errors.append({"code": code, **details})


def estimated_narration_seconds(value):
    text = str(value or "")
    cjk_count = len(re.findall(r"[\u3400-\u9fff]", text))
    word_count = len(re.findall(r"[A-Za-z0-9]+(?:[-'][A-Za-z0-9]+)*", text))
    pause_count = len(re.findall(r"[.!?。！？]", text))
    return round(cjk_count / 4.0 + word_count / 2.5 + pause_count * 0.15, 3)


requested = request.get("must_show") if isinstance(request.get("must_show"), list) else []
coverage = plan.get("must_show_coverage") if isinstance(plan.get("must_show_coverage"), list) else []
coverage_requirements = [
    item.get("requirement") if isinstance(item, dict) else None
    for item in coverage
]
missing_requirements = [item for item in requested if item not in coverage_requirements]
if coverage_requirements != requested:
    fail(
        "coverage_requirements_mismatch",
        expected=requested,
        actual=coverage_requirements,
    )

declared_scene_ids = {
    scene_id for scene_id in plan.get("scene_ids", [])
    if isinstance(scene_id, str) and scene_id
}
scene_list = plan.get("scenes") if isinstance(plan.get("scenes"), list) else []
scenes_by_id = {}
for scene in scene_list:
    if not isinstance(scene, dict):
        continue
    scene_id = scene.get("scene_id")
    if not isinstance(scene_id, str) or not scene_id:
        continue
    if scene_id in scenes_by_id:
        invalid_scene_ids.add(scene_id)
        fail("duplicate_plan_scene_id", scene_id=scene_id)
    scenes_by_id[scene_id] = scene

scene_object_ids = set(scenes_by_id)
if declared_scene_ids != scene_object_ids:
    invalid_scene_ids.update(declared_scene_ids ^ scene_object_ids)
    fail(
        "plan_scene_definitions_mismatch",
        declared_scene_ids=sorted(declared_scene_ids),
        scene_object_ids=sorted(scene_object_ids),
    )

for scene_id, scene in scenes_by_id.items():
    available_seconds = float(scene.get("target_duration_seconds") or 0)
    estimated_seconds = estimated_narration_seconds(scene.get("canonical_narration"))
    allowed_overage = min(0.75, available_seconds * 0.10)
    if available_seconds > 0 and estimated_seconds > available_seconds + allowed_overage:
        invalid_scene_ids.add(scene_id)
        fail(
            "scene_narration_duration_exceeded",
            scene_id=scene_id,
            estimated_narration_seconds=estimated_seconds,
            available_scene_seconds=available_seconds,
            allowed_overage_seconds=round(allowed_overage, 3),
        )

coverage_scene_ids = set()
for item in coverage:
    if not isinstance(item, dict):
        fail("invalid_coverage_entry")
        continue
    requirement = item.get("requirement")
    mapped_scene_ids = item.get("scene_ids") if isinstance(item.get("scene_ids"), list) else []
    coverage_scene_ids.update(mapped_scene_ids)
    coverage_evidence = (
        item.get("acceptance_evidence")
        if isinstance(item.get("acceptance_evidence"), list)
        else []
    )
    requirement_evidence = [
        evidence for evidence in coverage_evidence
        if isinstance(evidence, dict) and evidence.get("must_show") == requirement
    ]
    if len(requirement_evidence) != len(coverage_evidence) or not requirement_evidence:
        fail("coverage_acceptance_evidence_mismatch", requirement=requirement)
    for scene_id in mapped_scene_ids:
        if scene_id not in declared_scene_ids or scene_id not in scenes_by_id:
            invalid_scene_ids.add(scene_id)
            fail("unknown_coverage_scene_id", requirement=requirement, scene_id=scene_id)
            continue
        scene_evidence = scenes_by_id[scene_id].get("acceptance_evidence")
        scene_evidence = scene_evidence if isinstance(scene_evidence, list) else []
        if not any(evidence in scene_evidence for evidence in requirement_evidence):
            fail(
                "scene_acceptance_evidence_mismatch",
                requirement=requirement,
                scene_id=scene_id,
            )

if coverage_scene_ids != declared_scene_ids:
    invalid_scene_ids.update(coverage_scene_ids ^ declared_scene_ids)
    fail(
        "plan_scene_scope_mismatch",
        declared_scene_ids=sorted(declared_scene_ids),
        coverage_scene_ids=sorted(coverage_scene_ids),
    )

result = {
    "valid": not errors,
    "errors": errors,
    "missing_requirements": missing_requirements,
    "invalid_scene_ids": sorted(invalid_scene_ids),
    "invalid_evidence_ids": [],
}
print(json.dumps(result, sort_keys=True))
'''


COLLECTION_CONTRACT_VALIDATOR_CODE = r'''import json

selected_scenes = inputs.get("selected_scenes") if isinstance(inputs.get("selected_scenes"), list) else []
selected_scene_ids = inputs.get("selected_scene_ids") if isinstance(inputs.get("selected_scene_ids"), list) else []
granted_scene_ids = inputs.get("granted_scene_ids") if isinstance(inputs.get("granted_scene_ids"), list) else []
collection = inputs.get("collection_result") if isinstance(inputs.get("collection_result"), dict) else {}
segments = collection.get("segments") if isinstance(collection.get("segments"), list) else []
artifacts = collection.get("artifacts") if isinstance(collection.get("artifacts"), list) else []
errors = []
invalid_scene_ids = set()
invalid_artifact_ids = set()
rejected_segment_ids = set()
rejected_artifact_ids = set()
retry_scene_ids = set()
validated_segments = []
validated_artifacts = []
locked_scene_fields = (
    "target_page",
    "precondition",
    "ordered_actions",
    "expected_visual_state",
    "canonical_narration",
    "required_asset_types",
    "acceptance_evidence",
    "target_duration_seconds",
    "dependencies",
    "recovery",
    "privacy",
)


def fail(code, **details):
    errors.append({"code": code, **details})


def string_ids(values):
    return [value for value in values if isinstance(value, str) and value]


def artifact_scene_ids(artifact):
    scene_ids = set()
    if not isinstance(artifact, dict):
        return scene_ids
    provenance = artifact.get("provenance")
    provenance = provenance if isinstance(provenance, dict) else {}
    for value in (
        artifact.get("scene_id"),
        provenance.get("scene_id"),
        provenance.get("segment_id"),
    ):
        if isinstance(value, str) and value:
            scene_ids.add(value)
    for key in ("scene_ids", "segment_ids"):
        values = provenance.get(key)
        if isinstance(values, list):
            scene_ids.update(string_ids(values))
    return scene_ids


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def provenance_scene_ids(artifact):
    provenance = artifact.get("provenance")
    provenance = provenance if isinstance(provenance, dict) else {}
    scene_ids = set()
    for value in (provenance.get("scene_id"), provenance.get("segment_id")):
        if isinstance(value, str) and value:
            scene_ids.add(value)
    for key in ("scene_ids", "segment_ids"):
        values = provenance.get(key)
        if isinstance(values, list):
            scene_ids.update(string_ids(values))
    return scene_ids


expected_ids = string_ids(selected_scene_ids)
expected_id_set = set(expected_ids)
selected_scene_by_id = {
    scene.get("scene_id"): scene
    for scene in selected_scenes
    if isinstance(scene, dict) and isinstance(scene.get("scene_id"), str) and scene.get("scene_id")
}
selected_object_ids = set(selected_scene_by_id)
granted_ids = string_ids(granted_scene_ids)
segment_ids = [
    segment.get("scene_id")
    for segment in segments
    if isinstance(segment, dict) and isinstance(segment.get("scene_id"), str) and segment.get("scene_id")
]
segment_id_set = set(segment_ids)
segment_id_counts = {
    scene_id: segment_ids.count(scene_id)
    for scene_id in segment_id_set
}
scope_valid = True

if len(expected_ids) != len(expected_id_set) or selected_object_ids != expected_id_set:
    fail(
        "selected_scene_contract_mismatch",
        selected_scene_ids=expected_ids,
        selected_object_ids=sorted(selected_object_ids),
    )
    retry_scene_ids.update(expected_id_set)
    scope_valid = False
if len(granted_ids) != len(set(granted_ids)) or set(granted_ids) != expected_id_set:
    fail(
        "capture_grant_scene_ids_mismatch",
        expected=expected_ids,
        actual=granted_ids,
    )
    invalid_scene_ids.update(set(granted_ids) - expected_id_set)
    retry_scene_ids.update(expected_id_set)
    scope_valid = False
if len(segment_ids) != len(segments) or len(segment_ids) != len(segment_id_set) or segment_id_set != expected_id_set:
    fail(
        "collection_scene_ids_mismatch",
        expected=expected_ids,
        actual=segment_ids,
    )
    invalid_scene_ids.update(segment_id_set - expected_id_set)
    retry_scene_ids.update(expected_id_set - segment_id_set)
    retry_scene_ids.update(expected_id_set.intersection(segment_id_set))
    for scene_id in segment_id_set - expected_id_set:
        rejected_segment_ids.add(scene_id)
if collection.get("collection_complete") is not True:
    fail("collection_not_complete")
    requested_retry_ids = set(string_ids(collection.get("retry_segment_ids", [])))
    retry_scene_ids.update(requested_retry_ids.intersection(expected_id_set))
    blocker = collection.get("blocker")
    if isinstance(blocker, dict) and blocker.get("scene_id") in expected_id_set:
        retry_scene_ids.add(blocker["scene_id"])

segment_by_id = {
    segment.get("scene_id"): segment
    for segment in segments
    if isinstance(segment, dict) and isinstance(segment.get("scene_id"), str)
}
segment_contract_valid = {}
segment_completed = {}
for scene_id in expected_ids:
    approved_scene = selected_scene_by_id.get(scene_id)
    segment = segment_by_id.get(scene_id)
    if not scope_valid or not isinstance(approved_scene, dict) or not isinstance(segment, dict):
        segment_contract_valid[scene_id] = False
        retry_scene_ids.add(scene_id)
        continue
    if segment_id_counts.get(scene_id) != 1:
        fail("duplicate_segment_id", scene_id=scene_id)
        rejected_segment_ids.add(scene_id)
        retry_scene_ids.add(scene_id)
        segment_contract_valid[scene_id] = False
        continue
    mismatched_fields = [
        field for field in locked_scene_fields
        if canonical(segment.get(field)) != canonical(approved_scene.get(field))
    ]
    if mismatched_fields:
        fail(
            "segment_contract_mismatch",
            scene_id=scene_id,
            fields=mismatched_fields,
        )
        rejected_segment_ids.add(scene_id)
        retry_scene_ids.add(scene_id)
    if segment.get("status") != "completed":
        fail("segment_not_completed", scene_id=scene_id)
        retry_scene_ids.add(scene_id)
    segment_contract_valid[scene_id] = not mismatched_fields
    segment_completed[scene_id] = segment.get("status") == "completed"

asset_kinds = {"recording": "video", "screenshot": "image"}
artifact_id_counts = {}
for artifact in artifacts:
    if isinstance(artifact, dict) and isinstance(artifact.get("artifact_id"), str):
        artifact_id = artifact["artifact_id"]
        artifact_id_counts[artifact_id] = artifact_id_counts.get(artifact_id, 0) + 1


def validate_artifact(artifact):
    artifact_id = artifact.get("artifact_id") if isinstance(artifact, dict) else None

    def reject(code, **details):
        if isinstance(artifact_id, str) and artifact_id:
            rejected_artifact_ids.add(artifact_id)
            invalid_artifact_ids.add(artifact_id)
        fail(code, artifact_id=artifact_id, **details)

    if not isinstance(artifact, dict) or not isinstance(artifact_id, str) or not artifact_id:
        reject("artifact_id_missing")
        return False, None, None
    if artifact_id_counts.get(artifact_id, 1) > 1:
        reject("duplicate_artifact_id")
        return False, None, None
    if artifact.get("status") != "ready":
        reject("artifact_not_ready", status=artifact.get("status"))
        return False, None, None
    scene_ids = artifact_scene_ids(artifact)
    approved_scene_ids = scene_ids.intersection(expected_id_set)
    if len(scene_ids) != 1 or len(approved_scene_ids) != 1:
        invalid_scene_ids.update(scene_ids - expected_id_set)
        reject("artifact_scene_mismatch", scene_ids=sorted(scene_ids))
        return False, None, None
    scene_id = next(iter(approved_scene_ids))
    declared_provenance_scene_ids = provenance_scene_ids(artifact)
    if artifact.get("scene_id") != scene_id or (
        declared_provenance_scene_ids
        and declared_provenance_scene_ids != {scene_id}
    ):
        reject("artifact_scene_mismatch", scene_ids=sorted(scene_ids))
        retry_scene_ids.add(scene_id)
        return False, scene_id, None
    if not segment_contract_valid.get(scene_id):
        reject("artifact_segment_contract_invalid", scene_id=scene_id)
        retry_scene_ids.add(scene_id)
        return False, scene_id, None
    approved_scene = selected_scene_by_id[scene_id]
    required_asset_types = approved_scene.get("required_asset_types")
    required_asset_types = required_asset_types if isinstance(required_asset_types, list) else []
    expected_kind_by_type = {
        asset_type: asset_kinds.get(asset_type)
        for asset_type in required_asset_types
    }
    kind = artifact.get("kind")
    matching_asset_types = [
        asset_type for asset_type, expected_kind in expected_kind_by_type.items()
        if expected_kind == kind
    ]
    if len(matching_asset_types) != 1:
        reject("artifact_asset_type_mismatch", scene_id=scene_id, kind=kind)
        retry_scene_ids.add(scene_id)
        return False, scene_id, kind
    asset_type = matching_asset_types[0]
    expected_evidence = [
        item for item in approved_scene.get("acceptance_evidence", [])
        if isinstance(item, dict) and item.get("required_asset_type") == asset_type
    ]
    provenance = artifact.get("provenance")
    provenance = provenance if isinstance(provenance, dict) else {}
    artifact_evidence = provenance.get("acceptance_evidence")
    artifact_evidence = artifact_evidence if isinstance(artifact_evidence, list) else []
    expected_source_urls = {
        value for value in (
            approved_scene.get("target_page"),
            (
                approved_scene.get("expected_visual_state", {}).get("url")
                if isinstance(approved_scene.get("expected_visual_state"), dict)
                else None
            ),
        )
        if isinstance(value, str) and value
    }
    verification_field = (
        "acceptance_verified" if kind == "video" else "bitmap_acceptance_verified"
    )
    verified_capture_receipt = (
        artifact.get("source") == "browser_capture"
        and provenance.get(verification_field) is True
        and provenance.get("source_url") in expected_source_urls
    )
    evidence_matches = bool(expected_evidence) and (
        canonical(artifact_evidence) == canonical(expected_evidence)
        if artifact_evidence
        else verified_capture_receipt
    )
    if not evidence_matches:
        reject(
            "artifact_acceptance_evidence_mismatch",
            scene_id=scene_id,
            required_asset_type=asset_type,
        )
        retry_scene_ids.add(scene_id)
        return False, scene_id, kind
    provenance["scene_id"] = scene_id
    provenance["acceptance_evidence"] = expected_evidence
    artifact["provenance"] = provenance
    segment = segment_by_id[scene_id]
    segment_artifact_ids = string_ids(segment.get("artifact_ids", []))
    if artifact_id not in segment_artifact_ids:
        reject("segment_artifact_reference_missing", scene_id=scene_id)
        retry_scene_ids.add(scene_id)
        return False, scene_id, kind
    return True, scene_id, kind


validated_artifacts_by_scene = {}
for artifact in artifacts:
    artifact_valid, scene_id, kind = validate_artifact(artifact)
    if artifact_valid:
        validated_artifacts.append(artifact)
        validated_artifacts_by_scene.setdefault(scene_id, {}).setdefault(kind, []).append(artifact)

for scene_id in expected_ids:
    if not segment_contract_valid.get(scene_id):
        continue
    scene = selected_scene_by_id[scene_id]
    segment = segment_by_id[scene_id]
    required_kinds = [asset_kinds.get(item) for item in scene.get("required_asset_types", [])]
    scene_artifacts = validated_artifacts_by_scene.get(scene_id, {})
    missing_kinds = [kind for kind in required_kinds if kind not in scene_artifacts]
    for kind in missing_kinds:
        asset_type = next(
            (asset_type for asset_type, expected_kind in asset_kinds.items() if expected_kind == kind),
            None,
        )
        fail(
            "required_asset_missing",
            scene_id=scene_id,
            required_asset_type=asset_type,
            expected_kind=kind,
        )
    validated_ids = {
        artifact["artifact_id"]
        for artifacts_for_kind in scene_artifacts.values()
        for artifact in artifacts_for_kind
    }
    segment_artifact_ids = set(string_ids(segment.get("artifact_ids", [])))
    if segment_artifact_ids != validated_ids:
        fail(
            "segment_artifact_ids_mismatch",
            scene_id=scene_id,
            expected=sorted(validated_ids),
            actual=sorted(segment_artifact_ids),
        )
    if segment_artifact_ids != validated_ids or (
        segment_completed.get(scene_id) and missing_kinds
    ):
        rejected_segment_ids.add(scene_id)
        retry_scene_ids.add(scene_id)
        continue
    validated_segments.append(segment)

preserved_receipts = []
seen_receipts = set()
collector_blocker = collection.get("blocker")
collector_blocker = collector_blocker if isinstance(collector_blocker, dict) else {}
for artifact in validated_artifacts:
    seen_receipts.add(artifact["artifact_id"])
    preserved_receipts.append(artifact)
collection_artifact_ids = {
    artifact.get("artifact_id") for artifact in artifacts
    if isinstance(artifact, dict) and isinstance(artifact.get("artifact_id"), str)
}
blocker_receipts = collector_blocker.get("preserved_receipts")
blocker_receipts = blocker_receipts if isinstance(blocker_receipts, list) else []
for receipt in blocker_receipts:
    receipt_id = receipt.get("artifact_id") if isinstance(receipt, dict) else None
    if receipt_id in collection_artifact_ids or receipt_id in seen_receipts:
        continue
    receipt_valid, _scene_id, _kind = validate_artifact(receipt)
    if receipt_valid:
        validated_artifacts.append(receipt)
        seen_receipts.add(receipt_id)
        preserved_receipts.append(receipt)

validated_receipt_by_id = {
    receipt["artifact_id"]: receipt
    for receipt in preserved_receipts
    if isinstance(receipt, dict) and isinstance(receipt.get("artifact_id"), str)
}
sanitized_segments = []
for segment in validated_segments:
    sanitized_segment = json.loads(canonical(segment))
    segment_blocker = sanitized_segment.get("blocker")
    if isinstance(segment_blocker, dict):
        filtered_receipts = []
        for receipt in segment_blocker.get("preserved_receipts", []):
            receipt_id = receipt.get("artifact_id") if isinstance(receipt, dict) else None
            validated_receipt = validated_receipt_by_id.get(receipt_id)
            if validated_receipt is not None and canonical(receipt) == canonical(validated_receipt):
                filtered_receipts.append(validated_receipt)
        segment_blocker["preserved_receipts"] = filtered_receipts
    sanitized_segments.append(sanitized_segment)
validated_segments = sanitized_segments

ordered_retry_ids = [scene_id for scene_id in expected_ids if scene_id in retry_scene_ids]
for scene_id in sorted(retry_scene_ids - expected_id_set):
    ordered_retry_ids.append(scene_id)
if errors and not ordered_retry_ids:
    ordered_retry_ids = list(expected_ids)
blocker = None
if errors:
    blocker_scene_id = (
        ordered_retry_ids[0]
        if ordered_retry_ids
        else collector_blocker.get("scene_id")
        if isinstance(collector_blocker.get("scene_id"), str)
        else expected_ids[0]
        if expected_ids
        else "collection"
    )
    observed_state = {
        "validator_errors": errors,
        "rejected_segment_ids": sorted(rejected_segment_ids),
        "rejected_artifact_ids": sorted(rejected_artifact_ids),
        "collection_complete": collection.get("collection_complete"),
    }
    if isinstance(collector_blocker.get("observed_state"), dict):
        observed_state["collector_observed_state"] = collector_blocker["observed_state"]
    required_change = collector_blocker.get("required_change")
    if not isinstance(required_change, str) or not required_change:
        required_change = (
            "Correct the collection input or recapture only the listed scene IDs, "
            "then retry collection."
        )
    blocker = {
        "scene_id": blocker_scene_id,
        "observed_state": observed_state,
        "required_change": required_change,
        "preserved_receipts": preserved_receipts,
    }

result = {
    "valid": not errors,
    "errors": errors,
    "invalid_scene_ids": sorted(invalid_scene_ids),
    "invalid_artifact_ids": sorted(invalid_artifact_ids),
    "rejected_segment_ids": sorted(rejected_segment_ids),
    "rejected_artifact_ids": sorted(rejected_artifact_ids),
    "retry_segment_ids": ordered_retry_ids if errors else [],
    "validated_segments": validated_segments,
    "validated_artifacts": validated_artifacts,
    "preserved_receipts": preserved_receipts,
    "blocker": blocker,
}
print(json.dumps(result, sort_keys=True))
'''


QUALITY_CONTRACT_VALIDATOR_CODE = r'''import json

request = inputs.get("request") if isinstance(inputs.get("request"), dict) else {}
plan = inputs.get("plan") if isinstance(inputs.get("plan"), dict) else {}
quality = inputs.get("quality_result") if isinstance(inputs.get("quality_result"), dict) else {}
artifacts = inputs.get("artifacts") if isinstance(inputs.get("artifacts"), list) else []
timeline = inputs.get("timeline") if isinstance(inputs.get("timeline"), dict) else {}
media_probe = inputs.get("media_probe") if isinstance(inputs.get("media_probe"), dict) else {}
audio_analysis = inputs.get("audio_analysis") if isinstance(inputs.get("audio_analysis"), dict) else {}
subtitle_analysis = inputs.get("subtitle_analysis") if isinstance(inputs.get("subtitle_analysis"), dict) else {}
frame_evidence = inputs.get("frame_evidence") if isinstance(inputs.get("frame_evidence"), dict) else {}
errors = []
invalid_scene_ids = set()
invalid_evidence_ids = set()


def fail(code, **details):
    errors.append({"code": code, **details})


def artifact_scene_ids(artifact):
    scene_ids = set()
    if not isinstance(artifact, dict):
        return scene_ids
    provenance = artifact.get("provenance")
    provenance = provenance if isinstance(provenance, dict) else {}
    for value in (
        artifact.get("scene_id"),
        provenance.get("scene_id"),
        provenance.get("segment_id"),
    ):
        if isinstance(value, str) and value:
            scene_ids.add(value)
    for key in ("scene_ids", "segment_ids"):
        values = provenance.get(key)
        if isinstance(values, list):
            scene_ids.update(value for value in values if isinstance(value, str) and value)
    return scene_ids


requested = request.get("must_show") if isinstance(request.get("must_show"), list) else []
coverage = quality.get("coverage") if isinstance(quality.get("coverage"), list) else []
coverage_requirements = [
    item.get("requirement") if isinstance(item, dict) else None
    for item in coverage
]
missing_requirements = [item for item in requested if item not in coverage_requirements]
if coverage_requirements != requested:
    fail(
        "coverage_requirements_mismatch",
        expected=requested,
        actual=coverage_requirements,
    )
if quality.get("covered_must_show") != requested:
    fail("covered_must_show_mismatch")
if quality.get("verdict") != "machine_pass":
    fail("verdict_not_machine_pass", verdict=quality.get("verdict"))

declared_scene_ids = {
    scene_id for scene_id in plan.get("scene_ids", [])
    if isinstance(scene_id, str) and scene_id
}
scene_object_ids = {
    scene.get("scene_id") for scene in plan.get("scenes", [])
    if isinstance(scene, dict) and isinstance(scene.get("scene_id"), str)
}
valid_scene_ids = declared_scene_ids & scene_object_ids
approved_scene_ids_by_requirement = {}
for item in plan.get("must_show_coverage", []):
    if not isinstance(item, dict) or not isinstance(item.get("requirement"), str):
        continue
    approved_scene_ids_by_requirement[item["requirement"]] = {
        scene_id for scene_id in item.get("scene_ids", [])
        if isinstance(scene_id, str) and scene_id
    }
artifact_by_id = {
    artifact.get("artifact_id"): artifact
    for artifact in artifacts
    if isinstance(artifact, dict) and isinstance(artifact.get("artifact_id"), str)
}
frames_for_coverage = frame_evidence.get("frames")
frames_for_coverage = frames_for_coverage if isinstance(frames_for_coverage, list) else []
intervals_for_coverage = timeline.get("visual_scene_intervals")
intervals_for_coverage = intervals_for_coverage if isinstance(intervals_for_coverage, list) else []
frame_scene_ids_by_document_id = {}
for frame in frames_for_coverage:
    if not isinstance(frame, dict):
        continue
    document_id = frame.get("document_id")
    timestamp = frame.get("timestamp_seconds")
    if (
        not isinstance(document_id, str)
        or not document_id
        or not isinstance(timestamp, (int, float))
        or isinstance(timestamp, bool)
    ):
        continue
    mapped_scene_ids = set()
    for interval in intervals_for_coverage:
        if not isinstance(interval, dict):
            continue
        scene_id = interval.get("scene_id")
        start = interval.get("visual_start_seconds")
        end = interval.get("visual_end_seconds")
        if (
            scene_id in valid_scene_ids
            and isinstance(start, (int, float))
            and not isinstance(start, bool)
            and isinstance(end, (int, float))
            and not isinstance(end, bool)
            and start <= timestamp <= end
        ):
            mapped_scene_ids.add(scene_id)
    if mapped_scene_ids:
        frame_scene_ids_by_document_id[document_id] = mapped_scene_ids


def resolved_evidence_scene_ids(evidence_id):
    artifact = artifact_by_id.get(evidence_id)
    if isinstance(artifact, dict) and artifact.get("status") == "ready":
        return artifact_scene_ids(artifact)
    return frame_scene_ids_by_document_id.get(evidence_id, set())

for item in coverage:
    if not isinstance(item, dict):
        fail("invalid_coverage_entry")
        continue
    requirement = item.get("requirement")
    mapped_scene_ids = item.get("scene_ids") if isinstance(item.get("scene_ids"), list) else []
    mapped_scene_id_set = set(mapped_scene_ids)
    approved_scene_ids = approved_scene_ids_by_requirement.get(requirement, set())
    evidence_ids = (
        item.get("evidence_artifact_ids")
        if isinstance(item.get("evidence_artifact_ids"), list)
        else []
    )
    if item.get("status") != "covered":
        fail("coverage_not_covered", requirement=requirement)
    if mapped_scene_id_set != approved_scene_ids:
        invalid_scene_ids.update(mapped_scene_id_set - approved_scene_ids)
        fail(
            "qa_scene_mapping_mismatch",
            requirement=requirement,
            approved_scene_ids=sorted(approved_scene_ids),
            actual_scene_ids=sorted(mapped_scene_id_set),
        )
    for scene_id in mapped_scene_ids:
        if scene_id not in valid_scene_ids:
            invalid_scene_ids.add(scene_id)
            fail("unknown_coverage_scene_id", requirement=requirement, scene_id=scene_id)
    for evidence_id in evidence_ids:
        artifact = artifact_by_id.get(evidence_id)
        frame_scene_ids = frame_scene_ids_by_document_id.get(evidence_id, set())
        if artifact is None and not frame_scene_ids:
            invalid_evidence_ids.add(evidence_id)
            fail("missing_or_unready_evidence", requirement=requirement, evidence_id=evidence_id)
            continue
        if artifact is not None and artifact.get("status") != "ready":
            invalid_evidence_ids.add(evidence_id)
            fail("missing_or_unready_evidence", requirement=requirement, evidence_id=evidence_id)
            continue
        evidence_scene_ids = (
            artifact_scene_ids(artifact) if artifact is not None else frame_scene_ids
        )
        if not evidence_scene_ids.intersection(approved_scene_ids):
            invalid_evidence_ids.add(evidence_id)
            fail("evidence_scene_mismatch", requirement=requirement, evidence_id=evidence_id)
    for scene_id in approved_scene_ids:
        if scene_id in valid_scene_ids and not any(
            scene_id in resolved_evidence_scene_ids(evidence_id)
            for evidence_id in evidence_ids
        ):
            fail("scene_evidence_missing", requirement=requirement, scene_id=scene_id)

technical_checks = quality.get("technical_checks")
if not isinstance(technical_checks, list) or not technical_checks:
    fail("technical_checks_missing")
else:
    for check in technical_checks:
        if not isinstance(check, dict) or check.get("status") != "pass":
            fail("technical_check_failed", check_id=check.get("check_id") if isinstance(check, dict) else None)
for finding in quality.get("visual_findings", []):
    if isinstance(finding, dict) and finding.get("severity") == "blocking":
        fail("blocking_visual_finding", finding_id=finding.get("finding_id"))
if quality.get("scene_revisions"):
    fail("scene_revisions_present")

probe_report = media_probe.get("report")
probe_report = probe_report if isinstance(probe_report, dict) else {}
if media_probe.get("status") != "completed":
    fail("media_probe_failed", status=media_probe.get("status"))
if probe_report.get("decodable") is not True:
    fail("final_media_not_decodable")
if probe_report.get("has_video") is not True or probe_report.get("has_audio") is not True:
    fail(
        "final_media_streams_missing",
        has_video=probe_report.get("has_video"),
        has_audio=probe_report.get("has_audio"),
    )
if not isinstance(probe_report.get("video_stream"), dict) or not probe_report.get("video_stream"):
    fail("video_stream_probe_missing")
if not isinstance(probe_report.get("audio_stream"), dict) or not probe_report.get("audio_stream"):
    fail("audio_stream_probe_missing")

audio_findings = audio_analysis.get("findings")
if (
    audio_analysis.get("status") != "completed"
    or audio_analysis.get("verdict") != "pass"
    or audio_analysis.get("non_empty") is not True
    or not isinstance(audio_findings, list)
    or bool(audio_findings)
):
    fail(
        "audio_analysis_failed",
        status=audio_analysis.get("status"),
        verdict=audio_analysis.get("verdict"),
        non_empty=audio_analysis.get("non_empty"),
    )

subtitle_findings = subtitle_analysis.get("findings")
if (
    subtitle_analysis.get("status") != "completed"
    or subtitle_analysis.get("verdict") != "pass"
    or not isinstance(subtitle_analysis.get("cue_count"), int)
    or subtitle_analysis.get("cue_count", 0) <= 0
    or not isinstance(subtitle_findings, list)
    or bool(subtitle_findings)
):
    fail(
        "subtitle_analysis_failed",
        status=subtitle_analysis.get("status"),
        verdict=subtitle_analysis.get("verdict"),
    )
maximum_line_count = subtitle_analysis.get("maximum_line_count")
if (
    not isinstance(maximum_line_count, int)
    or isinstance(maximum_line_count, bool)
    or maximum_line_count < 1
    or maximum_line_count > 2
):
    fail("subtitle_line_limit_failed", maximum_line_count=maximum_line_count)
style_evidence = subtitle_analysis.get("style_evidence")
style_evidence = style_evidence if isinstance(style_evidence, dict) else {}
if not style_evidence:
    fail("subtitle_style_failed", reason="style_evidence_missing")
video_stream = probe_report.get("video_stream")
video_stream = video_stream if isinstance(video_stream, dict) else {}
video_width = video_stream.get("width")
video_height = video_stream.get("height")
expected_subtitle_font_size = None
expected_subtitle_outline = None
expected_subtitle_margin_v = None
if (
    isinstance(video_width, (int, float))
    and not isinstance(video_width, bool)
    and video_width > 0
    and isinstance(video_height, (int, float))
    and not isinstance(video_height, bool)
    and video_height > 0
):
    expected_subtitle_font_size = max(
        16,
        min(56, round(min(video_width, video_height) * 14 / 288)),
    )
    expected_subtitle_outline = max(
        1,
        min(2, round(min(video_width, video_height) / 540)),
    )
    expected_subtitle_margin_v = max(
        28,
        min(80, round(min(video_width, video_height) / 15)),
    )
for style_name, style in style_evidence.items():
    style = style if isinstance(style, dict) else {}
    font_size = style.get("font_size")
    margin_v = style.get("margin_v")
    style_valid = (
        isinstance(font_size, (int, float))
        and not isinstance(font_size, bool)
        and expected_subtitle_font_size is not None
        and abs(font_size - expected_subtitle_font_size) <= 2
        and style.get("outline") == expected_subtitle_outline
        and style.get("shadow") == 0
        and style.get("alignment") == 2
        and isinstance(margin_v, (int, float))
        and not isinstance(margin_v, bool)
        and expected_subtitle_margin_v is not None
        and abs(margin_v - expected_subtitle_margin_v) <= 4
        and style.get("bottom_safe") is True
    )
    if not style_valid:
        fail("subtitle_style_failed", style=style_name, evidence=style)

frames = frame_evidence.get("frames")
frames = frames if isinstance(frames, list) else []
sample_count = frame_evidence.get("sample_count")
valid_frames = [
    frame for frame in frames
    if isinstance(frame, dict)
    and isinstance(frame.get("document_id"), str)
    and frame.get("document_id")
    and isinstance(frame.get("timestamp_seconds"), (int, float))
    and not isinstance(frame.get("timestamp_seconds"), bool)
]
if (
    frame_evidence.get("status") != "completed"
    or not isinstance(sample_count, int)
    or isinstance(sample_count, bool)
    or sample_count <= 0
    or sample_count != len(frames)
    or len(valid_frames) != len(frames)
):
    fail(
        "frame_evidence_failed",
        status=frame_evidence.get("status"),
        sample_count=sample_count,
    )

visual_intervals = timeline.get("visual_scene_intervals")
visual_intervals = visual_intervals if isinstance(visual_intervals, list) else []
if not visual_intervals:
    fail("visual_scene_intervals_missing")
for interval in visual_intervals:
    if not isinstance(interval, dict):
        fail("invalid_visual_scene_interval")
        continue
    scene_id = interval.get("scene_id")
    start = interval.get("visual_start_seconds")
    end = interval.get("visual_end_seconds")
    if (
        not isinstance(scene_id, str)
        or not scene_id
        or not isinstance(start, (int, float))
        or isinstance(start, bool)
        or not isinstance(end, (int, float))
        or isinstance(end, bool)
        or end <= start
    ):
        fail("invalid_visual_scene_interval", scene_id=scene_id)
        continue
    if scene_id not in valid_scene_ids:
        invalid_scene_ids.add(scene_id)
        fail("unknown_visual_scene_interval", scene_id=scene_id)
    if not any(
        start <= frame["timestamp_seconds"] <= end
        for frame in valid_frames
    ):
        invalid_scene_ids.add(scene_id)
        fail("scene_frame_evidence_missing", scene_id=scene_id)

metrics = timeline.get("alignment_metrics")
metrics = metrics if isinstance(metrics, dict) else {}
scene_coverage = metrics.get("scene_coverage")
scene_coverage = scene_coverage if isinstance(scene_coverage, dict) else {}
timing_sources = metrics.get("timing_sources")
if not isinstance(metrics.get("similarity"), (int, float)) or metrics.get("similarity") < 0.90:
    fail("alignment_similarity_failed")
if not isinstance(metrics.get("coverage"), (int, float)) or metrics.get("coverage") < 0.95:
    fail("alignment_coverage_failed")
if metrics.get("measured_timestamps") is not True:
    fail("measured_timestamps_missing")
if not isinstance(timing_sources, list) or not timing_sources or not all(
    isinstance(source, str) and "measured" in source for source in timing_sources
):
    fail("measured_timing_sources_missing")
if not isinstance(metrics.get("transcription_model"), str) or not metrics.get("transcription_model"):
    fail("transcription_model_missing")
if not isinstance(scene_coverage.get("coverage"), (int, float)) or scene_coverage.get("coverage") < 0.95:
    fail("scene_coverage_failed")
for key in ("missing_scene_ids", "missing_interval_scene_ids", "unmapped_scene_ids"):
    if scene_coverage.get(key):
        fail("scene_alignment_incomplete", field=key, scene_ids=scene_coverage.get(key))

result = {
    "valid": not errors,
    "errors": errors,
    "missing_requirements": missing_requirements,
    "invalid_scene_ids": sorted(invalid_scene_ids),
    "invalid_evidence_ids": sorted(invalid_evidence_ids),
    "retry_from_step_id": (
        "produce_video"
        if quality.get("verdict") in {"machine_pass", "repairable_technical"}
        else "review_quality"
    ),
}
print(json.dumps(result, sort_keys=True))
'''


PRODUCTION_CONTRACT_VALIDATOR_CODE = r'''import json

production = inputs.get("production_result") if isinstance(inputs.get("production_result"), dict) else {}
plan = inputs.get("plan") if isinstance(inputs.get("plan"), dict) else {}
errors = []
invalid_scene_ids = set()
invalid_evidence_ids = set()


def fail(code, **details):
    errors.append({"code": code, **details})


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def close(actual, expected):
    return number(actual) and abs(float(actual) - float(expected)) <= 1e-6


declared_scene_ids = {
    scene_id for scene_id in plan.get("scene_ids", [])
    if isinstance(scene_id, str) and scene_id
}
scene_object_ids = {
    scene.get("scene_id") for scene in plan.get("scenes", [])
    if isinstance(scene, dict) and isinstance(scene.get("scene_id"), str)
}
valid_scene_ids = declared_scene_ids & scene_object_ids
status = production.get("status")
timeline = production.get("timeline")
timeline = timeline if isinstance(timeline, dict) else {}
computed_intervals = {}

if status in {"ready", "retake_required"}:
    intervals = timeline.get("visual_scene_intervals")
    intervals = intervals if isinstance(intervals, list) else []
    if not intervals:
        fail("visual_scene_intervals_missing")
    for interval in intervals:
        if not isinstance(interval, dict):
            fail("visual_scene_interval_invalid")
            continue
        scene_id = interval.get("scene_id")
        if scene_id in computed_intervals:
            fail("duplicate_visual_scene_interval", scene_id=scene_id)
            continue
        if scene_id not in valid_scene_ids:
            invalid_scene_ids.add(scene_id)
        visual_start = interval.get("visual_start_seconds")
        visual_end = interval.get("visual_end_seconds")
        narration_start = interval.get("narration_start_seconds")
        narration_end = interval.get("narration_end_seconds")
        if not all(number(value) for value in (visual_start, visual_end, narration_start, narration_end)):
            fail("visual_scene_interval_invalid", scene_id=scene_id)
            continue
        narration_span = narration_end - narration_start
        visual_duration = visual_end - visual_start
        if narration_span <= 0 or visual_duration <= 0:
            fail("visual_scene_interval_order_invalid", scene_id=scene_id)
            continue
        if status == "ready" and (
            narration_start < visual_start or narration_end > visual_end
        ):
            fail(
                "narration_outside_visual_scene_interval",
                scene_id=scene_id,
                visual_start_seconds=visual_start,
                visual_end_seconds=visual_end,
                narration_start_seconds=narration_start,
                narration_end_seconds=narration_end,
            )
        deficit = max(0.0, narration_span - visual_duration)
        allowed = min(0.75, 0.10 * narration_span)
        computed_intervals[scene_id] = {
            "required_narration_span_seconds": narration_span,
            "available_visual_duration_seconds": visual_duration,
            "deficit_seconds": deficit,
            "allowed_deficit_seconds": allowed,
            "exceeds_threshold": deficit > allowed,
        }
    if set(computed_intervals) != valid_scene_ids:
        invalid_scene_ids.update(set(computed_intervals) - valid_scene_ids)
        fail(
            "visual_scene_interval_ids_mismatch",
            expected=sorted(valid_scene_ids),
            actual=sorted(
                scene_id for scene_id in computed_intervals
                if isinstance(scene_id, str)
            ),
        )

if status == "ready":
    if production.get("deficient_scene_ids") or production.get("retry_segment_ids") or production.get("deficit_evidence"):
        fail("ready_result_has_scene_deficits")
    if production.get("blocker") is not None:
        fail("ready_result_has_blocker")
    artifact_ids = {
        artifact.get("artifact_id") for artifact in production.get("artifacts", [])
        if isinstance(artifact, dict) and isinstance(artifact.get("artifact_id"), str)
    }
    for field, kind in (("final_video", "video"), ("narration", "audio"), ("subtitles", "subtitle")):
        artifact = timeline.get(field)
        if not isinstance(artifact, dict) or artifact.get("kind") != kind or artifact.get("status") != "ready":
            fail("ready_artifact_invalid", field=field, expected_kind=kind)
            continue
        if artifact.get("artifact_id") not in artifact_ids:
            invalid_evidence_ids.add(artifact.get("artifact_id"))
            fail("ready_artifact_not_registered", field=field, artifact_id=artifact.get("artifact_id"))
    subtitles = timeline.get("subtitles")
    subtitles = subtitles if isinstance(subtitles, dict) else {}
    subtitle_path = subtitles.get("fs_path")
    subtitle_mime = str(subtitles.get("mime_type") or "").lower()
    if (
        not isinstance(subtitle_path, str)
        or not subtitle_path.lower().endswith(".ass")
        or subtitle_mime not in {"text/x-ass", "application/x-ass", "text/ass"}
    ):
        fail(
            "ready_subtitles_must_be_ass",
            fs_path=subtitle_path,
            mime_type=subtitles.get("mime_type"),
        )
    boundaries = timeline.get("scene_boundaries")
    if not isinstance(boundaries, list) or not boundaries or not all(number(item) and item >= 0 for item in boundaries):
        fail("scene_boundaries_invalid")
    ready_deficits = {
        scene_id: values for scene_id, values in computed_intervals.items()
        if values["exceeds_threshold"]
    }
    if ready_deficits:
        fail("ready_visual_deficit", deficient_intervals=ready_deficits)
    metrics = timeline.get("alignment_metrics")
    metrics = metrics if isinstance(metrics, dict) else {}
    scene_coverage = metrics.get("scene_coverage")
    scene_coverage = scene_coverage if isinstance(scene_coverage, dict) else {}
    timing_sources = metrics.get("timing_sources")
    if not number(metrics.get("similarity")) or metrics.get("similarity") < 0.90:
        fail("alignment_similarity_failed")
    if not number(metrics.get("coverage")) or metrics.get("coverage") < 0.95:
        fail("alignment_coverage_failed")
    if metrics.get("measured_timestamps") is not True:
        fail("measured_timestamps_missing")
    if not isinstance(timing_sources, list) or not timing_sources or not all(
        isinstance(source, str) and "measured" in source for source in timing_sources
    ):
        fail("measured_timing_sources_missing")
    if not isinstance(metrics.get("transcription_model"), str) or not metrics.get("transcription_model"):
        fail("transcription_model_missing")
    if not number(scene_coverage.get("coverage")) or scene_coverage.get("coverage") < 0.95:
        fail("scene_coverage_failed")
    for key in ("missing_scene_ids", "missing_interval_scene_ids", "unmapped_scene_ids"):
        if scene_coverage.get(key):
            fail("scene_alignment_incomplete", field=key, scene_ids=scene_coverage.get(key))
    covered_scene_ids = set(scene_coverage.get("covered_scene_ids", []))
    if covered_scene_ids != valid_scene_ids:
        invalid_scene_ids.update(covered_scene_ids - valid_scene_ids)
        fail("covered_scene_ids_mismatch")
elif status == "retake_required":
    if production.get("blocker") is not None:
        fail("retake_result_has_blocker")
    evidence = production.get("deficit_evidence")
    evidence = evidence if isinstance(evidence, list) else []
    evidence_by_scene_id = {}
    for item in evidence:
        if not isinstance(item, dict):
            fail("deficit_evidence_invalid")
            continue
        scene_id = item.get("scene_id")
        if scene_id in evidence_by_scene_id:
            fail("duplicate_deficit_scene_id", scene_id=scene_id)
            continue
        evidence_by_scene_id[scene_id] = item
        if scene_id not in valid_scene_ids:
            invalid_scene_ids.add(scene_id)
    exceeding_scene_ids = {
        scene_id for scene_id, values in computed_intervals.items()
        if values["exceeds_threshold"]
    }
    if set(evidence_by_scene_id) != exceeding_scene_ids:
        invalid_scene_ids.update(set(evidence_by_scene_id) - valid_scene_ids)
        fail(
            "retake_evidence_scene_ids_mismatch",
            expected=sorted(exceeding_scene_ids),
            actual=sorted(
                scene_id for scene_id in evidence_by_scene_id
                if isinstance(scene_id, str)
            ),
        )
    for scene_id in exceeding_scene_ids & set(evidence_by_scene_id):
        item = evidence_by_scene_id[scene_id]
        computed = computed_intervals[scene_id]
        mismatched = False
        for field in (
            "required_narration_span_seconds",
            "available_visual_duration_seconds",
            "deficit_seconds",
            "allowed_deficit_seconds",
        ):
            if not close(item.get(field), computed[field]):
                mismatched = True
                fail(
                    "allowed_deficit_mismatch" if field == "allowed_deficit_seconds" else "deficit_evidence_value_mismatch",
                    scene_id=scene_id,
                    field=field,
                    expected=computed[field],
                    actual=item.get(field),
                )
        if item.get("exceeds_threshold") is not computed["exceeds_threshold"]:
            mismatched = True
            fail(
                "exceeds_threshold_mismatch",
                scene_id=scene_id,
                expected=computed["exceeds_threshold"],
            )
        if mismatched:
            fail("retake_evidence_mismatch", scene_id=scene_id)
    deficient_scene_ids = set(production.get("deficient_scene_ids", []))
    retry_segment_ids = set(production.get("retry_segment_ids", []))
    for scene_id in deficient_scene_ids | retry_segment_ids:
        if scene_id not in valid_scene_ids:
            invalid_scene_ids.add(scene_id)
    if not exceeding_scene_ids or deficient_scene_ids != exceeding_scene_ids or retry_segment_ids != exceeding_scene_ids:
        fail(
            "retake_scene_ids_mismatch",
            expected=sorted(exceeding_scene_ids),
            deficient_scene_ids=sorted(deficient_scene_ids),
            retry_segment_ids=sorted(retry_segment_ids),
        )
    if not isinstance(production.get("required_change"), str) or not production.get("required_change"):
        fail("retake_required_change_missing")
elif status == "blocked":
    if production.get("deficient_scene_ids") or production.get("retry_segment_ids") or production.get("deficit_evidence"):
        fail("blocked_result_has_fabricated_deficits")
    blocker = production.get("blocker")
    blocker = blocker if isinstance(blocker, dict) else {}
    for key in ("blocker_type", "required_change"):
        if not isinstance(blocker.get(key), str) or not blocker.get(key):
            fail("blocked_field_missing", field=key)
    if not isinstance(blocker.get("observed_problem"), dict) or not blocker.get("observed_problem"):
        fail("blocked_field_missing", field="observed_problem")
    if not isinstance(blocker.get("preserved_receipts"), list):
        fail("blocked_field_missing", field="preserved_receipts")
else:
    fail("unknown_production_status", status=status)

result = {
    "valid": not errors,
    "errors": errors,
    "missing_requirements": [],
    "invalid_scene_ids": sorted(item for item in invalid_scene_ids if isinstance(item, str)),
    "invalid_evidence_ids": sorted(item for item in invalid_evidence_ids if isinstance(item, str)),
}
print(json.dumps(result, sort_keys=True))
'''


REVISION_PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "semantic_change": {"type": "boolean"},
        "post_only": {"type": "boolean"},
        "updated_plan": PRODUCT_VIDEO_PLAN_SCHEMA,
        "selected_scene_ids": {
            "type": "array",
            "uniqueItems": True,
            "items": {"type": "string", "minLength": 1},
        },
        "selected_scenes": {"type": "array", "items": SCENE_PLAN_SCHEMA},
        "revision_entry": {
            "type": "object",
            "properties": {
                "revision_id": {"type": "string", "minLength": 1},
            },
            "required": ["revision_id"],
        },
    },
    "required": [
        "semantic_change",
        "post_only",
        "updated_plan",
        "selected_scene_ids",
        "selected_scenes",
        "revision_entry",
    ],
    "additionalProperties": False,
}


CAPTURE_GRANT_SCOPE_SCHEMA = {
    "type": "object",
    "properties": {
        "approved_plan_version": {"type": "integer", "minimum": 1},
        "scene_ids": {"type": "array", "minItems": 1, "items": {"type": "string"}},
        "allowed_actions": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "string",
                "enum": [
                    "start_tab_recording",
                    "click_element",
                    "fill_or_select",
                    "click_point",
                    "type_text",
                    "press_key",
                    "keyboard",
                    "upload",
                    "computer",
                    "inject_script",
                ],
            },
        },
    },
    "required": ["approved_plan_version", "scene_ids", "allowed_actions"],
    "additionalProperties": False,
}


def initial_product_video_project(request: dict[str, Any]) -> dict[str, Any]:
    return {
        "request": deepcopy(request),
        "project_root": None,
        "ledger_path": None,
        "artifact_manifest_path": None,
        "current_phase": "request",
        "business_outcome": "in_progress",
        "product_source": {
            "start_url": request["start_url"],
            "browser_session": request["browser_session"],
        },
        "output_profile": deepcopy(request["output_profile"]),
        "discovered_journey": None,
        "plan": None,
        "approved_plan_version": None,
        "capture_grant_id": None,
        "scenes": [],
        "segments": [],
        "artifacts": [],
        "checkpoints": {},
        "retry_state": None,
        "final_artifacts": [],
        "history": [],
        "timeline": None,
        "quality_result": None,
        "operator_acceptance": None,
        "revision_history": [],
        "blockers": [],
    }


RECOMMENDED_MCP = ["chrome", "chrome_knowledge_local"]
RECOMMENDED_SKILLS = [
    "product_experience_mapper",
    "content_angle_planner",
    "script_storyboard_planner",
    "screen_asset_collector",
    "video_post_producer",
    "video_quality_reviewer",
]

EXPLORER_TOOLS = ("read_file", "manor", "invoke_skill")
DISCOVERY_BROWSER_DISCIPLINE = (
    "Before declaring any must-show label missing, call read_page with filter set to that item's "
    "exact distinctive label on the current page state. Treat a matching filtered result as observed "
    "evidence even when an earlier compact page summary omitted it. Do not inspect adjacent or unrelated "
    "controls merely because they are visible; every opened control must directly advance a named "
    "must-show requirement. Do not use computer, coordinates, or screenshot fallback during discovery. "
    "For each reversible click_element call, use a timeout of at most 10 seconds; after one stale-ref retry "
    "with a fresh filtered read, report the exact blocker instead of trying another fallback. "
)
PLANNER_TOOLS = ("read_file", "generate_file", "manor")
COLLECTOR_TOOLS = ("read_file", "generate_file", "manor", "invoke_skill")
PRODUCER_TOOLS = (
    "read_file",
    "generate_file",
    "merge_videos",
    "normalize_audio_loudness",
    "align_subtitles",
    "compose_video_timeline",
    "still_to_video",
    "manor",
)
QUALITY_EVIDENCE_TOOLS = (
    "probe_media",
    "render_frame_samples",
    "analyze_audio",
    "validate_subtitles",
)
QUALITY_TOOLS = (
    "read_file",
    "generate_file",
)


@dataclass(frozen=True)
class RoleSpec:
    service_key: str
    agent_name: str
    skill_bindings: tuple[str, ...]
    mcp_bindings: tuple[str, ...]
    tool_bindings: tuple[str, ...]
    system_prompt: str


ROLE_SPECS = [
    RoleSpec(
        "product_video.exploration",
        "Product Explorer",
        ("product_experience_mapper", "chrome"),
        tuple(RECOMMENDED_MCP),
        EXPLORER_TOOLS,
        (
            "Explore only the supplied browser product and evidence. Separate observations from "
            "assumptions, use reversible navigation before approval, never record during discovery, "
            "never request credentials, and return only the requested JSON contract."
        ),
    ),
    RoleSpec(
        "product_video.planning",
        "Video Planner",
        ("content_angle_planner", "script_storyboard_planner"),
        (),
        PLANNER_TOOLS,
        (
            "Create evidence-backed product video requests, scripts, and executable scene plans. Treat "
            "request.must_show as the authoritative generated-video scope: every item maps to explicit "
            "scene_id and acceptance_evidence values in must_show_coverage, and coverage completeness is "
            "reported before approval. No unsupported product claim becomes a scene. "
            "Preserve must-not-show, language, and output profile. Never operate the browser, generate "
            "final media, or choose a model/provider."
        ),
    ),
    RoleSpec(
        "product_video.capture",
        "Scene Collector",
        ("screen_asset_collector", "chrome"),
        tuple(RECOMMENDED_MCP),
        COLLECTOR_TOOLS,
        (
            "Capture only the approved scene and grant scope by executing ordered_actions on target_page "
            "and observing expected_visual_state. Return required_asset_types plus acceptance_evidence, "
            "or stop with the structured scene blocker and preserved receipts. Never use unrelated proxy "
            "footage, publish, or synthesize product UI."
        ),
    ),
    RoleSpec(
        "product_video.production",
        "Video Producer",
        ("video_post_producer",),
        (),
        PRODUCER_TOOLS,
        (
            "Produce a restartable picture master, one continuous final narration take from the plan's "
            "canonical_narration, measured semantic subtitles, explicit visual scene intervals, timeline, "
            "and final MP4 from ready project artifacts. Reuse completed checkpoints and never capture, "
            "stitch per-scene TTS, proportionally scale cues, change product claims, or publish."
        ),
    ),
    RoleSpec(
        "product_video.quality",
        "Quality Reviewer",
        ("video_quality_reviewer",),
        (),
        QUALITY_TOOLS,
        (
            "Classify deterministic final audio/video probes, must-show scene evidence, sampled frames, "
            "privacy, subtitle style, and measured cue-to-scene sync into machine_pass, "
            "repairable_technical, or revision_required. Set coverage_complete, evidence_complete, and "
            "measured_sync_passed from actual evidence; machine_pass requires all three and every must-show "
            "coverage entry. Machine pass never replaces final operator playback and acceptance."
        ),
    ),
]


USER_WORKFLOW_SLUGS = [
    "create-product-video-v1",
    "plan-product-video-v1",
    "revise-product-video-v1",
]
INTERNAL_WORKFLOW_SLUGS = [
]
WORKFLOW_SLUGS = [*USER_WORKFLOW_SLUGS, *INTERNAL_WORKFLOW_SLUGS]
MANAGED_WORKFLOW_TAG = "product-video-studio-managed"
WORKFLOW_TAGS = ["media", "video", "product", MANAGED_WORKFLOW_TAG]


def _agent_step(
    step_id: str,
    name: str,
    *,
    service_key: str,
    prompt: str,
    output_var: str,
    output_schema: dict[str, Any],
    tools: tuple[str, ...] = (),
    skill: str | None = None,
    forced_tool_calls: list[dict[str, Any]] | None = None,
    next_steps: list[str] | None = None,
    timeout: int = 900,
) -> dict[str, Any]:
    config: dict[str, Any] = {
        "service_key": service_key,
        "input": prompt,
        "output_var": output_var,
        "output_format": "json",
        "output_schema": output_schema,
        "tools": list(tools),
        "timeout": timeout,
        "chat_projection": "status",
    }
    if skill:
        config["skill"] = skill
    if forced_tool_calls:
        config["forced_tool_calls"] = deepcopy(forced_tool_calls)
    return {
        "id": step_id,
        "type": "agent",
        "name": name,
        "config": config,
        "next": list(next_steps or []),
    }


def _initial_state_bindings(request_ref: str) -> dict[str, Any]:
    return {
        "request": request_ref,
        "project_root": None,
        "ledger_path": None,
        "artifact_manifest_path": None,
        "current_phase": "request",
        "business_outcome": "in_progress",
        "product_source": {
            "start_url": "{{request.start_url}}",
            "browser_session": "{{request.browser_session}}",
        },
        "output_profile": "{{request.output_profile}}",
        "discovered_journey": None,
        "plan": None,
        "approved_plan_version": None,
        "capture_grant_id": None,
        "scenes": [],
        "segments": [],
        "artifacts": [],
        "checkpoints": {},
        "retry_state": None,
        "final_artifacts": [],
        "history": [],
        "timeline": None,
        "quality_result": None,
        "operator_acceptance": None,
        "revision_history": [],
        "blockers": [],
    }


def _plan_steps() -> list[dict[str, Any]]:
    return [
        {"id": "start", "type": "trigger", "name": "Start", "config": {"trigger_type": "mcp"}, "next": ["normalize_request"]},
        _agent_step(
            "normalize_request",
            "Normalize product video request",
            service_key="product_video.planning",
            prompt=(
                "Validate and normalize this structured product video request into exactly the supplied "
                "JSON schema. "
                "Do not invent a product, URL, audience, CTA, credentials, or product journey. Apply "
                f"this output profile only for omitted presentation fields: {DEFAULT_OUTPUT_PROFILE}. "
                "Request: {{request}}"
            ),
            output_var="request",
            output_schema=PRODUCT_VIDEO_REQUEST_SCHEMA,
            next_steps=["initialize_project"],
        ),
        {
            "id": "initialize_project",
            "type": "workflow_project",
            "name": "Initialize durable project",
            "config": {
                "operation": "create",
                "project_type": PROJECT_TYPE,
                "schema_version": PROJECT_SCHEMA_VERSION,
                "current_stage": "draft",
                "state": _initial_state_bindings("{{request}}"),
                "state_schema": "{{product_video_project_schema}}",
                "output_var": "project",
            },
            "next": ["browser_preflight"],
        },
        _agent_step(
            "browser_preflight",
            "Check browser readiness",
            service_key="product_video.exploration",
            skill="product_experience_mapper",
            tools=EXPLORER_TOOLS,
            prompt=(
                "Check whether the current paired Chrome session can safely open the supplied start URL. "
                "Use read-only discovery only. Return available, requires_login, and one blocker or null. "
                "Request: {{request}}"
            ),
            output_var="browser_preflight",
            output_schema=BROWSER_PREFLIGHT_SCHEMA,
            forced_tool_calls=[
                {
                    "name": "invoke_skill",
                    "arguments": {
                        "skill": "chrome",
                        "input": (
                            "Use Chrome read-only to verify the current paired session and open the exact "
                            "start URL {{request.start_url}}. Report observed readiness, login "
                            "requirements, and blockers without filling, submitting, or changing state."
                        ),
                    },
                }
            ],
            next_steps=["browser_ready"],
        ),
        {
            "id": "browser_ready",
            "type": "condition",
            "name": "Browser is ready",
            "config": {"expression": "browser_preflight.available == true", "chat_projection": "status"},
            "true_next": ["discover_product"],
            "false_next": ["browser_handoff"],
            "next": [],
        },
        {
            "id": "browser_handoff",
            "type": "wait",
            "name": "Connect or sign in to the product",
            "config": {
                "wait_type": "event",
                "message": "Browser readiness issue:\n{{browser_preflight.blocker}}\nFix the reported connection, login, or page-state problem, then resume planning.",
                "chat_projection": "action",
            },
            "next": ["discover_product"],
        },
        {
            "id": "discover_product",
            "type": "subworkflow",
            "name": "Discover product",
            "config": {
                "workflow_id": "discover-product-v1",
                "inputs": [
                    {"key": "project_id", "value": "{{project.project_id}}"},
                    {"key": "request", "value": "{{request}}", "type": "json"},
                ],
                "output_var": "discovery",
            },
            "next": ["plan_video"],
        },
        _agent_step(
            "plan_video",
            "Create script and scene plan",
            service_key="product_video.planning",
            tools=PLANNER_TOOLS,
            prompt=(
                "Create one evidence-backed product video plan. Every scene needs a stable scene_id, "
                "target_page, precondition, ordered_actions, expected_visual_state, canonical_narration, "
                "required_asset_types, acceptance_evidence, target_duration_seconds, dependencies, recovery, "
                "and privacy. Every request.must_show item must map to one or more explicit scene_id values and "
                "acceptance_evidence entries. Create generated video scenes only for request.must_show; never "
                "turn a generic promotion_goal claim or workflow acceptance check into another scene. No product "
                "claim may appear in canonical_narration without an evidence-backed scene. Knowledge completeness "
                "and final playback are acceptance checks, not generated video scenes. "
                "Budget every scene's canonical narration for a clear natural delivery within that scene's "
                "target_duration_seconds; shorten copy instead of assuming production can speed it up. "
                "Browser scenes may depend only on states available before post-production. Narration audio, "
                "subtitle files, final MP4 playback, machine QA, and the final operator acceptance gate are "
                "downstream workflow outputs; never put them in a scene precondition, browser action, expected "
                "visual state, required asset type, or acceptance evidence. "
                "Use only routes and controls observed in discovery; never substitute a similarly named global "
                "or sidebar control for an unverified product-specific entry point. "
                "Never add inferred claims as the core promise. Request: {{request}}. "
                "Discovery: {{discovery.discovered_journey}}. Return only JSON."
            ),
            output_var="plan",
            output_schema=PRODUCT_VIDEO_PLAN_SCHEMA,
            next_steps=["save_plan"],
        ),
        {
            "id": "save_plan",
            "type": "workflow_project",
            "name": "Save validated plan",
            "config": {
                "operation": "patch",
                "project_type": PROJECT_TYPE,
                "project_id": "{{project.project_id}}",
                "expected_revision": "{{discovery.project.revision}}",
                "patch": {"plan": "{{plan}}", "scenes": "{{plan.scenes}}"},
                "current_stage": "awaiting_plan_approval",
                "allowed_stages": PROJECT_STAGES,
                "state_schema": "{{product_video_project_schema}}",
                "output_var": "project",
            },
            "next": ["done"],
        },
        {"id": "done", "type": "end", "name": "Plan ready", "config": {"no_external_publish": True}, "next": []},
    ]


def _create_steps() -> list[dict[str, Any]]:
    return [
        {"id": "start", "type": "trigger", "name": "Start", "config": {"trigger_type": "mcp"}, "next": ["plan_product_video"]},
        {
            "id": "plan_product_video",
            "type": "subworkflow",
            "name": "Plan product video",
            "config": {
                "workflow_id": "plan-product-video-v1",
                "inputs": [{"key": "request", "value": "{{request}}", "type": "json"}],
                "output_var": "planning",
            },
            "next": ["load_planned_project"],
        },
        {
            "id": "load_planned_project",
            "type": "workflow_project",
            "name": "Load planned project",
            "config": {
                "operation": "get",
                "project_id": "{{planning.project.project_id}}",
                "project_type": PROJECT_TYPE,
                "state_schema": "{{product_video_project_schema}}",
                "output_var": "project",
            },
            "next": ["approve_plan"],
        },
        {
            "id": "approve_plan",
            "type": "wait",
            "name": "Approve product video plan",
            "config": {
                "wait_type": "approval",
                "message": "Review the promise, narration, scenes, side effects, output profile, and capture scope.",
                "review_title": "Product video plan",
                "review": "{{planning.plan}}",
                "response_variable": "plan_decision",
                "options": ["approve", "revise"],
                "approval_values": ["approve"],
                "requires_operator_approval": True,
                "chat_projection": "action",
            },
            "next": ["plan_approved"],
        },
        {
            "id": "plan_approved",
            "type": "condition",
            "name": "Plan approved",
            "config": {"expression": "plan_decision.choice == 'approve'"},
            "true_next": ["lock_approved_plan"],
            "false_next": ["load_revision_project"],
            "next": [],
        },
        {
            "id": "grant_capture",
            "type": "workflow_action_grant",
            "name": "Grant approved browser capture",
            "config": {
                "operation": "create",
                "approval_step_id": "approve_plan",
                "project_id": "{{project.project_id}}",
                "grant_type": "browser_capture",
                "scope": {
                    "approved_plan_version": "{{planning.plan.plan_version}}",
                    "scene_ids": "{{planning.plan.scene_ids}}",
                    "allowed_actions": [
                        "start_tab_recording",
                        "click_element",
                        "fill_or_select",
                        "click_point",
                        "type_text",
                        "press_key",
                        "keyboard",
                        "upload",
                        "computer",
                    ],
                },
                "scope_schema": CAPTURE_GRANT_SCOPE_SCHEMA,
                "ttl_seconds": 86400,
                "output_var": "capture_grant",
            },
            "next": ["attach_capture_grant"],
        },
        {
            "id": "lock_approved_plan",
            "type": "workflow_project",
            "name": "Lock approved plan",
            "config": {
                "operation": "patch",
                "project_id": "{{project.project_id}}",
                "expected_revision": "{{project.revision}}",
                "patch": {
                    "approved_plan_version": "{{planning.plan.plan_version}}",
                },
                "current_stage": "awaiting_plan_approval",
                "allowed_stages": PROJECT_STAGES,
                "state_schema": "{{product_video_project_schema}}",
                "output_var": "project",
            },
            "next": ["grant_capture"],
        },
        {
            "id": "attach_capture_grant",
            "type": "workflow_project",
            "name": "Attach capture grant",
            "config": {
                "operation": "patch",
                "project_id": "{{project.project_id}}",
                "expected_revision": "{{project.revision}}",
                "patch": {"capture_grant_id": "{{capture_grant.grant_id}}"},
                "current_stage": "capturing",
                "allowed_stages": PROJECT_STAGES,
                "state_schema": "{{product_video_project_schema}}",
                "output_var": "project",
            },
            "next": ["capture_scenes"],
        },
        {
            "id": "capture_scenes",
            "type": "foreach_subworkflow",
            "name": "Capture every approved scene",
            "config": {
                "workflow_id": "capture-product-video-scene-v1",
                "over": "{{planning.plan.scenes}}",
                "item_var": "scene",
                "item_key": "scene_id",
                "concurrency": 1,
                "max_attempts": 2,
                "inputs": [
                    {"key": "project_id", "value": "{{project.project_id}}"},
                    {"key": "workflow_project_id", "value": "{{project.project_id}}"},
                    {"key": "workflow_action_grant_id", "value": "{{capture_grant.grant_id}}"},
                    {"key": "approved_plan_version", "value": "{{planning.plan.plan_version}}"},
                    {"key": "workflow_scene_id", "value": "{{scene.scene_id}}"},
                ],
                "output_var": "scene_results",
            },
            "next": ["reload_captured_project"],
        },
        {
            "id": "reload_captured_project",
            "type": "workflow_project",
            "name": "Reload captured project",
            "config": {
                "operation": "get",
                "project_id": "{{project.project_id}}",
                "state_schema": "{{product_video_project_schema}}",
                "output_var": "project",
            },
            "next": ["produce_video"],
        },
        {
            "id": "produce_video",
            "type": "subworkflow",
            "name": "Produce video",
            "config": {
                "workflow_id": "produce-product-video-v1",
                "inputs": [{"key": "project_id", "value": "{{project.project_id}}"}],
                "output_var": "production",
            },
            "next": ["quality_gate"],
        },
        {
            "id": "quality_gate",
            "type": "subworkflow",
            "name": "Quality gate",
            "config": {
                "workflow_id": "quality-gate-product-video-v1",
                "inputs": [{"key": "project_id", "value": "{{project.project_id}}"}],
                "output_var": "quality_gate",
            },
            "next": ["route_quality"],
        },
        {
            "id": "route_quality",
            "type": "switch",
            "name": "Route quality result",
            "config": {
                "cases": [
                    {"expression": "quality_gate.quality_result.verdict == 'machine_pass'", "next": ["load_acceptance_project"]},
                    {"expression": "quality_gate.quality_result.verdict == 'repairable_technical'", "next": ["repair_video"]},
                ],
                "default_next": ["load_revision_project"],
            },
            "next": [],
        },
        {
            "id": "repair_video",
            "type": "subworkflow",
            "name": "Repair deterministic technical issues once",
            "config": {
                "workflow_id": "produce-product-video-v1",
                "inputs": [
                    {"key": "project_id", "value": "{{project.project_id}}"},
                    {"key": "repair_request", "value": "{{quality_gate.quality_result}}", "type": "json"},
                ],
                "output_var": "repair",
            },
            "next": ["quality_recheck"],
        },
        {
            "id": "quality_recheck",
            "type": "subworkflow",
            "name": "Recheck full quality gate",
            "config": {
                "workflow_id": "quality-gate-product-video-v1",
                "inputs": [{"key": "project_id", "value": "{{project.project_id}}"}],
                "output_var": "quality_recheck",
            },
            "next": ["repair_passed"],
        },
        {
            "id": "repair_passed",
            "type": "condition",
            "name": "Repair passed",
            "config": {"expression": "quality_recheck.quality_result.verdict == 'machine_pass'"},
            "true_next": ["load_acceptance_project"],
            "false_next": ["load_revision_project"],
            "next": [],
        },
        {
            "id": "load_acceptance_project",
            "type": "workflow_project",
            "name": "Load final project",
            "config": {
                "operation": "get",
                "project_id": "{{project.project_id}}",
                "project_type": PROJECT_TYPE,
                "state_schema": "{{product_video_project_schema}}",
                "output_var": "final_project",
            },
            "next": ["operator_acceptance"],
        },
        {
            "id": "operator_acceptance",
            "type": "wait",
            "name": "Watch and accept final video",
            "config": {
                "wait_type": "approval",
                "message": "Watch the final MP4 and either accept it or provide revision notes.",
                "response_variable": "acceptance_decision",
                "options": ["accept", "revise"],
                "approval_values": ["accept"],
                "requires_operator_approval": True,
                "chat_projection": "action",
            },
            "next": ["accepted"],
        },
        {
            "id": "accepted",
            "type": "condition",
            "name": "Final video accepted",
            "config": {"expression": "acceptance_decision.choice == 'accept'"},
            "true_next": ["mark_accepted"],
            "false_next": ["load_revision_project"],
            "next": [],
        },
        {
            "id": "mark_accepted",
            "type": "workflow_project",
            "name": "Mark project accepted",
            "config": {
                "operation": "patch",
                "project_id": "{{final_project.project_id}}",
                "expected_revision": "{{final_project.revision}}",
                "patch": {
                    "operator_acceptance": {
                        "status": "accepted",
                        "source": "explicit_operator_playback",
                    }
                },
                "current_stage": "accepted",
                "allowed_stages": PROJECT_STAGES,
                "state_schema": "{{product_video_project_schema}}",
                "output_var": "project",
            },
            "next": ["done"],
        },
        {
            "id": "load_revision_project",
            "type": "workflow_project",
            "name": "Load project for revision",
            "config": {
                "operation": "get",
                "project_id": "{{project.project_id}}",
                "project_type": PROJECT_TYPE,
                "state_schema": "{{product_video_project_schema}}",
                "output_var": "revision_project",
            },
            "next": ["mark_revision_required"],
        },
        {
            "id": "mark_revision_required",
            "type": "workflow_project",
            "name": "Mark revision required",
            "config": {
                "operation": "patch",
                "project_id": "{{revision_project.project_id}}",
                "expected_revision": "{{revision_project.revision}}",
                "patch": {"operator_acceptance": {"status": "revision_requested"}},
                "current_stage": "revision_required",
                "allowed_stages": PROJECT_STAGES,
                "state_schema": "{{product_video_project_schema}}",
                "output_var": "project",
            },
            "next": ["revision_required"],
        },
        {"id": "done", "type": "end", "name": "Accepted product video", "config": {"no_external_publish": True}, "next": []},
        {"id": "revision_required", "type": "end", "name": "Revision required", "config": {"no_external_publish": True}, "next": []},
    ]


def _discover_steps() -> list[dict[str, Any]]:
    return [
        {"id": "start", "type": "trigger", "name": "Start", "config": {"trigger_type": "internal"}, "next": ["load_project"]},
        {
            "id": "load_project",
            "type": "workflow_project",
            "name": "Load project",
            "config": {
                "operation": "get",
                "project_id": "{{project_id}}",
                "project_type": PROJECT_TYPE,
                "state_schema": "{{product_video_project_schema}}",
                "output_var": "project",
            },
            "next": ["mark_discovering"],
        },
        {
            "id": "mark_discovering",
            "type": "workflow_project",
            "name": "Mark discovery active",
            "config": {
                "operation": "patch",
                "project_id": "{{project.project_id}}",
                "expected_revision": "{{project.revision}}",
                "patch": {},
                "current_stage": "discovering",
                "allowed_stages": PROJECT_STAGES,
                "state_schema": "{{product_video_project_schema}}",
                "output_var": "project",
            },
            "next": ["explore_product"],
        },
        _agent_step(
            "explore_product",
            "Explore current product state",
            service_key="product_video.exploration",
            skill="product_experience_mapper",
            tools=EXPLORER_TOOLS,
            prompt=(
                "Explore the supplied product from its exact start URL using the current paired Chrome "
                "session. Use reversible navigation and reading only; do not record, screenshot, fill, "
                "submit, upload, or alter state. Execute the complete reversible route now instead of only "
                "recommending a route. Follow visible navigation controls to each must-show page and read the "
                "resulting state. Do not return navigation steps that you did not perform; report a gap only "
                "after the matching reversible navigation attempt is blocked or the target remains absent. "
                f"{DISCOVERY_BROWSER_DISCIPLINE}"
                "When a requested capability uses a selector, picker, or "
                "menu, open reversible option menus, enumerate every visible option, then close them without "
                "selecting an option. For every must-show requirement, report the exact route "
                "and control label that exposes it, or list the missing route as a gap and keep planning "
                "readiness false. Return observed URLs/labels, recommended steps, assumptions, gaps, "
                "privacy risks, and planning readiness. Request: {{request}}"
            ),
            output_var="discovered_journey",
            output_schema=DISCOVERY_SCHEMA,
            forced_tool_calls=[
                {
                    "name": "invoke_skill",
                    "arguments": {
                        "skill": "chrome",
                        "input": (
                            "Use Chrome to explore the exact start URL {{request.start_url}} with reversible, "
                            "read-only navigation. The authoritative request, including every must-show and "
                            "must-not-show item, is: {{request}}. Execute the complete reversible route now instead of only "
                            "recommending a route. Follow visible navigation controls to each must-show page and "
                            "read the resulting state. Do not return navigation steps that you did not perform; "
                            "report a gap only after the matching reversible navigation attempt is blocked or the "
                            "target remains absent. When a requested capability uses a selector, picker, or menu, "
                            f"{DISCOVERY_BROWSER_DISCIPLINE}"
                            "open reversible option menus, enumerate every visible option, then close them without "
                            "selecting an option. For every requested must-show capability, observe the exact "
                            "route and control label that exposes it or return a concrete gap. Observe URLs, "
                            "labels, starting state, safe stopping point, login or data blockers, and privacy "
                            "risks. Do not record or take screenshots."
                        ),
                    },
                }
            ],
            next_steps=["save_discovery"],
        ),
        {
            "id": "save_discovery",
            "type": "workflow_project",
            "name": "Save discovery evidence",
            "config": {
                "operation": "patch",
                "project_id": "{{project.project_id}}",
                "expected_revision": "{{project.revision}}",
                "patch": {"discovered_journey": "{{discovered_journey}}"},
                "state_schema": "{{product_video_project_schema}}",
                "output_var": "project",
            },
            "next": ["done"],
        },
        {"id": "done", "type": "end", "name": "Discovery complete", "config": {}, "next": []},
    ]


def _capture_steps() -> list[dict[str, Any]]:
    incomplete_scene_expression = (
        "scene_result.scene.status == 'blocked' or "
        "scene_result.scene.status == 'failed'"
    )
    return [
        {"id": "start", "type": "trigger", "name": "Start", "config": {"trigger_type": "internal"}, "next": ["load_project"]},
        {
            "id": "load_project",
            "type": "workflow_project",
            "name": "Load project",
            "config": {
                "operation": "get",
                "project_id": "{{project_id}}",
                "project_type": PROJECT_TYPE,
                "state_schema": "{{product_video_project_schema}}",
                "output_var": "project",
            },
            "next": ["decide_effect"],
        },
        {
            "id": "decide_effect",
            "type": "browser_effect",
            "name": "Decide browser effect",
            "config": {
                "operation": "decide",
                "record": "{{scene.primary_effect}}",
                "output_var": "effect_decision",
            },
            "next": ["capture_scene"],
        },
        _agent_step(
            "capture_scene",
            "Capture or safely observe scene",
            service_key="product_video.capture",
            skill="screen_asset_collector",
            tools=COLLECTOR_TOOLS,
            prompt=(
                "Use Chrome to execute only this approved scene: {{scene}}. Durable effect decision: "
                "{{effect_decision}}. If decision is observe_or_pause, perform read-only observation of "
                "the expected postcondition and never execute the side effect. Execute or retry only when "
                "the deterministic decision permits it. A reuse decision applies only to the product-side "
                "effect and does not authorize skipping the requested screenshot or recording. Reuse capture "
                "media when the project already contains existing ready artifact IDs for this scene: reuse each "
                "ready artifact type unless the selected scene explicitly requests that type be retaken, capture "
                "only missing or blocked artifact types, and return the complete ready artifact set. "
                "Preserve scene_id, capture_type, and screenshot_requirements exactly from the approved scene. "
                "Authoritative product source: {{project.state.product_source}}. Return to the scene's approved "
                "precondition or exact route before interacting, and do not substitute a similarly named global "
                "or sidebar control. "
                "Use the approved Workflow grant context, stop recording before a long wait, and return the "
                "full scene state, durable artifacts, and a bounded wait request. After approved navigation, "
                "wait for the target state and re-read it before reporting an access blocker. Never publish."
            ),
            output_var="scene_result",
            output_schema=CAPTURE_RESULT_SCHEMA,
            forced_tool_calls=[
                {
                    "name": "invoke_skill",
                    "arguments": {
                        "skill": "chrome",
                        "input": (
                            "Use Chrome for only this approved product-video scene: {{scene}}. Follow this "
                            "durable effect decision: {{effect_decision}}. Respect the scoped Workflow grant, "
                            "using authoritative product source {{project.state.product_source}}. Return to the "
                            "approved precondition or exact route before interacting; never substitute a similarly "
                            "named global or sidebar control for the scene's product-specific entry point. "
                            "Treat reuse only as protection against repeating a product-side effect. Unless "
                            "the selected scene explicitly requests a retake, reuse existing ready artifact types, "
                            "capture only missing or blocked artifact types, and return the existing plus newly "
                            "captured durable receipts. Stop recording before long waits, collect matching "
                            "evidence, wait for the requested state and re-read it after navigation, continue through "
                            "visible nested navigation within the approved read-only scene, and report an access "
                            "blocker only for explicit login, permission, or access-denied UI. Do not add scrolling or "
                            "navigation unless the approved scene requires it; hold a verified static state until the "
                            "recording receipt is ready. Once the acceptance state is visible, stop exploring immediately, "
                            "When acceptance requires a complete form, use inspect_selector on every required form field "
                            "immediately before capture and reject any empty or placeholder-only value. "
                            "capture exactly one durable PNG and one durable WebM receipt, inspect the actual captured bitmap "
                            "against every acceptance criterion, and return them only when every must-show item is simultaneously "
                            "visible without failed states, unrelated history, stale operator messages, overlays, or partial forms. Do not call "
                            "finalize_tabs during this per-scene capture; the parent Workflow owns the persistent Chrome "
                            "session. Never publish."
                        ),
                    },
                }
            ],
            next_steps=["scene_blocked"],
            timeout=3600,
        ),
        {
            "id": "scene_blocked",
            "type": "condition",
            "name": "Scene needs recovery",
            "config": {"expression": incomplete_scene_expression},
            "true_next": ["persist_blocked"],
            "false_next": ["scene_waits"],
            "next": [],
        },
        {
            "id": "scene_waits",
            "type": "condition",
            "name": "Scene needs a durable wait",
            "config": {"expression": "scene_result.wait_required == true"},
            "true_next": ["wait_for_product"],
            "false_next": ["persist_scene"],
            "next": [],
        },
        {
            "id": "wait_for_product",
            "type": "wait",
            "name": "Wait for product result",
            "config": {
                "wait_type": "timer",
                "duration_seconds": "{{scene_result.wait_seconds}}",
                "message": "Waiting for the product result with recording stopped.",
                "chat_projection": "status",
            },
            "next": ["finish_scene"],
        },
        _agent_step(
            "finish_scene",
            "Capture completed result state",
            service_key="product_video.capture",
            skill="screen_asset_collector",
            tools=COLLECTOR_TOOLS,
            prompt=(
                "Resume this same approved scene in Chrome after its durable wait. Re-observe the expected product "
                "state first, record only the requested result, capture matching screenshots, resolve all "
                "durable receipts, and return the complete scene result JSON. Prior result: {{scene_result}}"
            ),
            output_var="scene_result",
            output_schema=CAPTURE_RESULT_SCHEMA,
            forced_tool_calls=[
                {
                    "name": "invoke_skill",
                    "arguments": {
                        "skill": "chrome",
                        "input": (
                            "Use Chrome to resume the same approved scene after its wait. Re-observe the "
                            "expected state first, record only the requested completed result, capture matching "
                            "evidence, resolve durable receipts, and never publish. Prior result: {{scene_result}}"
                        ),
                    },
                }
            ],
            next_steps=["finished_scene_blocked"],
            timeout=3600,
        ),
        {
            "id": "finished_scene_blocked",
            "type": "condition",
            "name": "Finished scene needs recovery",
            "config": {"expression": incomplete_scene_expression},
            "true_next": ["persist_blocked"],
            "false_next": ["persist_scene"],
            "next": [],
        },
        {
            "id": "persist_scene",
            "type": "workflow_project",
            "name": "Persist completed scene",
            "config": {
                "operation": "patch",
                "project_id": "{{project.project_id}}",
                "expected_revision": "{{project.revision}}",
                "patch": {},
                "list_upserts": [
                    {"path": "scenes", "key": "scene_id", "item": "{{scene_result.scene}}"}
                ],
                "list_removes": [
                    {
                        "path": "artifacts",
                        "field": "scene_id",
                        "values": ["{{scene_result.scene.scene_id}}"],
                    },
                ],
                "list_appends": [
                    {"path": "artifacts", "key": "artifact_id", "items": "{{scene_result.artifacts}}"}
                ],
                "state_schema": "{{product_video_project_schema}}",
                "output_var": "project",
            },
            "next": ["done"],
        },
        {
            "id": "persist_blocked",
            "type": "workflow_project",
            "name": "Persist blocked scene",
            "config": {
                "operation": "patch",
                "project_id": "{{project.project_id}}",
                "expected_revision": "{{project.revision}}",
                "patch": {},
                "list_upserts": [
                    {"path": "scenes", "key": "scene_id", "item": "{{scene_result.scene}}"}
                ],
                "list_appends": [
                    {"path": "artifacts", "key": "artifact_id", "items": "{{scene_result.artifacts}}"}
                ],
                "state_schema": "{{product_video_project_schema}}",
                "output_var": "project",
            },
            "next": ["stop_blocked"],
        },
        {
            "id": "stop_blocked",
            "type": "stop",
            "name": "Stop incomplete scene",
            "config": {
                "message": (
                    "Scene {{scene_result.scene.scene_id}} did not complete "
                    "({{scene_result.scene.status}}): {{scene_result.scene.blocker}} "
                    "Update the Revise product video inputs for project {{project.project_id}} "
                    "with the corrected page state or instructions, then retry only this scene and "
                    "only its missing or failed artifact types. "
                    "Completed scene artifacts remain saved."
                )
            },
            "next": [],
        },
        {"id": "done", "type": "end", "name": "Scene complete", "config": {}, "next": []},
    ]


def _produce_steps() -> list[dict[str, Any]]:
    return [
        {"id": "start", "type": "trigger", "name": "Start", "config": {"trigger_type": "internal"}, "next": ["load_project"]},
        {
            "id": "load_project",
            "type": "workflow_project",
            "name": "Load project",
            "config": {
                "operation": "get",
                "project_id": "{{project_id}}",
                "project_type": PROJECT_TYPE,
                "state_schema": "{{product_video_project_schema}}",
                "output_var": "project",
            },
            "next": ["mark_producing"],
        },
        {
            "id": "mark_producing",
            "type": "workflow_project",
            "name": "Mark production active",
            "config": {
                "operation": "patch",
                "project_id": "{{project.project_id}}",
                "expected_revision": "{{project.revision}}",
                "patch": {},
                "current_stage": "producing",
                "allowed_stages": PROJECT_STAGES,
                "state_schema": "{{product_video_project_schema}}",
                "output_var": "project",
            },
            "next": ["render_video"],
        },
        _agent_step(
            "render_video",
            "Render product video package",
            service_key="product_video.production",
            tools=PRODUCER_TOOLS,
            prompt=(
                "Produce from this validated project state: {{project.state}}. Require every scene to "
                "have ready durable artifacts. Build the picture master, convert only planned stills, "
                "remove or accelerate non-critical waits, generate one continuous narration take, "
                "normalize it, align semantic bottom-safe subtitles, compose the final MP4, and return "
                "timeline plus ready artifact refs. Reuse matching checkpoints. Repair request: "
                "{{repair_request}}. Never change product claims or publish."
            ),
            output_var="production_result",
            output_schema=PRODUCTION_RESULT_SCHEMA,
            next_steps=["save_production"],
            timeout=3600,
        ),
        {
            "id": "save_production",
            "type": "workflow_project",
            "name": "Save production artifacts",
            "config": {
                "operation": "patch",
                "project_id": "{{project.project_id}}",
                "expected_revision": "{{project.revision}}",
                "patch": {"timeline": "{{production_result.timeline}}"},
                "list_appends": [
                    {"path": "artifacts", "key": "artifact_id", "items": "{{production_result.artifacts}}"}
                ],
                "current_stage": "quality_gate",
                "allowed_stages": PROJECT_STAGES,
                "state_schema": "{{product_video_project_schema}}",
                "output_var": "project",
            },
            "next": ["done"],
        },
        {"id": "done", "type": "end", "name": "Production complete", "config": {}, "next": []},
    ]


def _quality_steps() -> list[dict[str, Any]]:
    return [
        {"id": "start", "type": "trigger", "name": "Start", "config": {"trigger_type": "internal"}, "next": ["load_project"]},
        {
            "id": "load_project",
            "type": "workflow_project",
            "name": "Load project",
            "config": {
                "operation": "get",
                "project_id": "{{project_id}}",
                "project_type": PROJECT_TYPE,
                "state_schema": "{{product_video_project_schema}}",
                "output_var": "project",
            },
            "next": ["final_media_ready"],
        },
        {
            "id": "final_media_ready",
            "type": "condition",
            "name": "Final media is ready",
            "config": {"expression": "project.state.timeline.final_video.status == 'ready'"},
            "true_next": ["probe_final"],
            "false_next": ["build_unready_result"],
            "next": [],
        },
        {
            "id": "build_unready_result",
            "type": "transform",
            "name": "Build blocked production result",
            "config": {
                "set": {
                    "quality_result": {
                        "verdict": "revision_required",
                        "technical_checks": [
                            {
                                "check_id": "final_media_ready",
                                "status": "fail",
                                "evidence": {
                                    "artifact_id": "{{project.state.timeline.final_video.artifact_id}}",
                                    "artifact_status": "{{project.state.timeline.final_video.status}}",
                                    "reason": "{{project.state.timeline.final_video.provenance.reason}}",
                                },
                            }
                        ],
                        "visual_findings": [
                            {
                                "finding_id": "final-media-not-ready",
                                "severity": "blocking",
                                "scene_id": None,
                                "description": "Final production media is not ready for machine QA.",
                                "evidence_artifact_ids": [],
                            }
                        ],
                        "coverage": [
                            {
                                "requirement": "Final production media is ready for machine QA",
                                "status": "missing",
                                "scene_ids": [],
                                "evidence_artifact_ids": [],
                            }
                        ],
                        "scene_revisions": [],
                        "operator_checks": [
                            "Resolve blocked production artifacts before playback review."
                        ],
                    }
                }
            },
            "next": ["save_revision"],
        },
        {
            "id": "probe_final",
            "type": "tool",
            "name": "Probe final media",
            "config": {
                "tool": "probe_media",
                "args": {"input_path": "{{project.state.timeline.final_video.fs_path}}"},
                "output_format": "json",
                "output_var": "media_probe",
            },
            "next": ["analyze_final_audio"],
        },
        {
            "id": "analyze_final_audio",
            "type": "tool",
            "name": "Analyze final audio",
            "config": {
                "tool": "analyze_audio",
                "args": {"input_path": "{{project.state.timeline.final_video.fs_path}}"},
                "output_format": "json",
                "output_var": "audio_analysis",
            },
            "next": ["validate_final_subtitles"],
        },
        {
            "id": "validate_final_subtitles",
            "type": "tool",
            "name": "Validate final subtitles",
            "config": {
                "tool": "validate_subtitles",
                "args": {
                    "subtitle_path": "{{project.state.timeline.subtitles.fs_path}}",
                    "media_path": "{{project.state.timeline.final_video.fs_path}}",
                    "max_lines": 2,
                },
                "output_format": "json",
                "output_var": "subtitle_analysis",
            },
            "next": ["render_evidence_frames"],
        },
        {
            "id": "render_evidence_frames",
            "type": "tool",
            "name": "Render evidence frames",
            "config": {
                "tool": "render_frame_samples",
                "args": {
                    "input_path": "{{project.state.timeline.final_video.fs_path}}",
                    "output_dir": "qa/frames",
                    "scene_boundaries": "{{project.state.timeline.scene_boundaries}}",
                    "max_samples": 24,
                },
                "output_format": "json",
                "output_var": "frame_evidence",
            },
            "next": ["review_quality"],
        },
        _agent_step(
            "review_quality",
            "Review deterministic and visual evidence",
            service_key="product_video.quality",
            tools=QUALITY_TOOLS,
            prompt=(
                "Review the approved request, plan, artifacts, final media probe, audio analysis, "
                "subtitle validation, and sampled frames. Check must-show coverage, wrong or private "
                "states, critical UI overlap, product claims, and no-publish status. Return only the "
                "QualityResult JSON. A successful machine result is machine_pass, never operator "
                "acceptance. Project: {{project.state}}. Probe: {{media_probe}}. Audio: "
                "{{audio_analysis}}. Subtitles: {{subtitle_analysis}}. Frames: {{frame_evidence}}."
            ),
            output_var="quality_result",
            output_schema=QUALITY_RESULT_SCHEMA,
            next_steps=["route_verdict"],
            timeout=1800,
        ),
        {
            "id": "route_verdict",
            "type": "switch",
            "name": "Route quality verdict",
            "config": {
                "cases": [
                    {"expression": "quality_result.verdict == 'machine_pass'", "next": ["save_pass"]},
                    {"expression": "quality_result.verdict == 'repairable_technical'", "next": ["save_repair"]},
                ],
                "default_next": ["save_revision"],
            },
            "next": [],
        },
        {
            "id": "save_pass",
            "type": "workflow_project",
            "name": "Save machine pass",
            "config": {
                "operation": "patch",
                "project_id": "{{project.project_id}}",
                "expected_revision": "{{project.revision}}",
                "patch": {"quality_result": "{{quality_result}}"},
                "current_stage": "awaiting_acceptance",
                "allowed_stages": PROJECT_STAGES,
                "state_schema": "{{product_video_project_schema}}",
                "output_var": "project",
            },
            "next": ["done"],
        },
        {
            "id": "save_repair",
            "type": "workflow_project",
            "name": "Save repairable result",
            "config": {
                "operation": "patch",
                "project_id": "{{project.project_id}}",
                "expected_revision": "{{project.revision}}",
                "patch": {"quality_result": "{{quality_result}}"},
                "current_stage": "quality_gate",
                "allowed_stages": PROJECT_STAGES,
                "state_schema": "{{product_video_project_schema}}",
                "output_var": "project",
            },
            "next": ["done"],
        },
        {
            "id": "save_revision",
            "type": "workflow_project",
            "name": "Save revision result",
            "config": {
                "operation": "patch",
                "project_id": "{{project.project_id}}",
                "expected_revision": "{{project.revision}}",
                "patch": {"quality_result": "{{quality_result}}"},
                "current_stage": "revision_required",
                "allowed_stages": PROJECT_STAGES,
                "state_schema": "{{product_video_project_schema}}",
                "output_var": "project",
            },
            "next": ["done"],
        },
        {"id": "done", "type": "end", "name": "Quality result ready", "config": {}, "next": []},
    ]


def _revision_steps() -> list[dict[str, Any]]:
    return [
        {"id": "start", "type": "trigger", "name": "Start", "config": {"trigger_type": "mcp"}, "next": ["load_project"]},
        {
            "id": "load_project",
            "type": "workflow_project",
            "name": "Load existing project",
            "config": {
                "operation": "get",
                "project_id": "{{project_id}}",
                "project_type": PROJECT_TYPE,
                "state_schema": "{{product_video_project_schema}}",
                "output_var": "project",
            },
            "next": ["plan_revision"],
        },
        _agent_step(
            "plan_revision",
            "Plan targeted revision",
            service_key="product_video.planning",
            tools=PLANNER_TOOLS,
            prompt=(
                "Plan a targeted revision against this same project. Use only the supplied finding IDs "
                "or operator notes, preserve unaffected scenes, and preserve every ready artifact for both "
                "selected and unselected scenes; retry only missing or blocked artifact types unless the operator "
                "explicitly requests a retake; encode any explicit retake type in the selected scene recovery "
                "strategy. Identify whether the change is semantic "
                "or post-production-only, and return an updated complete plan plus selected scenes. "
                "Browser scenes may depend only on states available before post-production. Narration audio, "
                "subtitle files, final MP4 playback, machine QA, and the final operator acceptance gate are "
                "downstream workflow outputs; never put them in a scene precondition, browser action, expected "
                "postcondition, screenshot requirement, or acceptance criterion. "
                "Project: {{project.state}}. Finding IDs: {{finding_ids}}. Notes: {{revision_notes}}."
            ),
            output_var="revision_plan",
            output_schema=REVISION_PLAN_SCHEMA,
            next_steps=["save_revision_plan"],
        ),
        {
            "id": "save_revision_plan",
            "type": "workflow_project",
            "name": "Save revision plan",
            "config": {
                "operation": "patch",
                "project_id": "{{project.project_id}}",
                "expected_revision": "{{project.revision}}",
                "patch": {
                    "plan": "{{revision_plan.updated_plan}}",
                },
                "list_reconciles": [
                    {
                        "path": "scenes",
                        "key": "scene_id",
                        "keys": "{{revision_plan.updated_plan.scene_ids}}",
                        "items": "{{revision_plan.selected_scenes}}",
                    },
                ],
                "list_appends": [
                    {"path": "revision_history", "key": "revision_id", "items": ["{{revision_plan.revision_entry}}"]}
                ],
                "current_stage": "awaiting_plan_approval",
                "allowed_stages": PROJECT_STAGES,
                "state_schema": "{{product_video_project_schema}}",
                "output_var": "project",
            },
            "next": ["post_only"],
        },
        {
            "id": "post_only",
            "type": "condition",
            "name": "Post-production-only revision",
            "config": {"expression": "revision_plan.post_only == true"},
            "true_next": ["produce_video"],
            "false_next": ["approve_revision"],
            "next": [],
        },
        {
            "id": "approve_revision",
            "type": "wait",
            "name": "Approve revision capture",
            "config": {
                "wait_type": "approval",
                "message": "Review the revised plan and selected capture scope.",
                "response_variable": "revision_decision",
                "options": ["approve", "cancel"],
                "approval_values": ["approve"],
                "requires_operator_approval": True,
            },
            "next": ["revision_approved"],
        },
        {
            "id": "revision_approved",
            "type": "condition",
            "name": "Revision approved",
            "config": {"expression": "revision_decision.choice == 'approve'"},
            "true_next": ["lock_revision"],
            "false_next": ["cancelled"],
            "next": [],
        },
        {
            "id": "grant_capture",
            "type": "workflow_action_grant",
            "name": "Grant revision capture",
            "config": {
                "operation": "create",
                "approval_step_id": "approve_revision",
                "project_id": "{{project.project_id}}",
                "grant_type": "browser_capture",
                "scope": {
                    "approved_plan_version": "{{revision_plan.updated_plan.plan_version}}",
                    "scene_ids": "{{revision_plan.selected_scene_ids}}",
                    "allowed_actions": [
                        "start_tab_recording",
                        "click_element",
                        "fill_or_select",
                        "click_point",
                        "type_text",
                        "press_key",
                        "keyboard",
                        "upload",
                        "computer",
                    ],
                },
                "scope_schema": CAPTURE_GRANT_SCOPE_SCHEMA,
                "output_var": "capture_grant",
            },
            "next": ["attach_revision_grant"],
        },
        {
            "id": "lock_revision",
            "type": "workflow_project",
            "name": "Lock revision plan",
            "config": {
                "operation": "patch",
                "project_id": "{{project.project_id}}",
                "expected_revision": "{{project.revision}}",
                "patch": {
                    "approved_plan_version": "{{revision_plan.updated_plan.plan_version}}",
                },
                "current_stage": "awaiting_plan_approval",
                "allowed_stages": PROJECT_STAGES,
                "state_schema": "{{product_video_project_schema}}",
                "output_var": "project",
            },
            "next": ["grant_capture"],
        },
        {
            "id": "attach_revision_grant",
            "type": "workflow_project",
            "name": "Attach revision grant",
            "config": {
                "operation": "patch",
                "project_id": "{{project.project_id}}",
                "expected_revision": "{{project.revision}}",
                "patch": {"capture_grant_id": "{{capture_grant.grant_id}}"},
                "current_stage": "capturing",
                "allowed_stages": PROJECT_STAGES,
                "state_schema": "{{product_video_project_schema}}",
                "output_var": "project",
            },
            "next": ["capture_scenes"],
        },
        {
            "id": "capture_scenes",
            "type": "foreach_subworkflow",
            "name": "Recapture selected scenes",
            "config": {
                "workflow_id": "capture-product-video-scene-v1",
                "over": "{{revision_plan.selected_scenes}}",
                "item_var": "scene",
                "item_key": "scene_id",
                "concurrency": 1,
                "max_attempts": 2,
                "inputs": [
                    {"key": "project_id", "value": "{{project.project_id}}"},
                    {"key": "workflow_project_id", "value": "{{project.project_id}}"},
                    {"key": "workflow_action_grant_id", "value": "{{capture_grant.grant_id}}"},
                    {"key": "approved_plan_version", "value": "{{revision_plan.updated_plan.plan_version}}"},
                    {"key": "workflow_scene_id", "value": "{{scene.scene_id}}"},
                ],
                "output_var": "scene_results",
            },
            "next": ["reload_captured_project"],
        },
        {
            "id": "reload_captured_project",
            "type": "workflow_project",
            "name": "Reload recaptured project",
            "config": {
                "operation": "get",
                "project_id": "{{project.project_id}}",
                "state_schema": "{{product_video_project_schema}}",
                "output_var": "project",
            },
            "next": ["produce_video"],
        },
        {
            "id": "produce_video",
            "type": "subworkflow",
            "name": "Rebuild product video",
            "config": {
                "workflow_id": "produce-product-video-v1",
                "inputs": [{"key": "project_id", "value": "{{project.project_id}}"}],
                "output_var": "production",
            },
            "next": ["quality_gate"],
        },
        {
            "id": "quality_gate",
            "type": "subworkflow",
            "name": "Rerun full quality gate",
            "config": {
                "workflow_id": "quality-gate-product-video-v1",
                "inputs": [{"key": "project_id", "value": "{{project.project_id}}"}],
                "output_var": "quality_gate",
            },
            "next": ["ready_for_acceptance"],
        },
        {
            "id": "ready_for_acceptance",
            "type": "condition",
            "name": "Revision machine pass",
            "config": {"expression": "quality_gate.quality_result.verdict == 'machine_pass'"},
            "true_next": ["load_final_project"],
            "false_next": ["revision_required"],
            "next": [],
        },
        {
            "id": "load_final_project",
            "type": "workflow_project",
            "name": "Load revised project",
            "config": {
                "operation": "get",
                "project_id": "{{project.project_id}}",
                "project_type": PROJECT_TYPE,
                "state_schema": "{{product_video_project_schema}}",
                "output_var": "project",
            },
            "next": ["operator_acceptance"],
        },
        {
            "id": "operator_acceptance",
            "type": "wait",
            "name": "Watch and accept revised video",
            "config": {
                "wait_type": "approval",
                "message": "Watch the revised final MP4 and accept or request another revision.",
                "response_variable": "acceptance_decision",
                "options": ["accept", "revise"],
                "approval_values": ["accept"],
                "requires_operator_approval": True,
            },
            "next": ["accepted"],
        },
        {
            "id": "accepted",
            "type": "condition",
            "name": "Revision accepted",
            "config": {"expression": "acceptance_decision.choice == 'accept'"},
            "true_next": ["mark_accepted"],
            "false_next": ["revision_required"],
            "next": [],
        },
        {
            "id": "mark_accepted",
            "type": "workflow_project",
            "name": "Mark revised project accepted",
            "config": {
                "operation": "patch",
                "project_id": "{{project.project_id}}",
                "expected_revision": "{{project.revision}}",
                "patch": {"operator_acceptance": {"status": "accepted", "source": "explicit_operator_playback"}},
                "current_stage": "accepted",
                "allowed_stages": PROJECT_STAGES,
                "state_schema": "{{product_video_project_schema}}",
                "output_var": "project",
            },
            "next": ["done"],
        },
        {"id": "done", "type": "end", "name": "Accepted revised video", "config": {"no_external_publish": True}, "next": []},
        {"id": "revision_required", "type": "end", "name": "Further revision required", "config": {"no_external_publish": True}, "next": []},
        {"id": "cancelled", "type": "end", "name": "Revision cancelled", "config": {"no_external_publish": True}, "next": []},
    ]


def _flat_planning_steps(*, terminal: bool) -> list[dict[str, Any]]:
    after_plan = "build_plan_result" if terminal else "approve_plan"
    return [
        {
            "id": "start",
            "type": "trigger",
            "name": "Start",
            "config": {"trigger_type": "mcp"},
            "next": ["normalize_request"],
        },
        _agent_step(
            "normalize_request",
            "Normalize product video request",
            service_key="product_video.planning",
            prompt=(
                "Validate and normalize this structured product video request into exactly the supplied "
                "JSON schema. Do not invent a product, URL, audience, CTA, credentials, or product journey. "
                f"Apply this output profile only for omitted presentation fields: {DEFAULT_OUTPUT_PROFILE}. "
                "Request: {{request}}"
            ),
            output_var="request",
            output_schema=PRODUCT_VIDEO_REQUEST_SCHEMA,
            next_steps=["initialize_project"],
        ),
        {
            "id": "initialize_project",
            "type": "workflow_project",
            "name": "Initialize durable project",
            "config": {
                "operation": "create",
                "project_type": PROJECT_TYPE,
                "schema_version": PROJECT_SCHEMA_VERSION,
                "current_stage": "draft",
                "state": _initial_state_bindings("{{request}}"),
                "state_schema": "{{product_video_project_schema}}",
                "output_var": "project",
            },
            "next": ["assign_project_storage"],
        },
        {
            "id": "assign_project_storage",
            "type": "workflow_project",
            "name": "Assign one project root",
            "config": {
                "operation": "patch",
                "project_id": "{{project.project_id}}",
                "expected_revision": "{{project.revision}}",
                "patch": {
                    "project_root": "Product Videos/product-video-{{project.project_id}}",
                    "ledger_path": "Product Videos/product-video-{{project.project_id}}/03-file-ledger.json",
                    "artifact_manifest_path": "Product Videos/product-video-{{project.project_id}}/technical/asset-manifest.json",
                    "current_phase": "discovery",
                    "business_outcome": "in_progress",
                    "checkpoints": {
                        "request": {
                            "status": "completed",
                            "step_id": "assign_project_storage",
                        }
                    },
                },
                "state_schema": "{{product_video_project_schema}}",
                "output_var": "project",
            },
            "next": ["materialize_project_storage"],
        },
        {
            "id": "materialize_project_storage",
            "type": "tool",
            "name": "Create visible project folder",
            "config": {
                "tool": "generate_file",
                "args": {
                    "kind": "code",
                    "name": "{{project.state.project_root}}",
                    "params": {
                        "entry": "00-project-overview.md",
                        "files": [
                            {
                                "path": "00-project-overview.md",
                                "content": (
                                    "# {{request.product_name}} Product Video\n\n"
                                    "- Project ID: `{{project.project_id}}`\n"
                                    "- Phase: discovery\n"
                                    "- Status: initialized\n"
                                    "- Start URL: {{request.start_url}}\n"
                                    "- Project root: `{{project.state.project_root}}`\n\n"
                                    "Discovery is read-only. Recordings, screenshots, audio, and final video "
                                    "appear only after the plan is approved and capture begins.\n"
                                ),
                            },
                            {
                                "path": "technical/run-state.json",
                                "content": {
                                    "project_id": "{{project.project_id}}",
                                    "project_root": "{{project.state.project_root}}",
                                    "phase": "discovery",
                                    "status": "initialized",
                                    "request": "{{request}}",
                                    "artifact_count": 0,
                                },
                            },
                        ],
                    },
                },
                "output_format": "json",
                "output_var": "project_storage_receipt",
                "chat_projection": "hidden",
            },
            "next": ["browser_preflight"],
        },
        _agent_step(
            "browser_preflight",
            "Check browser readiness",
            service_key="product_video.exploration",
            skill="product_experience_mapper",
            tools=EXPLORER_TOOLS,
            prompt=(
                "Check whether the current paired Chrome session can safely open the supplied start URL. "
                "Use read-only discovery only. Return available, requires_login, and one blocker or null. "
                "Request: {{request}}"
            ),
            output_var="browser_preflight",
            output_schema=BROWSER_PREFLIGHT_SCHEMA,
            forced_tool_calls=[
                {
                    "name": "invoke_skill",
                    "arguments": {
                        "skill": "chrome",
                        "input": (
                            "Use Chrome read-only to verify the current paired session and open the exact "
                            "start URL {{request.start_url}}. Report observed readiness, login "
                            "requirements, and blockers without filling, submitting, or changing state."
                        ),
                    },
                }
            ],
            next_steps=["browser_ready"],
        ),
        {
            "id": "browser_ready",
            "type": "condition",
            "name": "Browser is ready",
            "config": {"expression": "browser_preflight.available == true"},
            "true_next": ["mark_discovering"],
            "false_next": ["browser_handoff"],
            "next": [],
        },
        {
            "id": "browser_handoff",
            "type": "wait",
            "name": "Connect or sign in to the product",
            "config": {
                "wait_type": "event",
                "message": "Browser readiness issue:\n{{browser_preflight.blocker}}\nFix the reported connection, login, or page-state problem, then resume planning.",
                "chat_projection": "action",
            },
            "next": ["mark_discovering"],
        },
        {
            "id": "mark_discovering",
            "type": "workflow_project",
            "name": "Mark discovery active",
            "config": {
                "operation": "patch",
                "project_id": "{{project.project_id}}",
                "expected_revision": "{{project.revision}}",
                "patch": {
                    "request": "{{request}}",
                    "product_source": {
                        "start_url": "{{request.start_url}}",
                        "browser_session": "{{request.browser_session}}",
                    },
                    "output_profile": "{{request.output_profile}}",
                    "current_phase": "discovery",
                    "business_outcome": "in_progress",
                },
                "current_stage": "discovering",
                "allowed_stages": PROJECT_STAGES,
                "state_schema": "{{product_video_project_schema}}",
                "output_var": "project",
            },
            "next": ["explore_product"],
        },
        _agent_step(
            "explore_product",
            "Explore current product state",
            service_key="product_video.exploration",
            skill="product_experience_mapper",
            tools=EXPLORER_TOOLS,
            prompt=(
                "Explore the supplied product from its exact start URL using the current paired Chrome "
                "session. Use reversible navigation and reading only; do not record, screenshot, fill, "
                "submit, upload, or alter state. Execute the complete reversible route now instead of only "
                "recommending a route. Follow visible navigation controls to each must-show page and read the "
                "resulting state. Do not return navigation steps that you did not perform; report a gap only "
                "after the matching reversible navigation attempt is blocked or the target remains absent. "
                f"{DISCOVERY_BROWSER_DISCIPLINE}"
                "When a requested capability uses a selector, picker, or "
                "menu, open reversible option menus, enumerate every visible option, then close them without "
                "selecting an option. Stop immediately once every must-show requirement is verified. "
                "Do not inspect unrelated selectors, menus, account controls, or routes. For every must-show "
                "requirement, report the exact route and control label. Readiness depends only on must-show "
                "coverage and actionable browser blockers; final_cta is downstream narration, not a discovery "
                "requirement. Do not open a control when must-show only asks to show its label or current state. "
                "For capabilities that must be opened, report the exact route and control "
                "label that exposes it, or list the missing route as a gap and keep planning readiness false. "
                "Return observations, recommended steps, assumptions, gaps, privacy risks, and readiness. "
                "Request: {{request}}"
            ),
            output_var="discovered_journey",
            output_schema=DISCOVERY_SCHEMA,
            forced_tool_calls=[
                {
                    "name": "invoke_skill",
                    "arguments": {
                        "skill": "chrome",
                        "input": (
                            "Use Chrome to explore the exact start URL {{request.start_url}} with "
                            "reversible, read-only navigation. The authoritative request, including every must-show "
                            "and must-not-show item, is: {{request}}. Execute the complete reversible route now instead "
                            "of only recommending a route. Follow visible navigation controls to each must-show "
                            "page and read the resulting state. Do not return navigation steps that you did not "
                            "perform; report a gap only after the matching reversible navigation attempt is "
                            "blocked or the target remains absent. "
                            f"{DISCOVERY_BROWSER_DISCIPLINE}"
                            "For each selector, open reversible option menus and enumerate every visible option "
                            "before closing them only when the selector is required by must-show. "
                            "Stop immediately once every must-show requirement is verified. Do not inspect unrelated "
                            "selectors, menus, account controls, or routes. Readiness depends only on must-show coverage "
                            "and actionable browser blockers; final_cta is downstream narration, not a discovery "
                            "requirement. Do not open a control when must-show only asks to show its label or current state. "
                            "Observe the exact route and control label for "
                            "each must-show capability, starting state, blockers, and privacy risks. Do not "
                            "record or take screenshots."
                        ),
                    },
                }
            ],
            next_steps=["save_discovery"],
        ),
        {
            "id": "save_discovery",
            "type": "workflow_project",
            "name": "Save discovery checkpoint",
            "config": {
                "operation": "patch",
                "project_id": "{{project.project_id}}",
                "expected_revision": "{{project.revision}}",
                "patch": {
                    "discovered_journey": "{{discovered_journey}}",
                    "checkpoints": {
                        "discovery": {
                            "status": "completed",
                            "step_id": "explore_product",
                            "ready_for_planning": "{{discovered_journey.ready_for_planning}}",
                        }
                    },
                },
                "state_schema": "{{product_video_project_schema}}",
                "output_var": "project",
            },
            "next": ["project_discovery_report"],
        },
        {
            "id": "project_discovery_report",
            "type": "tool",
            "name": "Save visible discovery report",
            "config": {
                "tool": "generate_file",
                "args": {
                    "kind": "code",
                    "name": "{{project.state.project_root}}",
                    "params": {
                        "entry": "00-project-overview.md",
                        "files": [
                            {
                                "path": "00-project-overview.md",
                                "content": (
                                    "# {{request.product_name}} Product Video\n\n"
                                    "- Project ID: `{{project.project_id}}`\n"
                                    "- Phase: discovery\n"
                                    "- Planning ready: {{discovered_journey.ready_for_planning}}\n"
                                    "- Start URL: {{request.start_url}}\n"
                                    "- Project root: `{{project.state.project_root}}`\n\n"
                                    "## Discovery gaps\n\n"
                                    "{{discovered_journey.gaps}}\n\n"
                                    "See `technical/discovery-report.json` for the complete structured report.\n"
                                ),
                            },
                            {
                                "path": "technical/run-state.json",
                                "content": {
                                    "project_id": "{{project.project_id}}",
                                    "project_root": "{{project.state.project_root}}",
                                    "phase": "discovery",
                                    "status": "completed",
                                    "ready_for_planning": "{{discovered_journey.ready_for_planning}}",
                                    "gaps": "{{discovered_journey.gaps}}",
                                    "artifact_count": 0,
                                },
                            },
                            {
                                "path": "technical/discovery-report.json",
                                "content": "{{discovered_journey}}",
                            },
                        ],
                    },
                },
                "output_format": "json",
                "output_var": "discovery_report_receipt",
                "chat_projection": "hidden",
            },
            "next": ["discovery_ready"],
        },
        {
            "id": "discovery_ready",
            "type": "condition",
            "name": "Discovery supports planning",
            "config": {"expression": "discovered_journey.ready_for_planning == true"},
            "true_next": ["plan_video"],
            "false_next": ["save_discovery_handoff"],
            "next": [],
        },
        {
            "id": "save_discovery_handoff",
            "type": "workflow_project",
            "name": "Save discovery handoff",
            "config": {
                "operation": "patch",
                "project_id": "{{project.project_id}}",
                "expected_revision": "{{project.revision}}",
                "patch": {
                    "business_outcome": "needs_input",
                    "retry_state": {
                        "phase": "discovery",
                        "step_id": "explore_product",
                        "segment_ids": [],
                        "observed_problem": "{{discovered_journey.gaps}}",
                        "required_change": "Correct the start URL, login state, permissions, or requested must-show scope.",
                        "editable_input_schema": deepcopy(PRODUCT_VIDEO_REQUEST_RETRY_SCHEMA),
                        "preserved_receipts": [],
                        "retry_from_step_id": "browser_preflight",
                    },
                },
                "current_stage": "blocked",
                "allowed_stages": PROJECT_STAGES,
                "state_schema": "{{product_video_project_schema}}",
                "output_var": "project",
            },
            "next": ["build_discovery_handoff"],
        },
        {
            "id": "build_discovery_handoff",
            "type": "transform",
            "name": "Build discovery handoff",
            "config": {
                "set": {
                    "input": {
                        "business_outcome": "needs_input",
                        "project_id": "{{project.project_id}}",
                        "project_root": "{{project.state.project_root}}",
                        "retry_from_step_id": "browser_preflight",
                        "retry_segment_ids": [],
                        "observed_problem": "{{project.state.retry_state.observed_problem}}",
                        "required_change": "{{project.state.retry_state.required_change}}",
                        "preserved_receipts": "{{project.state.retry_state.preserved_receipts}}",
                        "final_video": None,
                    }
                }
            },
            "next": ["needs_input"],
        },
        _agent_step(
            "plan_video",
            "Create script and asset plan",
            service_key="product_video.planning",
            tools=PLANNER_TOOLS,
            prompt=(
                "Create one evidence-backed product video plan and write 00-project-overview.md, "
                "01-script.md, 02-shot-list.md, and the planned 03-file-ledger.json under the exact project "
                "root {{project.state.project_root}}. For each code bundle call, set generate_file name exactly "
                "equal to {{project.state.project_root}} and use only relative child paths such as 01-script.md, "
                "02-shot-list.md, and 03-file-ledger.json. Never repeat project_root in a child path or add a "
                "wrapper project directory. Every scene is a stable Retry Segment ID with an exact "
                "target_page, precondition, ordered_actions, expected_visual_state, canonical_narration, "
                "required_asset_types, acceptance_evidence, target_duration_seconds, dependencies, recovery, and "
                "privacy. Every request.must_show item must map to one or more explicit scene_id values and "
                "acceptance_evidence entries; copy the requested item into acceptance_evidence.must_show and into "
                "one must_show_coverage entry containing the exact requirement, non-empty scene_ids, and non-empty "
                "acceptance_evidence. Set must_show_coverage_complete true only when every request.must_show value "
                "appears exactly in that map, and return covered_must_show as those mapped requirement strings in "
                "the exact request.must_show order. Create "
                "generated video scenes only for request.must_show, although one item may require more than one "
                "scene. No product claim may appear in canonical_narration without an evidence-backed scene. "
                "Knowledge completeness and final playback are acceptance checks, not generated video scenes. "
                "Budget every scene's canonical narration for a clear natural delivery within that scene's "
                "target_duration_seconds; shorten copy instead of assuming production can speed it up. "
                "Browser scenes may depend only on states available before post-production. Narration audio, "
                "subtitle files, final MP4 playback, machine QA, and final operator acceptance are downstream "
                "workflow outputs; never put them in a scene precondition, ordered action, expected visual state, "
                "required asset type, or acceptance evidence. Use only routes and controls observed in discovery. "
                "Treat the supplied start input as authoritative; promotion_goal and final_cta may shape claims "
                "only inside the evidence-backed must-show scenes. Request: {{request}}. "
                "Discovery: {{discovered_journey}}. Return only JSON."
            ),
            output_var="plan",
            output_schema=PRODUCT_VIDEO_PLAN_SCHEMA,
            next_steps=["validate_plan_contract"],
        ),
        {
            "id": "validate_plan_contract",
            "type": "code",
            "name": "Validate plan scene coverage",
            "config": {
                "language": "python",
                "code": PLAN_CONTRACT_VALIDATOR_CODE,
                "inputs": [
                    {"key": "request", "value": "{{request}}", "type": "json"},
                    {"key": "plan", "value": "{{plan}}", "type": "json"},
                ],
                "output_format": "json",
                "output_var": "plan_validation",
            },
            "next": ["plan_coverage_complete"],
        },
        {
            "id": "plan_coverage_complete",
            "type": "condition",
            "name": "Every must-show requirement is mapped",
            "config": {"expression": "plan_validation.valid == true"},
            "true_next": ["save_plan"],
            "false_next": ["save_plan_coverage_handoff"],
            "next": [],
        },
        {
            "id": "save_plan_coverage_handoff",
            "type": "workflow_project",
            "name": "Save incomplete plan coverage handoff",
            "config": {
                "operation": "patch",
                "project_id": "{{project.project_id}}",
                "expected_revision": "{{project.revision}}",
                "patch": {
                    "business_outcome": "needs_input",
                    "current_phase": "plan",
                    "retry_state": {
                        "phase": "plan",
                        "step_id": "plan_coverage_complete",
                        "segment_ids": "{{plan.scene_ids}}",
                        "observed_problem": "{{plan_validation}}",
                        "required_change": (
                            "Map every request.must_show value to explicit scene IDs and acceptance evidence."
                        ),
                        "editable_input_schema": deepcopy(
                            PRODUCT_VIDEO_REQUEST_RETRY_SCHEMA
                        ),
                        "preserved_receipts": [],
                        "retry_from_step_id": "plan_video",
                    },
                },
                "current_stage": "blocked",
                "allowed_stages": PROJECT_STAGES,
                "state_schema": "{{product_video_project_schema}}",
                "output_var": "project",
            },
            "next": ["build_plan_coverage_handoff"],
        },
        {
            "id": "build_plan_coverage_handoff",
            "type": "transform",
            "name": "Build incomplete plan coverage handoff",
            "config": {
                "set": {
                    "input": {
                        "business_outcome": "needs_input",
                        "project_id": "{{project.project_id}}",
                        "retry_from_step_id": "plan_video",
                        "retry_segment_ids": "{{plan.scene_ids}}",
                        "final_video": None,
                    }
                }
            },
            "next": ["needs_input"],
        },
        {
            "id": "save_plan",
            "type": "workflow_project",
            "name": "Save plan checkpoint",
            "config": {
                "operation": "patch",
                "project_id": "{{project.project_id}}",
                "expected_revision": "{{project.revision}}",
                "patch": {
                    "request": "{{request}}",
                    "product_source": {
                        "start_url": "{{request.start_url}}",
                        "browser_session": "{{request.browser_session}}",
                    },
                    "output_profile": "{{request.output_profile}}",
                    "plan": "{{plan}}",
                    "scenes": "{{plan.scenes}}",
                    "segments": "{{plan.scenes}}",
                    "current_phase": "plan",
                    "checkpoints": {
                        "plan": {
                            "status": "completed",
                            "step_id": "plan_video",
                            "ledger_path": "{{project.state.ledger_path}}",
                        }
                    },
                },
                "current_stage": "awaiting_plan_approval",
                "allowed_stages": PROJECT_STAGES,
                "state_schema": "{{product_video_project_schema}}",
                "output_var": "project",
            },
            "next": [after_plan],
        },
        {
            "id": "build_plan_result",
            "type": "transform",
            "name": "Build plan result",
            "config": {
                "set": {
                    "input": {
                        "business_outcome": "in_progress",
                        "project_id": "{{project.project_id}}",
                        "project_root": "{{project.state.project_root}}",
                        "plan": "{{project.state.plan}}",
                        "retry_from_step_id": None,
                        "retry_segment_ids": [],
                        "final_video": None,
                    }
                }
            },
            "next": ["plan_ready"],
        },
        {
            "id": "plan_ready",
            "type": "end",
            "name": "Product video plan ready",
            "config": {"no_external_publish": True},
            "next": [],
        },
        {
            "id": "needs_input",
            "type": "end",
            "name": "Input required",
            "config": {"no_external_publish": True},
            "next": [],
        },
    ]


def _collector_agent_step(
    step_id: str,
    name: str,
    *,
    segments_ref: str,
    retry_ids_ref: str,
    next_step: str,
) -> dict[str, Any]:
    return _agent_step(
        step_id,
        name,
        service_key="product_video.capture",
        skill="screen_asset_collector",
        tools=COLLECTOR_TOOLS,
        prompt=(
            "Collect the approved product-video asset batch in the current paired Chrome session. The complete "
            f"approved shot list is {segments_ref}; operate only on Retry Segment IDs {retry_ids_ref}. Use the "
            "authoritative product source {{project.state.product_source}}, capture grant "
            "{{capture_grant.grant_id}}, and exact project root {{project.state.project_root}}. Split work "
            "internally into bounded Chrome phases containing one coherent operation or at most two adjacent "
            "assets. Use one Browser Group, create separate retryable clips, stop recording during long waits, "
            "and call finalize_tabs only in the final phase. Execute only each scene's approved ordered_actions, "
            "in order, on target_page: restore precondition, observe expected_visual_state, and return every "
            "required_asset_types asset and its acceptance_evidence. A product-side effect reuse decision does not "
            "authorize skipping the requested screenshot or recording. Reuse existing ready artifact IDs only "
            "when their provenance and acceptance evidence match the same scene_id and required asset type. For "
            "retry or reuse, consider only validator-approved artifacts already persisted in project.state.artifacts "
            "({{project.state.artifacts}}). Never reuse raw artifacts or preserved receipts from collection_result "
            "or an earlier blocker; otherwise capture the missing asset. Preserve every Segment ID and required "
            "asset type. Dashboard, "
            "home, generic navigation, and other pages are unrelated proxy footage and cannot replace the approved "
            "target_page or expected_visual_state. Each ready browser capture must have a durable Document receipt "
            "under the project root. Update 03-file-ledger.json and technical/asset-manifest.json, inspect each "
            "final bitmap against all acceptance_evidence, and return the complete collection contract. On any "
            "blocker, return scene_id, observed_state, required_change, and preserved_receipts; do not continue to "
            "another scene or fabricate evidence, then stop this collection attempt so the user can edit input "
            "and retry that scene. Never publish."
        ),
        output_var="collection_result",
        output_schema=COLLECTION_RESULT_SCHEMA,
        forced_tool_calls=[
            {
                "name": "invoke_skill",
                "arguments": {
                    "skill": "chrome",
                    "input": (
                        "Use Chrome for the approved product-video asset batch at "
                        "{{project.state.product_source}}. Work only inside project root "
                        "{{project.state.project_root}} and only on the supplied Retry Segment IDs. Return to "
                        "each exact approved target_page and execute only ordered_actions after verifying the "
                        "precondition. Verify target_page, ordered_actions, and expected_visual_state exactly; "
                        "never substitute a similarly named global or sidebar control. Never substitute Dashboard, "
                        "home, or another page for the required target state. "
                        "Before deciding reuse, consider only validator-approved project artifacts from "
                        "project.state.artifacts and ignore raw collection results or blocker receipts. "
                        "Wait for requested states and re-read them after navigation before reporting an access "
                        "blocker. Do not invent scrolling or navigation for static recordings. Once acceptance is "
                        "visible, stop exploring, inspect required form fields and the actual bitmap, persist one "
                        "durable PNG and WebM when required, and preserve all completed receipts. Use bounded "
                        "phases and call finalize_tabs only after the final phase. Never publish."
                    ),
                },
            }
        ],
        next_steps=[next_step],
        timeout=7200,
    )


def _flat_media_pipeline_steps(
    *,
    selected_segments_ref: str,
    selected_segment_ids_ref: str,
) -> list[dict[str, Any]]:
    return [
        _collector_agent_step(
            "collect_assets",
            "Collect approved asset batch",
            segments_ref=selected_segments_ref,
            retry_ids_ref=(
                f"{selected_segment_ids_ref} on the first attempt, or only workflow input "
                "{{retry_segment_ids}} when non-empty on a Chat retry"
            ),
            next_step="validate_collection_contract",
        ),
        {
            "id": "validate_collection_contract",
            "type": "code",
            "name": "Validate collected scene evidence",
            "config": {
                "language": "python",
                "code": COLLECTION_CONTRACT_VALIDATOR_CODE,
                "inputs": [
                    {
                        "key": "selected_scenes",
                        "value": selected_segments_ref,
                        "type": "json",
                    },
                    {
                        "key": "selected_scene_ids",
                        "value": selected_segment_ids_ref,
                        "type": "json",
                    },
                    {
                        "key": "granted_scene_ids",
                        "value": "{{capture_grant.scope.scene_ids}}",
                        "type": "json",
                    },
                    {
                        "key": "collection_result",
                        "value": "{{collection_result}}",
                        "type": "json",
                    },
                ],
                "output_format": "json",
                "output_var": "collection_validation",
            },
            "next": ["collection_valid"],
        },
        {
            "id": "collection_valid",
            "type": "condition",
            "name": "Collection evidence is valid",
            "config": {"expression": "collection_validation.valid == true"},
            "true_next": ["use_collection_result"],
            "false_next": ["save_capture_handoff"],
            "next": [],
        },
        {
            "id": "use_collection_result",
            "type": "transform",
            "name": "Use initial collection",
            "config": {
                "set": {
                    "effective_collection": {
                        "manifest_path": "{{collection_result.manifest_path}}",
                        "segments": "{{collection_validation.validated_segments}}",
                        "artifacts": "{{collection_validation.validated_artifacts}}",
                    }
                }
            },
            "next": ["save_collection_checkpoint"],
        },
        {
            "id": "save_collection_checkpoint",
            "type": "workflow_project",
            "name": "Save collection checkpoint",
            "config": {
                "operation": "patch",
                "project_id": "{{project.project_id}}",
                "expected_revision": "{{project.revision}}",
                "patch": {
                    "artifact_manifest_path": "{{effective_collection.manifest_path}}",
                    "current_phase": "production",
                    "business_outcome": "in_progress",
                    "retry_state": None,
                    "checkpoints": {
                        "capture": {
                            "status": "completed",
                            "step_id": "save_collection_checkpoint",
                            "manifest_path": "{{effective_collection.manifest_path}}",
                        }
                    },
                },
                "list_appends": [
                    {
                        "path": "scenes",
                        "key": "scene_id",
                        "items": "{{effective_collection.segments}}",
                    },
                    {
                        "path": "segments",
                        "key": "scene_id",
                        "items": "{{effective_collection.segments}}",
                    },
                    {
                        "path": "artifacts",
                        "key": "artifact_id",
                        "items": "{{effective_collection.artifacts}}",
                    }
                ],
                "current_stage": "producing",
                "allowed_stages": PROJECT_STAGES,
                "state_schema": "{{product_video_project_schema}}",
                "output_var": "project",
            },
            "next": ["produce_video"],
        },
        {
            "id": "save_capture_handoff",
            "type": "workflow_project",
            "name": "Save capture handoff",
            "config": {
                "operation": "patch",
                "project_id": "{{project.project_id}}",
                "expected_revision": "{{project.revision}}",
                "patch": {
                    "artifact_manifest_path": "{{collection_result.manifest_path}}",
                    "current_phase": "capture",
                    "business_outcome": "needs_input",
                    "retry_state": {
                        "phase": "capture",
                        "step_id": "collect_assets",
                        "scene_id": "{{collection_validation.blocker.scene_id}}",
                        "segment_ids": "{{collection_validation.retry_segment_ids}}",
                        "observed_state": "{{collection_validation.blocker.observed_state}}",
                        "observed_problem": "{{collection_validation.blocker.observed_state}}",
                        "required_change": "{{collection_validation.blocker.required_change}}",
                        "editable_input_schema": {
                            "type": "object",
                            "properties": {
                                "retry_segment_ids": {
                                    "type": "array",
                                    "minItems": 1,
                                    "items": {"type": "string", "minLength": 1},
                                },
                                "revision_notes": {"type": "string"},
                            },
                            "required": ["retry_segment_ids"],
                            "additionalProperties": False,
                        },
                        "preserved_receipts": "{{collection_validation.blocker.preserved_receipts}}",
                        "retry_from_step_id": "collect_assets",
                    },
                },
                "list_appends": [
                    {
                        "path": "scenes",
                        "key": "scene_id",
                        "items": "{{collection_validation.validated_segments}}",
                    },
                    {
                        "path": "segments",
                        "key": "scene_id",
                        "items": "{{collection_validation.validated_segments}}",
                    },
                    {
                        "path": "artifacts",
                        "key": "artifact_id",
                        "items": "{{collection_validation.validated_artifacts}}",
                    }
                ],
                "current_stage": "blocked",
                "allowed_stages": PROJECT_STAGES,
                "state_schema": "{{product_video_project_schema}}",
                "output_var": "project",
            },
            "next": ["build_capture_handoff"],
        },
        {
            "id": "build_capture_handoff",
            "type": "transform",
            "name": "Build capture handoff",
            "config": {
                "set": {
                    "input": {
                        "business_outcome": "needs_input",
                        "project_id": "{{project.project_id}}",
                        "retry_from_step_id": "collect_assets",
                        "retry_segment_ids": "{{collection_validation.retry_segment_ids}}",
                        "final_video": None,
                    }
                }
            },
            "next": ["needs_input"],
        },
        _agent_step(
            "produce_video",
            "Produce final video package",
            service_key="product_video.production",
            tools=PRODUCER_TOOLS,
            prompt=(
                "Produce from this validated project state: {{project.state}}. Keep every output under the exact "
                "project root. Honor project.state.retry_state.required_change and operator revision notes. When "
                "an approved scene recording is known to contradict its verified screenshot and recapture is not "
                "required, use the planned screenshot as that scene's visual source instead of the contradictory "
                "recording; never fabricate product UI. Require every selected Segment ID to have ready durable "
                "recording and screenshot "
                "receipts. Pass recording document_id values directly to merge_videos in approved story order. "
                "Never pass a display path beginning with Knowledge/ as a media-tool path, and never reconstruct "
                "an executable path when a durable document_id is already present. Build or reuse the clean "
                "picture master, convert only planned stills, and remove or accelerate non-critical waits. Generate "
                "one continuous final narration take from plan.canonical_narration through the Account-selected "
                "BYOK voice model, then normalize that final audio. Do not synthesize or stitch per-scene narration. "
                "Pass the final narration audio, canonical transcript, and explicit visual scene intervals to "
                "align_subtitles and require measured semantic sentence/word timestamps. Never use proportional cue "
                "scaling. Produce ASS subtitles, never SRT, using the v3 visual baseline reduced by two type "
                "points: 52 px at 1080p and 35 px at 720p (scale by the canvas short edge), a two-line maximum, "
                "2 px outline and 72 px bottom-safe margin at 1080p. Map "
                "aligned sentence intervals to visual scene boundaries. Use the "
                "measured narration boundaries to rebuild the clean picture master so every narration interval falls "
                "inside its matching visual scene interval without recapturing already sufficient source footage. "
                "Compute each narration span "
                "and visual duration directly from timeline.visual_scene_intervals; when the resulting deficit "
                "exceeds min(0.75 seconds, 10% "
                "of the required narration span), return retake_required with deficient_scene_ids, matching "
                "retry_segment_ids, required_change, and per-scene deficit_evidence containing the narration span, "
                "available visual duration, computed deficit, allowed threshold, and threshold formula. Never return "
                "empty scene IDs or a narration-only generic cause. Never time-stretch unrelated video to hide a "
                "duration deficit. Compose the final MP4 only after every scene interval is supported. A ready result "
                "must return specialized narration/audio, subtitles/subtitle, and final_video/video ArtifactRefs, "
                "non-empty visual_scene_intervals and scene_boundaries, and alignment_metrics copied from measured "
                "alignment with similarity >= 0.90, coverage >= 0.95, plural timing_sources, transcription_model, "
                "and scene_coverage with no missing scene or interval IDs. If production cannot complete for a "
                "non-visual-deficit reason, return blocked with blocker_type, structured observed_problem, "
                "required_change, and preserved_receipts, and leave all scene deficit fields empty. Never mark a "
                "silent or unaligned final video ready. Reuse matching checkpoints. "
                "Never pin a provider or model, change product claims, or publish."
            ),
            output_var="production_result",
            output_schema=PRODUCTION_RESULT_SCHEMA,
            next_steps=["validate_production_contract"],
            timeout=7200,
        ),
        {
            "id": "validate_production_contract",
            "type": "code",
            "name": "Validate production result",
            "config": {
                "language": "python",
                "code": PRODUCTION_CONTRACT_VALIDATOR_CODE,
                "inputs": [
                    {
                        "key": "production_result",
                        "value": "{{production_result}}",
                        "type": "json",
                    },
                    {
                        "key": "plan",
                        "value": "{{project.state.plan}}",
                        "type": "json",
                    },
                ],
                "output_format": "json",
                "output_var": "production_validation",
            },
            "next": ["production_contract_valid"],
        },
        {
            "id": "production_contract_valid",
            "type": "condition",
            "name": "Production result is internally valid",
            "config": {"expression": "production_validation.valid == true"},
            "true_next": ["production_status_ready"],
            "false_next": ["save_invalid_production_handoff"],
            "next": [],
        },
        {
            "id": "production_status_ready",
            "type": "condition",
            "name": "Production is ready",
            "config": {"expression": "production_result.status == 'ready'"},
            "true_next": ["save_production_checkpoint"],
            "false_next": ["production_status_retake"],
            "next": [],
        },
        {
            "id": "production_status_retake",
            "type": "condition",
            "name": "Production needs scene retakes",
            "config": {"expression": "production_result.status == 'retake_required'"},
            "true_next": ["save_production_handoff"],
            "false_next": ["save_blocked_production_handoff"],
            "next": [],
        },
        {
            "id": "save_production_checkpoint",
            "type": "workflow_project",
            "name": "Save production checkpoint",
            "config": {
                "operation": "patch",
                "project_id": "{{project.project_id}}",
                "expected_revision": "{{project.revision}}",
                "patch": {
                    "timeline": "{{production_result.timeline}}",
                    "final_artifacts": "{{production_result.artifacts}}",
                    "current_phase": "quality",
                    "checkpoints": {
                        "production": {
                            "status": "completed",
                            "step_id": "produce_video",
                            "final_video": "{{production_result.timeline.final_video.artifact_id}}",
                        }
                    },
                },
                "list_appends": [
                    {
                        "path": "artifacts",
                        "key": "artifact_id",
                        "items": "{{production_result.artifacts}}",
                    }
                ],
                "current_stage": "quality_gate",
                "allowed_stages": PROJECT_STAGES,
                "state_schema": "{{product_video_project_schema}}",
                "output_var": "project",
            },
            "next": ["probe_final"],
        },
        {
            "id": "save_production_handoff",
            "type": "workflow_project",
            "name": "Save production handoff",
            "config": {
                "operation": "patch",
                "project_id": "{{project.project_id}}",
                "expected_revision": "{{project.revision}}",
                "patch": {
                    "timeline": "{{production_result.timeline}}",
                    "final_artifacts": "{{production_result.artifacts}}",
                    "current_phase": "production",
                    "business_outcome": "needs_input",
                    "retry_state": {
                        "phase": "production",
                        "step_id": "produce_video",
                        "segment_ids": "{{production_result.retry_segment_ids}}",
                        "observed_problem": "{{production_result.deficit_evidence}}",
                        "required_change": "{{production_result.required_change}}",
                        "editable_input_schema": {
                            "type": "object",
                            "properties": {
                                "retry_segment_ids": {
                                    "type": "array",
                                    "minItems": 1,
                                    "items": {"type": "string", "minLength": 1},
                                },
                                "revision_notes": {"type": "string"},
                            },
                            "required": ["retry_segment_ids"],
                            "additionalProperties": False,
                        },
                        "preserved_receipts": "{{production_result.artifacts}}",
                        "retry_from_step_id": "collect_assets",
                    },
                },
                "current_stage": "blocked",
                "allowed_stages": PROJECT_STAGES,
                "state_schema": "{{product_video_project_schema}}",
                "output_var": "project",
            },
            "next": ["build_production_handoff"],
        },
        {
            "id": "build_production_handoff",
            "type": "transform",
            "name": "Build production handoff",
            "config": {
                "set": {
                    "input": {
                        "business_outcome": "needs_input",
                        "project_id": "{{project.project_id}}",
                        "retry_from_step_id": "collect_assets",
                        "retry_segment_ids": "{{production_result.retry_segment_ids}}",
                        "final_video": None,
                    }
                }
            },
            "next": ["needs_input"],
        },
        {
            "id": "save_invalid_production_handoff",
            "type": "workflow_project",
            "name": "Save invalid production result handoff",
            "config": {
                "operation": "patch",
                "project_id": "{{project.project_id}}",
                "expected_revision": "{{project.revision}}",
                "patch": {
                    "current_phase": "production",
                    "business_outcome": "needs_input",
                    "retry_state": {
                        "phase": "production",
                        "step_id": "validate_production_contract",
                        "segment_ids": [],
                        "observed_problem": "{{production_validation}}",
                        "required_change": (
                            "Correct the production result contract and retry production."
                        ),
                        "editable_input_schema": {
                            "type": "object",
                            "properties": {"revision_notes": {"type": "string"}},
                            "additionalProperties": False,
                        },
                        "preserved_receipts": "{{production_result.artifacts}}",
                        "retry_from_step_id": "produce_video",
                    },
                },
                "list_appends": [
                    {
                        "path": "artifacts",
                        "key": "artifact_id",
                        "items": "{{production_result.artifacts}}",
                    }
                ],
                "current_stage": "blocked",
                "allowed_stages": PROJECT_STAGES,
                "state_schema": "{{product_video_project_schema}}",
                "output_var": "project",
            },
            "next": ["build_invalid_production_handoff"],
        },
        {
            "id": "build_invalid_production_handoff",
            "type": "transform",
            "name": "Build invalid production result handoff",
            "config": {
                "set": {
                    "input": {
                        "business_outcome": "needs_input",
                        "project_id": "{{project.project_id}}",
                        "retry_from_step_id": "produce_video",
                        "retry_segment_ids": [],
                        "final_video": None,
                    }
                }
            },
            "next": ["needs_input"],
        },
        {
            "id": "save_blocked_production_handoff",
            "type": "workflow_project",
            "name": "Save blocked production handoff",
            "config": {
                "operation": "patch",
                "project_id": "{{project.project_id}}",
                "expected_revision": "{{project.revision}}",
                "patch": {
                    "current_phase": "production",
                    "business_outcome": "needs_input",
                    "retry_state": {
                        "phase": "production",
                        "step_id": "produce_video",
                        "segment_ids": [],
                        "observed_problem": (
                            "{{production_result.blocker.observed_problem}}"
                        ),
                        "required_change": (
                            "{{production_result.blocker.required_change}}"
                        ),
                        "editable_input_schema": {
                            "type": "object",
                            "properties": {"revision_notes": {"type": "string"}},
                            "additionalProperties": False,
                        },
                        "preserved_receipts": (
                            "{{production_result.blocker.preserved_receipts}}"
                        ),
                        "retry_from_step_id": "produce_video",
                    },
                },
                "list_appends": [
                    {
                        "path": "artifacts",
                        "key": "artifact_id",
                        "items": "{{production_result.blocker.preserved_receipts}}",
                    }
                ],
                "current_stage": "blocked",
                "allowed_stages": PROJECT_STAGES,
                "state_schema": "{{product_video_project_schema}}",
                "output_var": "project",
            },
            "next": ["build_blocked_production_handoff"],
        },
        {
            "id": "build_blocked_production_handoff",
            "type": "transform",
            "name": "Build blocked production handoff",
            "config": {
                "set": {
                    "input": {
                        "business_outcome": "needs_input",
                        "project_id": "{{project.project_id}}",
                        "retry_from_step_id": "produce_video",
                        "retry_segment_ids": [],
                        "final_video": None,
                    }
                }
            },
            "next": ["needs_input"],
        },
        {
            "id": "probe_final",
            "type": "tool",
            "name": "Probe final media",
            "config": {
                "tool": "probe_media",
                "args": {"input_path": "{{project.state.timeline.final_video.fs_path}}"},
                "output_format": "json",
                "output_var": "media_probe",
            },
            "next": ["analyze_final_audio"],
        },
        {
            "id": "analyze_final_audio",
            "type": "tool",
            "name": "Analyze final audio",
            "config": {
                "tool": "analyze_audio",
                "args": {"input_path": "{{project.state.timeline.final_video.fs_path}}"},
                "output_format": "json",
                "output_var": "audio_analysis",
            },
            "next": ["validate_final_subtitles"],
        },
        {
            "id": "validate_final_subtitles",
            "type": "tool",
            "name": "Validate final subtitles",
            "config": {
                "tool": "validate_subtitles",
                "args": {
                    "subtitle_path": "{{project.state.timeline.subtitles.fs_path}}",
                    "media_path": "{{project.state.timeline.final_video.fs_path}}",
                    "max_lines": 2,
                },
                "output_format": "json",
                "output_var": "subtitle_analysis",
            },
            "next": ["render_evidence_frames"],
        },
        {
            "id": "render_evidence_frames",
            "type": "tool",
            "name": "Render evidence frames",
            "config": {
                "tool": "render_frame_samples",
                "args": {
                    "input_path": "{{project.state.timeline.final_video.fs_path}}",
                    "output_dir": "{{project.state.project_root}}/technical/qa-frames",
                    "scene_boundaries": "{{project.state.timeline.scene_boundaries}}",
                    "max_samples": 24,
                },
                "output_format": "json",
                "output_var": "frame_evidence",
            },
            "next": ["review_quality"],
        },
        _agent_step(
            "review_quality",
            "Review deterministic and visual evidence",
            service_key="product_video.quality",
            tools=QUALITY_TOOLS,
            prompt=(
                "Review the approved request, plan, asset manifest, final media probe, audio analysis, subtitle "
                "validation, and sampled frames. Treat the supplied probe, audio, subtitle, and frame results as authoritative; "
                "do not probe, analyze, validate, or render a second evidence set inside this review. Probe final video and narration audio evidence, then require acceptance "
                "evidence for every request.must_show scene; copy every request.must_show value into coverage and "
                "reject missing evidence and unrelated proxy footage. Open and inspect every sampled frame bitmap "
                "with read_file using its fs_path. Never infer visible content from URLs, receipt metadata, scene "
                "IDs, or timestamps. The visible labels and target state for each must-show item must appear in at "
                "least one inspected final-video frame; source recordings and screenshots cannot substitute for "
                "final-video evidence. Only cite frame document IDs that visibly satisfy the stated target, never "
                "every frame assigned to the scene by timestamp. At least one cited target-state frame must occur "
                "within that scene's measured narration interval; a target first appearing after narration ends is "
                "a blocking sync revision even when it appears before the visual scene interval ends. Return "
                "non-empty technical_checks and "
                "coverage. Set coverage_complete true only when every must-show requirement is covered by scene IDs; "
                "set evidence_complete true only when every covered scene has durable evidence; set "
                "measured_sync_passed true only when measured cue-to-scene sync passes. machine_pass requires all "
                "three booleans true and every coverage status covered. Return covered_must_show as the covered "
                "requirement strings in exact project.state.request.must_show order. "
                "Validate subtitle style at 52 px at 1080p or 35 px at 720p, no more than two lines, a proportional "
                "outline (2 px at 1080p), Shadow=0, and a proportional bottom-safe margin (72 px at 1080p). Check "
                "measured cue-to-scene sync against the explicit visual "
                "scene intervals, plus wrong or "
                "private states, UI overlap, claims, and no-publish status. When the source brief explicitly permits "
                "local test data, treat an obvious synthetic fixture identity such as John Doe in the product's "
                "standard sidebar as allowed test content, not personal data or identity emphasis by itself. This "
                "exception never covers credentials, tokens, email addresses, phone numbers, or other real "
                "identifiers, and an explicit request to hide all account labels still wins. Write 04-qa-report.md "
                "under the project "
                "root and return only the QualityResult JSON. A successful machine result is machine_pass, never "
                "operator acceptance. "
                "Project: {{project.state}}. Probe: {{media_probe}}. Audio: {{audio_analysis}}. Subtitles: "
                "{{subtitle_analysis}}. Frames: {{frame_evidence}}."
            ),
            output_var="quality_result",
            output_schema=QUALITY_RESULT_SCHEMA,
            next_steps=["validate_quality_contract"],
            timeout=1800,
        ),
        {
            "id": "validate_quality_contract",
            "type": "code",
            "name": "Validate quality evidence",
            "config": {
                "language": "python",
                "code": QUALITY_CONTRACT_VALIDATOR_CODE,
                "inputs": [
                    {
                        "key": "request",
                        "value": "{{project.state.request}}",
                        "type": "json",
                    },
                    {
                        "key": "plan",
                        "value": "{{project.state.plan}}",
                        "type": "json",
                    },
                    {
                        "key": "quality_result",
                        "value": "{{quality_result}}",
                        "type": "json",
                    },
                    {
                        "key": "artifacts",
                        "value": "{{project.state.artifacts}}",
                        "type": "json",
                    },
                    {
                        "key": "timeline",
                        "value": "{{project.state.timeline}}",
                        "type": "json",
                    },
                    {
                        "key": "media_probe",
                        "value": "{{media_probe}}",
                        "type": "json",
                    },
                    {
                        "key": "audio_analysis",
                        "value": "{{audio_analysis}}",
                        "type": "json",
                    },
                    {
                        "key": "subtitle_analysis",
                        "value": "{{subtitle_analysis}}",
                        "type": "json",
                    },
                    {
                        "key": "frame_evidence",
                        "value": "{{frame_evidence}}",
                        "type": "json",
                    },
                ],
                "output_format": "json",
                "output_var": "quality_validation",
            },
            "next": ["quality_passed"],
        },
        {
            "id": "quality_passed",
            "type": "condition",
            "name": "Quality machine pass",
            "config": {"expression": "quality_validation.valid == true"},
            "true_next": ["save_quality_ready"],
            "false_next": ["save_revision_required"],
            "next": [],
        },
        {
            "id": "save_quality_ready",
            "type": "workflow_project",
            "name": "Save completed quality checkpoint",
            "config": {
                "operation": "patch",
                "project_id": "{{project.project_id}}",
                "expected_revision": "{{project.revision}}",
                "patch": {
                    "quality_result": "{{quality_result}}",
                    "current_phase": "completed",
                    "business_outcome": "completed",
                    "retry_state": None,
                    "checkpoints": {
                        "quality": {"status": "completed", "step_id": "review_quality"}
                    },
                },
                "current_stage": "completed",
                "allowed_stages": PROJECT_STAGES,
                "state_schema": "{{product_video_project_schema}}",
                "output_var": "project",
            },
            "next": ["build_completed_result"],
        },
        {
            "id": "build_completed_result",
            "type": "transform",
            "name": "Build completed result",
            "config": {
                "set": {
                    "input": {
                        "business_outcome": "completed",
                        "project_id": "{{project.project_id}}",
                        "retry_from_step_id": None,
                        "retry_segment_ids": [],
                        "final_video": "{{project.state.timeline.final_video}}",
                    }
                }
            },
            "next": ["done"],
        },
        {
            "id": "save_revision_required",
            "type": "workflow_project",
            "name": "Save revision outcome",
            "config": {
                "operation": "patch",
                "project_id": "{{project.project_id}}",
                "expected_revision": "{{project.revision}}",
                "patch": {
                    "quality_result": "{{quality_result}}",
                    "business_outcome": "revision_required",
                    "current_phase": "revision",
                    "retry_state": {
                        "phase": "quality",
                        "step_id": "{{quality_validation.retry_from_step_id}}",
                        "segment_ids": "{{retry_segment_ids}}",
                        "observed_problem": {
                            "contract_validation": "{{quality_validation}}",
                            "visual_findings": "{{quality_result.visual_findings}}",
                        },
                        "required_change": "Select finding or Segment IDs and run Revise product video.",
                        "editable_input_schema": {
                            "type": "object",
                            "properties": {
                                "retry_segment_ids": {"type": "array", "items": {"type": "string"}},
                                "finding_ids": {"type": "array", "items": {"type": "string"}},
                                "revision_notes": {"type": "string"},
                            },
                        },
                        "preserved_receipts": "{{project.state.final_artifacts}}",
                        "retry_from_step_id": "{{quality_validation.retry_from_step_id}}",
                    },
                },
                "current_stage": "revision_required",
                "allowed_stages": PROJECT_STAGES,
                "state_schema": "{{product_video_project_schema}}",
                "output_var": "project",
            },
            "next": ["build_revision_result"],
        },
        {
            "id": "build_revision_result",
            "type": "transform",
            "name": "Build revision result",
            "config": {
                "set": {
                    "input": {
                        "business_outcome": "revision_required",
                        "project_id": "{{project.project_id}}",
                        "retry_from_step_id": "{{quality_validation.retry_from_step_id}}",
                        "retry_segment_ids": "{{retry_segment_ids}}",
                        "final_video": "{{project.state.timeline.final_video}}",
                    }
                }
            },
            "next": ["revision_required"],
        },
        {"id": "done", "type": "end", "name": "Completed product video", "config": {"no_external_publish": True}, "next": []},
        {"id": "revision_required", "type": "end", "name": "Revision required", "config": {"no_external_publish": True}, "next": []},
        {"id": "needs_input", "type": "end", "name": "Input required", "config": {"no_external_publish": True}, "next": []},
    ]


def _flat_create_steps() -> list[dict[str, Any]]:
    steps = _flat_planning_steps(terminal=False)
    steps = [
        step
        for step in steps
        if step["id"] not in {"build_plan_result", "plan_ready", "needs_input"}
    ]
    steps.extend(
        [
            {
                "id": "approve_plan",
                "type": "wait",
                "name": "Approve product video plan",
                "config": {
                    "wait_type": "approval",
                    "message": "Review the promise, narration, Segment IDs, side effects, output profile, and capture scope.",
                    "review_title": "Product video plan",
                    "review": "{{plan}}",
                    "response_variable": "plan_decision",
                    "options": ["approve", "revise"],
                    "approval_values": ["approve"],
                    "requires_operator_approval": True,
                    "chat_projection": "action",
                },
                "next": ["plan_approved"],
            },
            {
                "id": "plan_approved",
                "type": "condition",
                "name": "Plan approved",
                "config": {"expression": "plan_decision.choice == 'approve'"},
                "true_next": ["lock_approved_plan"],
                "false_next": ["save_plan_revision_required"],
                "next": [],
            },
            {
                "id": "save_plan_revision_required",
                "type": "workflow_project",
                "name": "Save requested plan revision",
                "config": {
                    "operation": "patch",
                    "project_id": "{{project.project_id}}",
                    "expected_revision": "{{project.revision}}",
                    "patch": {
                        "business_outcome": "revision_required",
                        "current_phase": "plan",
                        "retry_state": {
                            "phase": "plan",
                            "step_id": "approve_plan",
                            "segment_ids": [],
                            "observed_problem": "The operator requested plan changes.",
                            "required_change": "Update the request or revision notes and revise the plan.",
                            "editable_input_schema": deepcopy(PRODUCT_VIDEO_REQUEST_RETRY_SCHEMA),
                            "preserved_receipts": [],
                            "retry_from_step_id": "plan_video",
                        },
                    },
                    "current_stage": "revision_required",
                    "allowed_stages": PROJECT_STAGES,
                    "state_schema": "{{product_video_project_schema}}",
                    "output_var": "project",
                },
                "next": ["build_plan_revision_result"],
            },
            {
                "id": "build_plan_revision_result",
                "type": "transform",
                "name": "Build plan revision result",
                "config": {
                    "set": {
                        "input": {
                            "business_outcome": "revision_required",
                            "project_id": "{{project.project_id}}",
                            "retry_from_step_id": "plan_video",
                            "retry_segment_ids": [],
                            "final_video": None,
                        }
                    }
                },
                "next": ["revision_required"],
            },
            {
                "id": "lock_approved_plan",
                "type": "workflow_project",
                "name": "Lock approved plan",
                "config": {
                    "operation": "patch",
                    "project_id": "{{project.project_id}}",
                    "expected_revision": "{{project.revision}}",
                    "patch": {
                        "approved_plan_version": "{{plan.plan_version}}",
                        "current_phase": "capture",
                    },
                    "state_schema": "{{product_video_project_schema}}",
                    "output_var": "project",
                },
                "next": ["grant_capture"],
            },
            {
                "id": "grant_capture",
                "type": "workflow_action_grant",
                "name": "Grant approved browser capture",
                "config": {
                    "operation": "create",
                    "approval_step_id": "approve_plan",
                    "project_id": "{{project.project_id}}",
                    "grant_type": "browser_capture",
                    "scope": {
                        "approved_plan_version": "{{plan.plan_version}}",
                        "scene_ids": "{{plan.scene_ids}}",
                        "allowed_actions": [
                            "start_tab_recording", "click_element", "fill_or_select",
                            "click_point", "type_text", "press_key", "keyboard", "upload",
                            "computer",
                        ],
                    },
                    "scope_schema": CAPTURE_GRANT_SCOPE_SCHEMA,
                    "ttl_seconds": 86400,
                    "output_var": "capture_grant",
                },
                "next": ["attach_capture_grant"],
            },
            {
                "id": "attach_capture_grant",
                "type": "workflow_project",
                "name": "Attach capture grant",
                "config": {
                    "operation": "patch",
                    "project_id": "{{project.project_id}}",
                    "expected_revision": "{{project.revision}}",
                    "patch": {"capture_grant_id": "{{capture_grant.grant_id}}"},
                    "current_stage": "capturing",
                    "allowed_stages": PROJECT_STAGES,
                    "state_schema": "{{product_video_project_schema}}",
                    "output_var": "project",
                },
                "next": ["collect_assets"],
            },
        ]
    )
    steps.extend(_flat_media_pipeline_steps(
        selected_segments_ref="{{project.state.plan.scenes}}",
        selected_segment_ids_ref="{{project.state.plan.scene_ids}}",
    ))
    progress_names = {
        "start": "Start",
        "initialize_project": "Prepare project",
        "browser_preflight": "Check browser",
        "explore_product": "Discover product",
        "plan_video": "Plan video",
        "approve_plan": "Approve plan",
        "collect_assets": "Collect assets",
        "produce_video": "Produce package",
        "review_quality": "Review quality",
    }
    for step in steps:
        progress_name = progress_names.get(step["id"])
        if progress_name:
            step["name"] = progress_name
            continue
        step["config"] = {
            **(step.get("config") or {}),
            "chat_projection": "hidden",
        }
    return steps


_CREATE_STAGE_LAYOUT: tuple[
    tuple[str, str, str, tuple[str, ...], dict[str, str | None]],
    ...,
] = (
    (
        "initialize_project",
        "Prepare project",
        "normalize_request",
        (
            "normalize_request",
            "initialize_project",
            "assign_project_storage",
            "materialize_project_storage",
        ),
        {"browser_preflight": "browser_preflight"},
    ),
    (
        "browser_preflight",
        "Check browser",
        "browser_preflight",
        ("browser_preflight", "browser_ready", "browser_handoff"),
        {"mark_discovering": "explore_product"},
    ),
    (
        "explore_product",
        "Discover product",
        "mark_discovering",
        (
            "mark_discovering",
            "explore_product",
            "save_discovery",
            "project_discovery_report",
            "discovery_ready",
            "save_discovery_handoff",
            "build_discovery_handoff",
        ),
        {"plan_video": "plan_video", "needs_input": None},
    ),
    (
        "plan_video",
        "Plan video",
        "plan_video",
        (
            "plan_video",
            "validate_plan_contract",
            "plan_coverage_complete",
            "save_plan_coverage_handoff",
            "build_plan_coverage_handoff",
            "save_plan",
        ),
        {"approve_plan": "approve_plan", "needs_input": None},
    ),
    (
        "approve_plan",
        "Approve plan",
        "approve_plan",
        (
            "approve_plan",
            "plan_approved",
            "save_plan_revision_required",
            "build_plan_revision_result",
            "lock_approved_plan",
            "grant_capture",
            "attach_capture_grant",
        ),
        {"collect_assets": "collect_assets", "revision_required": None},
    ),
    (
        "collect_assets",
        "Collect assets",
        "collect_assets",
        (
            "collect_assets",
            "validate_collection_contract",
            "collection_valid",
            "use_collection_result",
            "save_collection_checkpoint",
            "save_capture_handoff",
            "build_capture_handoff",
        ),
        {"produce_video": "produce_video", "needs_input": None},
    ),
    (
        "produce_video",
        "Produce package",
        "produce_video",
        (
            "produce_video",
            "validate_production_contract",
            "production_contract_valid",
            "production_status_ready",
            "production_status_retake",
            "save_production_checkpoint",
            "save_production_handoff",
            "build_production_handoff",
            "save_invalid_production_handoff",
            "build_invalid_production_handoff",
            "save_blocked_production_handoff",
            "build_blocked_production_handoff",
            "probe_final",
            "analyze_final_audio",
            "validate_final_subtitles",
            "render_evidence_frames",
        ),
        {"review_quality": "review_quality", "needs_input": None},
    ),
    (
        "review_quality",
        "Review quality",
        "review_quality",
        (
            "review_quality",
            "validate_quality_contract",
            "quality_passed",
            "save_quality_ready",
            "build_completed_result",
            "save_revision_required",
            "build_revision_result",
        ),
        {"done": None, "revision_required": None},
    ),
)


def _operation_targets(step: dict[str, Any]) -> list[str]:
    targets: list[str] = []
    for key in ("next", "true_next", "false_next"):
        value = step.get(key) or []
        targets.extend(value if isinstance(value, list) else [value])
    if step.get("type") == "switch":
        config = step.get("config") or {}
        for case in config.get("cases") or []:
            if isinstance(case, dict):
                value = case.get("next") or []
                targets.extend(value if isinstance(value, list) else [value])
        value = config.get("default_next") or []
        targets.extend(value if isinstance(value, list) else [value])
    return list(dict.fromkeys(str(target) for target in targets if target))


def _pack_create_stages() -> list[dict[str, Any]]:
    flat_steps = _flat_create_steps()
    flat_by_id = {str(step["id"]): step for step in flat_steps}
    expected_operation_ids = [
        str(step["id"])
        for step in flat_steps
        if step.get("type") not in {"trigger", "end"}
    ]
    assigned_ids = [
        operation_id
        for _stage_id, _name, _entry, operation_ids, _routes in _CREATE_STAGE_LAYOUT
        for operation_id in operation_ids
    ]
    if assigned_ids != expected_operation_ids or len(set(assigned_ids)) != len(assigned_ids):
        raise ValueError("Create product video stage layout does not cover the flat graph exactly once")

    stages: list[dict[str, Any]] = []
    for stage_id, name, entry_id, operation_ids, routes in _CREATE_STAGE_LAYOUT:
        owned_ids = set(operation_ids)
        external_edges = {
            target
            for operation_id in operation_ids
            for target in _operation_targets(flat_by_id[operation_id])
            if target not in owned_ids
        }
        if external_edges != set(routes):
            raise ValueError(
                f"Create product video stage '{stage_id}' routes do not match its flat graph edges"
            )
        next_steps = list(dict.fromkeys(
            target for target in routes.values() if target is not None
        ))
        stages.append({
            "id": stage_id,
            "type": "stage",
            "name": name,
            "config": {
                "entry_operation_id": entry_id,
                "operations": [deepcopy(flat_by_id[operation_id]) for operation_id in operation_ids],
                "routes": deepcopy(routes),
            },
            "next": next_steps,
        })

    start = deepcopy(flat_by_id["start"])
    start["next"] = ["initialize_project"]
    return [start, *stages]


def _flat_revision_steps() -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = [
        {"id": "start", "type": "trigger", "name": "Start", "config": {"trigger_type": "mcp"}, "next": ["load_project"]},
        {
            "id": "load_project",
            "type": "workflow_project",
            "name": "Load existing project",
            "config": {
                "operation": "get",
                "project_id": "{{project_id}}",
                "project_type": PROJECT_TYPE,
                "state_schema": "{{product_video_project_schema}}",
                "output_var": "project",
            },
            "next": ["plan_revision"],
        },
        _agent_step(
            "plan_revision",
            "Plan targeted revision",
            service_key="product_video.planning",
            tools=PLANNER_TOOLS,
            prompt=(
                "Plan the smallest valid revision to this existing project without creating a new project root. "
                "Use retry_segment_ids {{retry_segment_ids}}, finding_ids {{finding_ids}}, request_patch "
                "{{request_patch}}, and revision_notes {{revision_notes}}. Always preserve every ready artifact and "
                "retry only missing or blocked artifact types unless a retake is explicit. Semantic changes must "
                "produce a complete updated plan and approval; page-state corrections keep the approved plan "
                "version. Recompute updated_plan.must_show_coverage and covered_must_show from the authoritative "
                "project request, and set must_show_coverage_complete only when every requirement remains mapped. "
                "Browser scenes may depend only on states available before post-production. Narration, "
                "subtitles, final MP4 playback, QA, and acceptance are downstream workflow outputs; never put them "
                "in a scene precondition or browser acceptance criterion. Project: {{project.state}}."
            ),
            output_var="revision_plan",
            output_schema=REVISION_PLAN_SCHEMA,
            next_steps=["validate_revision_plan_contract"],
        ),
        {
            "id": "validate_revision_plan_contract",
            "type": "code",
            "name": "Validate revised plan scene coverage",
            "config": {
                "language": "python",
                "code": PLAN_CONTRACT_VALIDATOR_CODE,
                "inputs": [
                    {
                        "key": "request",
                        "value": "{{project.state.request}}",
                        "type": "json",
                    },
                    {
                        "key": "plan",
                        "value": "{{revision_plan.updated_plan}}",
                        "type": "json",
                    },
                ],
                "output_format": "json",
                "output_var": "plan_validation",
            },
            "next": ["revision_coverage_complete"],
        },
        {
            "id": "revision_coverage_complete",
            "type": "condition",
            "name": "Revised plan preserves must-show coverage",
            "config": {"expression": "plan_validation.valid == true"},
            "true_next": ["post_only"],
            "false_next": ["save_revision_coverage_handoff"],
            "next": [],
        },
        {
            "id": "save_revision_coverage_handoff",
            "type": "workflow_project",
            "name": "Save incomplete revision coverage handoff",
            "config": {
                "operation": "patch",
                "project_id": "{{project.project_id}}",
                "expected_revision": "{{project.revision}}",
                "patch": {
                    "business_outcome": "needs_input",
                    "current_phase": "revision",
                    "retry_state": {
                        "phase": "revision",
                        "step_id": "revision_coverage_complete",
                        "segment_ids": "{{revision_plan.updated_plan.scene_ids}}",
                        "observed_problem": "{{plan_validation}}",
                        "required_change": (
                            "Restore every requested must-show mapping before revision approval."
                        ),
                        "editable_input_schema": {
                            "type": "object",
                            "properties": {
                                "retry_segment_ids": {
                                    "type": "array",
                                    "items": {"type": "string", "minLength": 1},
                                },
                                "finding_ids": {
                                    "type": "array",
                                    "items": {"type": "string", "minLength": 1},
                                },
                                "revision_notes": {"type": "string"},
                                "request_patch": {"type": "object"},
                            },
                            "additionalProperties": False,
                        },
                        "preserved_receipts": "{{project.state.artifacts}}",
                        "retry_from_step_id": "plan_revision",
                    },
                },
                "current_stage": "blocked",
                "allowed_stages": PROJECT_STAGES,
                "state_schema": "{{product_video_project_schema}}",
                "output_var": "project",
            },
            "next": ["build_revision_coverage_handoff"],
        },
        {
            "id": "build_revision_coverage_handoff",
            "type": "transform",
            "name": "Build incomplete revision coverage handoff",
            "config": {
                "set": {
                    "input": {
                        "business_outcome": "needs_input",
                        "project_id": "{{project.project_id}}",
                        "retry_from_step_id": "plan_revision",
                        "retry_segment_ids": "{{revision_plan.updated_plan.scene_ids}}",
                        "final_video": "{{project.state.timeline.final_video}}",
                    }
                }
            },
            "next": ["needs_input"],
        },
        {
            "id": "post_only",
            "type": "condition",
            "name": "Post-production-only revision",
            "config": {"expression": "revision_plan.post_only == true"},
            "true_next": ["mark_revision_producing"],
            "false_next": ["approve_revision"],
            "next": [],
        },
        {
            "id": "approve_revision",
            "type": "wait",
            "name": "Approve revision capture",
            "config": {
                "wait_type": "approval",
                "message": "Review the revised plan and selected Segment IDs.",
                "review_title": "Product video revision",
                "review": "{{revision_plan}}",
                "response_variable": "revision_decision",
                "options": ["approve", "cancel"],
                "approval_values": ["approve"],
                "requires_operator_approval": True,
                "chat_projection": "action",
            },
            "next": ["revision_approved"],
        },
        {
            "id": "revision_approved",
            "type": "condition",
            "name": "Revision approved",
            "config": {"expression": "revision_decision.choice == 'approve'"},
            "true_next": ["save_revision_plan"],
            "false_next": ["save_cancelled"],
            "next": [],
        },
        {
            "id": "save_revision_plan",
            "type": "workflow_project",
            "name": "Save revision plan",
            "config": {
                "operation": "patch",
                "project_id": "{{project.project_id}}",
                "expected_revision": "{{project.revision}}",
                "patch": {
                    "plan": "{{revision_plan.updated_plan}}",
                    "approved_plan_version": "{{revision_plan.updated_plan.plan_version}}",
                    "current_phase": "capture",
                    "business_outcome": "in_progress",
                    "retry_state": None,
                },
                "list_reconciles": [
                    {
                        "path": "scenes",
                        "key": "scene_id",
                        "keys": "{{revision_plan.updated_plan.scene_ids}}",
                        "items": "{{revision_plan.selected_scenes}}",
                    },
                    {
                        "path": "segments",
                        "key": "scene_id",
                        "keys": "{{revision_plan.updated_plan.scene_ids}}",
                        "items": "{{revision_plan.selected_scenes}}",
                    },
                ],
                "list_appends": [
                    {"path": "revision_history", "key": "revision_id", "items": ["{{revision_plan.revision_entry}}"]}
                ],
                "state_schema": "{{product_video_project_schema}}",
                "output_var": "project",
            },
            "next": ["grant_capture"],
        },
        {
            "id": "grant_capture",
            "type": "workflow_action_grant",
            "name": "Grant revision capture",
            "config": {
                "operation": "create",
                "approval_step_id": "approve_revision",
                "project_id": "{{project.project_id}}",
                "grant_type": "browser_capture",
                "scope": {
                    "approved_plan_version": "{{revision_plan.updated_plan.plan_version}}",
                    "scene_ids": "{{revision_plan.selected_scene_ids}}",
                    "allowed_actions": [
                        "start_tab_recording", "click_element", "fill_or_select", "click_point",
                        "type_text", "press_key", "keyboard", "upload", "computer",
                    ],
                },
                "scope_schema": CAPTURE_GRANT_SCOPE_SCHEMA,
                "output_var": "capture_grant",
            },
            "next": ["attach_revision_grant"],
        },
        {
            "id": "attach_revision_grant",
            "type": "workflow_project",
            "name": "Attach revision grant",
            "config": {
                "operation": "patch",
                "project_id": "{{project.project_id}}",
                "expected_revision": "{{project.revision}}",
                "patch": {"capture_grant_id": "{{capture_grant.grant_id}}"},
                "current_stage": "capturing",
                "allowed_stages": PROJECT_STAGES,
                "state_schema": "{{product_video_project_schema}}",
                "output_var": "project",
            },
            "next": ["collect_assets"],
        },
        {
            "id": "mark_revision_producing",
            "type": "workflow_project",
            "name": "Resume production checkpoint",
            "config": {
                "operation": "patch",
                "project_id": "{{project.project_id}}",
                "expected_revision": "{{project.revision}}",
                "patch": {"current_phase": "production", "business_outcome": "in_progress", "retry_state": None},
                "current_stage": "producing",
                "allowed_stages": PROJECT_STAGES,
                "state_schema": "{{product_video_project_schema}}",
                "output_var": "project",
            },
            "next": ["produce_video"],
        },
        {
            "id": "save_cancelled",
            "type": "workflow_project",
            "name": "Save cancelled revision",
            "config": {
                "operation": "patch",
                "project_id": "{{project.project_id}}",
                "expected_revision": "{{project.revision}}",
                "patch": {"business_outcome": "cancelled", "current_phase": "revision"},
                "state_schema": "{{product_video_project_schema}}",
                "output_var": "project",
            },
            "next": ["build_cancelled_result"],
        },
        {
            "id": "build_cancelled_result",
            "type": "transform",
            "name": "Build cancelled result",
            "config": {"set": {"input": {"business_outcome": "cancelled", "project_id": "{{project.project_id}}", "retry_from_step_id": None, "retry_segment_ids": [], "final_video": "{{project.state.timeline.final_video}}"}}},
            "next": ["cancelled"],
        },
        {"id": "cancelled", "type": "end", "name": "Revision cancelled", "config": {"no_external_publish": True}, "next": []},
    ]
    steps.extend(_flat_media_pipeline_steps(
        selected_segments_ref="{{revision_plan.selected_scenes}}",
        selected_segment_ids_ref="{{revision_plan.selected_scene_ids}}",
    ))
    return steps


WORKFLOW_SPECS: list[dict[str, Any]] = [
    {
        "slug": "create-product-video-v1",
        "name": "Create product video",
        "description": "Plan, capture, produce, and finish with machine quality review.",
        "user_facing": True,
        "variables": {
            "request": deepcopy(DEFAULT_PRODUCT_VIDEO_REQUEST),
            "retry_segment_ids": [],
            "revision_notes": "",
            "plan_decision": None,
        },
        "input_prefill": deepcopy(PRODUCT_VIDEO_REQUEST_PREFILL),
        "run_inputs": deepcopy(PRODUCT_VIDEO_REQUEST_RUN_INPUTS),
        "steps": _pack_create_stages(),
    },
    {
        "slug": "plan-product-video-v1",
        "name": "Plan product video",
        "description": "Discover the product and create a validated script and scene plan without capture.",
        "user_facing": True,
        "variables": {"request": deepcopy(DEFAULT_PRODUCT_VIDEO_REQUEST)},
        "input_prefill": deepcopy(PRODUCT_VIDEO_REQUEST_PREFILL),
        "run_inputs": deepcopy(PRODUCT_VIDEO_REQUEST_RUN_INPUTS),
        "steps": _flat_planning_steps(terminal=True),
    },
    {
        "slug": "revise-product-video-v1",
        "name": "Revise product video",
        "description": "Revise selected scenes or post-production in the same durable project.",
        "user_facing": True,
        "variables": {
            "project_id": "",
            "retry_segment_ids": [],
            "finding_ids": [],
            "revision_notes": "",
            "request_patch": {},
            "revision_decision": None,
        },
        "run_inputs": [
            {"key": "project_id", "label": "Project ID", "type": "string", "required": True},
            {"key": "retry_segment_ids", "label": "Retry Segment IDs", "type": "json", "required": False},
            {"key": "finding_ids", "label": "QA finding IDs", "type": "json", "required": False},
            {"key": "revision_notes", "label": "Revision notes", "type": "string", "required": False},
            {"key": "request_patch", "label": "Request corrections", "type": "json", "required": False},
        ],
        "steps": _flat_revision_steps(),
    },
]

for _workflow_spec in WORKFLOW_SPECS:
    _workflow_spec["variables"]["product_video_project_schema"] = (
        PRODUCT_VIDEO_PROJECT_SCHEMA
    )
    _workflow_spec["steps"][0]["config"]["run_inputs"] = deepcopy(
        _workflow_spec["run_inputs"]
    )
    if _workflow_spec.get("input_prefill"):
        _workflow_spec["steps"][0]["config"]["run_input_prefill"] = deepcopy(
            _workflow_spec["input_prefill"]
        )
    _executable_steps = [
        operation
        for _top_level_step in _workflow_spec["steps"]
        for operation in (
            [_top_level_step]
            if _top_level_step.get("type") != "stage"
            else (_top_level_step.get("config") or {}).get("operations") or []
        )
    ]
    for _workflow_step in _executable_steps:
        if _workflow_step["type"] == "workflow_project":
            _workflow_step["config"].setdefault("project_type", PROJECT_TYPE)
            _workflow_step["config"].setdefault(
                "schema_version",
                PROJECT_SCHEMA_VERSION,
            )
            _history_events = {
                "assign_project_storage": ("request", "completed", []),
                "save_discovery": ("discovery", "completed", []),
                "save_discovery_handoff": ("discovery", "needs_input", []),
                "save_plan_coverage_handoff": ("plan", "needs_input", []),
                "save_plan": ("plan", "completed", ["{{project.state.ledger_path}}"]),
                "save_plan_revision_required": ("plan", "revision_required", []),
                "lock_approved_plan": ("plan", "approved", []),
                "attach_capture_grant": ("capture", "authorized", []),
                "save_collection_checkpoint": (
                    "capture",
                    "completed",
                    "{{effective_collection.artifacts}}",
                ),
                "save_capture_handoff": (
                    "capture",
                    "needs_input",
                    "{{collection_validation.preserved_receipts}}",
                ),
                "save_production_checkpoint": (
                    "production",
                    "completed",
                    "{{production_result.artifacts}}",
                ),
                "save_production_handoff": (
                    "production",
                    "needs_input",
                    "{{production_result.artifacts}}",
                ),
                "save_invalid_production_handoff": (
                    "production",
                    "needs_input",
                    "{{production_result.artifacts}}",
                ),
                "save_blocked_production_handoff": (
                    "production",
                    "needs_input",
                    "{{production_result.blocker.preserved_receipts}}",
                ),
                "save_quality_ready": (
                    "quality",
                    "completed",
                    "{{project.state.final_artifacts}}",
                ),
                "save_revision_required": (
                    "quality",
                    "revision_required",
                    "{{project.state.final_artifacts}}",
                ),
                "save_revision_plan": ("revision", "completed", []),
                "save_revision_coverage_handoff": (
                    "revision",
                    "needs_input",
                    "{{project.state.artifacts}}",
                ),
                "attach_revision_grant": ("capture", "authorized", []),
                "mark_revision_producing": ("production", "resumed", []),
                "save_cancelled": ("revision", "cancelled", []),
            }
            _history_event = _history_events.get(_workflow_step["id"])
            if _history_event:
                _phase, _status, _receipt_ids = _history_event
                _workflow_step["config"].setdefault(
                    "history_event",
                    {
                        "phase": _phase,
                        "status": _status,
                        "receipt_ids": _receipt_ids,
                    },
                )


@dataclass
class ProductVideoStudioTemplate:
    key: str = "product_video_studio"
    title: str = "Product Video Studio"
    summary: str = (
        "A browser product-video workspace for discovery, approved scene capture, "
        "post-production, and terminal machine QA without publishing."
    )
    params_schema: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.params_schema is None:
            self.params_schema = {"type": "object", "required": [], "properties": {}}

    async def apply(self, db: AsyncSession, inp: TemplateInput) -> TemplateResult:
        provisioning = [
            await _provision_role_agent(
                db,
                entity_id=inp.entity_id,
                workspace_id=inp.workspace_id,
                role=role,
            )
            for role in ROLE_SPECS
        ]
        await _seed_workspace_workflows(
            db,
            entity_id=inp.entity_id,
            workspace_id=inp.workspace_id,
            user_id=inp.user_id,
        )
        warnings = [warning for item in provisioning for warning in item["warnings"]]
        notes = [
            "Provisioned five bounded product-video roles.",
            "Installed Create product video, Plan product video, and Revise product video.",
            "Installed restartable, checkpointed graphs for all user-facing workflows.",
            "No product, URL, request, account, or external publishing default was installed.",
        ]
        if warnings:
            notes.append("Provisioning warnings: " + "; ".join(warnings))
        return TemplateResult(
            template_key=self.key,
            goal_id=None,
            task_ids=[],
            scheduled_job_ids=[],
            notes=notes,
        )


async def _seed_workspace_workflows(
    db: AsyncSession,
    *,
    entity_id: str,
    workspace_id: str,
    user_id: str | None,
) -> list[dict[str, str]]:
    from sqlalchemy import select

    from packages.core.models.workflow import WorkflowDefinition
    from packages.core.services import workflow_service

    seeded: list[dict[str, str]] = []
    for spec in WORKFLOW_SPECS:
        workflow = (await db.execute(
            select(WorkflowDefinition).where(
                WorkflowDefinition.entity_id == entity_id,
                WorkflowDefinition.name == spec["slug"],
            ).limit(1)
        )).scalar_one_or_none()
        fields = {
            "name": spec["slug"],
            "description": spec["description"],
            "icon": "video",
            "trigger_type": "mcp" if spec["user_facing"] else "internal",
            "trigger_config": {},
            "steps": spec["steps"],
            "variables": spec["variables"],
            "category": "media",
            "tags": list(WORKFLOW_TAGS),
        }
        if workflow is None:
            workflow = await workflow_service.create_workflow(
                db,
                entity_id=entity_id,
                created_by=user_id,
                **fields,
            )
        else:
            workflow = await workflow_service.update_workflow(
                db,
                workflow.id,
                entity_id,
                **fields,
            )
        if workflow is None:
            continue

        binding_id = ""
        if spec["user_facing"]:
            bindings = await workflow_service.list_bindings(
                db,
                entity_id,
                workspace_id=workspace_id,
                workflow_id=workflow.id,
            )
            binding = next(
                (item for item in bindings if item.trigger_type == "mcp" and item.status == "active"),
                None,
            )
            config = {
                "template_key": "product_video_studio",
                "workflow_slug": spec["slug"],
                "operator_surface": "chat",
                "chat_entrypoint": {
                    "enabled": True,
                    "title": spec["name"],
                    "description": spec["description"],
                    "intent": {
                        "enabled": True,
                        "description": spec["description"],
                        "minimum_confidence": 0.85,
                    },
                    "projection": {"progress": True, "step_outputs": "explicit"},
                    "wait_bridge": True,
                },
                "no_external_publish": True,
            }
            if binding is None:
                binding = await workflow_service.create_workflow_binding(
                    db,
                    entity_id=entity_id,
                    workflow_id=workflow.id,
                    workspace_id=workspace_id,
                    business_line="media-production",
                    name=f"{spec['name']} (Chat)",
                    trigger_type="mcp",
                    variables=spec["variables"],
                    config=config,
                )
            else:
                binding = await workflow_service.update_binding(
                    db,
                    binding.id,
                    entity_id,
                    name=f"{spec['name']} (Chat)",
                    business_line="media-production",
                    variables=spec["variables"],
                    config={**(binding.config or {}), **config},
                )
            binding_id = binding.id if binding else ""
        seeded.append({"slug": spec["slug"], "workflow_id": workflow.id, "binding_id": binding_id})
    return seeded


async def _provision_role_agent(
    db: AsyncSession,
    *,
    entity_id: str,
    workspace_id: str,
    role: RoleSpec,
) -> dict[str, Any]:
    from packages.core.services.agent_provisioning_service import CustomAgentSpec, provision_custom_agent
    from packages.core.services.builtin_skill_loader import seed_builtin_skills
    from packages.core.services.workspace_service import map_agent_to_service

    await seed_builtin_skills(db)
    warnings = await _install_marketplace_skills(
        db,
        entity_id=entity_id,
        skills=list(role.skill_bindings),
    )
    result = await provision_custom_agent(
        db,
        entity_id=entity_id,
        spec=CustomAgentSpec(
            agent_name=role.agent_name,
            system_prompt=role.system_prompt,
            description=f"{role.agent_name} role for Product Video Studio.",
            category="media-production",
            tags=["media", "video", "product", "marketplace-template"],
            tool_bindings=list(role.tool_bindings),
            skill_bindings=list(role.skill_bindings),
            mcp_bindings=list(role.mcp_bindings),
            source="marketplace_template",
            workspace_id=workspace_id,
            service_key=role.service_key,
        ),
    )
    subscription = await map_agent_to_service(
        db,
        workspace_id=workspace_id,
        entity_id=entity_id,
        service_key=role.service_key,
        agent_id=result.agent_id,
        custom_prompt=(
            f"Act only as {role.agent_name} in Product Video Studio. "
            "Use durable Workflow state and never publish externally."
        ),
    )
    return {
        "service_key": role.service_key,
        "subscription_id": subscription.id,
        "agent_id": result.agent_id,
        "warnings": [*warnings, *result.warnings],
    }


async def _install_marketplace_skills(
    db: AsyncSession,
    *,
    entity_id: str,
    skills: list[str],
) -> list[str]:
    supporting = {"chrome"}
    marketplace_skills = [skill for skill in skills if skill not in supporting]
    if not marketplace_skills:
        return []

    # The marketplace catalog is a cloud-only surface (excluded from the OSS
    # export, see .ossexclude) — self-hosted deployments simply have no
    # marketplace skills to resolve, and every requested slug falls through
    # to the "unavailable in this deployment" warning below.
    slug_to_id: dict[str, str] = {}
    if is_cloud():
        pass

    warnings: list[str] = []
    for slug in marketplace_skills:
        marketplace_id = slug_to_id.get(slug)
        if marketplace_id is None:
            warnings.append(f"Marketplace skill {slug!r} is unavailable in this deployment.")
            continue
        pass
    return warnings


register(ProductVideoStudioTemplate())
