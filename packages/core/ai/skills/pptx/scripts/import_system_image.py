#!/usr/bin/env python3
"""Import a system-generated image from the Manor workspace mount.

The Manor image tool stores generated files in the entity filesystem and
returns an ``image_url`` such as ``/api/v1/fs/<entity>/<rel_path>`` plus, when
available, ``fs_path``. The pptx sandbox sees that entity filesystem mounted
read-only at ``/workspace``. This script copies the generated binary into the
current pptx project so SVG pages can reference ``images/<filename>`` directly.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path


def workspace_relative_from_reference(reference: str) -> str:
    """Return entity-filesystem relative path from an fs_path or /api/v1/fs URL."""
    text = reference.strip()
    if not text:
        raise ValueError("workspace path or image URL is empty")

    if text.startswith("/workspace/"):
        return text[len("/workspace/") :]

    marker = "/api/v1/fs/"
    if text.startswith(marker):
        rest = text[len(marker) :]
        parts = rest.split("/", 1)
        if len(parts) != 2 or not parts[1]:
            raise ValueError(f"Cannot extract workspace path from image URL: {text}")
        return parts[1]

    return text.lstrip("/")


def safe_join_workspace(workspace_root: Path, rel_path: str) -> Path:
    """Resolve a path under /workspace and reject traversal outside it."""
    candidate = (workspace_root / rel_path).resolve()
    root = workspace_root.resolve()
    if candidate != root and root not in candidate.parents:
        raise ValueError(f"Workspace path escapes {root}: {rel_path}")
    return candidate


def safe_destination(project_path: Path, filename: str) -> Path:
    """Build a destination under project/images from a manifest filename."""
    name_path = Path(filename)
    if name_path.name != filename or filename in {"", ".", ".."}:
        raise ValueError(
            "filename must be a simple file name from image_prompts.json, "
            f"got: {filename!r}"
        )
    return project_path / "images" / filename


def _nonempty_file(path: Path) -> bool:
    """Return True when `path` is a readable, non-empty regular file."""
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def search_workspace_by_name(workspace_root: Path, filename: str) -> Path | None:
    """Find a non-empty file named `filename` anywhere under the workspace root.

    Generated images land in the entity filesystem under a path the caller may
    not predict exactly (the prefix the tool returns can differ from where the
    bytes land on the mount). When the exact rel-path misses, a basename search
    under the read-only mount recovers it without trusting the caller's prefix.
    """
    if not filename:
        return None
    try:
        root = workspace_root.resolve()
    except OSError:
        return None
    if not root.is_dir():
        return None
    matches = [p for p in root.rglob(filename) if _nonempty_file(p)]
    if not matches:
        return None
    # Prefer the most recently modified match — the freshly generated image.
    return max(matches, key=lambda p: p.stat().st_mtime)


def resolve_source(
    workspace_root: Path,
    rel_path: str,
    *,
    retries: int = 6,
    retry_delay: float = 2.0,
) -> Path:
    """Locate the generated image on the mount, tolerating propagation lag.

    The entity filesystem is bind-mounted read-only at the workspace root, but a
    just-written image can take a few seconds to appear inside the sandbox
    (JuiceFS/FUSE propagation). Poll the exact path first, then fall back to a
    basename search, retrying with a linear backoff before giving up.
    """
    exact = safe_join_workspace(workspace_root, rel_path)
    basename = Path(rel_path).name
    attempts = max(1, retries)
    last_seen: list[str] = []
    for attempt in range(attempts):
        if _nonempty_file(exact):
            return exact
        found = search_workspace_by_name(workspace_root, basename)
        if found is not None:
            return found
        last_seen = [str(exact)]
        if attempt < attempts - 1:
            time.sleep(retry_delay)
    raise FileNotFoundError(
        "Generated image did not appear on the workspace mount after "
        f"{attempts} attempt(s): looked for {last_seen[0] if last_seen else exact} "
        f"and any '{basename}' under {workspace_root}. The image may still be "
        "syncing, or this sandbox has no entity-filesystem mount. Do not retry "
        "blindly or switch tasks — re-run this import once more, and if it still "
        "fails, mark the image Needs-Manual and stop the page loop."
    )


def copy_binary(src: Path, dst: Path) -> None:
    """Copy a non-empty binary file atomically."""
    if not src.is_file():
        raise FileNotFoundError(f"Workspace image not found: {src}")
    if src.stat().st_size <= 0:
        raise ValueError(f"Workspace image is empty: {src}")

    dst.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=dst.name + ".", suffix=".tmp", dir=dst.parent)
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        shutil.copyfile(src, tmp_path)
        if tmp_path.stat().st_size <= 0:
            raise ValueError(f"Copied image is empty: {tmp_path}")
        os.replace(tmp_path, dst)
    except Exception:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Copy a system-generated image from /workspace into a pptx project."
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--workspace-path",
        help="Relative entity filesystem path, absolute /workspace path, or fs_path.",
    )
    source.add_argument(
        "--image-url",
        help="Image URL returned by generate_file, e.g. /api/v1/fs/<entity>/<path>.",
    )
    parser.add_argument("--project", required=True, help="PPT project path.")
    parser.add_argument(
        "--filename",
        required=True,
        help="Destination filename from image_prompts.json.",
    )
    parser.add_argument(
        "--workspace-root",
        default="/workspace",
        help="Sandbox workspace mount root (default: /workspace).",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=6,
        help="How many times to poll the mount for the image (default: 6).",
    )
    parser.add_argument(
        "--retry-delay",
        type=float,
        default=2.0,
        help="Seconds to wait between mount polls (default: 2.0).",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        reference = args.workspace_path or args.image_url
        rel_path = workspace_relative_from_reference(reference)
        workspace_root = Path(args.workspace_root)
        src = resolve_source(
            workspace_root,
            rel_path,
            retries=args.retries,
            retry_delay=args.retry_delay,
        )
        project_path = Path(args.project).resolve()
        dst = safe_destination(project_path, args.filename)
        copy_binary(src, dst)
        print(dst)
        return 0
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
