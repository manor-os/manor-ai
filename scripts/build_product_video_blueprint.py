"""Build the frozen Product Video Studio blueprint from the recipe contracts."""
from __future__ import annotations

import json
from pathlib import Path

from packages.core.services.github_skill_installer import _parse_skill_frontmatter
from packages.core.templates.recipes import product_video_studio as studio


ROOT = Path(__file__).parents[1]
OUTPUT = ROOT / "packages/core/blueprints/configs/solo_company/product-video-studio-v1.json"


SKILLS = {
    "content_angle_planner": {
        "name": "Content Angle Planner",
        "description": "Turns product goals and evidence into a focused audience, promise, and narrative angle.",
        "prompt": (
            "Plan an evidence-backed product video angle. Preserve must-show, must-not-show, language, "
            "CTA, and output-profile constraints. Mark claims as observed, supplied, documented, or "
            "inferred. Never operate a browser, generate media, or publish."
        ),
        "tools": [],
    },
    "product_experience_mapper": {
        "name": "Product Experience Mapper",
        "description": "Maps observable browser product journeys before planning or capture.",
        "prompt": (
            "Explore the exact supplied product source with reversible navigation and reading only. "
            "Separate observation from assumption and report URLs, labels, states, gaps, reset steps, "
            "and privacy risks. Never record, fill, submit, upload, read credentials, or bypass security."
        ),
        "tools": list(studio.EXPLORER_TOOLS),
    },
    "script_storyboard_planner": {
        "name": "Script Storyboard Planner",
        "description": "Creates canonical narration and approved asset-level product scenes.",
        "prompt": (
            "Create a strict ProductVideoPlan from validated request and discovery evidence. Give every "
            "scene a stable ID, precondition, bounded browser actions, primary effect record, expected "
            "postcondition, narration, capture and screenshot requirements, privacy rules, recovery, and "
            "acceptance criteria. Never operate the browser or mutate durable project state."
        ),
        "tools": list(studio.PLANNER_TOOLS),
    },
    "screen_asset_collector": {
        "name": "Screen Asset Collector",
        "description": "Collects approved browser segments in bounded phases and returns durable artifact evidence.",
        "prompt": (
            "Execute only the approved Segment IDs and durable grant scope. Split the batch into bounded "
            "Chrome phases, never blindly retry unknown side effects, stop recording during long waits, "
            "re-observe the result before resuming, capture screenshots in the same state, and return durable receipts. "
            "Never synthesize product UI or publish."
        ),
        "tools": list(studio.COLLECTOR_TOOLS),
    },
    "video_post_producer": {
        "name": "Video Post Producer",
        "description": "Builds restartable narration, subtitles, timeline, and final MP4 artifacts.",
        "prompt": (
            "Use ready project artifacts to build a picture master, one continuous narration take, "
            "normalized audio, semantic subtitles, an edit timeline, and the final MP4. Convert only "
            "explicitly planned still scenes. Reuse matching checkpoints and never capture, change claims, "
            "select a provider, or publish."
        ),
        "tools": list(studio.PRODUCER_TOOLS),
    },
    "video_quality_reviewer": {
        "name": "Video Quality Reviewer",
        "description": "Classifies deterministic media evidence and sampled product frames.",
        "prompt": (
            "Classify probe, audio, subtitle, sampled-frame, coverage, privacy, and narrative evidence into "
            "machine_pass, repairable_technical, or revision_required. Never mutate media or treat a machine "
            "pass as final operator acceptance."
        ),
        "tools": list(studio.QUALITY_TOOLS),
    },
}


CHROME_BINDINGS = [
    {
        "server_slug": "chrome",
        "allowed_tools": [
            "mcp__chrome__open_or_reuse",
            "mcp__chrome__read_page",
            "mcp__chrome__navigate",
            "mcp__chrome__click_element",
            "mcp__chrome__fill_or_select",
            "mcp__chrome__wait",
            "mcp__chrome__scroll",
            "mcp__chrome__screenshot",
            "mcp__chrome__start_tab_recording",
            "mcp__chrome__get_tab_recording",
            "mcp__chrome__stop_tab_recording",
            "mcp__chrome__cancel_tab_recording",
            "mcp__chrome__finalize_tabs",
        ],
        "config_override_allowlist": [],
    },
    {
        "server_slug": "chrome_knowledge_local",
        "allowed_tools": ["mcp__chrome_knowledge_local__prepare_upload"],
        "config_override_allowlist": [],
    },
]


def skill_payload(slug: str) -> dict:
    item = SKILLS[slug]
    skill_path = (
        ROOT
        / "packages/core/ai/marketplace_skills"
        / slug.replace("_", "-")
        / "SKILL.md"
    )
    source = _parse_skill_frontmatter(skill_path.read_text(encoding="utf-8"))
    return {
        "slug": slug,
        "name": source.get("display_name") or item["name"],
        "display_name": source.get("display_name") or item["name"],
        "description": source.get("description") or item["description"],
        "system_prompt": source["body"],
        "tools": list(source.get("tools") or item["tools"]),
        "input_schema": {
            "type": "object",
            "properties": {
                "request": {"type": "string"},
                "workspace_context": {"type": "object"},
            },
        },
        "output_format": "json",
        "category": "media",
        "tags": ["workspace-blueprint", "product-video-studio-v1"],
        "version": str(source.get("version") or "2.0.0"),
        "config": {"marketplace_id": slug, "frozen_at": "2026-07-27"},
        "status": "active",
    }


def agent_payload(index: int, role: studio.RoleSpec) -> dict:
    slug = [
        "product-video-explorer-v1",
        "product-video-planner-v1",
        "product-video-scene-collector-v1",
        "product-video-producer-v1",
        "product-video-quality-reviewer-v1",
    ][index]
    return {
        "slug": slug,
        "name": role.agent_name,
        "description": f"{role.agent_name} role for Product Video Studio.",
        "system_prompt": role.system_prompt,
        "category": "media-production",
        "tags": ["workspace-blueprint", "product-video-studio-v1"],
        "version": "2.0.0",
        "config": {"business_capabilities": [role.service_key]},
        "tool_bindings": list(role.tool_bindings),
        "skill_bindings": list(role.skill_bindings),
        "mcp_bindings": CHROME_BINDINGS if role.mcp_bindings else [],
        "starter_memory": [],
    }


def workflow_payload(spec: dict) -> dict:
    return {
        "slug": spec["slug"],
        "name": spec["name"],
        "description": spec["description"],
        "trigger_type": "mcp" if spec["user_facing"] else "internal",
        "internal": not spec["user_facing"],
        "variables": [
            {"key": key, "default": value}
            for key, value in spec["variables"].items()
        ],
        "run_inputs": spec["run_inputs"],
        "binding_config": {
            "chat_entrypoint": (
                {
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
                }
                if spec["user_facing"]
                else {"enabled": False}
            ),
            "template_key": "product_video_studio",
            "no_external_publish": True,
        },
        "steps": spec["steps"],
        "category": "media",
        "tags": list(studio.WORKFLOW_TAGS),
        "version": 3 if spec["slug"] == "create-product-video-v1" else 2,
    }


def build_payload() -> dict:
    all_tools = sorted(
        {
            tool
            for role in studio.ROLE_SPECS
            for tool in role.tool_bindings
        } | set(studio.QUALITY_EVIDENCE_TOOLS)
    )
    agents = [agent_payload(index, role) for index, role in enumerate(studio.ROLE_SPECS)]
    subscriptions = [
        {
            "service_key": role.service_key,
            "agent_slug": agents[index]["slug"],
            "custom_prompt": (
                f"Act only as {role.agent_name}. Use validated Workflow inputs and durable project state. "
                "Never publish externally."
            ),
            "config": {
                "workspace_blueprint_slug": "product-video-studio-v1",
                "primary_service": role.service_key,
                "service_name": role.agent_name,
            },
        }
        for index, role in enumerate(studio.ROLE_SPECS)
    ]
    return {
        "manifest": {
            "blueprint_version": "1.1",
            "slug": "product-video-studio-v1",
            "title": "Product Video Studio",
            "summary": "Plan, capture, produce, and review browser product videos in one durable Workspace.",
            "use_when": "Use for browser-based walkthrough, launch, onboarding, support, or feature-promotion videos.",
            "outcome_summary": "A review-ready MP4 package with source evidence, narration, subtitles, machine QA, and explicit operator acceptance.",
            "description": (
                "A generic browser product-video Workspace. It discovers the current logged-in product, "
                "requests one scoped plan approval, captures restartable scenes, stops during long waits, "
                "produces a final MP4, runs deterministic and visual QA, and never publishes externally."
            ),
            "tags": ["product-video", "screen-recording", "audio", "qa"],
            "kind": "one_person_company",
            "category": "studio.product_video",
            "author": {"handle": "manor", "display_name": "Manor AI"},
            "cover_image_url": None,
            "forked_from_id": None,
            "changelog": (
                "v2.1 (2026-08-03): Create product video now uses nine "
                "restartable business stages."
            ),
        },
        "contract": {
            "variables": [],
            "channels": [],
            "sessions": [
                {
                    "provider": "chrome",
                    "label": "capture",
                    "purpose": "Current paired browser session for product discovery and approved capture.",
                    "required": False,
                }
            ],
            "requires": {
                "manor_min_version": "1.0",
                "tools": all_tools,
                "mcp_servers": [],
                "skills": [{"slug": "chrome", "min_version": "2.4.0"}],
                "agents": [],
            },
        },
        "embedded": {
            "skills": [skill_payload(slug) for slug in SKILLS],
            "agents": agents,
            "knowledge_packs": [],
        },
        "recipe": {
            "operating_model": {
                "kind": "one_person_company",
                "context": "One operator creates browser product videos for products they can access.",
                "primary_work": "Discover, approve, capture, produce, machine-check, and accept product videos.",
                "settings": {
                    "install_expectation": "workflow_first",
                    "external_publishing": "disabled",
                },
                "services": [
                    {
                        "key": role.service_key,
                        "name": role.agent_name,
                        "description": role.system_prompt,
                    }
                    for role in studio.ROLE_SPECS
                ],
                "rules": [
                    "WorkflowProject state is authoritative; generated files are projections or artifacts.",
                    "Discovery is read-only and capture starts only after scoped plan approval.",
                    "Unknown browser effects are observed or paused and never blindly retried.",
                    "Long product waits happen with recording stopped and resume durably.",
                    "Machine QA never replaces final operator playback and acceptance.",
                    "No external publishing, messaging, purchase, deletion, or legal acceptance is allowed.",
                ],
            },
            "strategist": None,
            "prompts": [],
            "subscriptions": subscriptions,
            "scheduled_jobs": [],
            "workflows": [workflow_payload(spec) for spec in studio.WORKFLOW_SPECS],
            "goals": [],
            "starter_tasks": [],
            "task_categories": [],
            "custom_fields": [],
            "sla_policies": [],
            "escalation_rules": [],
        },
        "policy": {
            "governance": {
                "never_allow_actions": [
                    "external.publish",
                    "external.schedule",
                    "external.upload_public",
                    "external.send_message",
                    "browser.inspect_credentials",
                    "browser.inspect_storage",
                    "commerce.purchase",
                    "legal.accept_terms",
                    "data.delete",
                ],
                "hitl_required_actions": [
                    "browser.record_screen",
                    "browser.modify_product_data",
                    "media.final_package_acceptance",
                ],
                "auto_approve_actions": [],
                "max_risk_level": "medium",
            },
            "post_install_checks": [
                {
                    "kind": "workflow_present",
                    "workflow_slug": slug,
                    "blocking": True,
                }
                for slug in studio.USER_WORKFLOW_SLUGS
            ],
            "expected_baseline": {
                "user_workflows": studio.USER_WORKFLOW_SLUGS,
                "internal_workflows": studio.INTERNAL_WORKFLOW_SLUGS,
                "role_count": len(studio.ROLE_SPECS),
                "external_publishing": False,
                "maturity_level": "Validated workflow blueprint",
                "validation_summary": (
                    "Installs a generic Product Video Studio with durable project state, scoped browser "
                    "capture approval, restartable Segment checkpoints, deterministic media QA, and final "
                    "operator playback acceptance. A paired Chrome session is needed when a real run "
                    "reaches discovery or capture."
                ),
                "runnable_in_simulation": True,
                "blocking_todos_expected": 0,
                "first_week_outputs": [
                    "An evidence-backed product journey and approved script and scene plan.",
                    "Restartable scene recordings and screenshots registered as Workspace artifacts.",
                    "A narrated MP4, subtitle file, edit timeline, source manifest, and machine QA report.",
                    "A final playback review that records explicit operator acceptance or revision notes.",
                ],
                "validation_evidence": [
                    "WorkflowProject is the authoritative cross-run state for the production.",
                    "Browser capture grants are scoped to one approved plan version and its scenes.",
                    "Long product waits resume through durable stage checkpoints without recording the wait.",
                    "Media probes, audio analysis, subtitle checks, and sampled frames run before review.",
                ],
                "acceptance_criteria": [
                    "No product, URL, request, account, or test fixture is installed by default.",
                    "Every required scene resolves to durable recording or screenshot artifacts.",
                    "The final MP4 contains usable video and narration and passes the configured checks.",
                    "The operator opens and watches the final video before accepting the project.",
                    "No external publishing action occurs.",
                ],
                "not_included": [
                    "Automatic external publishing, messaging, purchase, deletion, or legal acceptance.",
                    "Credential, cookie, browser-storage, CAPTCHA, or access-control bypass.",
                    "Automatic redaction of sensitive footage or a general nonlinear video editor.",
                ],
            },
        },
    }


if __name__ == "__main__":
    OUTPUT.write_text(
        json.dumps(build_payload(), ensure_ascii=True, indent=2) + "\n",
        encoding="utf-8",
    )
