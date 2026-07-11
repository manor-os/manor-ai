"""User-friendly filenames for generated media artifacts."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select


WORKSPACE_ARTIFACT_ROOT = "Workspaces"
_PATH_SEGMENT_MAX_BYTES = 180
_GENERATED_MEDIA_FILENAME_MAX_BYTES = 180


@dataclass(frozen=True)
class GeneratedMediaTarget:
    """Resolved filesystem/document target for a generated media artifact."""

    filename: str
    rel_dir: str
    rel_path: str
    abs_dir: str | None = None
    abs_path: str | None = None


def build_workspace_artifact_base_dir(
    *,
    workspace_name: str | None = None,
    workspace_id: str | None = None,
) -> str:
    """Return the visible Knowledge folder used for a workspace's artifacts."""
    label = _clean_path_segment(workspace_name or "") or _clean_path_segment(_workspace_id_label(workspace_id))
    return _clean_rel_dir(f"{WORKSPACE_ARTIFACT_ROOT}/{label or 'Workspace'}")


async def resolve_workspace_artifact_base_dir(
    *,
    entity_id: str | None,
    workspace_id: str | None,
) -> str:
    """Resolve ``Workspaces/<workspace name>`` for generated artifacts.

    The helper is intentionally generic: it scopes any user-visible generated
    artifact to its Workspace without making assumptions about product/domain.
    """
    workspace_id = (workspace_id or "").strip()
    if not workspace_id:
        return ""

    workspace_name = ""
    try:
        from packages.core.database import async_session
        from packages.core.models.workspace import Workspace

        async with async_session() as db:
            stmt = select(Workspace).where(
                Workspace.id == workspace_id,
                Workspace.deleted_at.is_(None),
            )
            if entity_id:
                stmt = stmt.where(Workspace.entity_id == entity_id)
            workspace = (await db.execute(stmt.limit(1))).scalar_one_or_none()
            workspace_name = str(getattr(workspace, "name", "") or "")
    except Exception:
        workspace_name = ""

    return build_workspace_artifact_base_dir(
        workspace_name=workspace_name,
        workspace_id=workspace_id,
    )


def workspace_artifact_default_dir(workspace_base_dir: str | None, default_dir: str) -> str:
    """Nest a default artifact folder under the workspace when available."""
    default_dir = _clean_rel_dir(default_dir)
    workspace_base_dir = _clean_rel_dir(workspace_base_dir or "")
    if not workspace_base_dir:
        return default_dir
    return _clean_rel_dir("/".join(part for part in (workspace_base_dir, default_dir) if part))


def scope_workspace_artifact_path(
    path: str | None,
    workspace_base_dir: str | None,
    *,
    default_subdir: str | None = None,
    preserve_leaf_default: bool = False,
) -> str:
    """Scope a user-requested artifact path under its Workspace folder.

    ``preserve_leaf_default`` lets media filenames such as ``hero.png`` keep
    using the caller's default media directory, e.g.
    ``Workspaces/<workspace>/images/hero.png``.
    """
    rel = _clean_rel_dir(path or "")
    workspace_base_dir = _clean_rel_dir(workspace_base_dir or "")
    if not rel or not workspace_base_dir:
        return rel

    if rel == workspace_base_dir or rel.startswith(f"{workspace_base_dir}/"):
        return rel
    if rel == WORKSPACE_ARTIFACT_ROOT or rel.startswith(f"{WORKSPACE_ARTIFACT_ROOT}/"):
        return rel
    if preserve_leaf_default and "/" not in rel:
        return rel

    parts = [workspace_base_dir]
    if default_subdir and "/" not in rel:
        parts.append(default_subdir)
    parts.append(rel)
    return _clean_rel_dir("/".join(parts))


def build_generated_media_filename(
    *,
    prompt: str,
    ext: str,
    desired_name: str | None = None,
    fallback: str = "generated-media",
    unique_dir: str | None = None,
) -> str:
    """Build a safe, readable filename and avoid collisions in ``unique_dir``.

    Explicit names are treated as user intent and are not given hash suffixes.
    Prompt-derived names get a short content suffix so repeated concepts remain
    recognizable without falling back to opaque ``gen_*`` / ``vid_*`` names.
    """
    ext = _normalize_ext(ext)
    explicit = _clean_label(desired_name or "")
    stem = explicit or _slugify_label(prompt, fallback=fallback)
    if not explicit:
        digest = _short_digest(prompt)
        stem = _fit_stem_to_filename_bytes(
            stem,
            suffix=f"-{digest}{ext}",
            max_bytes=_GENERATED_MEDIA_FILENAME_MAX_BYTES,
        )
        stem = f"{stem}-{digest}"
    else:
        stem = _fit_stem_to_filename_bytes(
            stem,
            suffix=ext,
            max_bytes=_GENERATED_MEDIA_FILENAME_MAX_BYTES,
        )
    filename = f"{stem}{ext}"
    if unique_dir:
        filename = _dedupe_filename(unique_dir, filename)
    return filename


def build_generated_media_target(
    *,
    prompt: str,
    ext: str,
    default_dir: str,
    desired_name: str | None = None,
    fallback: str = "generated-media",
    entity_root: str | None = None,
) -> GeneratedMediaTarget:
    """Build a safe media target while preserving user-requested folders.

    ``desired_name`` may include a relative folder path such as
    ``猫咪打工人动漫/videos/EP03.mp4``.  The folder portion is kept for the
    filesystem and Knowledge projection; only unsafe/traversal path segments
    are discarded.
    """
    rel_dir, leaf_name = _split_desired_target(desired_name or "", default_dir=default_dir)
    abs_dir = os.path.join(entity_root, rel_dir) if entity_root and rel_dir else entity_root
    filename = build_generated_media_filename(
        prompt=prompt,
        desired_name=leaf_name,
        ext=ext,
        fallback=fallback,
        unique_dir=abs_dir,
    )
    rel_path = "/".join(part for part in (rel_dir, filename) if part)
    abs_path = os.path.join(abs_dir, filename) if abs_dir else None
    return GeneratedMediaTarget(
        filename=filename,
        rel_dir=rel_dir,
        rel_path=rel_path,
        abs_dir=abs_dir,
        abs_path=abs_path,
    )


def _split_desired_target(desired_name: str, *, default_dir: str) -> tuple[str, str]:
    raw = (desired_name or "").strip()
    fallback_dir = _clean_rel_dir(default_dir)
    if not raw:
        return fallback_dir, ""

    normalized = raw.replace("\\", "/").strip().lstrip("/")
    trailing_slash = normalized.endswith("/")
    parts = [_clean_path_segment(part) for part in normalized.split("/")]
    parts = [part for part in parts if part]
    if not parts:
        return fallback_dir, ""
    if trailing_slash:
        return _clean_rel_dir("/".join(parts)) or fallback_dir, ""
    if len(parts) == 1:
        return fallback_dir, parts[0]
    rel_dir = _clean_rel_dir("/".join(parts[:-1])) or fallback_dir
    return rel_dir, parts[-1]


def _clean_rel_dir(value: str) -> str:
    parts = [_clean_path_segment(part) for part in (value or "").replace("\\", "/").split("/")]
    return "/".join(part for part in parts if part)


def _clean_path_segment(value: str) -> str:
    segment = re.sub(r"[\x00-\x1f\x7f]+", "-", (value or "").strip())
    segment = segment.strip("/\\")
    if not segment or segment in {".", ".."}:
        return ""
    segment = segment.lstrip(".").strip()
    segment = re.sub(r'[<>:"|?*]+', "-", segment)
    segment = re.sub(r"\s+", " ", segment).strip()
    if segment in {"", ".", ".."}:
        return ""
    return _truncate_utf8(segment, _PATH_SEGMENT_MAX_BYTES).rstrip(" .")


def _clean_label(value: str) -> str:
    raw = os.path.basename(value.replace("\\", "/")).strip()
    if not raw or raw in {".", ".."}:
        return ""
    stem = Path(raw).stem if Path(raw).suffix else raw
    return _slugify_label(stem, fallback="")


def _slugify_label(value: str, *, fallback: str, max_chars: int = 72) -> str:
    cleaned = re.sub(r"['\"]+", "", (value or "").strip().lower())
    cleaned = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "-", cleaned)
    cleaned = re.sub(r"-+", "-", cleaned).strip("-._")
    if len(cleaned) > max_chars:
        cleaned = cleaned[:max_chars].rstrip("-._")
    return cleaned or fallback


def _normalize_ext(ext: str) -> str:
    ext = (ext or "").strip().lower()
    if not ext:
        return ""
    if not ext.startswith("."):
        ext = f".{ext}"
    ext = re.sub(r"[^.a-z0-9]+", "", ext)
    return ext or ".bin"


def _truncate_utf8(value: str, max_bytes: int) -> str:
    data = (value or "").encode("utf-8")
    if len(data) <= max_bytes:
        return value or ""
    return data[:max_bytes].decode("utf-8", errors="ignore")


def _fit_stem_to_filename_bytes(stem: str, *, suffix: str, max_bytes: int) -> str:
    suffix_bytes = len((suffix or "").encode("utf-8"))
    budget = max(1, max_bytes - suffix_bytes)
    fitted = _truncate_utf8(stem or "", budget).rstrip("-._ ")
    if fitted:
        return fitted
    return _truncate_utf8("generated-media", budget).rstrip("-._ ") or "file"


def _short_digest(value: str) -> str:
    import hashlib

    return hashlib.sha256((value or "").encode("utf-8")).hexdigest()[:8]


def _dedupe_filename(directory: str, filename: str) -> str:
    stem, ext = os.path.splitext(filename)
    candidate = filename
    index = 2
    while os.path.exists(os.path.join(directory, candidate)):
        suffix = f"-{index}{ext}"
        fitted_stem = _fit_stem_to_filename_bytes(
            stem,
            suffix=suffix,
            max_bytes=_GENERATED_MEDIA_FILENAME_MAX_BYTES,
        )
        candidate = f"{fitted_stem}{suffix}"
        index += 1
    return candidate


def _workspace_id_label(workspace_id: str | None) -> str:
    workspace_id = (workspace_id or "").strip()
    if not workspace_id:
        return ""
    return f"workspace-{workspace_id[-8:]}"
