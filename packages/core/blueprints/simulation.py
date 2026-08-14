"""Blueprint-owned Workspace simulation experiences.

Every newly exported blueprint carries a small, portable demo contract.  The
contract describes the reviewable artifacts that a sandbox Chat run should
appear to produce; it never contains a credential, an external receipt, or a
claim that an outside system was changed.

Hand-authored blueprints may provide ``recipe.simulation_experience`` for a
domain-specific demo.  Older payloads remain installable: the installer calls
``resolve_simulation_experience`` and deterministically derives a useful
fallback from the blueprint's services, workflows, tools, and expected outputs.
"""
from __future__ import annotations

import re
from copy import deepcopy
from typing import Any, Iterable


SIMULATION_EXPERIENCE_VERSION = "1.0"
SIMULATION_ARTIFACT_KINDS = frozenset({
    "document",
    "image",
    "video",
    "audio",
    "spreadsheet",
    "presentation",
    "archive",
})
SIMULATION_STAGE_KINDS = frozenset({
    "request",
    "proposal",
    "context",
    "input",
    "workflow",
    "artifact",
    "approval",
    "failure",
    "receipt",
    "goal",
})
SIMULATION_STAGE_AUTHORS = frozenset({"user", "agent", "system"})
SIMULATION_ACTION_KINDS = frozenset({
    "approve_proposals",
    "governance_approval",
    "human_input",
    "needs_confirmation",
    "needs_input",
    "workflow_approval",
    "workflow_retry",
})


def _strings(node: Any) -> Iterable[str]:
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for value in node.values():
            yield from _strings(value)
    elif isinstance(node, list):
        for value in node:
            yield from _strings(value)


def _slug(value: Any, fallback: str = "workspace") -> str:
    resolved = re.sub(r"[^a-z0-9]+", "-", str(value or "").lower()).strip("-")
    return (resolved or fallback)[:64].rstrip("-")


def _contains(corpus: str, *terms: str) -> bool:
    return any(term in corpus for term in terms)


def _records(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _label(row: dict[str, Any], fallback: str) -> str:
    for key in ("title", "name", "label", "key", "service_key", "metric_key"):
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return fallback


def _service_key(row: dict[str, Any] | None) -> str | None:
    if not isinstance(row, dict):
        return None
    value = row.get("key") or row.get("service_key")
    return value.strip() if isinstance(value, str) and value.strip() else None


def _proposal_action(
    stage_id: str,
    rows: list[dict[str, Any]],
    *,
    fallback_title: str,
) -> dict[str, Any]:
    items = []
    for index, row in enumerate(rows[:3]):
        title = _label(row, f"{fallback_title} {index + 1}")
        summary = row.get("description") or row.get("summary") or title
        items.append({
            "item_id": f"{stage_id}-{index + 1}",
            "kind": "workflow_change",
            "risk_level": "low",
            "summary": str(summary).strip()[:300],
        })
    if not items:
        items.append({
            "item_id": f"{stage_id}-1",
            "kind": "workflow_change",
            "risk_level": "low",
            "summary": fallback_title,
        })
    return {
        "kind": "approve_proposals",
        "review_id": f"simulation-{stage_id}",
        "items": items,
        "options": ["approve", "always_approve", "reject"],
    }


def _workflow_meta(title: str, services: list[dict[str, Any]]) -> dict[str, Any]:
    chosen = services[:3] or [{"name": "Prepare"}, {"name": "Produce"}, {"name": "Verify"}]
    steps = [
        {
            "id": f"simulation-step-{index + 1}",
            "name": _label(service, f"Stage {index + 1}"),
            "status": "completed",
        }
        for index, service in enumerate(chosen)
    ]
    return {
        "workflow_title": title,
        "workflow_status": "completed",
        "workflow_business_outcome": "completed",
        "workflow_attempt_number": 1,
        "workflow_current_step_id": steps[-1]["id"],
        "workflow_steps": steps,
    }


def _generate_scenario(
    *,
    title: str,
    primary_work: str,
    artifacts: list[dict[str, Any]],
    recipe: dict[str, Any],
) -> list[dict[str, Any]]:
    """Build a complete, deterministic Chat walkthrough from Blueprint data.

    The scenario intentionally uses the same pending-action vocabulary and
    message shapes as a live Workspace.  It is data, not a browser timer: the
    server persists each stage and stops only at explicit operator gates.
    """
    operating_model = (
        recipe.get("operating_model")
        if isinstance(recipe.get("operating_model"), dict)
        else {}
    )
    services = _records(operating_model.get("services"))
    workflows = _records(recipe.get("workflows"))
    goals = _records(recipe.get("goals"))
    jobs = _records(recipe.get("scheduled_jobs"))

    goal_title = _label(goals[0], primary_work) if goals else primary_work
    workflow_title = (
        _label(workflows[0], "Blueprint production workflow")
        if workflows
        else _label(services[0], "Blueprint production workflow")
        if services
        else "Blueprint production workflow"
    )
    planning_services = services[:3]
    production_services = services[1:4] or services[:3]
    delivery_services = services[-3:] if len(services) > 3 else services[:3]
    first_service_key = _service_key(services[0] if services else None)
    production_service_key = _service_key(
        services[min(2, len(services) - 1)] if services else None
    )
    delivery_service_key = _service_key(services[-1] if services else None)
    audience_hint = str(
        operating_model.get("audience")
        or operating_model.get("customer")
        or "the Blueprint's primary audience"
    ).strip()
    artifact_titles = ", ".join(str(item.get("title") or item["id"]) for item in artifacts)
    cadence = _label(jobs[0], "the configured cadence") if jobs else "the configured cadence"

    return [
        {
            "id": "goal-request",
            "kind": "request",
            "title": "Start the Blueprint goal",
            "author": "user",
            "body": f"Run **{title}** from start to finish: **{goal_title}**. Keep the normal proposals, reviews, files, recovery, and Goal attribution visible in Workspace Chat.",
        },
        {
            "id": "plan-proposal",
            "kind": "proposal",
            "title": "Plan proposal",
            "author": "agent",
            "service_key": first_service_key,
            "body": f"I broke the goal into the first reviewable stage for **{primary_work}**. Choose the work that should enter the run.",
            "pending_action": _proposal_action(
                "plan-proposal",
                planning_services,
                fallback_title="Prepare the first Blueprint work packet",
            ),
        },
        {
            "id": "workspace-context",
            "kind": "context",
            "title": "Workspace context retrieved",
            "author": "agent",
            "service_key": first_service_key,
            "body": f"Workspace Knowledge, Blueprint rules, and the installed operating model are now in scope. The run is grounded in **{primary_work}** and will not call an external system.",
        },
        {
            "id": "operator-input",
            "kind": "input",
            "title": "Confirm the run focus",
            "author": "agent",
            "service_key": first_service_key,
            "body": "A choice from you is needed before the completed retrieval can continue.",
            "pending_action": {
                "kind": "needs_input",
                "title": "Choose the focus for this run",
                "context_summary": "Your response resumes this persisted run without repeating completed work.",
                "questions": [
                    {
                        "key": "audience",
                        "label": "Primary audience",
                        "type": "select",
                        "required": True,
                        "options": [
                            {"label": audience_hint[:80], "value": "primary"},
                            {"label": "A smaller test audience", "value": "test"},
                        ],
                    },
                    {
                        "key": "guidance",
                        "label": "Optional guidance",
                        "type": "textarea",
                        "required": False,
                        "placeholder": "Add a constraint or leave this blank",
                    },
                ],
            },
        },
        {
            "id": "production-proposal",
            "kind": "proposal",
            "title": "Production proposal",
            "author": "agent",
            "service_key": production_service_key or first_service_key,
            "body": f"The input has been applied. Review the production stage before **{workflow_title}** runs.",
            "pending_action": _proposal_action(
                "production-proposal",
                production_services,
                fallback_title=f"Run {workflow_title}",
            ),
        },
        {
            "id": "workflow-run",
            "kind": "workflow",
            "title": workflow_title,
            "author": "agent",
            "service_key": production_service_key or first_service_key,
            "body": f"The approved Blueprint workflow completed its simulated tool path and produced a reviewable checkpoint for **{primary_work}**.",
            "message_kind": "workflow_activity",
            "meta": _workflow_meta(workflow_title, production_services),
        },
        {
            "id": "artifact-packet",
            "kind": "artifact",
            "title": "Generated review packet",
            "author": "agent",
            "service_key": production_service_key or first_service_key,
            "body": f"Generated {len(artifacts)} Blueprint-defined reviewable outputs: {artifact_titles}. These previews are simulated and remain inside this Workspace.",
            "artifact_ids": [str(item["id"]) for item in artifacts],
        },
        {
            "id": "delivery-proposal",
            "kind": "proposal",
            "title": "Delivery proposal",
            "author": "agent",
            "service_key": delivery_service_key or production_service_key,
            "body": f"The artifacts passed the production checkpoint. Select the delivery and verification work that should run on **{cadence}**.",
            "pending_action": _proposal_action(
                "delivery-proposal",
                delivery_services,
                fallback_title="Package and verify the final result",
            ),
        },
        {
            "id": "exact-output-approval",
            "kind": "approval",
            "title": "Approve the exact output",
            "author": "agent",
            "service_key": delivery_service_key or production_service_key,
            "body": "The exact final packet is ready for the normal Workspace approval gate.",
            "pending_action": {
                "kind": "governance_approval",
                "hitl_type": "review",
                "prompt": "Approve this exact simulated output for the delivery step?",
                "review_title": "Final Blueprint output",
                "review": {
                    "summary": f"{artifact_titles}",
                    "checks": [
                        "Blueprint rules applied",
                        "Artifacts retained in Workspace Chat",
                        "No external side effect",
                    ],
                },
                "options": ["approve", "revise", "reject"],
            },
        },
        {
            "id": "safe-failure",
            "kind": "failure",
            "title": "One branch needs recovery",
            "author": "agent",
            "service_key": delivery_service_key or production_service_key,
            "body": "A simulated tool branch failed after the approval checkpoint.",
            "pending_action": {
                "kind": "workflow_retry",
                "hitl_type": "error",
                "title": "The simulated delivery reference expired",
                "payload": {
                    "what_happened": "The page changed, so the previous reference cannot be reused safely.",
                    "why": "The simulation reproduces the same stale-reference recovery used by a live Workspace.",
                    "action_to_take": "Retry only the failed branch; completed work and approvals remain intact.",
                },
            },
        },
        {
            "id": "branch-retry",
            "kind": "workflow",
            "title": "Failed branch retried",
            "author": "agent",
            "service_key": delivery_service_key or production_service_key,
            "body": "Only the failed branch was retried. Completed retrieval, production, artifacts, and approvals were reused without duplicate work.",
            "message_kind": "workflow_activity",
            "meta": {
                **_workflow_meta("Recover and verify the final delivery", delivery_services),
                "workflow_attempt_number": 2,
            },
        },
        {
            "id": "simulation-receipt",
            "kind": "receipt",
            "title": "Verified simulation receipt",
            "author": "system",
            "body": "Verification complete. A durable simulated receipt was written to Workspace Chat; no external account, file store, publisher, or customer was changed.",
        },
        {
            "id": "goal-completed",
            "kind": "goal",
            "title": "Goal completed",
            "author": "system",
            "message_kind": "goal_alert",
            "body": f"**Goal completed (simulation)**\n\n**{goal_title}**\n\nThe Blueprint-specific run completed every proposed stage, review gate, generated artifact, recovery branch, verification receipt, and Goal attribution.",
        },
    ]


def generate_simulation_experience(payload: dict[str, Any]) -> dict[str, Any]:
    """Derive a deterministic demo contract from a v1.1 blueprint payload.

    The fallback stays deliberately compact: one run brief plus up to three
    media/business artifacts signalled by the blueprint.  Authors can replace
    it with a richer explicit contract when a particular order matters.
    """
    manifest = payload.get("manifest") if isinstance(payload.get("manifest"), dict) else {}
    recipe = payload.get("recipe") if isinstance(payload.get("recipe"), dict) else {}
    policy = payload.get("policy") if isinstance(payload.get("policy"), dict) else {}
    contract = payload.get("contract") if isinstance(payload.get("contract"), dict) else {}
    operating_model = recipe.get("operating_model") if isinstance(recipe.get("operating_model"), dict) else {}
    expected = policy.get("expected_baseline") if isinstance(policy.get("expected_baseline"), dict) else {}

    title = str(manifest.get("title") or "Workspace").strip()
    blueprint_slug = _slug(manifest.get("slug") or title)
    primary_work = str(operating_model.get("primary_work") or manifest.get("summary") or "Complete the Workspace's primary work.").strip()
    signal_nodes = [
        manifest.get("title"),
        manifest.get("summary"),
        manifest.get("description"),
        manifest.get("category"),
        operating_model.get("primary_work"),
        operating_model.get("services"),
        recipe.get("workflows"),
        expected.get("first_week_outputs"),
        (contract.get("requires") or {}).get("tools") if isinstance(contract.get("requires"), dict) else None,
    ]
    corpus = " ".join(_strings(signal_nodes)).lower()

    artifacts: list[dict[str, Any]] = [
        {
            "id": "run-brief",
            "kind": "document",
            "title": "Workspace run brief",
            "filename": f"{blueprint_slug}-run-brief.md",
            "mime_type": "text/markdown",
            "summary": primary_work[:240],
            "stage": "planning",
        }
    ]

    candidates: list[tuple[bool, dict[str, Any]]] = [
        (
            _contains(corpus, "video", "mp4", "movie", "视频"),
            {
                "id": "generated-video",
                "kind": "video",
                "title": "Generated video draft",
                "filename": f"{blueprint_slug}-demo.mp4",
                "mime_type": "video/mp4",
                "summary": "Reviewable simulated video output with duration and generation receipt.",
                "stage": "production",
                "duration_seconds": 45,
            },
        ),
        (
            _contains(corpus, "image", "visual", "illustration", "cover", "图片", "视觉"),
            {
                "id": "generated-visual",
                "kind": "image",
                "title": "Generated visual",
                "filename": f"{blueprint_slug}-visual.png",
                "mime_type": "image/png",
                "summary": "Reviewable simulated visual generated from the approved brief.",
                "stage": "production",
            },
        ),
        (
            _contains(corpus, "audio", "narration", "voiceover", "podcast", "旁白", "音频"),
            {
                "id": "generated-audio",
                "kind": "audio",
                "title": "Generated narration",
                "filename": f"{blueprint_slug}-narration.mp3",
                "mime_type": "audio/mpeg",
                "summary": "Simulated narration output retained as a reusable checkpoint.",
                "stage": "production",
                "duration_seconds": 45,
            },
        ),
        (
            _contains(corpus, "spreadsheet", "xlsx", "sheet", "dashboard", "表格"),
            {
                "id": "generated-spreadsheet",
                "kind": "spreadsheet",
                "title": "Generated operating sheet",
                "filename": f"{blueprint_slug}-tracker.xlsx",
                "mime_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "summary": "Simulated spreadsheet output populated from the Workspace run.",
                "stage": "delivery",
            },
        ),
        (
            _contains(corpus, "presentation", "slides", "slide deck", "pptx", "演示文稿"),
            {
                "id": "generated-presentation",
                "kind": "presentation",
                "title": "Generated presentation",
                "filename": f"{blueprint_slug}-review.pptx",
                "mime_type": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
                "summary": "Simulated presentation prepared for operator review.",
                "stage": "delivery",
            },
        ),
    ]
    for matched, artifact in candidates:
        if matched and len(artifacts) < 4:
            artifacts.append(artifact)

    # A blueprint with no media/file signal still demonstrates a concrete
    # review packet instead of ending with chat prose only.
    if len(artifacts) == 1:
        artifacts.append({
            "id": "review-packet",
            "kind": "document",
            "title": "Workspace review packet",
            "filename": f"{blueprint_slug}-review-packet.md",
            "mime_type": "text/markdown",
            "summary": "Simulated final packet containing the run result, checks, and next action.",
            "stage": "delivery",
        })

    experience = {
        "schema_version": SIMULATION_EXPERIENCE_VERSION,
        "title": f"Try {title}",
        "sample_prompt": primary_work[:500],
        "artifacts": artifacts,
        "completion_summary": "The artifacts, approvals, blockers, retries, and receipts are simulated inside Workspace Chat; no external system is changed.",
    }
    experience["stages"] = _generate_scenario(
        title=title,
        primary_work=primary_work,
        artifacts=artifacts,
        recipe=recipe,
    )
    return experience


def resolve_simulation_experience(payload: dict[str, Any]) -> dict[str, Any]:
    """Return an authored contract, or generate the backwards-compatible one."""
    recipe = payload.get("recipe") if isinstance(payload.get("recipe"), dict) else {}
    authored = recipe.get("simulation_experience")
    if isinstance(authored, dict) and authored:
        resolved = deepcopy(authored)
        if not isinstance(resolved.get("stages"), list) or not resolved["stages"]:
            generated = generate_simulation_experience(payload)
            resolved["stages"] = generated["stages"]
        return resolved
    return generate_simulation_experience(payload)
