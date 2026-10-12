#!/usr/bin/env python3
"""Rasterize validated slide SVGs to full-page PNGs for image-mode export."""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
from pathlib import Path

from config import CANVAS_FORMATS
from project_utils import parse_project_name


def render_page_images(project: Path, *, output_dir: Path | None = None) -> list[Path]:
    project = project.expanduser().resolve()
    source = project / "svg_final"
    if not source.is_dir():
        source = project / "svg_output"
    def page_order(path: Path) -> tuple[int, str]:
        match = re.search(r"(\d+)$", path.stem)
        return (int(match.group(1)) if match else 0, path.name)

    pages = sorted(source.glob("*.svg"), key=page_order)
    if not pages:
        raise ValueError(f"No SVG pages found under {project}")
    if shutil.which("rsvg-convert") is None:
        raise RuntimeError("Required renderer is unavailable: rsvg-convert")
    fmt = parse_project_name(project.name).get("format") or "ppt169"
    canvas = CANVAS_FORMATS.get(fmt, CANVAS_FORMATS["ppt169"])
    destination = (output_dir or project / "page_images").expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    results = []
    for index, svg in enumerate(pages, start=1):
        png = destination / f"page_{index:02d}.png"
        subprocess.run(
            ["rsvg-convert", "--width", str(canvas["width"]), "--height",
             str(canvas["height"]), "--output", str(png), str(svg)],
            check=True, capture_output=True, text=True, timeout=60,
        )
        if not png.is_file() or png.stat().st_size == 0:
            raise RuntimeError(f"Renderer produced no image for {svg.name}")
        results.append(png)
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project", type=Path)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    for page in render_page_images(args.project, output_dir=args.output_dir):
        print(page)


if __name__ == "__main__":
    main()
