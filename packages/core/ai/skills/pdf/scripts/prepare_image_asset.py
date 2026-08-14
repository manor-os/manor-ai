from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from PIL import Image, ImageColor, ImageOps, UnidentifiedImageError


SOURCE_TYPES = frozenset({"user", "downloaded", "generated"})
OUTPUT_SUFFIXES = frozenset({".jpg", ".jpeg", ".png"})


def _has_alpha(image: Image.Image) -> bool:
    return "A" in image.getbands() or "transparency" in image.info


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_manifest(path: Path, record: dict[str, Any]) -> None:
    payload: dict[str, Any] = {"version": 1, "assets": []}
    if path.exists():
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict) or not isinstance(loaded.get("assets"), list):
            raise ValueError(f"Invalid image asset manifest: {path}")
        payload = loaded

    assets = [
        item
        for item in payload["assets"]
        if not isinstance(item, dict) or item.get("output_path") != record["output_path"]
    ]
    assets.append(record)
    payload["version"] = 1
    payload["assets"] = assets
    path.parent.mkdir(parents=True, exist_ok=True)

    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o644)
    os.replace(temporary, path)


def prepare_image_asset(
    source: str | Path,
    output: str | Path,
    *,
    source_type: str,
    source_ref: str | None = None,
    license_name: str | None = None,
    attribution: str | None = None,
    prompt: str | None = None,
    manifest_path: str | Path | None = None,
    max_long_edge: int = 2400,
    jpeg_quality: int = 92,
    background: str = "#FFFFFF",
) -> dict[str, Any]:
    source_path = Path(source).expanduser()
    output_path = Path(output).expanduser()
    normalized_source_type = str(source_type or "").strip().lower()

    if normalized_source_type not in SOURCE_TYPES:
        raise ValueError(f"source_type must be one of {sorted(SOURCE_TYPES)}")
    if normalized_source_type == "downloaded" and not str(source_ref or "").strip():
        raise ValueError("Downloaded images require source_ref provenance")
    if normalized_source_type == "downloaded" and not str(license_name or "").strip():
        raise ValueError("Downloaded images require an explicit license or user-authorized value")
    if normalized_source_type == "generated" and not str(prompt or "").strip():
        raise ValueError("Generated images require the generation prompt")
    if not source_path.is_file():
        raise FileNotFoundError(f"Image source not found: {source_path}")
    if source_path.resolve() == output_path.resolve():
        raise ValueError("Source and prepared image paths must differ")
    if output_path.suffix.lower() not in OUTPUT_SUFFIXES:
        raise ValueError(f"Output must use one of {sorted(OUTPUT_SUFFIXES)}")
    if max_long_edge < 1:
        raise ValueError("max_long_edge must be positive")
    if not 1 <= jpeg_quality <= 100:
        raise ValueError("jpeg_quality must be between 1 and 100")

    try:
        with Image.open(source_path) as opened:
            source_format = str(opened.format or "unknown").upper()
            original_size = opened.size
            image = ImageOps.exif_transpose(opened).copy()
    except UnidentifiedImageError as exc:
        raise ValueError(f"Unsupported or corrupt image: {source_path}") from exc

    resized = False
    if max(image.size) > max_long_edge:
        scale = max_long_edge / max(image.size)
        target = (
            max(1, round(image.width * scale)),
            max(1, round(image.height * scale)),
        )
        image = image.resize(target, Image.Resampling.LANCZOS)
        resized = True

    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=f".{output_path.stem}-",
            suffix=output_path.suffix,
            dir=output_path.parent,
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)

        suffix = output_path.suffix.lower()
        if suffix == ".png":
            prepared = image.convert("RGBA" if _has_alpha(image) else "RGB")
            prepared.save(temporary_path, format="PNG", optimize=True)
        else:
            if _has_alpha(image):
                rgba = image.convert("RGBA")
                matte = Image.new("RGBA", rgba.size, ImageColor.getcolor(background, "RGBA"))
                matte.alpha_composite(rgba)
                prepared = matte.convert("RGB")
            else:
                prepared = image.convert("RGB")
            prepared.save(
                temporary_path,
                format="JPEG",
                quality=jpeg_quality,
                optimize=True,
                progressive=True,
            )

        with Image.open(temporary_path) as verified:
            verified.verify()
        os.chmod(temporary_path, 0o644)
        os.replace(temporary_path, output_path)
        temporary_path = None
    finally:
        if temporary_path and temporary_path.exists():
            temporary_path.unlink()

    with Image.open(output_path) as final_image:
        final_width, final_height = final_image.size
        final_mode = final_image.mode

    record: dict[str, Any] = {
        "output_path": str(output_path),
        "source_path": str(source_path),
        "source_type": normalized_source_type,
        "source_ref": str(source_ref or "").strip() or None,
        "source_format": source_format,
        "license": str(license_name or "").strip() or None,
        "attribution": str(attribution or "").strip() or None,
        "generation_prompt": str(prompt or "").strip() or None,
        "original_width": original_size[0],
        "original_height": original_size[1],
        "width": final_width,
        "height": final_height,
        "mode": final_mode,
        "resized": resized,
        "bytes": output_path.stat().st_size,
        "sha256": _sha256(output_path),
    }

    if manifest_path is not None:
        _write_manifest(Path(manifest_path).expanduser(), record)
    return record


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Normalize a local image for deterministic PDF embedding and record provenance."
    )
    parser.add_argument("source")
    parser.add_argument("output")
    parser.add_argument("--source-type", choices=sorted(SOURCE_TYPES), required=True)
    parser.add_argument("--source-ref")
    parser.add_argument("--license", dest="license_name")
    parser.add_argument("--attribution")
    parser.add_argument("--prompt")
    parser.add_argument("--manifest")
    parser.add_argument("--max-long-edge", type=int, default=2400)
    parser.add_argument("--jpeg-quality", type=int, default=92)
    parser.add_argument("--background", default="#FFFFFF")
    return parser


def main() -> int:
    args = _parser().parse_args()
    record = prepare_image_asset(
        args.source,
        args.output,
        source_type=args.source_type,
        source_ref=args.source_ref,
        license_name=args.license_name,
        attribution=args.attribution,
        prompt=args.prompt,
        manifest_path=args.manifest,
        max_long_edge=args.max_long_edge,
        jpeg_quality=args.jpeg_quality,
        background=args.background,
    )
    print(json.dumps(record, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
