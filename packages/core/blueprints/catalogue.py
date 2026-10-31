"""Validated platform catalogue, shared by Cloud and self-hosted seeding."""
from __future__ import annotations

import json
from pathlib import Path

from packages.core.blueprints.payload import validate_payload
from packages.core.blueprints.solo_company import get_solo_company_blueprints

PRACTICAL_CONFIG_DIR = Path(__file__).with_name("configs") / "practical"
RECORDING_CONFIG_DIR = Path(__file__).with_name("configs") / "recording"
RECORDING_CONFIG_ORDER = ("recording-shorts-v1.json", "recording-tutorial-v1.json")
CREATIVE_CONFIG_DIR = Path(__file__).with_name("configs") / "creative"
# The Carousel studio is the Agent-and-Skill version; the Flow version is retired.
CREATIVE_CONFIG_ORDER = ("creative-presentation-v1.json", "creative-podcast-v1.json", "creative-video-v1.json",
                         "creative-carousel-agent-v1.json", "creative-dubbing-v1.json", "creative-handbook-v1.json")
PRACTICAL_CONFIG_ORDER = (
    "practical-meeting-notes-v1.json",
    "practical-weekly-update-v1.json",
    "practical-feedback-triage-v1.json",
    "practical-support-replies-v1.json",
    "practical-proposal-draft-v1.json",
    "practical-sop-builder-v1.json",
    "practical-job-application-v1.json",
    "practical-content-repurpose-v1.json",
    "practical-bilingual-editor-v1.json",
    "practical-source-brief-v1.json",
)


def get_practical_blueprints() -> list[dict]:
    """Load retained document-flow examples, not Marketplace listings."""
    payloads = []
    for filename in PRACTICAL_CONFIG_ORDER:
        payload = json.loads((PRACTICAL_CONFIG_DIR / filename).read_text(encoding="utf-8"))
        validate_payload(payload)
        payloads.append(payload)
    return payloads


def get_platform_blueprints() -> list[dict]:
    """Return independent payloads; runtime/API discovery uses seeded rows."""
    return [
        *get_solo_company_blueprints(),
        *get_recording_blueprints(),
        *get_creative_blueprints(),
    ]


def get_creative_blueprints() -> list[dict]:
    """Browser-verified non-code studios with actual-file production gates."""
    payloads = []
    for filename in CREATIVE_CONFIG_ORDER:
        payload = json.loads((CREATIVE_CONFIG_DIR / filename).read_text(encoding="utf-8"))
        validate_payload(payload)
        payloads.append(payload)
    return payloads


def get_recording_blueprints() -> list[dict]:
    """Source-preserving video production, with mandatory runtime preflight."""
    payloads = []
    for filename in RECORDING_CONFIG_ORDER:
        payload = json.loads((RECORDING_CONFIG_DIR / filename).read_text(encoding="utf-8"))
        validate_payload(payload)
        payloads.append(payload)
    return payloads


