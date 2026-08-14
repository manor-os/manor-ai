from __future__ import annotations

from copy import deepcopy

import pytest

from packages.core.blueprints.payload import PayloadError, validate_payload
from packages.core.blueprints.simulation import (
    generate_simulation_experience,
    resolve_simulation_experience,
)


def _payload() -> dict:
    return {
        "manifest": {
            "blueprint_version": "1.1",
            "slug": "video-content-lab",
            "title": "Video Content Lab",
            "summary": "Produce a brief, image, narration, and finished MP4.",
            "kind": "one_person_company",
            "category": "content.video",
        },
        "contract": {
            "variables": [],
            "channels": [],
            "sessions": [],
            "requires": {
                "manor_min_version": None,
                "tools": ["generate_image", "generate_video", "generate_file"],
                "mcp_servers": [],
                "skills": [],
                "agents": [],
            },
        },
        "embedded": {"skills": [], "agents": [], "knowledge_packs": []},
        "recipe": {
            "operating_model": {"primary_work": "Create one review-ready video."},
            "strategist": None,
            "prompts": [],
            "subscriptions": [],
            "scheduled_jobs": [],
            "workflows": [],
            "goals": [],
            "task_categories": [],
            "custom_fields": [],
            "sla_policies": [],
            "escalation_rules": [],
        },
        "policy": {
            "governance": {},
            "post_install_checks": [],
            "expected_baseline": {"runnable_in_simulation": True},
        },
    }


def test_generator_derives_blueprint_specific_media_artifacts():
    experience = generate_simulation_experience(_payload())
    assert experience["schema_version"] == "1.0"
    assert experience["sample_prompt"] == "Create one review-ready video."
    kinds = [artifact["kind"] for artifact in experience["artifacts"]]
    assert kinds == ["document", "video", "image", "audio"]
    assert all(artifact["filename"] for artifact in experience["artifacts"])
    assert "external system" in experience["completion_summary"]
    stages = experience["stages"]
    assert stages[0]["id"] == "goal-request"
    assert stages[-1]["id"] == "goal-completed"
    assert [stage["kind"] for stage in stages].count("proposal") == 3
    assert {stage["kind"] for stage in stages} >= {
        "input", "workflow", "artifact", "approval", "failure", "receipt", "goal",
    }


def test_authored_experience_overrides_generated_fallback_without_mutating_payload():
    payload = _payload()
    authored = generate_simulation_experience(payload)
    authored["title"] = "A deliberately authored demo"
    payload["recipe"]["simulation_experience"] = authored
    resolved = resolve_simulation_experience(payload)
    resolved["title"] = "changed copy"
    assert payload["recipe"]["simulation_experience"]["title"] == "A deliberately authored demo"


def test_legacy_authored_experience_is_enriched_with_a_blueprint_scenario():
    payload = _payload()
    authored = generate_simulation_experience(payload)
    authored.pop("stages")
    payload["recipe"]["simulation_experience"] = authored
    resolved = resolve_simulation_experience(payload)
    assert resolved["artifacts"] == authored["artifacts"]
    assert len(resolved["stages"]) == 13
    assert "stages" not in payload["recipe"]["simulation_experience"]


def test_payload_validates_safe_local_preview_contract():
    payload = _payload()
    experience = generate_simulation_experience(payload)
    experience["artifacts"][1]["preview_url"] = "/assets/samples/artifacts/video/course-teaser-video.mp4"
    payload["recipe"]["simulation_experience"] = experience
    validate_payload(payload)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda x: x.update({"schema_version": "2.0"}), "schema_version"),
        (lambda x: x["artifacts"][0].update({"filename": "../unsafe.md"}), "safe filename"),
        (lambda x: x["artifacts"][0].update({"preview_url": "https://example.com/demo.mp4"}), "local /assets/"),
        (lambda x: x["artifacts"].append(deepcopy(x["artifacts"][0])), "must be unique"),
        (lambda x: x["stages"][0].update({"kind": "browser_magic"}), "stages\\[0\\].kind"),
        (lambda x: x["stages"][1]["pending_action"].update({"kind": "made_up"}), "pending_action.kind"),
    ],
)
def test_payload_rejects_unsafe_or_ambiguous_simulation_artifacts(mutate, message):
    payload = _payload()
    experience = generate_simulation_experience(payload)
    mutate(experience)
    payload["recipe"]["simulation_experience"] = experience
    with pytest.raises(PayloadError, match=message):
        validate_payload(payload)
