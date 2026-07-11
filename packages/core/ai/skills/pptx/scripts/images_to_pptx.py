#!/usr/bin/env python3
"""Assemble full-page images into a PPTX (one image per slide).

This is the export step for the optional **Full-Page Image mode** of PPT Master:
instead of hand-authoring SVG pages and converting them to native editable
shapes, each slide is a single AI-generated raster image. By default the whole
image is centered inside the slide so edge text is never clipped. The resulting
deck plays back in PowerPoint but its content is NOT
editable — every slide is one flat picture.

Usage:
    python3 scripts/images_to_pptx.py <project_path> [options]

Options:
    --images-dir <name>   Subdirectory under the project holding the page
                          images (default: images).
    --glob <pattern>      Filename glob for page images
                          (default: "page_*"; matched case-insensitively
                          against .png/.jpg/.jpeg/.webp).
    --format <key>        Canvas format key (e.g. ppt169, story). When omitted,
                          it is parsed from the project directory name, falling
                          back to ppt169.
    --fit cover|contain|stretch
                          How each image fills the slide (default: contain).
                          cover   = fill, center-crop the overflow (no bars);
                          contain = fit inside, letterbox bars where needed;
                          stretch = distort to the exact slide rectangle.
    -o, --output <path>   Output pptx path. Default:
                          <project>/exports/<name>_<timestamp>.pptx
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime
from pathlib import Path

# Reuse the skill's canvas-format definitions and project-name parsing.
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from config import CANVAS_FORMATS
    from project_utils import parse_project_name
except ImportError:  # pragma: no cover - fallback for standalone execution
    CANVAS_FORMATS = {
        "ppt169": {"name": "PPT 16:9", "dimensions": "1280×720"},
    }

    def parse_project_name(dir_name: str) -> dict:
        return {"name": dir_name, "format": "unknown"}


EMU_PER_INCH = 914400
EMU_PER_PIXEL = EMU_PER_INCH / 96
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}
FIT_MODES = ("cover", "contain", "stretch")


def slide_pixels(canvas_format: str) -> tuple[int, int]:
    """Return (width_px, height_px) for a canvas format, defaulting to 16:9."""
    info = CANVAS_FORMATS.get(canvas_format) or CANVAS_FORMATS.get("ppt169", {})
    dimensions = info.get("dimensions", "1280×720")
    match = re.match(r"(\d+)[×x](\d+)", dimensions)
    if match:
        return int(match.group(1)), int(match.group(2))
    return 1280, 720


def _natural_key(path: Path) -> list:
    """Sort key that orders page_2 before page_10."""
    return [
        int(token) if token.isdigit() else token.lower()
        for token in re.split(r"(\d+)", path.name)
    ]


def collect_images(images_dir: Path, pattern: str) -> list[Path]:
    """Return page images matching `pattern`, in natural order."""
    if not images_dir.is_dir():
        raise FileNotFoundError(f"Images directory not found: {images_dir}")
    matches = [
        p
        for p in images_dir.glob(pattern)
        if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES
    ]
    return sorted(matches, key=_natural_key)


def _image_size(path: Path) -> tuple[int, int] | None:
    """Return (width, height) in pixels, or None when it cannot be read."""
    try:
        from PIL import Image  # python-pptx already pulls in Pillow
    except ImportError:
        return None
    try:
        with Image.open(path) as img:
            return img.width, img.height
    except Exception:
        return None


def _add_cover(slide, path: Path, slide_w: int, slide_h: int) -> None:
    """Fill the slide, center-cropping whichever axis overflows."""
    from pptx.util import Emu

    size = _image_size(path)
    if not size or size[1] == 0 or slide_h == 0:
        # Without dimensions we cannot crop safely; stretch to the slide.
        slide.shapes.add_picture(str(path), 0, 0, width=Emu(slide_w), height=Emu(slide_h))
        return

    img_w, img_h = size
    img_aspect = img_w / img_h
    slide_aspect = slide_w / slide_h
    pic = slide.shapes.add_picture(
        str(path), 0, 0, width=Emu(slide_w), height=Emu(slide_h)
    )
    if abs(img_aspect - slide_aspect) < 1e-6:
        return
    if img_aspect > slide_aspect:
        # Image is wider than the slide: trim the left/right edges.
        crop = (1 - slide_aspect / img_aspect) / 2
        pic.crop_left = crop
        pic.crop_right = crop
    else:
        # Image is taller than the slide: trim the top/bottom edges.
        crop = (1 - img_aspect / slide_aspect) / 2
        pic.crop_top = crop
        pic.crop_bottom = crop


def _add_contain(slide, path: Path, slide_w: int, slide_h: int) -> None:
    """Fit the whole image inside the slide, centered, with letterbox bars."""
    from pptx.util import Emu

    size = _image_size(path)
    if not size or size[0] == 0 or size[1] == 0:
        slide.shapes.add_picture(str(path), 0, 0, width=Emu(slide_w), height=Emu(slide_h))
        return

    img_w, img_h = size
    scale = min(slide_w / img_w, slide_h / img_h)
    draw_w = int(img_w * scale)
    draw_h = int(img_h * scale)
    left = (slide_w - draw_w) // 2
    top = (slide_h - draw_h) // 2
    slide.shapes.add_picture(
        str(path), Emu(left), Emu(top), width=Emu(draw_w), height=Emu(draw_h)
    )


def _add_stretch(slide, path: Path, slide_w: int, slide_h: int) -> None:
    """Stretch the image to the exact slide rectangle (may distort)."""
    from pptx.util import Emu

    slide.shapes.add_picture(str(path), 0, 0, width=Emu(slide_w), height=Emu(slide_h))


def build_pptx(
    images: list[Path],
    width_px: int,
    height_px: int,
    output_path: Path,
    fit: str = "cover",
) -> Path:
    """Build a PPTX with one full-bleed image per slide."""
    from pptx import Presentation

    slide_w = int(round(width_px * EMU_PER_PIXEL))
    slide_h = int(round(height_px * EMU_PER_PIXEL))

    prs = Presentation()
    prs.slide_width = slide_w
    prs.slide_height = slide_h
    blank_layout = prs.slide_layouts[6]  # fully blank layout

    adder = {"cover": _add_cover, "contain": _add_contain, "stretch": _add_stretch}[fit]
    for image_path in images:
        slide = prs.slides.add_slide(blank_layout)
        adder(slide, image_path, slide_w, slide_h)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    prs.save(str(output_path))
    return output_path


def resolve_format(project_path: Path, explicit: str | None) -> str:
    """Resolve the canvas format from a flag or the project directory name."""
    if explicit:
        return explicit
    parsed = parse_project_name(project_path.name)
    fmt = parsed.get("format", "unknown")
    return fmt if fmt in CANVAS_FORMATS else "ppt169"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Assemble full-page images into a one-image-per-slide PPTX."
    )
    parser.add_argument("project_path", help="PPT Master project directory.")
    parser.add_argument("--images-dir", default="images", help="Subdir with page images.")
    parser.add_argument("--glob", default="page_*", help="Filename glob for page images.")
    parser.add_argument("--format", dest="canvas_format", default=None, help="Canvas format key.")
    parser.add_argument("--fit", choices=FIT_MODES, default="contain", help="Slide fill mode.")
    parser.add_argument("-o", "--output", default=None, help="Output pptx path.")
    parser.add_argument(
        "--cleanup",
        action="store_true",
        help="Delete the source page images after the deck is written (keep only the .pptx).",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()

    project_path = Path(args.project_path).resolve()
    if not project_path.is_dir():
        print(f"Error: project directory not found: {project_path}", file=sys.stderr)
        return 1

    images_dir = project_path / args.images_dir
    try:
        images = collect_images(images_dir, args.glob)
    except FileNotFoundError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if not images:
        print(
            f"Error: no images matched {args.glob!r} in {images_dir} "
            f"(supported: {', '.join(sorted(IMAGE_SUFFIXES))})",
            file=sys.stderr,
        )
        return 1

    canvas_format = resolve_format(project_path, args.canvas_format)
    width_px, height_px = slide_pixels(canvas_format)

    if args.output:
        output_path = Path(args.output).resolve()
    else:
        name = parse_project_name(project_path.name).get("name", project_path.name)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = project_path / "exports" / f"{name}_{timestamp}.pptx"

    build_pptx(images, width_px, height_px, output_path, fit=args.fit)

    print(f"[OK] Built image-mode PPTX: {output_path}")
    print(f"     Slides: {len(images)} | Format: {canvas_format} "
          f"({width_px}×{height_px}) | Fit: {args.fit}")
    for image_path in images:
        print(f"       - {image_path.name}")

    if args.cleanup:
        removed = 0
        for image_path in images:
            try:
                image_path.unlink()
                removed += 1
            except OSError as exc:
                print(f"     [warn] could not remove {image_path.name}: {exc}", file=sys.stderr)
        print(f"     Cleaned up {removed} intermediate page image(s); kept only the deck.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
