#!/usr/bin/env python3
"""Compose a bundled Phosphor icon into a reusable PowerPoint-safe SVG."""

from __future__ import annotations

import argparse
import re
from pathlib import Path
from xml.etree import ElementTree as ET

from defusedxml import ElementTree as SafeET


SVG_NS = "http://www.w3.org/2000/svg"
ET.register_namespace("", SVG_NS)
ICON_DIR = Path(__file__).resolve().parents[1] / "templates" / "icons" / "phosphor-duotone"
HEX_COLOR = re.compile(r"#[0-9a-fA-F]{6}\Z")
ICON_NAME = re.compile(r"[a-z0-9-]+\Z")


def compose_icon(icon: str, output: Path, *, color: str, background: str) -> Path:
    if not ICON_NAME.fullmatch(icon):
        raise ValueError("icon must be a bundled kebab-case icon name")
    if not HEX_COLOR.fullmatch(color) or not HEX_COLOR.fullmatch(background):
        raise ValueError("color and background must be six-digit hex colors")
    source = ICON_DIR / f"{icon}.svg"
    if not source.is_file():
        raise ValueError(f"Bundled icon not found: {icon}")
    original = SafeET.parse(source).getroot()
    root = ET.Element(f"{{{SVG_NS}}}svg", {"viewBox": "0 0 320 320"})
    ET.SubElement(root, f"{{{SVG_NS}}}rect", {
        "x": "0", "y": "0", "width": "320", "height": "320",
        "rx": "64", "fill": background,
    })
    group = ET.SubElement(root, f"{{{SVG_NS}}}g", {
        "transform": "translate(32 32)", "fill": color,
    })
    for child in original:
        group.append(child)
    output = output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(root).write(output, encoding="unicode", xml_declaration=True)
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("icon", help="Bundled Phosphor icon name, without .svg")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--color", default="#183153")
    parser.add_argument("--background", default="#EEF3F6")
    args = parser.parse_args()
    print(compose_icon(args.icon, args.output, color=args.color, background=args.background))


if __name__ == "__main__":
    main()
