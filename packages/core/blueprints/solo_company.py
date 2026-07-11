"""Config-backed frozen workspace blueprints for one-person-company scenarios.

The blueprint source of truth is JSON under
``packages/core/blueprints/configs/solo_company``.  This module is only a thin
registry/loader so existing installer and API paths can consume the payloads as
validated dictionaries.
"""
from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from packages.core.blueprints.payload import validate_payload


FROZEN_AT = "2026-06-03"
CONFIG_DIR = Path(__file__).with_name("configs") / "solo_company"
CONFIG_ORDER = (
    "solo-video-account-studio-v1.json",
    "solo-productized-service-os-v1.json",
    "solo-digital-product-store-v1.json",
)


@dataclass(frozen=True)
class FrozenSoloCompanyBlueprint:
    slug: str
    title: str
    summary: str
    payload: dict[str, Any]


def _load_payload(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    validate_payload(payload)
    return payload


def _load_blueprints() -> tuple[FrozenSoloCompanyBlueprint, ...]:
    blueprints: list[FrozenSoloCompanyBlueprint] = []
    for file_name in CONFIG_ORDER:
        payload = _load_payload(CONFIG_DIR / file_name)
        manifest = payload["manifest"]
        blueprints.append(
            FrozenSoloCompanyBlueprint(
                slug=manifest["slug"],
                title=manifest["title"],
                summary=manifest["summary"],
                payload=payload,
            )
        )
    return tuple(blueprints)


SOLO_COMPANY_BLUEPRINTS: tuple[FrozenSoloCompanyBlueprint, ...] = _load_blueprints()


def get_solo_company_blueprints() -> list[dict[str, Any]]:
    """Return deep copies of all frozen solo-company blueprint configs."""
    return [deepcopy(bp.payload) for bp in SOLO_COMPANY_BLUEPRINTS]


def get_solo_company_blueprint(slug: str) -> dict[str, Any]:
    """Return a deep copy of one frozen solo-company blueprint config."""
    for bp in SOLO_COMPANY_BLUEPRINTS:
        if bp.slug == slug:
            return deepcopy(bp.payload)
    raise KeyError(f"unknown solo-company blueprint slug: {slug}")


def validate_solo_company_blueprints() -> None:
    """Validate every frozen config against the current payload schema."""
    for bp in SOLO_COMPANY_BLUEPRINTS:
        validate_payload(bp.payload)


__all__ = [
    "CONFIG_DIR",
    "CONFIG_ORDER",
    "FROZEN_AT",
    "FrozenSoloCompanyBlueprint",
    "SOLO_COMPANY_BLUEPRINTS",
    "get_solo_company_blueprint",
    "get_solo_company_blueprints",
    "validate_solo_company_blueprints",
]
