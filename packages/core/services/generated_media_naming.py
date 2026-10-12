"""User-friendly filenames for generated media artifacts."""
from __future__ import annotations

import os
import re
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path

from packages.core.services.workspace_artifacts import (
    WORKSPACE_FOLDER_ROOT,
    WORKSPACE_STORAGE_SEGMENT,
    workspace_artifact_storage_base,
)
from packages.core.services.workspace_layout import (
    TASK_ARTIFACT_SEGMENT, WORKFLOW_ARTIFACT_SEGMENT, WORKSPACE_LEDGER_DIRECTORIES,
)


WORKSPACE_ARTIFACT_ROOT = WORKSPACE_FOLDER_ROOT
_PATH_SEGMENT_MAX_BYTES = 180
_GENERATED_MEDIA_FILENAME_MAX_BYTES = 180


@dataclass(frozen=True)
class WorkspaceArtifactScope:
    """Trusted per-call output ownership, not a permission or an approval."""

    entity_id: str | None
    workspace_id: str | None
    workflow_run_id: str | None = None
    workflow_lineage_root_run_id: str | None = None
    workspace_shared: bool = False


_artifact_scope: ContextVar[WorkspaceArtifactScope | None] = ContextVar("workspace_artifact_owner", default=None)


@contextmanager
def workspace_artifact_scope(scope: WorkspaceArtifactScope | None):
    token = _artifact_scope.set(scope)
    try:
        yield
    finally:
        _artifact_scope.reset(token)


def current_workspace_artifact_scope(
    entity_id: str | None, workspace_id: str | None,
) -> WorkspaceArtifactScope | None:
    scope = _artifact_scope.get()
    if scope is not None and scope.entity_id == entity_id and scope.workspace_id == workspace_id:
        return scope
    return None

def narrator_audio_output_name(output_name: str, voice: str) -> str:
    """Keep an explicitly requested parent while grouping narration by voice.

    Bare names retain the default audio bucket. A supplied parent must not be
    replaced with that bucket: both generation and normalization are authorized
    for the requested directory. Reapplying this mapping is idempotent.
    """
    voice_slug = re.sub(r"[^a-z0-9]+", "-", str(voice or "").strip().lower()).strip("-")
    if not voice_slug:
        raise ValueError("Narrator voice must produce a non-empty storage folder name.")
    normalized = str(output_name or "").replace("\\", "/").strip("/")
    parent, _, filename = normalized.rpartition("/")
    if not parent:
        parent, filename = "audio", normalized
    if parent.rsplit("/", 1)[-1].lower() != voice_slug:
        parent = f"{parent}/{voice_slug}"
    return f"{parent}/{filename.strip()}"


def workspace_task_artifact_dir(
    workspace_base_dir: str | None,
    task_id: str | None,
) -> str:
    """Return the archive directory for one task's output.

    Falls back to the workspace base when there is no task (ad-hoc chat
    generation), so nothing loses its home.
    """
    base = _clean_rel_dir(workspace_base_dir or "")
    if not base:
        return base
    # A task id is one identifier segment. Anything else (a path, traversal,
    # punctuation) is not a task id — fall back to the workspace root rather
    # than inventing a directory tree from it.
    raw = str(task_id or "").strip()
    task_segment = raw if raw.isalnum() else ""
    if not task_segment:
        return base
    return _clean_rel_dir(f"{base}/{TASK_ARTIFACT_SEGMENT}/{task_segment}")


def workspace_execution_artifact_dir(
    workspace_base_dir: str | None,
    *,
    task_id: str | None = None,
    workflow_run_id: str | None = None,
    workflow_lineage_root_run_id: str | None = None,
    entity_root: str | None = None,
) -> str:
    """Select the producing runtime's archive, with stable Workflow retries."""
    base = _clean_rel_dir(workspace_base_dir or "")
    if not base:
        return ""
    run_id = str(workflow_run_id or "").strip()
    lineage = str(workflow_lineage_root_run_id or run_id).strip()
    if run_id:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", run_id) or not re.fullmatch(r"[A-Za-z0-9_-]+", lineage):
            raise ValueError("Workflow artifact identity must be a single identifier")
        current = f"{base}/{WORKFLOW_ARTIFACT_SEGMENT}/{lineage}"
        legacy = f"{base}/runs/{lineage}"
        # Keep already-materialized lineages in place across an upgrade.
        # New runs always select workflow; no folders are created by routing.
        if entity_root and not os.path.isdir(os.path.join(entity_root, current)) and os.path.isdir(os.path.join(entity_root, legacy)):
            return legacy
        return current
    return workspace_task_artifact_dir(base, task_id)


def workspace_storage_root(path: str) -> str:
    """Get the Workspace home from a canonical or legacy scoped path."""
    parts = _clean_rel_dir(path).split("/")
    if parts[:2] == [WORKSPACE_ARTIFACT_ROOT, WORKSPACE_STORAGE_SEGMENT] and len(parts) >= 3:
        return "/".join(parts[:3])
    if len(parts) >= 2 and parts[0] == WORKSPACE_ARTIFACT_ROOT:
        return "/".join(parts[:2])
    return ""


def _is_execution_artifact_dir(path: str) -> bool:
    root = workspace_storage_root(path)
    relative = path.removeprefix(f"{root}/") if root else ""
    parts = relative.split("/")
    return len(parts) >= 2 and parts[0] in {TASK_ARTIFACT_SEGMENT, WORKFLOW_ARTIFACT_SEGMENT, "runs"}


@dataclass(frozen=True)
class GeneratedMediaTarget:
    """Resolved filesystem/document target for a generated media artifact."""

    filename: str
    rel_dir: str
    rel_path: str
    abs_dir: str | None = None
    abs_path: str | None = None


MEDIA_OUTPUT_EXTENSIONS = {
    "image": (".png", ".jpg", ".webp"),
    "audio": (".mp3", ".wav", ".flac", ".opus", ".aac", ".ogg"),
    "video": (".mp4", ".webm"),
}


@dataclass(frozen=True)
class GeneratedMediaOutputPlan:
    """Exact CREATE targets selected before authorization and provider work.

    Providers may return different supported encodings. Each encoding has a
    finite, exact target; saving must select it without naming or deduping again.
    Only relative identities are persisted across approval/worker boundaries.
    """

    kind: str
    artifact_base_dir: str
    targets: tuple[GeneratedMediaTarget, ...]
    requested_name: str
    companion_paths: tuple[str, ...] = ()

    @property
    def all_paths(self) -> tuple[str, ...]:
        return tuple(target.rel_path for target in self.targets) + self.companion_paths

    def target_for_extension(self, ext: str) -> GeneratedMediaTarget:
        for target in self.targets:
            if os.path.splitext(target.filename)[1] == ext:
                return target
        raise ValueError("Generated media format was not prepared for this operation")

    def to_dict(self) -> dict:
        return {
            "version": 2 if self.companion_paths else 1,
            "kind": self.kind,
            "artifact_base_dir": self.artifact_base_dir,
            "paths": [target.rel_path for target in self.targets],
            "requested_name": self.requested_name,
            **({"companion_paths": list(self.companion_paths)} if self.companion_paths else {}),
        }

    @classmethod
    def from_dict(cls, value: dict) -> GeneratedMediaOutputPlan:
        if not isinstance(value, dict) or type(value.get("version")) is not int or value["version"] not in {1, 2}:
            raise ValueError("Invalid generated media output plan")
        kind, base, paths = value.get("kind"), value.get("artifact_base_dir"), value.get("paths")
        if (
            not isinstance(kind, str) or kind not in MEDIA_OUTPUT_EXTENSIONS
            or not isinstance(base, str) or base != _clean_rel_dir(base)
            or not isinstance(paths, list) or not paths
            or len(paths) > len(MEDIA_OUTPUT_EXTENSIONS[kind])
        ):
            raise ValueError("Invalid generated media output plan")
        targets = []
        extensions = set()
        for path in paths:
            if not isinstance(path, str) or not path or path != _clean_rel_dir(path) or (base and not path.startswith(f"{base}/")):
                raise ValueError("Generated media target escaped its prepared scope")
            directory, filename = os.path.split(path)
            ext = os.path.splitext(filename)[1]
            if ext not in MEDIA_OUTPUT_EXTENSIONS[kind] or ext in extensions:
                raise ValueError("Invalid generated media target format")
            extensions.add(ext)
            targets.append(GeneratedMediaTarget(filename, directory, path))
        name = value.get("requested_name")
        if (
            not isinstance(name, str) or name != _clean_rel_dir(name)
        ):
            raise ValueError("Invalid generated media requested target")
        companions = value.get("companion_paths", [])
        if value["version"] == 1:
            if companions:
                raise ValueError("Legacy media plans cannot authorize companions")
        elif (
            kind != "video" or len(targets) != 1
            or not targets[0].rel_path.endswith(".mp4")
            or companions != [video_editor_recipe_path(targets[0].rel_path)]
        ):
            raise ValueError("Invalid generated media companion target")
        return cls(kind, base, tuple(targets), name, tuple(companions))


def video_editor_recipe_path(video_path: str) -> str:
    """The single exact companion identity owned by video composition."""
    return os.path.splitext(video_path)[0] + ".video-edit.json"


def prepare_video_editor_output_pair(
    target: GeneratedMediaTarget, *, entity_root: str,
) -> tuple[GeneratedMediaTarget, str]:
    """Choose a collision-free MP4/recipe pair before either is authorized."""
    stem = os.path.splitext(target.filename)[0]
    index = 1
    while True:
        suffix = "" if index == 1 else f"-{index}"
        fitted = _fit_stem_to_filename_bytes(
            stem, suffix=f"{suffix}.video-edit.json",
            max_bytes=_GENERATED_MEDIA_FILENAME_MAX_BYTES,
        )
        filename = f"{fitted}{suffix}.mp4"
        path = "/".join(part for part in (target.rel_dir, filename) if part)
        companion = video_editor_recipe_path(path)
        if not any(os.path.lexists(os.path.join(entity_root, item)) for item in (path, companion)):
            return GeneratedMediaTarget(
                filename, target.rel_dir, path,
                os.path.join(entity_root, target.rel_dir), os.path.join(entity_root, path),
            ), companion
        index += 1


def build_workspace_artifact_base_dir(
    *,
    workspace_name: str | None = None,
    workspace_id: str | None = None,
    artifact_folder_id: str | None = None,
) -> str:
    """Return the immutable physical folder used for workspace artifacts.

    ``workspace_name`` and ``workspace_id`` remain accepted for call-site
    compatibility, but storage identity is exclusively the DocumentFolder ID.
    """
    del workspace_name, workspace_id
    return workspace_artifact_storage_base(artifact_folder_id)


async def resolve_workspace_artifact_base_dir(
    *,
    entity_id: str | None,
    workspace_id: str | None,
    task_id: str | None = None,
    workflow_run_id: str | None = None,
    workflow_lineage_root_run_id: str | None = None,
) -> str:
    """Resolve the artifact base dir for generated output.

    ``Workspaces/_by_id/<folder id>`` is the Workspace home. Trusted Workflow
    ownership selects its stable lineage archive before Task ownership selects
    ``tasks/<task id>``. Shared files stay at the home. Retries reuse their
    existing archive, including materialized legacy Workflow directories.
    """
    workspace_id = (workspace_id or "").strip()
    if not workspace_id:
        return ""
    scope = current_workspace_artifact_scope(entity_id, workspace_id)
    if scope is not None:
        if scope.workspace_shared:
            task_id = workflow_run_id = workflow_lineage_root_run_id = None
        else:
            workflow_run_id = workflow_run_id or scope.workflow_run_id
            workflow_lineage_root_run_id = workflow_lineage_root_run_id or scope.workflow_lineage_root_run_id

    from sqlalchemy import select

    from packages.core.database import async_session
    from packages.core.models.workspace import Workspace
    from packages.core.services.workspace_artifacts import ensure_workspace_artifact_folder

    async with async_session() as db:
        stmt = select(Workspace).where(
            Workspace.id == workspace_id,
            Workspace.deleted_at.is_(None),
        )
        if entity_id:
            stmt = stmt.where(Workspace.entity_id == entity_id)
        workspace = (await db.execute(stmt.with_for_update().limit(1))).scalar_one_or_none()
        if workspace is None:
            raise RuntimeError(f"Workspace artifact scope could not resolve workspace {workspace_id}")
        folder = await ensure_workspace_artifact_folder(db, workspace)
        await db.commit()
        base = build_workspace_artifact_base_dir(artifact_folder_id=folder.id)
        from packages.core.services.entity_fs import get_entity_root
        return workspace_execution_artifact_dir(
            base, task_id=task_id, workflow_run_id=workflow_run_id,
            workflow_lineage_root_run_id=workflow_lineage_root_run_id,
            entity_root=get_entity_root(workspace.entity_id),
        )


def workspace_artifact_default_dir(workspace_base_dir: str | None, default_dir: str) -> str:
    """Only execution archives receive automatic type buckets."""
    default_dir = _clean_rel_dir(default_dir)
    workspace_base_dir = _clean_rel_dir(workspace_base_dir or "")
    if not _is_execution_artifact_dir(workspace_base_dir):
        return workspace_base_dir
    return _clean_rel_dir("/".join(part for part in (workspace_base_dir, default_dir) if part))


def execution_output_directory(path: str, artifact_base: str) -> str:
    """Translate shipped production defaults without rewriting user paths."""
    normalized = _clean_rel_dir(path)
    phases = {"runs", "reviews", "sources", "deliveries"}
    if normalized in {f"production/{phase}" for phase in phases}:
        normalized = normalized.split("/", 1)[1]
    if normalized in phases:
        if normalized == "runs":
            normalized = "artifacts"
        root = workspace_storage_root(artifact_base)
        if root and artifact_base.startswith(f"{root}/tasks/") and normalized != "artifacts":
            normalized = f"artifacts/{normalized}"
    return normalized


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
    ``Workspaces/_by_id/<folder id>/images/hero.png``.
    """
    rel = _clean_rel_dir(path or "")
    workspace_base_dir = _clean_rel_dir(workspace_base_dir or "")
    if not rel or not workspace_base_dir:
        return rel

    if rel == workspace_base_dir or rel.startswith(f"{workspace_base_dir}/"):
        return rel
    home = workspace_storage_root(workspace_base_dir)
    if home and (rel == home or rel.startswith(f"{home}/")):
        # A canonical reference or an explicit Workspace destination already
        # identifies its home. Do not nest a sibling source under this Task.
        return rel
    if home and "/runs/" in workspace_base_dir:
        lineage = workspace_base_dir.removeprefix(f"{home}/runs/")
        if rel == f"workflow/{lineage}" or rel.startswith(f"workflow/{lineage}/"):
            rel = "runs/" + rel.removeprefix("workflow/")
        if rel == f"runs/{lineage}" or rel.startswith(f"runs/{lineage}/"):
            return f"{home}/{rel}"
    if home and rel.split("/", 1)[0] in WORKSPACE_LEDGER_DIRECTORIES | {TASK_ARTIFACT_SEGMENT, WORKFLOW_ARTIFACT_SEGMENT}:
        return f"{home}/{rel}"
    if rel == WORKSPACE_ARTIFACT_ROOT:
        return workspace_base_dir
    if rel.startswith(f"{WORKSPACE_ARTIFACT_ROOT}/"):
        legacy_parts = rel.split("/")
        if len(legacy_parts) >= 3 and legacy_parts[1] == WORKSPACE_STORAGE_SEGMENT:
            rel = "/".join(legacy_parts[3:])
        else:
            rel = "/".join(legacy_parts[2:])
        if not rel:
            return workspace_base_dir
    if preserve_leaf_default and "/" not in rel:
        return rel

    parts = [workspace_base_dir]
    if default_subdir and "/" not in rel and _is_execution_artifact_dir(workspace_base_dir):
        parts.append(default_subdir)
    parts.append(rel)
    return _clean_rel_dir("/".join(parts))


def collision_safe_artifact_path(entity_root: str, rel_path: str) -> str:
    """Return the exact new path a generated artifact should authorize.

    Callers must run their mutation guard against this returned path and then
    persist to the same path. Deterministic numeric suffixes keep an approval
    retry bound to the same target while that target remains available.
    """
    normalized = _clean_rel_dir(rel_path)
    target = os.path.join(entity_root, normalized)
    if not os.path.exists(target):
        return normalized

    rel_dir, filename = os.path.split(normalized)
    base, ext = os.path.splitext(filename)
    suffix = 1
    while True:
        candidate_name = f"{base}_{suffix}{ext}"
        candidate = "/".join(part for part in (rel_dir, candidate_name) if part)
        if not os.path.exists(os.path.join(entity_root, candidate)):
            return candidate
        suffix += 1


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
