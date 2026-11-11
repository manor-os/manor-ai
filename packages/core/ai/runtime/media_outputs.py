"""Shared media target identity at approval, execution, and worker handoff."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import replace
import os

from packages.core.services.generated_media_naming import (
    GeneratedMediaOutputPlan,
    GeneratedMediaTarget,
    MEDIA_OUTPUT_EXTENSIONS,
    build_generated_media_target,
    prepare_video_editor_output_pair,
    scope_workspace_artifact_path,
    workspace_artifact_default_dir,
    workspace_storage_root,
    video_editor_recipe_path,
)


_approval_plan: ContextVar[GeneratedMediaOutputPlan | None] = ContextVar(
    "runtime_media_output_approval_plan", default=None,
)


@contextmanager
def runtime_media_output_approval_scope(plan: GeneratedMediaOutputPlan | None):
    token = _approval_plan.set(plan)
    try:
        yield
    finally:
        _approval_plan.reset(token)


def runtime_media_output_approval_snapshot() -> dict | None:
    plan = _approval_plan.get()
    return plan.to_dict() if plan is not None else None


def runtime_media_output_approval_matches(context: dict) -> bool:
    return context.get("media_output_plan") == runtime_media_output_approval_snapshot()


def runtime_media_output_policy_resources() -> tuple[str, ...]:
    """Honor both physical and existing logical-path policy rules for all formats."""
    plan = _approval_plan.get()
    if plan is None:
        return ()
    paths = [plan.requested_name, plan.requested_name.removeprefix(f"{plan.artifact_base_dir}/")]
    for path in plan.all_paths:
        paths.extend((path, path.removeprefix(f"{plan.artifact_base_dir}/")))
    return tuple(dict.fromkeys(path for path in paths if path))


def prepare_runtime_media_output_plan(
    *, tool_name: str, arguments: dict, artifact_base_dir: str, entity_root: str,
) -> GeneratedMediaOutputPlan | None:
    from packages.core.ai.runtime.authorization_receipts import WorkspaceFileAuthorizationResourceFactory

    spec = WorkspaceFileAuthorizationResourceFactory.frozen_media_output_spec(tool_name)
    params = arguments.get("params")
    params = params if isinstance(params, dict) else {}
    if tool_name == "generate_file" and ("source" in arguments or "source" in params):
        # Format conversion has one exact output, not a provider-selected format.
        return None
    kind = spec.media_kind if spec is not None else (
        str(arguments.get("kind") or "").strip().lower()
        if tool_name == "generate_file"
        else {"generate_image": "image", "generate_audio": "audio", "generate_video": "video"}.get(tool_name)
    )
    if kind not in MEDIA_OUTPUT_EXTENSIONS:
        return None
    purpose = str(params.get("purpose", arguments.get("purpose")) or "").strip().lower()
    name = str(
        arguments.get("name") or params.get("name") or params.get("output_name")
        or params.get("filename") or arguments.get("output_name") or arguments.get("filename") or ""
    ).strip()
    if spec is not None:
        name = str(arguments.get(spec.argument_name) or spec.fallback_path).strip()
        if not name:
            raise ValueError(f"{spec.argument_name} is required")
    if kind == "audio" and purpose == "narration" and (
        "/" in name or workspace_artifact_default_dir(artifact_base_dir, "audio") != artifact_base_dir
    ):
        # Archives and explicit parents retain narration's voice directories.
        # Bare root output has no voice directory and can use an exact plan.
        return None
    if ".." in name.replace("\\", "/").split("/"):
        raise ValueError("Generated media output may not traverse parent directories")
    if (
        spec is not None and spec.video_edit_recipe and artifact_base_dir
        and name.replace("\\", "/").lstrip("/").startswith("Workspaces/_by_id/")
        and workspace_storage_root(name) != workspace_storage_root(artifact_base_dir)
    ):
        raise ValueError("Composed video output belongs to another Workspace")
    directory = spec.default_directory if spec else {"image": "images", "audio": "audio", "video": "videos"}[kind]
    extensions = spec.media_extensions if spec else MEDIA_OUTPUT_EXTENSIONS[kind]
    if kind == "image" and spec is None:
        # Animation is an explicit output request. Keep existing still-image
        # grants unchanged and authorize only the requested GIF destination.
        output_format = str(params.get("output_format", arguments.get("output_format")) or "png").strip().lower()
        extensions = (".gif",) if output_format == "gif" else tuple(ext for ext in extensions if ext != ".gif")
    targets = tuple(
        build_generated_media_target(
            prompt=str(arguments.get("prompt") or "").strip(),
            desired_name=scope_workspace_artifact_path(name, artifact_base_dir, preserve_leaf_default=True),
            ext=ext, fallback=f"generated-{kind}",
            default_dir=workspace_artifact_default_dir(artifact_base_dir, directory),
            entity_root=entity_root,
        )
        for ext in extensions
    )
    companions = ()
    if spec is not None and spec.video_edit_recipe:
        target, companion = prepare_video_editor_output_pair(targets[0], entity_root=entity_root)
        targets, companions = (target,), (companion,)
    return GeneratedMediaOutputPlan(
        kind, artifact_base_dir, targets,
        scope_workspace_artifact_path(name, artifact_base_dir, preserve_leaf_default=True),
        companions,
    )


def runtime_generated_media_target(
    *, kind: str, entity_id: str, workspace_base_dir: str, ext: str,
    prompt: str, output_name: str, entity_root: str,
) -> GeneratedMediaTarget:
    from packages.core.ai.runtime.authorization_receipts import (
        RuntimeToolAuthorizationWriteError,
        WorkspaceFileAuthorizationResourceFactory,
        runtime_current_tool_authorization_receipt,
    )

    receipt = runtime_current_tool_authorization_receipt()
    plan = receipt.media_output_plan if receipt else None
    if plan is not None:
        resolved_name = scope_workspace_artifact_path(output_name, workspace_base_dir, preserve_leaf_default=True)
        explicit_home_target = (
            plan.artifact_base_dir == workspace_storage_root(workspace_base_dir)
            and resolved_name.startswith(plan.artifact_base_dir + "/")
        )
        if receipt.entity_id != entity_id or plan.kind != kind or (
            plan.artifact_base_dir != workspace_base_dir and not explicit_home_target
        ):
            raise RuntimeToolAuthorizationWriteError("Generated media output scope changed after authorization")
        requested_name = resolved_name
        if requested_name != plan.requested_name:
            raise RuntimeToolAuthorizationWriteError("Generated media name changed after authorization")
        target = plan.target_for_extension(ext)
        spec = WorkspaceFileAuthorizationResourceFactory.frozen_media_output_spec(receipt.tool_name)
        if spec is not None and spec.video_edit_recipe and plan.companion_paths != (
            video_editor_recipe_path(target.rel_path),
        ):
            raise RuntimeToolAuthorizationWriteError("Video recipe output plan is missing or changed")
        # Persisted approval plans contain relative identities only. Rebinding
        # the trusted entity root must not select another name or collision suffix.
        return replace(
            target,
            abs_dir=os.path.join(entity_root, target.rel_dir),
            abs_path=os.path.join(entity_root, target.rel_path),
        )
    if receipt and WorkspaceFileAuthorizationResourceFactory.frozen_media_output_spec(receipt.tool_name):
        raise RuntimeToolAuthorizationWriteError("Media output plan is missing from this authorization")
    # Non-Workspace callers and the separate narration contract retain their
    # existing naming behavior; any active receipt still gates the atomic write.
    return build_generated_media_target(
        prompt=prompt, ext=ext, fallback=f"generated-{kind}", entity_root=entity_root,
        desired_name=scope_workspace_artifact_path(output_name, workspace_base_dir, preserve_leaf_default=True),
        default_dir=workspace_artifact_default_dir(
            workspace_base_dir, {"image": "images", "audio": "audio", "video": "videos"}[kind],
        ),
    )
