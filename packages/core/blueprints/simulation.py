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

from packages.core.services.viewer_language import localized


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


def _zh(row: dict[str, Any]) -> dict[str, Any]:
    """The row as a Chinese reader sees it, when the Blueprint translated it."""
    return localized(row, "zh")


def _summary(row: dict[str, Any], fallback: str) -> str:
    return str(row.get("description") or row.get("summary") or _label(row, fallback)).strip()[:300]


def _proposal_action(
    stage_id: str,
    rows: list[dict[str, Any]],
    *,
    fallback_title: str,
) -> dict[str, Any]:
    items = [
        {
            "item_id": f"{stage_id}-{index + 1}",
            "kind": "workflow_change",
            "risk_level": "low",
            "summary": _summary(row, f"{fallback_title} {index + 1}"),
        }
        for index, row in enumerate(rows[:3])
    ] or [{
        "item_id": f"{stage_id}-1",
        "kind": "workflow_change",
        "risk_level": "low",
        "summary": fallback_title,
    }]
    return {
        "kind": "approve_proposals",
        "review_id": f"simulation-{stage_id}",
        "items": items,
        # A walkthrough grants nothing, so it offers no standing approval.
        "options": ["approve", "reject"],
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
    primary_work: str,
    primary_work_zh: str,
    artifacts: list[dict[str, Any]],
    recipe: dict[str, Any],
) -> list[dict[str, Any]]:
    """Build a short, deterministic Chat walkthrough from Blueprint data.

    Three decisions, the same ones a live run stops for: the plan, a
    question, and the results before they go out. The scenario uses the same
    pending-action vocabulary and message shapes as a live Workspace. It is
    data, not a browser timer: the server persists each stage and stops only
    at those cards. Every stage carries a Chinese overlay (``i18n.zh``) that
    the runtime resolves for the person who drives the run.
    """
    operating_model = (
        recipe.get("operating_model")
        if isinstance(recipe.get("operating_model"), dict)
        else {}
    )
    services = _records(operating_model.get("services"))
    workflows = _records(recipe.get("workflows"))

    first = services[0] if services else {}
    last = services[-1] if services else {}
    first_title = _label(first, "Planning") if first else "Planning"
    first_title_zh = _label(_zh(first), "计划") if first else "计划"
    workflow = workflows[0] if workflows else last
    delivery_title = _label(workflow, "Delivery") if workflow else "Delivery"
    delivery_title_zh = _label(_zh(workflow), "交付") if workflow else "交付"
    first_service_key = _service_key(services[0] if services else None)
    production_service_key = _service_key(
        services[min(1, len(services) - 1)] if services else None
    )
    delivery_service_key = _service_key(last or None)
    artifact_titles = ", ".join(str(item.get("title") or item["id"]) for item in artifacts)
    artifact_titles_zh = "、".join(
        str(((item.get("i18n") or {}).get("zh") or {}).get("title") or item.get("title") or item["id"])
        for item in artifacts
    )
    plan = _proposal_action("plan-proposal", services, fallback_title="Prepare the first piece of work")
    plan_zh_items = [
        {"summary": _summary(_zh(row), f"计划 {index + 1}")}
        for index, row in enumerate(services[:3])
    ] or [{"summary": "准备第一项工作"}]

    return [
        {
            "id": "request",
            "kind": "request",
            "title": "Try a run",
            "author": "user",
            "body": primary_work,
            "i18n": {"zh": {"title": "试运行一次", "body": primary_work_zh}},
        },
        {
            "id": "plan-proposal",
            "kind": "proposal",
            "title": "Confirm the plan",
            "author": "agent",
            "service_key": first_service_key,
            "body": "Here is the plan. Confirm it and I'll start:",
            "pending_action": plan,
            "i18n": {"zh": {
                "title": "确认计划",
                "body": "我按这个计划做，先确认一下：",
                "pending_action": {"items": plan_zh_items},
            }},
        },
        {
            "id": "first-work",
            "kind": "workflow",
            "title": first_title,
            "author": "agent",
            "service_key": first_service_key,
            "body": f"{first_title} is done.",
            "message_kind": "workflow_activity",
            "meta": _workflow_meta(first_title, services[:1]),
            "i18n": {"zh": {
                "title": first_title_zh,
                "body": f"{first_title_zh}已完成。",
                "meta": {"workflow_title": first_title_zh},
            }},
        },
        {
            "id": "operator-input",
            "kind": "input",
            "title": "Who is this for?",
            "author": "agent",
            "service_key": production_service_key or first_service_key,
            "body": "One question before I continue:",
            "pending_action": {
                "kind": "needs_input",
                "title": "Who is this for?",
                "context_summary": "Your answer shapes the results.",
                "questions": [
                    {
                        "key": "audience",
                        "label": "Main audience",
                        "type": "select",
                        "required": True,
                        "options": [
                            {"label": "My current audience", "value": "current"},
                            {"label": "A new audience", "value": "new"},
                        ],
                    },
                    {
                        "key": "guidance",
                        "label": "Anything else (optional)",
                        "type": "textarea",
                        "required": False,
                        "placeholder": "For example: avoid Friday releases",
                    },
                ],
            },
            "i18n": {"zh": {
                "title": "这次主要面向谁？",
                "body": "继续之前问你一个问题：",
                "pending_action": {
                    "title": "这次主要面向谁？",
                    "context_summary": "成品会按你的回答来准备。",
                    "questions": [
                        {
                            "label": "主要面向",
                            "options": [{"label": "现有受众"}, {"label": "新受众"}],
                        },
                        {"label": "补充说明（可不填）", "placeholder": "比如：避开周五发布"},
                    ],
                },
            }},
        },
        {
            "id": "output-approval",
            "kind": "approval",
            "title": "Check the results",
            "author": "agent",
            "service_key": production_service_key or first_service_key,
            "body": "The results are ready. Take a look before they go out:",
            "artifact_ids": [str(item["id"]) for item in artifacts],
            "pending_action": {
                "kind": "governance_approval",
                "hitl_type": "review",
                "prompt": "Can these go out?",
                "review_title": "Results",
                "review": {"summary": artifact_titles},
                "options": ["approve", "revise", "reject"],
            },
            "i18n": {"zh": {
                "title": "检查成品",
                "body": "成品准备好了，发出去之前请你看一眼：",
                "pending_action": {
                    "prompt": "这些可以发出去吗？",
                    "review_title": "成品",
                    "review": {"summary": artifact_titles_zh},
                },
            }},
        },
        {
            "id": "delivery",
            "kind": "workflow",
            "title": f"{delivery_title} (simulated, nothing was sent)",
            "author": "agent",
            "service_key": delivery_service_key or production_service_key,
            "body": "Delivered in the simulation. Nothing was sent, published or changed outside Manor.",
            "message_kind": "workflow_activity",
            "meta": _workflow_meta(f"{delivery_title} (simulated, nothing was sent)", services[-1:]),
            "i18n": {"zh": {
                "title": f"{delivery_title_zh}（模拟，没有真的发出去）",
                "body": "模拟交付完成。没有发送、发布或修改 Manor 之外的任何东西。",
                "meta": {"workflow_title": f"{delivery_title_zh}（模拟，没有真的发出去）"},
            }},
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
    primary_work_zh = str(
        _zh(operating_model).get("primary_work") or _zh(manifest).get("summary") or primary_work
    ).strip()
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
            "title": "Run brief",
            "filename": f"{blueprint_slug}-run-brief.md",
            "mime_type": "text/markdown",
            "summary": primary_work[:240],
            "stage": "planning",
            "i18n": {"zh": {"title": "运行简报", "summary": primary_work_zh[:240]}},
        }
    ]

    candidates: list[tuple[bool, dict[str, Any]]] = [
        (
            _contains(corpus, "video", "mp4", "movie", "视频"),
            {
                "id": "generated-video",
                "kind": "video",
                "title": "Video draft",
                "filename": f"{blueprint_slug}-demo.mp4",
                "mime_type": "video/mp4",
                "summary": "Stands in for the video a live run makes.",
                "stage": "production",
                "duration_seconds": 45,
                "i18n": {"zh": {"title": "视频初稿", "summary": "代表正式运行会做出的视频。"}},
            },
        ),
        (
            _contains(corpus, "image", "visual", "illustration", "cover", "图片", "视觉"),
            {
                "id": "generated-visual",
                "kind": "image",
                "title": "Visual",
                "filename": f"{blueprint_slug}-visual.png",
                "mime_type": "image/png",
                "summary": "Stands in for the image a live run makes.",
                "stage": "production",
                "i18n": {"zh": {"title": "配图", "summary": "代表正式运行会做出的图片。"}},
            },
        ),
        (
            _contains(corpus, "audio", "narration", "voiceover", "podcast", "旁白", "音频"),
            {
                "id": "generated-audio",
                "kind": "audio",
                "title": "Narration",
                "filename": f"{blueprint_slug}-narration.mp3",
                "mime_type": "audio/mpeg",
                "summary": "Stands in for the audio a live run makes.",
                "stage": "production",
                "duration_seconds": 45,
                "i18n": {"zh": {"title": "旁白", "summary": "代表正式运行会做出的音频。"}},
            },
        ),
        (
            _contains(corpus, "spreadsheet", "xlsx", "sheet", "dashboard", "表格"),
            {
                "id": "generated-spreadsheet",
                "kind": "spreadsheet",
                "title": "Tracking sheet",
                "filename": f"{blueprint_slug}-tracker.xlsx",
                "mime_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "summary": "Stands in for the spreadsheet a live run fills in.",
                "stage": "delivery",
                "i18n": {"zh": {"title": "跟踪表", "summary": "代表正式运行会填好的表格。"}},
            },
        ),
        (
            _contains(corpus, "presentation", "slides", "slide deck", "pptx", "演示文稿"),
            {
                "id": "generated-presentation",
                "kind": "presentation",
                "title": "Presentation",
                "filename": f"{blueprint_slug}-review.pptx",
                "mime_type": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
                "summary": "Stands in for the deck a live run makes.",
                "stage": "delivery",
                "i18n": {"zh": {"title": "演示文稿", "summary": "代表正式运行会做出的演示文稿。"}},
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
            "title": "Results",
            "filename": f"{blueprint_slug}-review-packet.md",
            "mime_type": "text/markdown",
            "summary": "Stands in for what a live run hands over.",
            "stage": "delivery",
            "i18n": {"zh": {"title": "成果汇总", "summary": "代表正式运行会交出的成果。"}},
        })

    experience = {
        "schema_version": SIMULATION_EXPERIENCE_VERSION,
        "title": f"Try {title}",
        "sample_prompt": primary_work[:500],
        "artifacts": artifacts,
        "completion_summary": "Everything in this walkthrough happened in Workspace Chat; no external system was changed.",
    }
    experience["stages"] = _generate_scenario(
        primary_work=primary_work,
        primary_work_zh=primary_work_zh,
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
