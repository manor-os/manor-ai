"""Execution-scoped proof that one exact runtime tool call was authorized."""

from __future__ import annotations

from collections.abc import Iterable
from contextvars import ContextVar, Token
from dataclasses import dataclass
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


class WorkspaceFileMutationKind(str, Enum):
    CREATE = "create"
    MODIFY = "modify"
    WRITE = "write"
    DELETE = "delete"


class WorkspaceFileResourceMatchKind(str, Enum):
    EXACT = "exact"
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
class WorkspaceFileOutputResourceSpec:
    argument_name: str
    kind: WorkspaceFileOutputArgumentKind
    default_directory: str = ""
    fallback_path: str = ""


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
                fallback_path="technical/narration-manifest.json",
            ),
            WorkspaceFileOutputResourceSpec(
                "timeline_name",
                WorkspaceFileOutputArgumentKind.FILE,
                default_directory=WorkspaceArtifactDir.ARTIFACTS.value,
                fallback_path="timeline/narration-timeline.json",
            ),
            WorkspaceFileOutputResourceSpec(
                "cues_name",
                WorkspaceFileOutputArgumentKind.FILE,
                default_directory=WorkspaceArtifactDir.ARTIFACTS.value,
                fallback_path="subtitles/subtitle-cues.json",
            ),
        ),
        "compose_video_timeline": (
            WorkspaceFileOutputResourceSpec(
                "output_name",
                WorkspaceFileOutputArgumentKind.FILE,
                default_directory=WorkspaceArtifactDir.VIDEOS.value,
            ),
        ),
        "merge_videos": (
            WorkspaceFileOutputResourceSpec(
                "output_name",
                WorkspaceFileOutputArgumentKind.FILE,
                default_directory=WorkspaceArtifactDir.VIDEOS.value,
            ),
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
                fallback_path="technical/narration-manifest.json",
            ),
            WorkspaceFileOutputResourceSpec(
                "timeline_name",
                WorkspaceFileOutputArgumentKind.FILE,
                default_directory=WorkspaceArtifactDir.ARTIFACTS.value,
                fallback_path="timeline/narration-timeline.json",
            ),
            WorkspaceFileOutputResourceSpec(
                "cues_name",
                WorkspaceFileOutputArgumentKind.FILE,
                default_directory=WorkspaceArtifactDir.ARTIFACTS.value,
                fallback_path="subtitles/subtitle-cues.json",
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
        resource = _file_resource_id(resource_id)
        direct_directory = cls._DIRECT_GENERATION_DIRECTORIES.get(request.tool_name)
        if direct_directory is not None:
            return cls.direct_generation_resources(
                request.arguments,
                default_directory=direct_directory,
            )
        if request.tool_name == "generate_file":
            return cls.generate_file_resources(request.arguments, resource)
        if request.tool_name == "generate_document_file":
            return cls.generate_document_resources(
                request.arguments,
                resource,
                workspace_scoped=bool(_scope(request.workspace_id)),
            )
        if request.tool_name == "sandbox_save_result":
            return cls.sandbox_save_result_resources(request.arguments)
        if request.tool_name == "bash":
            return cls._bash_resources(request.arguments)
        media_resources = cls.media_tool_resources(
            request.tool_name,
            request.arguments,
        )
        if media_resources:
            return media_resources
        if resource is None:
            return ()
        if (
            request.tool_name == "write_file"
            and action_key == "workspace.file.create"
        ):
            return (WorkspaceFileAuthorizationResource(
                resource_id=_default_artifact_resource_id(
                    resource,
                    WorkspaceArtifactDir.DOCUMENTS.value,
                ),
                match_kind=WorkspaceFileResourceMatchKind.EXACT,
            ),)
        if (
            request.tool_name in {"write_file", "edit_file", "patch_file", "delete_file"}
            and request.workspace_id
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
            "generate_document_file",
            "generate_file",
            "sandbox_save_result",
            "write_file",
            *cls._DIRECT_GENERATION_DIRECTORIES,
            *cls._MEDIA_OUTPUT_SPECS,
        }

    @classmethod
    def media_tool_resources(
        cls,
        tool_name: str,
        arguments: dict[str, object],
    ) -> tuple[WorkspaceFileAuthorizationResource, ...]:
        """Authorize only output directories declared by a media tool contract."""

        resources: list[WorkspaceFileAuthorizationResource] = []
        for spec in cls._MEDIA_OUTPUT_SPECS.get(tool_name, ()):
            raw_path = str(
                arguments.get(spec.argument_name) or spec.fallback_path
            ).strip()
            resource = _file_resource_id(raw_path)
            if spec.kind is WorkspaceFileOutputArgumentKind.FILE:
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
            resource_id=f"{WorkspaceArtifactDir.ARTIFACTS.value}/{filename}",
            match_kind=WorkspaceFileResourceMatchKind.COLLISION_SAFE_FILE,
        ),)

    @classmethod
    def _bash_resources(
        cls,
        arguments: dict[str, object],
    ) -> tuple[WorkspaceFileAuthorizationResource, ...]:
        from packages.core.ai.runtime.approval_classifier import bash_write_targets

        command = str(arguments.get("command") or "").strip()
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
            if resource is None or any(
                item.resource_id == resource for item in resources
            ):
                continue
            resources.append(WorkspaceFileAuthorizationResource(
                resource_id=resource,
                match_kind=WorkspaceFileResourceMatchKind.EXACT,
            ))
        return tuple(resources)

    @classmethod
    def generate_file_resources(
        cls,
        arguments: dict[str, object],
        resource: str | None,
    ) -> tuple[WorkspaceFileAuthorizationResource, ...]:
        raw_kind = str(arguments.get("kind") or "").strip().lower().replace("-", "_")
        try:
            kind = GenerateFileKind(raw_kind)
        except ValueError:
            if resource is None:
                return ()
            return (WorkspaceFileAuthorizationResource(
                resource_id=resource,
                match_kind=WorkspaceFileResourceMatchKind.EXACT,
            ),)
        artifact_directory = cls._GENERATE_FILE_DIRECTORIES.get(kind)
        if resource is None:
            if artifact_directory is None:
                return ()
            return (WorkspaceFileAuthorizationResource(
                resource_id=artifact_directory,
                match_kind=WorkspaceFileResourceMatchKind.TREE,
            ),)
        raw_params = arguments.get("params")
        params = raw_params if isinstance(raw_params, dict) else {}
        purpose = str(
            arguments.get("purpose") or params.get("purpose") or ""
        ).strip().lower().replace("-", "_")
        if (
            kind is GenerateFileKind.AUDIO
            and purpose == AudioGenerationPurpose.NARRATION.value
        ):
            narrator_directory = (
                posixpath.dirname(resource)
                if resource.startswith("runs/")
                else WorkspaceArtifactDir.AUDIO.value
            )
            return (WorkspaceFileAuthorizationResource(
                resource_id=narrator_directory,
                match_kind=WorkspaceFileResourceMatchKind.TREE,
            ),)
        if kind is GenerateFileKind.CODE:
            resource_directory, basename = posixpath.split(resource)
            stem, extension = posixpath.splitext(basename)
            if extension:
                resource = posixpath.join(resource_directory, stem)
        elif kind is GenerateFileKind.DIAGRAM:
            lower_resource = resource.lower()
            if lower_resource.endswith(".diagram"):
                resource = f"{resource}.json"
            elif not lower_resource.endswith(".diagram.json"):
                root, extension = posixpath.splitext(resource)
                resource = f"{root if extension else resource}.diagram.json"
        elif kind is GenerateFileKind.DOCUMENT and "." not in posixpath.basename(resource):
            file_type = str(
                arguments.get("file_type") or params.get("file_type") or "md"
            ).strip().lower().lstrip(".")
            resource = f"{resource}.{file_type or 'md'}"
        if artifact_directory:
            resource = _default_artifact_resource_id(resource, artifact_directory)
        return (WorkspaceFileAuthorizationResource(
            resource_id=resource,
            match_kind=(
                WorkspaceFileResourceMatchKind.TREE
                if kind is GenerateFileKind.CODE
                else WorkspaceFileResourceMatchKind.EXACT
            ),
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
            and self.workspace_id
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
            if authoritative_request.workspace_file_scope is not None
            else WorkspaceFileScopeBinding.UNBOUND
        )

        return RuntimeToolAuthorizationReceipt(
            tool_name=str(authoritative_request.tool_name or "").strip(),
            entity_id=str(authoritative_request.entity_id or "").strip(),
            user_id=_scope(authoritative_request.user_id),
            workspace_id=_scope(authoritative_request.workspace_id),
            conversation_id=_scope(authoritative_request.conversation_id),
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
            public_argument_keys=public_argument_keys,
            workspace_scope_binding=workspace_scope_binding,
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
