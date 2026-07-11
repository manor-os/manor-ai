#!/usr/bin/env python3
"""Manage AI image prompt manifests for the built-in pptx skill.

This script does not call any image generation provider. It validates
``image_prompts.json``, renders the paste-ready ``image_prompts.md`` sidecar,
updates item statuses, and verifies generated files exist.
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path

STATUS_PENDING = "Pending"
STATUS_GENERATED = "Generated"
STATUS_FAILED = "Failed"
STATUS_NEEDS_MANUAL = "Needs-Manual"
VALID_STATUSES = {STATUS_PENDING, STATUS_GENERATED, STATUS_FAILED, STATUS_NEEDS_MANUAL}
REQUIRED_ITEM_FIELDS = ("filename", "prompt", "aspect_ratio", "status")


def load_manifest(path: str | Path) -> dict:
    """Load and validate an image prompt manifest."""
    manifest_path = Path(path)
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Invalid JSON in {manifest_path}: {exc.msg} "
            f"(line {exc.lineno}, col {exc.colno})"
        ) from exc

    if not isinstance(data, dict):
        raise ValueError(
            f"{manifest_path}: top level must be a JSON object, "
            f"got {type(data).__name__}"
        )

    items = data.get("items")
    if not isinstance(items, list) or not items:
        raise ValueError(f"{manifest_path}: 'items' must be a non-empty array")

    seen_filenames: set[str] = set()
    for i, item in enumerate(items):
        prefix = f"{manifest_path}: items[{i}]"
        if not isinstance(item, dict):
            raise ValueError(f"{prefix} must be an object")
        for field in REQUIRED_ITEM_FIELDS:
            if field not in item:
                raise ValueError(f"{prefix} missing required field '{field}'")
            if not isinstance(item[field], str) or not item[field].strip():
                raise ValueError(f"{prefix} field '{field}' must be a non-empty string")
        if item["status"] not in VALID_STATUSES:
            raise ValueError(
                f"{prefix} status '{item['status']}' is invalid. "
                f"Valid: {sorted(VALID_STATUSES)}"
            )
        fname = item["filename"]
        if fname in seen_filenames:
            raise ValueError(f"{prefix} duplicate filename '{fname}'")
        seen_filenames.add(fname)

    return data


def save_manifest(path: str | Path, data: dict) -> None:
    """Atomically write a manifest back to disk."""
    target = Path(path)
    fd, tmp_path = tempfile.mkstemp(
        prefix=target.stem + ".",
        suffix=".tmp",
        dir=str(target.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp_path, target)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def render_manifest_md(manifest: dict) -> str:
    """Render the manifest into a paste-ready Markdown sidecar."""
    lines: list[str] = [
        "# Image Generation Prompts",
        "",
        "> Auto-generated from `image_prompts.json` by `image_prompts.py --render-md`.",
        "> Do not hand-edit; rerun the command to refresh.",
        "",
    ]

    project = manifest.get("project")
    generated_at = manifest.get("generated_at")
    color_scheme = manifest.get("color_scheme") or {}
    rendering = manifest.get("deck_rendering")
    palette = manifest.get("deck_palette")

    if project:
        lines.append(f"> Project: {project}")
    if generated_at:
        lines.append(f"> Generated: {generated_at}")
    if rendering:
        lines.append(f"> Deck Rendering: {rendering}")
    if palette:
        lines.append(f"> Deck Palette: {palette}")
    if color_scheme:
        cs = " | ".join(f"{k.capitalize()} {v}" for k, v in color_scheme.items())
        lines.append(f"> Color scheme: {cs}")

    lines.extend(["", "---", ""])

    for i, item in enumerate(manifest["items"], start=1):
        lines.extend([
            f"### Image {i}: {item['filename']}",
            "",
            "| Attribute | Value |",
            "|---|---|",
        ])
        for label, key in (
            ("Purpose", "purpose"),
            ("Type", "type"),
            ("Page role", "page_role"),
            ("Text policy", "text_policy"),
            ("Aspect ratio", "aspect_ratio"),
            ("Image size", "image_size"),
            ("Status", "status"),
        ):
            value = item.get(key)
            if value:
                lines.append(f"| {label} | {value} |")
        if item.get("last_error"):
            lines.append(f"| Last error | {item['last_error']} |")
        lines.extend(["", "**Prompt**:", "", item["prompt"], ""])
        if item.get("alt_text"):
            lines.extend(["**Alt Text**:", f"> {item['alt_text']}", ""])
        lines.extend(["---", ""])

    return "\n".join(lines).rstrip() + "\n"


def render_manifest_md_to_file(path: str | Path, manifest: dict | None = None) -> Path:
    """Render the Markdown sidecar next to the JSON manifest."""
    manifest_path = Path(path)
    if manifest is None:
        manifest = load_manifest(manifest_path)
    md_path = manifest_path.with_suffix(".md")
    md_path.write_text(render_manifest_md(manifest), encoding="utf-8")
    return md_path


def mark_status(
    manifest_path: str | Path,
    filename: str,
    status: str,
    *,
    error: str | None = None,
) -> None:
    """Update one item status by filename and refresh the Markdown sidecar."""
    manifest = load_manifest(manifest_path)
    for item in manifest["items"]:
        if item["filename"] != filename:
            continue
        item["status"] = status
        if error:
            item["last_error"] = error[:500]
        else:
            item.pop("last_error", None)
        save_manifest(manifest_path, manifest)
        render_manifest_md_to_file(manifest_path, manifest)
        return
    raise ValueError(f"{manifest_path}: filename not found in manifest: {filename}")


def check_files(manifest_path: str | Path) -> list[str]:
    """Return Generated items whose expected output file is missing."""
    manifest_path = Path(manifest_path)
    manifest = load_manifest(manifest_path)
    base_dir = manifest_path.parent
    missing: list[str] = []
    for item in manifest["items"]:
        if item["status"] != STATUS_GENERATED:
            continue
        expected = base_dir / item["filename"]
        if not expected.exists():
            missing.append(str(expected))
    return missing


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate and maintain image_prompts.json manifests."
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--validate", metavar="IMAGE_PROMPTS_JSON")
    group.add_argument("--render-md", metavar="IMAGE_PROMPTS_JSON")
    group.add_argument("--check-files", metavar="IMAGE_PROMPTS_JSON")
    group.add_argument("--mark", metavar="IMAGE_PROMPTS_JSON")
    parser.add_argument("--filename", help="Manifest item filename for --mark.")
    parser.add_argument("--status", choices=sorted(VALID_STATUSES), help="New status.")
    parser.add_argument("--error", help="Optional last_error text for --mark.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.validate:
            manifest = load_manifest(args.validate)
            print(f"OK: {args.validate} ({len(manifest['items'])} item(s))")
            return 0

        if args.render_md:
            manifest = load_manifest(args.render_md)
            md_path = render_manifest_md_to_file(args.render_md, manifest)
            print(f"Rendered Markdown sidecar: {md_path}")
            return 0

        if args.check_files:
            missing = check_files(args.check_files)
            if missing:
                print("Missing generated image files:")
                for path in missing:
                    print(f"- {path}")
                return 1
            print("OK: all Generated image files exist.")
            return 0

        if args.mark:
            if not args.filename or not args.status:
                parser.error("--mark requires --filename and --status")
            mark_status(args.mark, args.filename, args.status, error=args.error)
            print(f"Updated {args.filename} -> {args.status}")
            return 0
    except ValueError as exc:
        print(f"Error: {exc}")
        return 1

    parser.error("no action selected")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
