#!/usr/bin/env python3
"""Validate Manor's structured slide-layout and visual-rhythm contract."""

from __future__ import annotations

import argparse
import json
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

BACKGROUND_MODES = {"light", "dark", "split", "image", "gradient"}
DOMINANT_VISUALS = {
    "chart",
    "diagram",
    "framework",
    "image",
    "media",
    "metric",
    "process",
    "roadmap",
    "table",
    "typography",
}
DENSITIES = {"sparse", "medium", "dense"}
QUALITY_TIERS = {"standard", "premium"}
TOKEN_RE = re.compile(r"^[a-z][a-z0-9-]{1,63}$")
TEXT_ROLE_REGISTRY = Path(__file__).resolve().parents[1] / "templates" / "layouts" / "text_role_registry.json"


def _load_text_roles() -> set[str]:
    try:
        payload = json.loads(TEXT_ROLE_REGISTRY.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Cannot read text-role registry: {exc}") from exc
    roles = payload.get("roles") if isinstance(payload, dict) else None
    if not isinstance(roles, dict) or not roles:
        raise RuntimeError("text_role_registry.json must define a non-empty roles object")
    return {str(role) for role in roles}


TEXT_ROLES = _load_text_roles()


@dataclass
class LayoutPlanResult:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)

    def error(self, message: str) -> None:
        if message not in self.errors:
            self.errors.append(message)

    def warn(self, message: str) -> None:
        if message not in self.warnings:
            self.warnings.append(message)

    def payload(self) -> dict[str, Any]:
        return {
            "status": "pass" if not self.errors else "fail",
            "errors": self.errors,
            "warnings": self.warnings,
            "metrics": self.metrics,
        }


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def load_layout_plan(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot read layout plan {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise TypeError("layout_plan.json must contain one JSON object")
    return payload


def _ordered_slides(slides: dict[str, Any], result: LayoutPlanResult) -> list[tuple[int, str, dict]]:
    ordered: list[tuple[int, str, dict]] = []
    for key, value in slides.items():
        match = re.fullmatch(r"P(\d{2,3})", str(key))
        if not match:
            result.error(f"Invalid slide key {key!r}; use P01, P02, ...")
            continue
        ordered.append((int(match.group(1)), str(key), _as_dict(value)))
    ordered.sort()
    if ordered:
        actual = [number for number, _key, _spec in ordered]
        expected = list(range(1, max(actual) + 1))
        if actual != expected:
            result.error("layout_plan.json slide keys must form one continuous sequence from P01")
    return ordered


def _validate_budget(key: str, budget: dict[str, Any], result: LayoutPlanResult) -> None:
    limits = {
        "title_lines": (1, 2),
        "max_words": (1, 120),
        "max_items": (1, 9),
    }
    for field_name, (minimum, maximum) in limits.items():
        value = budget.get(field_name)
        if not isinstance(value, int) or not minimum <= value <= maximum:
            result.error(f"{key} content_budget.{field_name} must be an integer between {minimum} and {maximum}")


def _validate_regions(
    key: str,
    regions: list[Any],
    canvas_width: float,
    canvas_height: float,
    result: LayoutPlanResult,
) -> None:
    ids: set[str] = set()
    for index, raw_region in enumerate(regions, start=1):
        region = _as_dict(raw_region)
        region_id = str(region.get("id") or "")
        if not region_id or not TOKEN_RE.fullmatch(region_id):
            result.error(f"{key} region {index} requires a kebab-case id")
            continue
        if region_id in ids:
            result.error(f"{key} repeats region id {region_id!r}")
        ids.add(region_id)
        try:
            x = float(region["x"])
            y = float(region["y"])
            width = float(region["width"])
            height = float(region["height"])
        except (KeyError, TypeError, ValueError):
            result.error(f"{key} region {region_id!r} requires numeric x, y, width, and height")
            continue
        if x < 0 or y < 0 or width <= 0 or height <= 0:
            result.error(f"{key} region {region_id!r} has invalid geometry")
        elif x + width > canvas_width or y + height > canvas_height:
            result.error(f"{key} region {region_id!r} crosses the canvas boundary")


def _root_metadata(svg_path: Path) -> dict[str, str]:
    try:
        root = ET.parse(svg_path).getroot()
    except (OSError, ET.ParseError) as exc:
        raise ValueError(f"Cannot parse {svg_path}: {exc}") from exc
    return {
        "layout_family": str(root.get("data-layout-family") or ""),
        "background": str(root.get("data-background") or ""),
        "dominant_visual": str(root.get("data-dominant-visual") or ""),
        "density": str(root.get("data-density") or ""),
        "text_roles": str(root.get("data-text-roles") or ""),
    }


def validate_layout_plan(
    plan: dict[str, Any],
    *,
    svg_dir: Path | None = None,
    expected_slide_count: int | None = None,
) -> dict[str, Any]:
    result = LayoutPlanResult()
    if plan.get("version") != 1:
        result.error("layout_plan.json version must be 1")
    quality_tier = str(plan.get("quality_tier") or "standard")
    if quality_tier not in QUALITY_TIERS:
        result.error(f"layout_plan.json quality_tier must be one of {sorted(QUALITY_TIERS)}")
    canvas = _as_dict(plan.get("canvas"))
    try:
        canvas_width = float(canvas.get("width"))
        canvas_height = float(canvas.get("height"))
    except (TypeError, ValueError):
        canvas_width = canvas_height = 0
    if canvas_width <= 0 or canvas_height <= 0:
        result.error("layout_plan.json canvas requires positive width and height")

    slides = _as_dict(plan.get("slides"))
    if not slides:
        result.error("layout_plan.json slides must be a non-empty object")
        return result.payload()
    ordered = _ordered_slides(slides, result)
    if expected_slide_count is not None and len(ordered) != expected_slide_count:
        result.error(f"layout_plan.json contains {len(ordered)} slides, expected {expected_slide_count}")

    signatures: list[tuple[str, str, str, str]] = []
    family_sequence: list[str] = []
    families: set[str] = set()
    backgrounds: set[str] = set()
    dominant_visuals: set[str] = set()
    deck_text_roles: set[str] = set()
    text_role_signatures: list[tuple[str, ...]] = []
    svg_files = sorted(svg_dir.glob("*.svg")) if svg_dir and svg_dir.is_dir() else []
    if svg_dir is not None and len(svg_files) != len(ordered):
        result.error(f"SVG directory contains {len(svg_files)} pages for {len(ordered)} layout rows")

    for position, (_number, key, spec) in enumerate(ordered):
        family = str(spec.get("layout_family") or "")
        background = str(spec.get("background") or "")
        dominant = str(spec.get("dominant_visual") or "")
        density = str(spec.get("density") or "")
        role = str(spec.get("role") or "")
        if not TOKEN_RE.fullmatch(family):
            result.error(f"{key} layout_family must be a kebab-case token")
        if not TOKEN_RE.fullmatch(role):
            result.error(f"{key} role must be a kebab-case token")
        if background not in BACKGROUND_MODES:
            result.error(f"{key} background must be one of {sorted(BACKGROUND_MODES)}")
        if dominant not in DOMINANT_VISUALS:
            result.error(f"{key} dominant_visual must be one of {sorted(DOMINANT_VISUALS)}")
        if density not in DENSITIES:
            result.error(f"{key} density must be one of {sorted(DENSITIES)}")
        raw_text_roles = _as_list(spec.get("text_roles"))
        text_roles = [str(role) for role in raw_text_roles]
        if not text_roles and quality_tier == "premium":
            result.error(f"{key} premium layout requires a non-empty text_roles array")
        elif len(text_roles) != len(set(text_roles)):
            result.error(f"{key} text_roles must not contain duplicates")
        unknown_text_roles = sorted(set(text_roles) - TEXT_ROLES)
        if unknown_text_roles:
            result.error(f"{key} text_roles contains unknown role(s): {', '.join(unknown_text_roles)}")
        minimum_roles = 3 if density == "sparse" else 4
        if quality_tier == "premium" and len(text_roles) < minimum_roles:
            result.error(f"{key} {quality_tier} {density} layout requires at least {minimum_roles} text roles")
        deck_text_roles.update(text_roles)
        text_role_signatures.append(tuple(sorted(text_roles)))
        _validate_budget(key, _as_dict(spec.get("content_budget")), result)
        budget = _as_dict(spec.get("content_budget"))
        if quality_tier == "premium":
            word_caps = {"sparse": 45, "medium": 75, "dense": 105}
            item_caps = {"sparse": 4, "medium": 6, "dense": 9}
            if isinstance(budget.get("max_words"), int) and budget["max_words"] > word_caps.get(density, 105):
                result.error(f"{key} premium {density} layout allows at most {word_caps.get(density, 105)} words")
            if isinstance(budget.get("max_items"), int) and budget["max_items"] > item_caps.get(density, 9):
                result.error(f"{key} premium {density} layout allows at most {item_caps.get(density, 9)} items")
        regions = _as_list(spec.get("regions"))
        if not regions:
            result.error(f"{key} must declare at least one bounded layout region")
        else:
            _validate_regions(key, regions, canvas_width, canvas_height, result)

        signatures.append((family, background, dominant, density))
        family_sequence.append(family)
        families.add(family)
        backgrounds.add(background)
        dominant_visuals.add(dominant)

        if position < len(svg_files):
            svg_path = svg_files[position]
            try:
                metadata = _root_metadata(svg_path)
            except ValueError as exc:
                result.error(str(exc))
            else:
                expected_metadata = {
                    "layout_family": family,
                    "background": background,
                    "dominant_visual": dominant,
                    "density": density,
                }
                for field_name, expected in expected_metadata.items():
                    if metadata[field_name] != expected:
                        result.error(
                            f"{svg_path.name} data-{field_name.replace('_', '-')} "
                            f"is {metadata[field_name]!r}, expected {expected!r}"
                        )
                svg_text_roles = {role.strip() for role in metadata["text_roles"].split(",") if role.strip()}
                if svg_text_roles != set(text_roles):
                    result.error(
                        f"{svg_path.name} data-text-roles is "
                        f"{sorted(svg_text_roles)!r}, expected {sorted(set(text_roles))!r}"
                    )

    for index in range(max(0, len(signatures) - 2)):
        if len(set(signatures[index : index + 3])) == 1:
            result.error(f"{ordered[index][1]}-{ordered[index + 2][1]} repeat the same complete silhouette")
    if quality_tier == "premium":
        for index in range(max(0, len(signatures) - 1)):
            if signatures[index] == signatures[index + 1]:
                result.error(f"{ordered[index][1]}-{ordered[index + 1][1]} repeat the same adjacent premium silhouette")
            if text_role_signatures[index] == text_role_signatures[index + 1]:
                result.warn(f"{ordered[index][1]}-{ordered[index + 1][1]} repeat the same adjacent text-role signature")
    if len(ordered) >= 6:
        if quality_tier == "premium":
            if len(ordered) >= 17:
                minimum_families = 8
            elif len(ordered) >= 12:
                minimum_families = 7
            elif len(ordered) >= 9:
                minimum_families = 5
            else:
                minimum_families = 4
        else:
            minimum_families = min(4, math.ceil(len(ordered) / 3))
        if len(families) < minimum_families:
            result.error(f"Deck uses {len(families)} layout families; at least {minimum_families} are required")
        if len(backgrounds) == 1:
            result.warn("Every slide uses the same background mode; confirm this is intentional")
        if len(dominant_visuals) == 1:
            result.warn("Every slide uses the same dominant visual type")
        if quality_tier == "premium":
            minimum_visuals = 5 if len(ordered) >= 12 else 4 if len(ordered) >= 9 else 3
            if len(dominant_visuals) < minimum_visuals:
                result.error(
                    f"Premium deck uses {len(dominant_visuals)} dominant visual types; "
                    f"at least {minimum_visuals} are required"
                )
            if len(ordered) >= 8 and len(backgrounds) < 2:
                result.error("Premium deck requires at least two background modes")
            family_counts = Counter(family_sequence)
            most_common_family, most_common_count = family_counts.most_common(1)[0]
            maximum_family_count = max(2, math.floor(len(ordered) * 0.35))
            if most_common_count > maximum_family_count:
                result.error(
                    f"Premium deck uses layout family {most_common_family!r} on "
                    f"{most_common_count}/{len(ordered)} slides; maximum is {maximum_family_count}"
                )
            minimum_text_roles = 9 if len(ordered) >= 12 else 8 if len(ordered) >= 9 else 7
            if len(deck_text_roles) < minimum_text_roles:
                result.error(
                    f"Premium deck uses {len(deck_text_roles)} text roles; at least {minimum_text_roles} are required"
                )

    result.metrics.update(
        {
            "slide_count": len(ordered),
            "quality_tier": quality_tier,
            "layout_family_count": len(families),
            "background_mode_count": len(backgrounds),
            "dominant_visual_count": len(dominant_visuals),
            "text_role_count": len(deck_text_roles),
            "unique_silhouette_count": len(set(signatures)),
        }
    )
    return result.payload()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("layout_plan", type=Path)
    parser.add_argument("--svg-dir", type=Path)
    parser.add_argument("--slide-count", type=int)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    try:
        plan = load_layout_plan(args.layout_plan)
        payload = validate_layout_plan(
            plan,
            svg_dir=args.svg_dir,
            expected_slide_count=args.slide_count,
        )
    except ValueError as exc:
        payload = {"status": "fail", "errors": [str(exc)], "warnings": [], "metrics": {}}
    rendered = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if payload["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
