"""Knowledge visibility rules for filesystem-backed content."""
from __future__ import annotations

import os
import re

from packages.core.services.entity_fs import SYSTEM_DIRS, SYSTEM_FILES

# Hidden/internal path prefixes (relative to entity root).
HIDDEN_PREFIXES: tuple[str, ...] = (
    ".ai/",
    ".cache/",
    "tmp/",
    "temp/",
    "avatars/",
    # Runtime input / task storage. These are not final user-facing outputs.
    "uploads/",
    "tasks/",
    "$sandbox_output/",
    "$sandbox-output/",
    "$SANDBOX_OUTPUT_DIR/",
    "sandbox_output/",
    "sandbox-output/",
    "__pycache__/",
    # PPTX generation/editing work directories. The final .pptx is the
    # Knowledge artifact; intermediate SVG slide sources should not surface as
    # top-level Knowledge files after filesystem reconciliation.
    "svg_output/",
    "svg_final/",
    "svg_output_flattext/",
    "svg_final_flattext/",
    "svg-flat/",
)

# Final media artifacts may live under these physical filesystem folders.
# The files can be visible as Documents, but the storage folders themselves
# should not appear in the user-facing Knowledge tree unless we add an explicit
# "save to folder" action that sets Document.folder_id.
STORAGE_ONLY_PREFIXES: tuple[str, ...] = (
    "images/",
    "videos/",
    "audio/",
)

PPTX_INTERMEDIATE_SVG_RE = re.compile(
    r"^(?:slide|master|layout)_\d{1,3}(?:[_.-].*)?\.svg$",
    re.IGNORECASE,
)


def normalize_rel_path(path: str) -> str:
    """Normalize a relative path to a forward-slash form without leading slash."""
    p = (path or "").replace("\\", "/").strip()
    p = p.lstrip("/")
    p = os.path.normpath(p).replace("\\", "/")
    return "" if p == "." else p


def is_user_visible_path(path: str) -> bool:
    """Return True when a path should be visible in user-facing knowledge views."""
    rel = normalize_rel_path(path)
    if not rel:
        return False
    parts = [part for part in rel.split("/") if part]
    if not parts:
        return False
    if any(part.startswith(".") for part in parts):
        return False
    hidden_dir_names = {prefix.rstrip("/") for prefix in HIDDEN_PREFIXES}
    if any(part in hidden_dir_names for part in parts):
        return False
    if any(part in SYSTEM_FILES or part in SYSTEM_DIRS for part in parts):
        return False
    if PPTX_INTERMEDIATE_SVG_RE.match(parts[-1]):
        return False
    for prefix in HIDDEN_PREFIXES:
        if rel == prefix.rstrip("/") or rel.startswith(prefix):
            return False
    return True


def is_user_visible_folder_path(path: str) -> bool:
    """Return True when a filesystem directory should be projected as a folder."""
    rel = normalize_rel_path(path)
    if not is_user_visible_path(rel):
        return False
    for prefix in STORAGE_ONLY_PREFIXES:
        if rel == prefix.rstrip("/") or rel.startswith(prefix):
            return False
    return True


def is_storage_only_path(path: str) -> bool:
    """Return True for final-artifact storage paths whose folder is hidden."""
    rel = normalize_rel_path(path)
    return any(rel == prefix.rstrip("/") or rel.startswith(prefix) for prefix in STORAGE_ONLY_PREFIXES)
