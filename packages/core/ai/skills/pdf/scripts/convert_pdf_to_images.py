from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import tempfile
from pathlib import Path


def _require_command(name: str) -> str:
    command = shutil.which(name)
    if command is None:
        raise RuntimeError(
            f"Required Poppler command `{name}` was not found. Install poppler-utils before rendering PDFs."
        )
    return command


def _png_dimensions(path: Path) -> tuple[int, int]:
    with path.open("rb") as stream:
        header = stream.read(24)
    if len(header) != 24 or header[:8] != b"\x89PNG\r\n\x1a\n" or header[12:16] != b"IHDR":
        raise ValueError(f"Poppler did not produce a valid PNG: {path}")
    return int.from_bytes(header[16:20], "big"), int.from_bytes(header[20:24], "big")


def convert(
    pdf_path: str | Path,
    output_dir: str | Path,
    *,
    dpi: int = 150,
    first_page: int | None = None,
    last_page: int | None = None,
    prefix: str = "page",
    password: str | None = None,
) -> list[dict[str, object]]:
    input_path = Path(pdf_path).expanduser().resolve()
    destination = Path(output_dir).expanduser().resolve()
    if not input_path.is_file():
        raise FileNotFoundError(f"PDF not found: {input_path}")
    if input_path.suffix.lower() != ".pdf":
        raise ValueError(f"Expected a .pdf input, got: {input_path}")
    if dpi < 72 or dpi > 600:
        raise ValueError("DPI must be between 72 and 600")
    if first_page is not None and first_page < 1:
        raise ValueError("First page must be 1 or greater")
    if last_page is not None and last_page < 1:
        raise ValueError("Last page must be 1 or greater")
    if first_page is not None and last_page is not None and first_page > last_page:
        raise ValueError("First page cannot be greater than last page")
    if not prefix or "/" in prefix or "\\" in prefix:
        raise ValueError("Prefix must be a non-empty filename prefix")

    pdftoppm = _require_command("pdftoppm")
    destination.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".pdf-render-", dir=destination) as temp_dir:
        temp_prefix = Path(temp_dir) / prefix
        command = [pdftoppm, "-png", "-r", str(dpi)]
        if password:
            command.extend(["-upw", password])
        if first_page is not None:
            command.extend(["-f", str(first_page)])
        if last_page is not None:
            command.extend(["-l", str(last_page)])
        command.extend([str(input_path), str(temp_prefix)])
        completed = subprocess.run(command, capture_output=True, text=True, check=False)
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip() or "unknown Poppler error"
            raise RuntimeError(f"pdftoppm failed: {detail}")

        rendered = sorted(
            Path(temp_dir).glob(f"{prefix}-*.png"),
            key=lambda path: int(path.stem.rsplit("-", 1)[1]),
        )
        if not rendered:
            raise RuntimeError("pdftoppm completed without producing any page images")

        manifest: list[dict[str, object]] = []
        for temporary_image in rendered:
            page_number = int(temporary_image.stem.rsplit("-", 1)[1])
            output_image = destination / temporary_image.name
            temporary_image.replace(output_image)
            width, height = _png_dimensions(output_image)
            manifest.append(
                {
                    "path": str(output_image),
                    "page": page_number,
                    "width": width,
                    "height": height,
                    "bytes": output_image.stat().st_size,
                }
            )
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description="Render PDF pages to full-resolution PNGs with Poppler")
    parser.add_argument("input_pdf")
    parser.add_argument("output_directory")
    parser.add_argument("--dpi", type=int, default=150)
    parser.add_argument("--first-page", type=int)
    parser.add_argument("--last-page", type=int)
    parser.add_argument("--prefix", default="page")
    parser.add_argument("--password", help="User password for an encrypted PDF")
    parser.add_argument("--json", action="store_true", help="Print a machine-readable render manifest")
    args = parser.parse_args()
    images = convert(
        args.input_pdf,
        args.output_directory,
        dpi=args.dpi,
        first_page=args.first_page,
        last_page=args.last_page,
        prefix=args.prefix,
        password=args.password,
    )
    result = {"pages_rendered": len(images), "dpi": args.dpi, "images": images}
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    else:
        for image in images:
            print(
                f"Rendered page {image['page']}: {image['path']} "
                f"({image['width']}x{image['height']}, {image['bytes']} bytes)"
            )
        print(f"Rendered {len(images)} page(s) at {args.dpi} DPI")


if __name__ == "__main__":
    main()
