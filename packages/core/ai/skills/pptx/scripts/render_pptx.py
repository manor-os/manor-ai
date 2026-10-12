#!/usr/bin/env python3
"""Render a PPTX to numbered PNG slides for the final quality gate."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import tempfile
from pathlib import Path


def render_pptx(deck: Path, output_dir: Path, *, dpi: int = 120) -> list[Path]:
    deck = deck.expanduser().resolve()
    if not deck.is_file() or deck.suffix.lower() != ".pptx":
        raise ValueError(f"PPTX file not found: {deck}")
    if not 72 <= dpi <= 240:
        raise ValueError("dpi must be between 72 and 240")
    for command in ("libreoffice", "pdftoppm"):
        if shutil.which(command) is None:
            raise RuntimeError(f"Required renderer is unavailable: {command}")

    with tempfile.TemporaryDirectory(prefix="manor-pptx-render-") as scratch:
        work = Path(scratch)
        profile = work / "lo-profile"
        subprocess.run(
            [
                "libreoffice", f"-env:UserInstallation={profile.as_uri()}",
                "--headless", "--convert-to", "pdf:impress_pdf_Export",
                "--outdir", str(work), str(deck),
            ],
            check=True, capture_output=True, text=True, timeout=120,
        )
        pdf = work / f"{deck.stem}.pdf"
        if not pdf.is_file() or pdf.stat().st_size == 0:
            raise RuntimeError("LibreOffice did not produce a PDF")
        subprocess.run(
            ["pdftoppm", "-png", "-r", str(dpi), str(pdf), str(work / "page")],
            check=True, capture_output=True, text=True, timeout=180,
        )
        pages = sorted(
            work.glob("page-*.png"),
            key=lambda path: int(path.stem.split("-")[-1]),
        )
        if not pages:
            raise RuntimeError("PDF renderer did not produce any slide images")
        output_dir = output_dir.expanduser().resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        for old_page in output_dir.glob("slide-*.png"):
            old_page.unlink()
        results = []
        for index, page in enumerate(pages, start=1):
            destination = output_dir / f"slide-{index}.png"
            shutil.copyfile(page, destination)
            results.append(destination)
        return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pptx", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--dpi", type=int, default=120)
    args = parser.parse_args()
    for page in render_pptx(args.pptx, args.output_dir, dpi=args.dpi):
        print(page)


if __name__ == "__main__":
    main()
