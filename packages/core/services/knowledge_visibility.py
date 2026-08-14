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
    # Runtime INPUT storage: reference frames and other material handed to a
    # provider. Genuinely not user-facing.
    "uploads/",
    # Entity-ROOT task scratch. Matched as a prefix only (see the loop at the
    # end of is_user_visible_path) — never as a mid-path segment, or the
    # canonical workspace artifact path
    # Workspaces/_by_id/<folder>/tasks/<task>/videos/<file> would be caught by
    # it, which is exactly what cost a produced 16.5 MB MP4 its Document row.
    # It also bounds what knowledge URL signing will hand to browser tools, so
    # the root prefix has to keep blocking.
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

# Names that mean "internal" wherever they appear, at any depth: build caches,
# scratch dirs, sandbox spill.
#
# The rest of HIDDEN_PREFIXES is position-dependent — ``tasks/`` and
# ``uploads/`` name entity-ROOT storage areas, and matching those names at any
# depth is what made every workspace task artifact invisible: the canonical
# artifact path is
# ``Workspaces/_by_id/<workspace>/tasks/<task>/videos/<file>.mp4``, whose
# ``tasks`` segment collided with the root-level rule. Knowledge sync refused
# to register the file, so a produced MP4 existed on disk with no Document row
# — and the chat's file card, finding nothing to open, dropped the user on an
# empty Knowledge page. STORAGE_ONLY_PREFIXES below says these paths are meant
# to sync (they are projected through Document.folder_id); only their physical
# folders stay out of the tree.
ANYWHERE_HIDDEN_DIR_NAMES: frozenset[str] = frozenset({
    ".ai",
    ".cache",
    "tmp",
    "temp",
    "__pycache__",
    "$sandbox_output",
    "$sandbox-output",
    "$SANDBOX_OUTPUT_DIR",
    "sandbox_output",
    "sandbox-output",
    "svg_output",
    "svg_final",
    "svg_output_flattext",
    "svg_final_flattext",
    "svg-flat",
})

# Final media artifacts may live under these physical filesystem folders.
# The files can be visible as Documents, but the storage folders themselves
# should not appear in the user-facing Knowledge tree unless we add an explicit
# "save to folder" action that sets Document.folder_id.
STORAGE_ONLY_PREFIXES: tuple[str, ...] = (
    "images/",
    "videos/",
    "audio/",
    # Physical workspace artifacts are projected through Document.folder_id
    # into the mutable user-facing Workspaces/<name> tree.
    "Workspaces/_by_id/",
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
    if any(part in ANYWHERE_HIDDEN_DIR_NAMES for part in parts):
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
