"""Execution-scoped proof that one exact runtime tool call was authorized."""

from __future__ import annotations

from collections.abc import Iterable
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
import hashlib
import json
import posixpath
from typing import TYPE_CHECKING

from packages.core.contracts.audio_generation import (
    AudioGenerationPurpose,
    GenerateFileKind,
)
from packages.core.services.runtime_authorization.domain import (
    RuntimeAuthorizationAccess,
)
from packages.core.services.workspace_layout import WorkspaceArtifactDir
from packages.core.services.generated_media_naming import (
    GeneratedMediaOutputPlan, workspace_artifact_default_dir,
)

if TYPE_CHECKING:
    from packages.core.ai.runtime.approvals import (
        RuntimeApprovalDecision,
        RuntimeApprovalRequest,
    )


def _scope(value: str | None) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


_RUNTIME_INJECTED_SCOPE_KEYS = frozenset({
    "workspace_id",
    "conversation_id",
    "task_id",
})
_RUNTIME_EPHEMERAL_ARGUMENT_KEYS = frozenset({
    "approval_token",
    "active_user_message",
})


@dataclass(frozen=True)
class TikTokPublishConsent:
    """Frozen operator-reviewed input, issued only after a durable grant is spent."""

    approval_id: str
    evidence_json: str
    fields_json: str

    @property
    def evidence(self) -> dict:
        return json.loads(self.evidence_json)

    @property
    def fields(self) -> dict:
        return json.loads(self.fields_json)


_tiktok_consent_capture: ContextVar[list[TikTokPublishConsent] | None] = ContextVar(
    "runtime_tiktok_consent_capture", default=None,
)


@contextmanager
def runtime_capture_tiktok_publish_consent():
    """Transport gate output within one preparation, never through model arguments."""
    captured: list[TikTokPublishConsent] = []
    token = _tiktok_consent_capture.set(captured)
    try:
        yield captured
    finally:
        _tiktok_consent_capture.reset(token)


def runtime_record_tiktok_publish_consent(consent: TikTokPublishConsent) -> None:
    captured = _tiktok_consent_capture.get()
    if captured is not None:
        captured.append(consent)


@dataclass(frozen=True)
class ChromePublicationBinding:
    connection_id: str
    credential_revision: str
    connection_epoch: str
    approval_id: str | None = None
    consumed_at: datetime | None = None


_chrome_publication_capture: ContextVar[list[ChromePublicationBinding] | None] = ContextVar(
    "runtime_chrome_publication_capture", default=None,
)


@contextmanager
def runtime_capture_chrome_publication_binding():
    captured: list[ChromePublicationBinding] = []
    token = _chrome_publication_capture.set(captured)
    try:
        yield captured
    finally:
        _chrome_publication_capture.reset(token)


def runtime_record_chrome_publication_binding(binding: ChromePublicationBinding) -> None:
    captured = _chrome_publication_capture.get()
    if captured is not None:
        captured.append(binding)


def _authorization_arguments_payload(value: object) -> object:
    if isinstance(value, dict):
        return {
            str(key): _authorization_arguments_payload(item)
            for key, item in value.items()
            if not (
                str(key).startswith("_")
                and str(key).endswith("_from_context")
            )
        }
    if isinstance(value, list):
        return [_authorization_arguments_payload(item) for item in value]
    return value


def runtime_authorization_arguments_hash(
    arguments: dict[str, object],
    *,
    public_argument_keys: frozenset[str],
) -> str:
    """Hash executed public arguments while excluding injected Runtime scope."""

    payload = {
        str(key): _authorization_arguments_payload(value)
        for key, value in arguments.items()
        if str(key) not in _RUNTIME_EPHEMERAL_ARGUMENT_KEYS
        and not (
            str(key) in _RUNTIME_INJECTED_SCOPE_KEYS
            and str(key) not in public_argument_keys
        )
        and not (
            str(key).startswith("_")
            and str(key).endswith("_from_context")
        )
    }
    encoded = json.dumps(
        payload,
        sort_keys=True,
        ensure_ascii=False,
        default=str,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _file_resource_id(value: str | None) -> str | None:
    raw = str(value or "").strip().replace("\\", "/").strip("/")
    if not raw:
        return None
    parts = [part for part in raw.split("/") if part and part != "."]
    if not parts or any(part == ".." for part in parts):
        return None
    return "/".join(parts)


def workspace_file_resource_id(value: str | None) -> str | None:
    """Canonical relative resource id, or None for empty/traversing paths."""

    return _file_resource_id(value)


class WorkspaceFileMutationKind(str, Enum):
    CREATE = "create"
    MODIFY = "modify"
    WRITE = "write"
    DELETE = "delete"


class WorkspaceFileResourceMatchKind(str, Enum):
    EXACT = "exact"
    ENTITY_EXACT = "entity_exact"
    TREE = "tree"
    COLLISION_SAFE_FILE = "collision_safe_file"


class WorkspaceFileOutputArgumentKind(str, Enum):
    FILE = "file"
    DIRECTORY = "directory"


class WorkspaceFileScopeBinding(str, Enum):
    UNBOUND = "unbound"
    EXACT = "exact"
    UNRESOLVED = "unresolved"


def _workspace_file_mutation_kind(
    action_key: str | None,
) -> WorkspaceFileMutationKind | None:
    key = str(action_key or "").strip()
    return {
        "workspace.file.create": WorkspaceFileMutationKind.CREATE,
        "workspace.file.modify": WorkspaceFileMutationKind.MODIFY,
        "workspace.file.write": WorkspaceFileMutationKind.WRITE,
        "workspace.file.delete": WorkspaceFileMutationKind.DELETE,
    }.get(key)


def _workspace_logical_file_path(
    path: str,
    task_id: str | None,
    artifact_base_dir: str | None,
) -> str | None:
    normalized = _file_resource_id(path)
    if normalized is None:
        return None
    normalized_base = _file_resource_id(artifact_base_dir)
    if normalized_base is not None:
        if normalized == normalized_base:
            return None
        prefix = f"{normalized_base}/"
        if not normalized.startswith(prefix):
            return None
        return normalized[len(prefix):] or None
    parts = normalized.split("/")
    normalized_task_id = _scope(task_id)
    if normalized_task_id:
        for index in range(len(parts) - 1):
            if parts[index : index + 2] == ["tasks", normalized_task_id]:
                tail = parts[index + 2 :]
                return "/".join(tail) or None
    if len(parts) >= 4 and parts[:2] == ["Workspaces", "_by_id"]:
        return "/".join(parts[3:]) or None
    return normalized


def _file_resource_matches(
    *,
    approved_resource: "WorkspaceFileAuthorizationResource",
    actual_path: str,
    task_id: str | None,
    artifact_base_dir: str | None,
) -> bool:
    approved = _file_resource_id(approved_resource.resource_id)
    if approved_resource.match_kind is WorkspaceFileResourceMatchKind.ENTITY_EXACT:
        return approved is not None and approved == _file_resource_id(actual_path)
    normalized_base = _file_resource_id(artifact_base_dir)
    if approved is not None and normalized_base is not None:
        if approved == normalized_base:
            approved = None
        elif approved.startswith(f"{normalized_base}/"):
            approved = approved[len(normalized_base) + 1 :] or None
    logical_path = _workspace_logical_file_path(
        actual_path,
        task_id,
        artifact_base_dir,
    )
    if approved is None or logical_path is None:
        return False
    if logical_path == approved:
        return True
    if approved_resource.match_kind is WorkspaceFileResourceMatchKind.TREE:
        return logical_path.startswith(f"{approved}/")
    if (
        approved_resource.match_kind
        is WorkspaceFileResourceMatchKind.COLLISION_SAFE_FILE
    ):
        approved_dir, approved_name = posixpath.split(approved)
        actual_dir, actual_name = posixpath.split(logical_path)
        approved_stem, approved_ext = posixpath.splitext(approved_name)
        actual_stem, actual_ext = posixpath.splitext(actual_name)
        suffix = actual_stem[len(approved_stem) + 1 :]
        deterministic_suffix = (
            suffix.isdecimal()
            and not suffix.startswith("0")
        )
        legacy_ulid_suffix = (
            len(suffix) == 26
            and all(
                character in "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
                for character in suffix
            )
        )
        return (
            actual_dir == approved_dir
            and actual_ext == approved_ext
            and actual_stem.startswith(f"{approved_stem}_")
            and (deterministic_suffix or legacy_ulid_suffix)
        )
    return False


def _default_artifact_resource_id(resource_id: str, directory: str) -> str:
    return resource_id if "/" in resource_id else f"{directory}/{resource_id}"


@dataclass(frozen=True)
class WorkspaceFileAuthorizationResource:
    resource_id: str
    match_kind: WorkspaceFileResourceMatchKind


@dataclass(frozen=True)
class GenerateFileDestination:
    """Resolved output of one ``generate_file`` call, before Workspace scoping."""

    kind: GenerateFileKind
    name: str | None
    directory: str | None
    match_kind: WorkspaceFileResourceMatchKind


@dataclass(frozen=True)
class WorkspaceFileOutputResourceSpec:
    argument_name: str
    kind: WorkspaceFileOutputArgumentKind
    default_directory: str = ""
    fallback_path: str = ""
    media_kind: str | None = None
    media_extensions: tuple[str, ...] = ()
    video_edit_recipe: bool = False


class WorkspaceFileAuthorizationResourceFactory:
    """Project public tool arguments to canonical logical artifact targets."""

    _GENERATE_FILE_DIRECTORIES = {
        GenerateFileKind.DIAGRAM: WorkspaceArtifactDir.DOCUMENTS.value,
        GenerateFileKind.CODE: WorkspaceArtifactDir.CODE.value,
        GenerateFileKind.DOCUMENT: WorkspaceArtifactDir.DOCUMENTS.value,
        GenerateFileKind.WORD_DOCUMENT: WorkspaceArtifactDir.DOCUMENTS.value,
        GenerateFileKind.PDF: WorkspaceArtifactDir.DOCUMENTS.value,
        GenerateFileKind.PRESENTATION: WorkspaceArtifactDir.PRESENTATIONS.value,
        GenerateFileKind.SPREADSHEET: WorkspaceArtifactDir.SPREADSHEETS.value,
        GenerateFileKind.IMAGE: WorkspaceArtifactDir.IMAGES.value,
        GenerateFileKind.VIDEO: WorkspaceArtifactDir.VIDEOS.value,
        GenerateFileKind.AUDIO: WorkspaceArtifactDir.AUDIO.value,
    }
    _DIRECT_GENERATION_DIRECTORIES = {
        "generate_image": WorkspaceArtifactDir.IMAGES.value,
        "generate_audio": WorkspaceArtifactDir.AUDIO.value,
        "generate_video": WorkspaceArtifactDir.VIDEOS.value,
    }
    _MEDIA_OUTPUT_SPECS = {
        "align_subtitles": (
            WorkspaceFileOutputResourceSpec(
                "output_name",
                WorkspaceFileOutputArgumentKind.FILE,
                default_directory="subtitles",
            ),
        ),
        "build_narration_timeline": (
            WorkspaceFileOutputResourceSpec(
                "manifest_name",
                WorkspaceFileOutputArgumentKind.FILE,
                default_directory=WorkspaceArtifactDir.ARTIFACTS.value,
                fallback_path="narration-manifest.json",
            ),
            WorkspaceFileOutputResourceSpec(
                "timeline_name",
                WorkspaceFileOutputArgumentKind.FILE,
                default_directory=WorkspaceArtifactDir.ARTIFACTS.value,
                fallback_path="narration-timeline.json",
            ),
            WorkspaceFileOutputResourceSpec(
                "cues_name",
                WorkspaceFileOutputArgumentKind.FILE,
                default_directory=WorkspaceArtifactDir.ARTIFACTS.value,
                fallback_path="subtitle-cues.json",
            ),
        ),
        "compose_video_timeline": (
            WorkspaceFileOutputResourceSpec(
                "output_name",
                WorkspaceFileOutputArgumentKind.FILE,
                default_directory=WorkspaceArtifactDir.VIDEOS.value,
                media_kind="video",
                media_extensions=(".mp4",),
                video_edit_recipe=True,
            ),
        ),
        "merge_videos": (
            WorkspaceFileOutputResourceSpec(
                "output_name",
                WorkspaceFileOutputArgumentKind.FILE,
                default_directory=WorkspaceArtifactDir.VIDEOS.value,
                media_kind="video",
                media_extensions=(".mp4",),
            ),
        ),
        "prepare_recording": (
            WorkspaceFileOutputResourceSpec("output_directory", WorkspaceFileOutputArgumentKind.DIRECTORY),
        ),
        "prepare_creative_studio": (
            WorkspaceFileOutputResourceSpec("output_directory", WorkspaceFileOutputArgumentKind.DIRECTORY),
        ),
        "verify_creative_studio": (
            WorkspaceFileOutputResourceSpec("output_directory", WorkspaceFileOutputArgumentKind.DIRECTORY),
        ),
        "compose_creative_publication": (
            WorkspaceFileOutputResourceSpec("output_directory", WorkspaceFileOutputArgumentKind.DIRECTORY),
        ),
        "compose_creative_dub": (
            WorkspaceFileOutputResourceSpec("output_directory", WorkspaceFileOutputArgumentKind.DIRECTORY),
        ),
        "render_recording": (
            WorkspaceFileOutputResourceSpec("output_directory", WorkspaceFileOutputArgumentKind.DIRECTORY),
        ),
        "normalize_audio_loudness": (
            WorkspaceFileOutputResourceSpec(
                "output_name",
                WorkspaceFileOutputArgumentKind.FILE,
                default_directory=WorkspaceArtifactDir.AUDIO.value,
            ),
        ),
        "prepare_narration_timeline": (
            WorkspaceFileOutputResourceSpec(
                "normalized_output_directory",
                WorkspaceFileOutputArgumentKind.DIRECTORY,
            ),
            WorkspaceFileOutputResourceSpec(
                "manifest_name",
                WorkspaceFileOutputArgumentKind.FILE,
                default_directory=WorkspaceArtifactDir.ARTIFACTS.value,
                fallback_path="narration-manifest.json",
            ),
            WorkspaceFileOutputResourceSpec(
                "timeline_name",
                WorkspaceFileOutputArgumentKind.FILE,
                default_directory=WorkspaceArtifactDir.ARTIFACTS.value,
                fallback_path="narration-timeline.json",
            ),
            WorkspaceFileOutputResourceSpec(
                "cues_name",
                WorkspaceFileOutputArgumentKind.FILE,
                default_directory=WorkspaceArtifactDir.ARTIFACTS.value,
                fallback_path="subtitle-cues.json",
            ),
        ),
        "render_frame_samples": (
            WorkspaceFileOutputResourceSpec(
                "output_dir",
                WorkspaceFileOutputArgumentKind.DIRECTORY,
            ),
        ),
        "still_to_video": (
            WorkspaceFileOutputResourceSpec(
                "output_name",
                WorkspaceFileOutputArgumentKind.FILE,
                default_directory="video",
            ),
        ),
    }

    @classmethod
    def create(
        cls,
        *,
        request: "RuntimeApprovalRequest",
        action_key: str,
        resource_id: str | None,
    ) -> tuple[WorkspaceFileAuthorizationResource, ...]:
        from packages.core.ai.runtime.composite_tools import (
            RuntimeCompositeToolCallFactory,
        )

        canonical_call = RuntimeCompositeToolCallFactory.create(
            request.tool_name,
            request.arguments,
        )
        tool_name = canonical_call.tool_name
        arguments = canonical_call.arguments
        plan = request.workspace_file_scope.media_output_plan if request.workspace_file_scope else None
        if plan is not None:
            return tuple(
                WorkspaceFileAuthorizationResource(path, WorkspaceFileResourceMatchKind.EXACT)
                for path in plan.all_paths
            )
        if request.workspace_file_scope and request.workspace_file_scope.file_targets:
            return tuple(
                WorkspaceFileAuthorizationResource(path, WorkspaceFileResourceMatchKind.EXACT)
                for path in request.workspace_file_scope.file_targets
            )
        if tool_name == "code_project":
            project_root = _file_resource_id(
                request.workspace_file_scope.artifact_base_dir
                if request.workspace_file_scope else None
            )
            if project_root is None:
                return ()
            from packages.core.ai.runtime.approval_classifier import (
                code_project_target_paths,
            )

            return tuple(
                WorkspaceFileAuthorizationResource(target, WorkspaceFileResourceMatchKind.ENTITY_EXACT)
                for target in code_project_target_paths(project_root, arguments)
            )
        resource = _file_resource_id(resource_id)
        artifact_base = request.workspace_file_scope.artifact_base_dir if request.workspace_file_scope else None
        typed_directories = (
            workspace_artifact_default_dir(artifact_base, "documents") != artifact_base
            if artifact_base else bool(request.workspace_id and request.task_id)
        )
        direct_directory = cls._DIRECT_GENERATION_DIRECTORIES.get(tool_name)
        if direct_directory is not None:
            return cls.direct_generation_resources(
                arguments,
                default_directory=direct_directory,
            )
        if tool_name == "generate_file":
            return cls.generate_file_resources(arguments, resource, typed_directories=typed_directories)
        if tool_name == "generate_document_file":
            return cls.generate_document_resources(
                arguments,
                resource,
                workspace_scoped=typed_directories,
            )
        if tool_name == "sandbox_save_result":
            return cls.sandbox_save_result_resources(arguments, typed_directories=typed_directories)
        if tool_name == "bash":
            return cls._bash_resources(arguments, artifact_base_dir=artifact_base, entity_id=request.entity_id)
        media_resources = cls.media_tool_resources(
            tool_name,
            arguments,
            artifact_base_dir=artifact_base or "",
            entity_id=request.entity_id,
        )
        if media_resources:
            return media_resources
        if resource is None:
            return ()
        if (
            tool_name == "write_file"
            and action_key == "workspace.file.create"
            and typed_directories
        ):
            return (WorkspaceFileAuthorizationResource(
                resource_id=_default_artifact_resource_id(
                    resource,
                    WorkspaceArtifactDir.DOCUMENTS.value,
                ),
                match_kind=WorkspaceFileResourceMatchKind.EXACT,
            ),)
        if (
            tool_name in {"write_file", "edit_file", "patch_file", "replace_file", "delete_file"}
            and typed_directories
            and "/" not in resource
        ):
            return (
                WorkspaceFileAuthorizationResource(
                    resource_id=resource,
                    match_kind=WorkspaceFileResourceMatchKind.EXACT,
                ),
                WorkspaceFileAuthorizationResource(
                    resource_id=_default_artifact_resource_id(
                        resource,
                        WorkspaceArtifactDir.DOCUMENTS.value,
                    ),
                    match_kind=WorkspaceFileResourceMatchKind.EXACT,
                ),
            )
        return (WorkspaceFileAuthorizationResource(
            resource_id=resource,
            match_kind=WorkspaceFileResourceMatchKind.EXACT,
        ),)

    @classmethod
    def supports_workspace_scope(cls, tool_name: str) -> bool:
        return tool_name in {
            "delete_file",
            "edit_file",
            "patch_file",
            "replace_file",
            "generate_document_file",
            "generate_file",
            "sandbox_save_result",
            "sandbox",
            "bash",
            "write_file",
            "archive_file_collection",
            *cls._DIRECT_GENERATION_DIRECTORIES,
            *cls._MEDIA_OUTPUT_SPECS,
        }

    @classmethod
    def frozen_media_output_spec(
        cls, tool_name: str,
    ) -> WorkspaceFileOutputResourceSpec | None:
        """One registered output contract owns approval and execution naming.

        Registered companions share that exact plan. Other multi-file writers
        retain their existing declared directory contract.
        """
        specs = tuple(
            spec for spec in cls._MEDIA_OUTPUT_SPECS.get(tool_name, ())
            if spec.media_kind is not None
        )
        if len(specs) > 1:
            raise ValueError("A frozen media writer must declare one output specification")
        return specs[0] if specs else None

    @classmethod
    def media_tool_resources(
        cls,
        tool_name: str,
        arguments: dict[str, object],
        *, artifact_base_dir: str | None = None, entity_id: str | None = None,
    ) -> tuple[WorkspaceFileAuthorizationResource, ...]:
        """Authorize only output directories declared by a media tool contract."""

        resources: list[WorkspaceFileAuthorizationResource] = []
        for spec in cls._MEDIA_OUTPUT_SPECS.get(tool_name, ()):
            raw_path = str(
                arguments.get(spec.argument_name) or spec.fallback_path
            ).strip()
            if spec.argument_name == "output_directory" and artifact_base_dir is not None:
                from packages.core.services.generated_media_naming import execution_output_directory
                raw_path = execution_output_directory(raw_path, artifact_base_dir)
            resource = _file_resource_id(raw_path)
            if spec.kind is WorkspaceFileOutputArgumentKind.FILE:
                if (
                    resource and "/" not in resource and artifact_base_dir is not None
                    and workspace_artifact_default_dir(artifact_base_dir, "artifacts") == artifact_base_dir
                ):
                    from packages.core.services.entity_fs import get_entity_root
                    from packages.core.services.generated_media_naming import build_generated_media_target
                    extension = posixpath.splitext(resource)[1] or ".json"
                    if tool_name in {"compose_video_timeline", "merge_videos", "still_to_video"}:
                        extension = ".mp4"
                    elif tool_name == "normalize_audio_loudness":
                        fmt = str(arguments.get("output_format") or "wav").lower()
                        extension = "." + (fmt if fmt in {"wav", "mp3", "m4a", "flac"} else "wav")
                    elif tool_name == "align_subtitles":
                        fmt = str(arguments.get("format") or "srt").lower()
                        extension = "." + (fmt if fmt in {"srt", "vtt", "ass"} else "srt")
                    target = build_generated_media_target(
                        prompt=resource, desired_name=resource, ext=extension,
                        default_dir=artifact_base_dir,
                        entity_root=get_entity_root(entity_id) if entity_id else None,
                    )
                    resources.append(WorkspaceFileAuthorizationResource(
                        target.rel_path, WorkspaceFileResourceMatchKind.EXACT,
                    ))
                    continue
                resource = (
                    posixpath.dirname(resource)
                    if resource and "/" in resource
                    else _file_resource_id(spec.default_directory)
                )
            if resource is None or any(
                item.resource_id == resource for item in resources
            ):
                continue
            resources.append(WorkspaceFileAuthorizationResource(
                resource_id=resource,
                match_kind=WorkspaceFileResourceMatchKind.TREE,
            ))
        return tuple(resources)

    @staticmethod
    def direct_generation_resources(
        arguments: dict[str, object],
        *,
        default_directory: str,
    ) -> tuple[WorkspaceFileAuthorizationResource, ...]:
        """Bind direct media output to its requested or default directory."""

        raw_path = str(
            arguments.get("name")
            or arguments.get("output_name")
            or arguments.get("filename")
            or ""
        ).strip()
        resource = _file_resource_id(raw_path)
        if raw_path and resource is None:
            return ()
        directory = (
            posixpath.dirname(resource)
            if resource and "/" in resource
            else _file_resource_id(default_directory)
        )
        if directory is None:
            return ()
        return (WorkspaceFileAuthorizationResource(
            resource_id=directory,
            match_kind=WorkspaceFileResourceMatchKind.TREE,
        ),)

    @staticmethod
    def sandbox_save_result_resources(
        arguments: dict[str, object],
        *, typed_directories: bool = True,
    ) -> tuple[WorkspaceFileAuthorizationResource, ...]:
        """Bind a sandbox export to its requested collision-safe filename."""
        filename = str(arguments.get("filename") or "").strip().replace("\\", "/")
        if (
            not filename
            or filename in {".", ".."}
            or "/" in filename
            or _file_resource_id(filename) is None
        ):
            return ()
        return (WorkspaceFileAuthorizationResource(
            resource_id=f"{WorkspaceArtifactDir.ARTIFACTS.value}/{filename}" if typed_directories else filename,
            match_kind=WorkspaceFileResourceMatchKind.COLLISION_SAFE_FILE,
        ),)

    @classmethod
    def _bash_resources(
        cls,
        arguments: dict[str, object],
        *, artifact_base_dir: str | None = None, entity_id: str | None = None,
    ) -> tuple[WorkspaceFileAuthorizationResource, ...]:
        from packages.core.ai.runtime.approval_classifier import bash_write_targets

        command = str(arguments.get("command") or "").strip()
        if artifact_base_dir and entity_id:
            from packages.core.ai.runtime.bash_output_paths import BashOutputPathError, scope_bash_output_command
            from packages.core.services.entity_fs import get_entity_root
            try:
                command = scope_bash_output_command(
                    command, artifact_base_dir=artifact_base_dir, entity_root=get_entity_root(entity_id),
                ).command
            except BashOutputPathError:
                return ()
        targets = bash_write_targets(command)
        try:
            import shlex

            parts = shlex.split(command)
        except ValueError:
            parts = []
        if parts and parts[0].rsplit("/", 1)[-1] in {"mv", "cp"}:
            targets = [part for part in parts[1:] if not part.startswith("-")]
        resources = []
        for target in targets:
            resource = _file_resource_id(target)
            if artifact_base_dir and resource and resource.startswith("Workspaces/"):
                from packages.core.services.generated_media_naming import workspace_storage_root
                if not resource.startswith(workspace_storage_root(artifact_base_dir) + "/"):
                    return ()
            if resource is None or any(
                item.resource_id == resource for item in resources
            ):
                continue
            resources.append(WorkspaceFileAuthorizationResource(
                resource_id=resource,
                match_kind=WorkspaceFileResourceMatchKind.ENTITY_EXACT if artifact_base_dir else WorkspaceFileResourceMatchKind.EXACT,
            ))
        return tuple(resources)

    @classmethod
    def generate_file_destination(
        cls,
        arguments: dict[str, object],
        name: str | None = None,
    ) -> "GenerateFileDestination | None":
        """The single output target of one ``generate_file`` call.

        The approval receipt (via :meth:`generate_file_resources`) and the
        ``generate_file`` handler both derive the write target from this value.
        ``name`` is the logical path handed to the materializer; ``directory``
        is the typed artifact bucket, which Workspace scoping applies only
        inside an execution archive (``scope_workspace_artifact_path``). Neither
        side may add its own prefix, suffix or bucket: any divergence makes the
        inner write fail the receipt with ``authorization_receipt_mismatch``.
        """

        raw_kind = str(arguments.get("kind") or "").strip().lower().replace("-", "_")
        try:
            kind = GenerateFileKind(raw_kind)
        except ValueError:
            return None
        raw_params = arguments.get("params")
        params = raw_params if isinstance(raw_params, dict) else {}
        if kind is GenerateFileKind.DOCUMENT and (
            arguments.get("files") is not None or params.get("files") is not None
        ):
            # The handler materializes a document with bundle files as code.
            kind = GenerateFileKind.CODE
        elif kind is GenerateFileKind.DOCUMENT and not any(
            key in source
            for source in (arguments, params)
            for key in ("operations", "template", "source")
        ):
            content = arguments.get("content")
            if content is None:
                content = params.get("content")
            requested_type = str(
                arguments.get("file_type") or params.get("file_type") or ""
            ).strip().lower().lstrip(".")
            if not content and (
                requested_type in {"diagram", "diagram.json"}
                or str(name or "").strip().lower().endswith((".diagram", ".diagram.json"))
            ):
                # A content-less diagram document is rendered from its prompt.
                kind = GenerateFileKind.DIAGRAM
        resource = _file_resource_id(name)
        # Handlers read only the top-level prompt (``params.prompt`` is dropped
        # by the merged params); a diagram falls back to its content.
        prompt = str(arguments.get("prompt") or "").strip()
        if not prompt and kind is GenerateFileKind.DIAGRAM:
            prompt = str(arguments.get("content") or params.get("content") or "").strip()
        operation_type = cls.generate_file_operation_file_type(kind, arguments, resource or "")
        if resource is None:
            # Resolve deterministic leaf defaults before binding authorization.
            # An unscoped export must never receive an entire Knowledge-root grant.
            if operation_type is not None:
                resource = f"generated-document.{operation_type}"
            elif kind is GenerateFileKind.DOCUMENT:
                resource = "generated-document.md"
            elif kind is GenerateFileKind.CODE:
                from packages.core.ai.tools.generate_file.code import _bundle_name
                resource = _bundle_name("", prompt)
            elif kind is GenerateFileKind.DIAGRAM:
                from packages.core.ai.tools.generate_file.diagram import _diagram_filename
                resource = _diagram_filename("", prompt)
        elif kind is GenerateFileKind.CODE:
            from packages.core.ai.tools.generate_file.code import _bundle_name
            resource = _bundle_name(resource, "")
        elif kind is GenerateFileKind.DIAGRAM:
            from packages.core.ai.tools.generate_file.diagram import _diagram_filename
            resource = _diagram_filename(resource, "")
        else:
            # The document materializer appends ``.<file_type>`` to an
            # extensionless name; mirror it for every route that reaches it.
            file_type = cls._generate_file_materializer_type(
                kind, arguments, operation_type=operation_type,
            )
            if file_type and "." not in posixpath.basename(resource):
                resource = f"{resource}.{file_type}"
        return GenerateFileDestination(
            kind=kind,
            name=resource,
            directory=cls._GENERATE_FILE_DIRECTORIES.get(kind),
            match_kind=(
                WorkspaceFileResourceMatchKind.TREE
                if kind is GenerateFileKind.CODE
                else WorkspaceFileResourceMatchKind.EXACT
            ),
        )

    _GENERATE_FILE_OPERATION_TYPES = {
        GenerateFileKind.WORD_DOCUMENT: "docx",
        GenerateFileKind.PRESENTATION: "pptx",
        GenerateFileKind.SPREADSHEET: "xlsx",
    }

    @classmethod
    def generate_file_operation_file_type(
        cls,
        kind: GenerateFileKind,
        arguments: dict[str, object],
        name: str,
    ) -> str | None:
        """Output type of a native Office ``operations``/``template`` call, else None."""

        raw_params = arguments.get("params")
        params = raw_params if isinstance(raw_params, dict) else {}
        if not any(
            key in source
            for source in (arguments, params)
            for key in ("operations", "template")
        ):
            return None
        if kind is not GenerateFileKind.DOCUMENT and kind not in cls._GENERATE_FILE_OPERATION_TYPES:
            return None
        from packages.core.contracts.file_engine import file_type_from_path

        requested = str(
            arguments.get("file_type") or params.get("file_type") or ""
        ).strip().lower().lstrip(".")
        return (
            requested
            or cls._GENERATE_FILE_OPERATION_TYPES.get(kind)
            or file_type_from_path(name)
            or None
        )

    @classmethod
    def _generate_file_materializer_type(
        cls,
        kind: GenerateFileKind,
        arguments: dict[str, object],
        *,
        operation_type: str | None,
    ) -> str | None:
        raw_params = arguments.get("params")
        params = raw_params if isinstance(raw_params, dict) else {}
        requested = str(
            arguments.get("file_type") or params.get("file_type") or ""
        ).strip().lower().lstrip(".")
        if "source" in arguments or "source" in params:
            return requested or ("pdf" if kind is GenerateFileKind.PDF else None)
        if operation_type is not None:
            return operation_type
        if kind is GenerateFileKind.DOCUMENT:
            return requested or "md"
        return None

    @classmethod
    def generate_file_resources(
        cls,
        arguments: dict[str, object],
        resource: str | None,
        *, typed_directories: bool = True,
    ) -> tuple[WorkspaceFileAuthorizationResource, ...]:
        destination = cls.generate_file_destination(arguments, resource)
        if destination is None:
            resource = _file_resource_id(resource)
            if resource is None:
                return ()
            return (WorkspaceFileAuthorizationResource(
                resource_id=resource,
                match_kind=WorkspaceFileResourceMatchKind.EXACT,
            ),)
        if destination.name is None:
            if destination.directory is None or not typed_directories:
                return ()
            return (WorkspaceFileAuthorizationResource(
                resource_id=destination.directory,
                match_kind=WorkspaceFileResourceMatchKind.TREE,
            ),)
        raw_params = arguments.get("params")
        params = raw_params if isinstance(raw_params, dict) else {}
        purpose = str(
            arguments.get("purpose") or params.get("purpose") or ""
        ).strip().lower().replace("-", "_")
        if (
            destination.kind is GenerateFileKind.AUDIO
            and purpose == AudioGenerationPurpose.NARRATION.value
        ):
            narrator_directory = (
                posixpath.dirname(destination.name)
                if "/" in destination.name
                else WorkspaceArtifactDir.AUDIO.value
            )
            return (WorkspaceFileAuthorizationResource(
                resource_id=narrator_directory,
                match_kind=WorkspaceFileResourceMatchKind.TREE,
            ),)
        resource = destination.name
        if destination.directory and typed_directories:
            resource = _default_artifact_resource_id(resource, destination.directory)
        return (WorkspaceFileAuthorizationResource(
            resource_id=resource,
            match_kind=destination.match_kind,
        ),)

    @classmethod
    def generate_document_resources(
        cls,
        arguments: dict[str, object],
        resource: str | None,
        *,
        workspace_scoped: bool,
    ) -> tuple[WorkspaceFileAuthorizationResource, ...]:
        if resource is None:
            return ()
        if "." not in posixpath.basename(resource):
            file_type = str(arguments.get("file_type") or "txt").strip().lstrip(".")
            resource = f"{resource}.{file_type or 'txt'}"
        if workspace_scoped:
            resource = _default_artifact_resource_id(
                resource,
                WorkspaceArtifactDir.DOCUMENTS.value,
            )
        return (WorkspaceFileAuthorizationResource(
            resource_id=resource,
            match_kind=WorkspaceFileResourceMatchKind.EXACT,
        ),)


@dataclass(frozen=True)
class RuntimeToolAuthorizationReceipt:
    """Immutable subject from one allowed public runtime tool invocation."""

    tool_name: str
    entity_id: str
    user_id: str | None
    workspace_id: str | None
    conversation_id: str | None
    task_id: str | None
    action_key: str
    capability_id: str | None
    access: RuntimeAuthorizationAccess
    arguments_hash: str
    resource_kind: str | None
    file_resources: tuple[WorkspaceFileAuthorizationResource, ...]
    workspace_artifact_base_dir: str | None = None
    public_argument_keys: frozenset[str] = frozenset()
    workspace_scope_binding: WorkspaceFileScopeBinding = (
        WorkspaceFileScopeBinding.UNBOUND
    )
    tiktok_publish_consent: TikTokPublishConsent | None = None
    chrome_publication_binding: ChromePublicationBinding | None = None
    media_output_plan: GeneratedMediaOutputPlan | None = None
    workflow_run_id: str | None = None
    # Filled only from this invocation's server-owned approval-consume capture,
    # never from a model argument or an older same-payload grant.
    consumed_approval_request_id: str | None = None
    operation_id: str | None = None

    @property
    def resource_ids(self) -> tuple[str, ...]:
        return tuple(resource.resource_id for resource in self.file_resources)

    def authorizes_workspace_file_mutation(
        self,
        *,
        capability_id: str,
        action_key: str,
        paths: Iterable[str],
        entity_id: str,
        user_id: str | None,
        workspace_id: str | None,
        conversation_id: str | None,
        task_id: str | None,
    ) -> bool:
        """Match the exact Workspace scope, mutation kind, and file targets."""

        receipt_kind = _workspace_file_mutation_kind(self.action_key)
        requested_kind = _workspace_file_mutation_kind(action_key)
        if not (
            self.access is RuntimeAuthorizationAccess.ACTION
            and self.capability_id == capability_id
            and self.resource_kind in {"file", "workspace_file"}
            and receipt_kind is not None
            and receipt_kind is requested_kind
            and self.entity_id == str(entity_id or "").strip()
            and self.user_id == _scope(user_id)
            and self.workspace_id == _scope(workspace_id)
            and self.conversation_id == _scope(conversation_id)
            and self.task_id == _scope(task_id)
            and self.workspace_scope_binding is not WorkspaceFileScopeBinding.UNRESOLVED
        ):
            return False
        actual_paths = tuple(
            str(path or "").strip()
            for path in paths
            if str(path or "").strip()
        )
        if not actual_paths:
            return False
        if not self.file_resources:
            return False
        return all(
            any(
                _file_resource_matches(
                    approved_resource=resource,
                    actual_path=path,
                    task_id=task_id,
                    artifact_base_dir=self.workspace_artifact_base_dir,
                )
                for resource in self.file_resources
            )
            for path in actual_paths
        )

    def authorizes_workspace_file_commit(
        self,
        *,
        entity_id: str,
        path: str,
        target_exists: bool,
    ) -> bool:
        """Revalidate action kind and exact target inside the atomic write lock."""

        mutation_kind = _workspace_file_mutation_kind(self.action_key)
        if not (
            self.access is RuntimeAuthorizationAccess.ACTION
            and self.entity_id == str(entity_id or "").strip()
            and self.capability_id == "file.write"
            and self.workspace_id
            and mutation_kind is not None
            and mutation_kind is not WorkspaceFileMutationKind.DELETE
            and self.file_resources
            and self.workspace_scope_binding is not WorkspaceFileScopeBinding.UNRESOLVED
        ):
            return False
        matching_resources = tuple(
            resource
            for resource in self.file_resources
            if _file_resource_matches(
                approved_resource=resource,
                actual_path=path,
                task_id=self.task_id,
                artifact_base_dir=self.workspace_artifact_base_dir,
            )
        )
        if not matching_resources:
            return False
        if mutation_kind is WorkspaceFileMutationKind.WRITE:
            return True
        if mutation_kind is WorkspaceFileMutationKind.CREATE:
            return not target_exists
        if target_exists:
            return True
        # A bundle/code generator modifies an approved tree by creating or
        # replacing children. Exact MODIFY receipts may never create a path.
        return any(
            resource.match_kind is WorkspaceFileResourceMatchKind.TREE
            for resource in matching_resources
        )


class RuntimeToolAuthorizationWriteError(PermissionError):
    """The target changed between approval and the atomic filesystem commit."""


class RuntimeToolAuthorizationReceiptFactory:
    """Create receipts only from the exact typed decision returned by the gate."""

    @staticmethod
    def create(
        *,
        decision: "RuntimeApprovalDecision",
        request: "RuntimeApprovalRequest",
    ) -> RuntimeToolAuthorizationReceipt | None:
        if not decision.allowed:
            return None
        authoritative_request = decision.request or request
        classification = decision.classification
        authorization = classification.authorization
        if authorization is None:
            return None
        action = classification.action
        resource_id = (
            _file_resource_id(action.resource_id) if action is not None else None
        )
        public_argument_keys = frozenset(
            str(key)
            for key in authoritative_request.arguments
            if str(key) not in _RUNTIME_EPHEMERAL_ARGUMENT_KEYS
        )
        resolved_artifact_base = (
            _scope(authoritative_request.workspace_file_scope.artifact_base_dir)
            if authoritative_request.workspace_file_scope is not None
            else None
        )
        workspace_scope_binding = (
            WorkspaceFileScopeBinding.EXACT
            if resolved_artifact_base
            else WorkspaceFileScopeBinding.UNRESOLVED
            if authoritative_request.workspace_file_scope is not None and authoritative_request.workspace_id
            else WorkspaceFileScopeBinding.UNBOUND
        )

        from packages.core.ai.runtime.sandbox import runtime_is_task_binding_key

        # A conversation-less task worker authorizes sandbox-bound tools under
        # its synthetic binding key (see runtime_authorization_conversation_id).
        # That key identifies the sandbox binding, not a conversation: the
        # inner file mutation is checked against the real conversation (none),
        # so the receipt must record none too or every save would mismatch.
        receipt_conversation_id = _scope(authoritative_request.conversation_id)
        if receipt_conversation_id and runtime_is_task_binding_key(receipt_conversation_id):
            receipt_conversation_id = None

        from packages.core.ai.runtime.surfaces import ChatSurface

        surface = getattr(authoritative_request.envelope, "surface", None)
        workflow_run_id = (
            _scope(authoritative_request.arguments.get("_workflow_run_id_from_context"))
            if getattr(surface, "value", surface) == ChatSurface.WORKFLOW_AGENT_STEP.value
            else None
        )

        return RuntimeToolAuthorizationReceipt(
            tool_name=str(authoritative_request.tool_name or "").strip(),
            operation_id=authoritative_request.operation_id,
            entity_id=str(authoritative_request.entity_id or "").strip(),
            user_id=_scope(authoritative_request.user_id),
            workspace_id=_scope(authoritative_request.workspace_id),
            conversation_id=receipt_conversation_id,
            task_id=_scope(authoritative_request.task_id),
            action_key=authorization.action_key,
            capability_id=authorization.capability_id,
            access=authorization.access,
            arguments_hash=runtime_authorization_arguments_hash(
                authoritative_request.arguments,
                public_argument_keys=public_argument_keys,
            ),
            resource_kind=_scope(action.resource_kind) if action is not None else None,
            file_resources=WorkspaceFileAuthorizationResourceFactory.create(
                request=authoritative_request,
                action_key=authorization.action_key,
                resource_id=resource_id,
            ),
            workspace_artifact_base_dir=resolved_artifact_base,
            media_output_plan=(
                authoritative_request.workspace_file_scope.media_output_plan
                if authoritative_request.workspace_file_scope else None
            ),
            public_argument_keys=public_argument_keys,
            workspace_scope_binding=workspace_scope_binding,
            workflow_run_id=workflow_run_id,
        )


async def runtime_workflow_receipt_is_executable(receipt: RuntimeToolAuthorizationReceipt | None) -> bool:
    """Recheck delegated Workflow state immediately before invoking any tool handler."""
    if receipt is None or receipt.workflow_run_id is None:
        return True
    from packages.core.database import async_session
    from packages.core.governance.service import workflow_run_is_executable

    async with async_session() as db:
        return await workflow_run_is_executable(
            db, entity_id=receipt.entity_id, workspace_id=receipt.workspace_id,
            user_id=receipt.user_id, workflow_run_id=receipt.workflow_run_id,
        )


class RuntimeAuthorizationLeaseState(str, Enum):
    ISSUED = "issued"
    ACTIVE = "active"
    CLOSED = "closed"


class RuntimeAuthorizationScopeFailure(str, Enum):
    ARGUMENTS_MISMATCH = "arguments_mismatch"
    ALREADY_CONSUMED = "already_consumed"


@dataclass
class RuntimeToolAuthorizationLease:
    receipt: RuntimeToolAuthorizationReceipt
    state: RuntimeAuthorizationLeaseState = RuntimeAuthorizationLeaseState.ISSUED
    tiktok_publish_claimed: bool = False

    def activate(self) -> bool:
        if self.state is not RuntimeAuthorizationLeaseState.ISSUED:
            return False
        self.state = RuntimeAuthorizationLeaseState.ACTIVE
        return True

    def close(self) -> None:
        self.state = RuntimeAuthorizationLeaseState.CLOSED

    def current_receipt(self) -> RuntimeToolAuthorizationReceipt | None:
        if self.state is RuntimeAuthorizationLeaseState.ACTIVE:
            return self.receipt
        return None


class RuntimeToolAuthorizationLeaseFactory:
    @staticmethod
    def create(
        receipt: RuntimeToolAuthorizationReceipt | None,
    ) -> RuntimeToolAuthorizationLease | None:
        return RuntimeToolAuthorizationLease(receipt) if receipt is not None else None


@dataclass(frozen=True)
class RuntimeToolAuthorizationScope:
    token: Token
    lease: RuntimeToolAuthorizationLease | None
    valid: bool
    failure: RuntimeAuthorizationScopeFailure | None = None


_lease_var: ContextVar[RuntimeToolAuthorizationLease | None] = ContextVar(
    "runtime_tool_authorization_lease",
    default=None,
)


def runtime_begin_tool_authorization_scope(
    authorization: RuntimeToolAuthorizationReceipt | RuntimeToolAuthorizationLease | None,
    *,
    arguments: dict[str, object],
) -> RuntimeToolAuthorizationScope:
    """Bind a revocable lease to this handler and inherited child contexts."""

    lease = (
        authorization
        if isinstance(authorization, RuntimeToolAuthorizationLease)
        else RuntimeToolAuthorizationLease(authorization)
        if authorization is not None
        else None
    )
    failure = None
    if lease is not None:
        if runtime_authorization_arguments_hash(
            arguments,
            public_argument_keys=lease.receipt.public_argument_keys,
        ) != lease.receipt.arguments_hash:
            lease.close()
            valid = False
            failure = RuntimeAuthorizationScopeFailure.ARGUMENTS_MISMATCH
        else:
            valid = lease.activate()
            if not valid:
                failure = RuntimeAuthorizationScopeFailure.ALREADY_CONSUMED
    else:
        valid = True
    return RuntimeToolAuthorizationScope(
        token=_lease_var.set(lease if valid else None),
        lease=lease,
        valid=valid,
        failure=failure,
    )


def runtime_end_tool_authorization_scope(
    scope: RuntimeToolAuthorizationScope,
) -> None:
    if scope.valid and scope.lease is not None:
        scope.lease.close()
    _lease_var.reset(scope.token)


def runtime_current_tool_authorization_receipt(
) -> RuntimeToolAuthorizationReceipt | None:
    lease = _lease_var.get()
    return lease.current_receipt() if lease is not None else None


def runtime_claim_tiktok_publish_consent(consent: TikTokPublishConsent) -> bool:
    """Spend the exact consent once, inside its active execution lease only."""
    lease = _lease_var.get()
    if (
        lease is None
        or lease.current_receipt() is None
        or lease.receipt.tiktok_publish_consent is not consent
        or lease.tiktok_publish_claimed
    ):
        return False
    lease.tiktok_publish_claimed = True
    return True
