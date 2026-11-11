"""Workspace-as-code: export, validate, install, compare, promote and upgrade Blueprints.

Payloads travel as files through transfer slots (see ``transfers``), never
through tool arguments: an export returns a download URL, a local file is sent
with create_upload and referenced by its upload_id.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from apps.api.mcp import transfers
from apps.api.mcp.registry import Tool, ToolContext, ToolError, boolean, obj, require, string
from apps.api.mcp.tools_workspace import _shell_quote

_WS = "/api/v1/workspaces"
_SCRIPTS = Path(__file__).resolve().parents[3] / ".agents" / "skills" / "manor-workspace-blueprint" / "scripts"


@lru_cache(maxsize=None)
def _script(name: str):
    """The Blueprint skill's stdlib checkers, shared with the repository tooling."""
    spec = importlib.util.spec_from_file_location(f"manor_blueprint_{name}", _SCRIPTS / f"{name}.py")
    if spec is None or spec.loader is None:
        raise ToolError(f"Blueprint checker {name} is not installed on this server")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _canonical(payload: dict) -> bytes:
    return json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False).encode("utf-8") + b"\n"


async def _uploaded_bytes(ctx: ToolContext, upload_id: str) -> bytes:
    try:
        upload = await transfers.load_upload_for(upload_id, user_id=ctx.user_id, entity_id=ctx.entity_id)
    except ValueError as e:
        raise ToolError(str(e)) from None
    return upload.data


def _package_payload(data: bytes) -> tuple[dict, list]:
    """A package's payload and assets, placeholders intact (still validatable)."""
    from packages.core.blueprints.package import PackageError, unpack

    try:
        return unpack(data)
    except PackageError as e:
        raise ToolError(str(e)) from None


async def _uploaded_payload(ctx: ToolContext, upload_id: str) -> dict:
    data = await _uploaded_bytes(ctx, upload_id)
    from packages.core.blueprints.package import is_package

    if is_package(data):
        return _package_payload(data)[0]
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise ToolError("the uploaded file is not JSON") from None
    if not isinstance(payload, dict):
        raise ToolError("a Blueprint payload is a JSON object")
    return payload


async def _export(ctx: ToolContext, workspace_id: str, options: dict | None) -> dict:
    return await ctx.api.post(f"{_WS}/{workspace_id}/export-blueprint-payload", options or {})


def _summary(payload: dict) -> dict:
    manifest = payload.get("manifest") or {}
    recipe = payload.get("recipe") or {}
    embedded = payload.get("embedded") or {}
    counts = {
        f"{section}.{key}": len(value)
        for section, part in (("recipe", recipe), ("embedded", embedded))
        if isinstance(part, dict)
        for key, value in part.items() if isinstance(value, list) and value
    }
    return {
        "blueprint_version": payload.get("blueprint_version") or manifest.get("blueprint_version"),
        "title": manifest.get("title") or manifest.get("name"),
        "slug": manifest.get("slug"),
        "counts": counts,
    }


async def export_blueprint(ctx: ToolContext, args: dict) -> Any:
    ws_id = require(args, "workspace_id")
    source = args.get("format") == "source"
    if args.get("format") == "package" or source:
        data, _content_type = await ctx.api.download(
            f"{_WS}/{ws_id}/export-blueprint-package", method="POST", json_body=args.get("export_options") or {},
            layout="source" if source else "package",
        )
        if len(data) > transfers.MAX_TRANSFER_BYTES:
            raise ToolError("the package is larger than a transfer slot; export it from the Workspace page instead")
        payload, assets = _package_payload(data)
        filename = args.get("filename") or ("blueprint" if source else "blueprint.manor")
        content_type = "application/gzip"
    else:
        payload, assets = await _export(ctx, ws_id, args.get("export_options")), []
        data = _canonical(payload)
        filename = args.get("filename") or "blueprint.json"
        content_type = "application/json"
    slot = await transfers.create(
        kind=transfers.DOWNLOAD, user_id=ctx.user_id, entity_id=ctx.entity_id,
        filename=filename, content_type=content_type, data=data,
    )
    url = ctx.transfer_url(slot.id)
    command = f"curl -sS --fail -o {_shell_quote(filename)} {_shell_quote(url)}"
    if source:
        # Replace the directory only once the download succeeded, so removed
        # components leave no stale files; git shows what changed.
        folder, archive = _shell_quote(filename), _shell_quote(f"{filename}.manor.tmp")
        command = (
            f"curl -sS --fail -o {archive} {_shell_quote(url)} && rm -rf {folder} && mkdir -p {folder} "
            f"&& tar -xzf {archive} -C {folder} && rm {archive}"
        )
    return {
        "download_url": url,
        "command": command,
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "expires_in_seconds": transfers.TRANSFER_TTL_SECONDS,
        "summary": _summary(payload),
        "assets": [{"name": asset.name, "size": len(asset.data), "mime_type": asset.mime_type} for asset in assets],
        "note": (
            "A source tree: one file per component, long text as .md. Commit the folder; to install, "
            f"validate or publish it, pack it with `tar -czf blueprint.manor -C {filename} .` and send that "
            "with create_upload."
            if source else
            "The export already passed the portable payload validator. Commit the file; "
            "re-export after every live change rather than hand-editing behaviour."
        ),
    }


async def configure_knowledge_starter(ctx: ToolContext, args: dict) -> Any:
    workspace_id = require(args, "workspace_id")
    document_id = require(args, "document_id")
    if "body_md" not in args:
        raise ToolError("'body_md' is required; use null to remove the initial template")
    return await ctx.api.put(
        f"{_WS}/{workspace_id}/blueprint-export/knowledge-documents/{document_id}/initial-template",
        {"body_md": args["body_md"]},
    )


async def validate_blueprint(ctx: ToolContext, args: dict) -> Any:
    from packages.core.blueprints.payload import PayloadError, validate_payload

    payload = await _uploaded_payload(ctx, require(args, "upload_id"))
    errors: list[str] = []
    try:
        validate_payload(payload)
    except PayloadError as e:
        errors.append(str(e))
    invariants = _script("check_blueprint").check(payload)
    preflight = None
    if not errors:
        preflight = await ctx.api.post("/api/v1/blueprints/install-payload-preflight", {
            "payload": payload,
            "mode": args.get("mode") or "simulate",
            "variable_values": args.get("variable_values") or {},
        })
    return {
        "valid": not errors and not invariants,
        "payload_errors": errors,
        "invariant_errors": invariants,
        "install_preflight": preflight,
        "summary": _summary(payload),
    }


async def install_blueprint(ctx: ToolContext, args: dict) -> Any:
    upload_id = args.get("upload_id")
    source_id = args.get("source_workspace_id")
    if bool(upload_id) == bool(source_id):
        raise ToolError("pass exactly one of 'upload_id' (a local payload) or 'source_workspace_id'")
    body: dict[str, Any] = {
        "mode": args.get("mode") or "simulate",
        "workspace_name": args.get("workspace_name"),
        "variable_values": args.get("variable_values") or {},
        "create_missing_agents": bool(args.get("create_missing_agents", True)),
    }
    if upload_id:
        data = await _uploaded_bytes(ctx, upload_id)
        from packages.core.blueprints.package import is_package

        if is_package(data) and not args.get("preflight_only"):
            # The server writes the package's files into this team first.
            options = {key: value for key, value in body.items() if value is not None}
            return await ctx.api.request(
                "POST", "/api/v1/blueprints/install-package",
                files={"file": ("blueprint.manor", data, "application/gzip"),
                       "options": (None, json.dumps(options), "application/json")},
            )
        body["payload"] = _package_payload(data)[0] if is_package(data) else await _uploaded_payload(ctx, upload_id)
    else:
        # Same-account round trip: the server re-exports the source under its
        # locks and refuses a stale payload, so no paid raw-import is needed.
        options = args.get("export_options") or {}
        body["payload"] = await _export(ctx, source_id, options)
        body["workspace_source"] = {"workspace_id": source_id, "export_options": options}
    if args.get("preflight_only"):
        return await ctx.api.post("/api/v1/blueprints/install-payload-preflight", {
            "payload": body["payload"], "mode": body["mode"], "variable_values": body["variable_values"],
        })
    return await ctx.api.post("/api/v1/blueprints/install-payload", body)


async def _side(ctx: ToolContext, side: Any, name: str) -> dict:
    if not isinstance(side, dict) or bool(side.get("upload_id")) == bool(side.get("workspace_id")):
        raise ToolError(f"'{name}' needs exactly one of upload_id or workspace_id")
    if side.get("upload_id"):
        return await _uploaded_payload(ctx, side["upload_id"])
    return await _export(ctx, side["workspace_id"], side.get("export_options"))


async def compare_blueprints(ctx: ToolContext, args: dict) -> Any:
    left = await _side(ctx, args.get("left"), "left")
    right = await _side(ctx, args.get("right"), "right")
    differences = _script("compare_roundtrip").compare(left, right)
    return {"equivalent": not differences, "differences": differences}


async def promote_workspace(ctx: ToolContext, args: dict) -> Any:
    ws_id = require(args, "workspace_id")
    unmet = await ctx.api.get(f"{_WS}/{ws_id}/promote/preflight")
    if args.get("preflight_only"):
        return {"unmet": unmet}
    return await ctx.api.post(f"{_WS}/{ws_id}/promote", {"force": bool(args.get("force", False))})


_UPGRADE_ITEM_FIELDS = ("key", "kind", "slug", "name", "action", "changes", "baseline_known", "compatibility_issues")


def _upgrade_summary(preview: dict, include_content: bool) -> dict:
    """The preview without each item's full before/after JSON unless asked."""
    items = preview.get("items") or []
    if not include_content:
        items = [{field: item[field] for field in _UPGRADE_ITEM_FIELDS if field in item} for item in items]
    return {**preview, "items": items}


async def preview_workspace_upgrade(ctx: ToolContext, args: dict) -> Any:
    ws_id = require(args, "workspace_id")
    body = {key: args[key] for key in ("target_version", "variable_values", "channel_config_ids") if args.get(key)}
    preview = await ctx.api.post(f"{_WS}/{ws_id}/blueprint/upgrade/v3/preview", body)
    return _upgrade_summary(preview if isinstance(preview, dict) else {}, bool(args.get("include_content")))


async def apply_workspace_upgrade(ctx: ToolContext, args: dict) -> Any:
    ws_id = require(args, "workspace_id")
    if args.get("recommended"):
        body = {key: args[key] for key in ("target_version",) if args.get(key)}
        return await ctx.api.post(f"{_WS}/{ws_id}/blueprint/upgrade/v3/recommended", body)
    body = {
        "expected_blueprint_fingerprint": require(args, "expected_blueprint_fingerprint"),
        "expected_plan_fingerprint": require(args, "expected_plan_fingerprint"),
        "configuration_resolutions": list(args.get("configuration_resolutions") or []),
        **{key: args[key] for key in ("target_version", "variable_values", "channel_config_ids") if args.get(key)},
    }
    return await ctx.api.post(f"{_WS}/{ws_id}/blueprint/upgrade/v3", body)


async def revert_workspace_upgrade(ctx: ToolContext, args: dict) -> Any:
    return await ctx.api.post(f"{_WS}/{require(args, 'workspace_id')}/blueprint/revert", {})


async def publish_workspace_release(ctx: ToolContext, args: dict) -> Any:
    ws_id = require(args, "workspace_id")
    data = await _uploaded_bytes(ctx, require(args, "upload_id"))
    form = {key: (None, "true" if args.get(key) else "false") for key in ("dry_run", "adopt")}
    if args.get("release_notes"):
        form["release_notes"] = (None, args["release_notes"])
    return await ctx.api.request(
        "POST", f"{_WS}/{ws_id}/blueprint/releases",
        files={"file": ("blueprint", data, "application/octet-stream"), **form},
    )


_EXPORT_OPTIONS = obj(
    "ExportBlueprintPayloadRequest options: title, summary, description, usage_guide, tags, "
    "install_variables, include_* toggles (all default true), knowledge_pack_mode "
    "(skeleton | inline_text), knowledge_document_ids, align_to_installed_release (default true: "
    "a Workspace installed from a release exports in that release's order, keeps its listing copy "
    "and channel requirements it never connected, and takes its title unless one is given, so a "
    "file in git changes only where the Workspace did).",
)
_UPLOAD_ID = string("upload_id of a payload (.json) or package (.manor) sent with create_upload.")
_SIDE = {
    "type": "object",
    "description": "{upload_id} for a local payload or {workspace_id, export_options?} for a live export.",
    "properties": {"upload_id": _UPLOAD_ID, "workspace_id": string("Workspace to export."),
                   "export_options": _EXPORT_OPTIONS},
}

_TARGET_VERSION = string("A listed release of the Workspace's own Blueprint to move to, newer or older "
                         "(see available_versions); omit for the latest.")
_UPGRADE_INPUTS = {
    "variable_values": obj("Install variable values, when the target release declares new ones."),
    "channel_config_ids": obj("Channel account ids by channel key, when the target release needs a channel."),
}

_UPGRADE_TOOLS = [
    Tool("preview_workspace_upgrade", "Preview Workspace upgrade",
         "Plan moving an installed Workspace to a release of its Blueprint (latest by default, or "
         "target_version, including an older one). Nothing changes. Returns every differing item with "
         "its action (update, keep_yours, customized, missing, unchanged), the installed and target "
         "versions, available_versions, version_change (the semver step and why), the changelog, "
         "setup preflight, active_runs (an open or paused run blocks apply) and the two fingerprints "
         "apply needs. Items omit their full before/after content unless include_content=true.",
         preview_workspace_upgrade,
         properties={"workspace_id": string("Installed Workspace."), "target_version": _TARGET_VERSION,
                     **_UPGRADE_INPUTS,
                     "include_content": boolean("Include each item's current, new and baseline content.", False)},
         required=("workspace_id",), read_only=True, idempotent=True),
    Tool("apply_workspace_upgrade", "Apply Workspace upgrade",
         "Apply a reviewed preview: pass its blueprint_fingerprint and plan_fingerprint and one "
         "configuration_resolutions entry {key, resolution: keep_yours | use_blueprint} for every item "
         "whose action is not unchanged or customized (customized defaults to keep_yours). Changes the "
         "Workspace's agents, Skills, Flows and configuration; refused if anything changed since the "
         "preview or a run is open. Get the user's decision on each conflict first. "
         "recommended=true is the one-step update instead (no fingerprints or resolutions): every "
         "author change is taken and every edit the Workspace made itself is kept; a 409 "
         "upgrade_needs_review names what needs a person (active_runs, setup, variables). It also "
         "closes the update card the Workspace chat shows. One step can be undone with "
         "revert_workspace_upgrade. A reviewed update also refreshes the installed usage "
         "guide; undo restores the prior guide and refuses to overwrite later guide edits.",
         apply_workspace_upgrade,
         properties={"workspace_id": string("Installed Workspace."), "target_version": _TARGET_VERSION,
                     "expected_blueprint_fingerprint": string("blueprint_fingerprint from the preview."),
                     "expected_plan_fingerprint": string("plan_fingerprint from the preview."),
                     "configuration_resolutions": {
                         "type": "array", "description": "One choice per differing item.",
                         "items": {"type": "object", "required": ["key", "resolution"], "properties": {
                             "key": string("Item key from the preview."),
                             "resolution": {"enum": ["keep_yours", "use_blueprint"]}}}},
                     "recommended": boolean("Update in one step with the default choices; ignores the "
                                            "fingerprints and resolutions.", False),
                     **_UPGRADE_INPUTS},
         required=("workspace_id",), destructive=True),
    Tool("revert_workspace_upgrade", "Undo Workspace upgrade",
         "Put back what the most recent upgrade changed. Refused once the Workspace has been changed "
         "since that upgrade. To return to an older release later, preview it with target_version instead.",
         revert_workspace_upgrade,
         properties={"workspace_id": string("Installed Workspace.")},
         required=("workspace_id",), destructive=True),
    Tool("publish_workspace_release", "Publish Workspace release",
         "Make a Blueprint file built in any environment (a .manor package or payload exported from "
         "local, staging or git) the next release of this Workspace's own package. Nothing in the "
         "Workspace changes: it then shows the update, which preview_workspace_upgrade / "
         "apply_workspace_upgrade take through the normal review. The version moves only when "
         "installable content changed. A Workspace with no package yet (built by hand or installed "
         "before packages) needs adopt=true: its components are matched to the file's and its current "
         "content becomes the merge base. dry_run=true returns the version step, reasons and, for an "
         "adoption, which components matched; run it first and show the user. A Workspace installed "
         "from a Marketplace listing or client delivery updates from there instead.",
         publish_workspace_release,
         properties={"workspace_id": string("Workspace to receive the release."), "upload_id": _UPLOAD_ID,
                     "release_notes": string("What changed, shown with the update."),
                     "dry_run": boolean("Report the release and matches; write nothing.", False),
                     "adopt": boolean("Give a Workspace with no package this one.", False)},
         # Records a release and, with adopt, links the Workspace; its content is unchanged.
         required=("workspace_id", "upload_id"), destructive=True, idempotent=True),
]

TOOLS = [
    Tool("configure_knowledge_starter", "Configure Knowledge starter",
         "Set explicit initial Markdown or CSV content for a Workspace Knowledge file. "
         "Blueprint export and client delivery use this initial template when that file is selected, "
         "while the working file and its runtime data remain unchanged. Pass body_md=null to remove "
         "the template. Requires Workspace management and document edit access; ordinary runtime "
         "artifacts remain excluded. Review the initial content before configuring it.",
         configure_knowledge_starter,
         properties={"workspace_id": string("Workspace owning the Knowledge file."),
                     "document_id": string("Exact Knowledge document id."),
                     "body_md": {"type": ["string", "null"], "description": "Explicit safe initial text, or null to remove the template."}},
         required=("workspace_id", "document_id", "body_md"), idempotent=True, destructive=True),
    Tool("export_blueprint", "Export Blueprint",
         "Export a Workspace's portable configuration as a validated Blueprint payload. Returns a "
         "30-minute download URL and a curl command that writes it to a file in your repo. "
         "format=package exports a .manor file that also carries the Workspace's own files its App "
         "and Skills point at (uploaded covers, motion covers, choice pictures and samples, Skill "
         "scripts and templates), so another account sees the same App; Knowledge files and run "
         "data never travel. format=source is the same package as a folder for git — one file per "
         "Workflow, Skill and Agent, prompts and other long text as .md — so people editing different "
         "components or paragraphs merge cleanly; the command replaces the folder. Every tool that takes "
         "an upload reads a packed folder (tar -czf blueprint.manor -C <folder> .).",
         export_blueprint,
         properties={"workspace_id": string("Workspace to export."),
                     "export_options": _EXPORT_OPTIONS,
                     "format": {"enum": ["json", "package", "source"], "default": "json",
                                "description": "json (a payload), package (.manor with its files) or "
                                               "source (that package as a folder for git)."},
                     "filename": string("Local file or, for source, folder name for the command; default "
                                        "blueprint.json, blueprint.manor or blueprint.")},
         required=("workspace_id",), read_only=True),
    Tool("validate_blueprint", "Validate Blueprint",
         "Validate an uploaded Blueprint payload or .manor package: portable-payload rules, cross-section invariants, "
         "and the install preflight (missing integrations, channels, variables). Validation shows "
         "shape and installability, not runtime parity; use compare_blueprints for that.",
         validate_blueprint,
         properties={"upload_id": _UPLOAD_ID,
                     "mode": {"enum": ["simulate", "live"], "default": "simulate"},
                     "variable_values": obj("Install variable values.")},
         required=("upload_id",), read_only=True, idempotent=True),
    Tool("install_blueprint", "Install Blueprint",
         "Install a Blueprint as a new Workspace in this team: from an uploaded payload or .manor "
         "package (its files are stored in this team first), or from "
         "source_workspace_id (a verified same-account round trip). mode=simulate (default) "
         "builds a sandboxed simulation Workspace; live creates a working one and may start "
         "configured automations that modify data, send messages, or call external services. Missing integrations stay declared and the Workspace asks its installer to connect them before that work runs. "
         "preflight_only=true reports setup requirements without installing.",
         install_blueprint,
         properties={"upload_id": _UPLOAD_ID,
                     "source_workspace_id": string("Re-export this Workspace and install the result."),
                     "export_options": _EXPORT_OPTIONS,
                     "mode": {"enum": ["simulate", "live"], "default": "simulate"},
                     "workspace_name": string("Name of the new Workspace."),
                     "variable_values": obj("Install variable values."),
                     "create_missing_agents": boolean("Create agents the Blueprint needs.", True),
                     "preflight_only": boolean("Only report setup requirements.", False)},
         destructive=True, open_world=True),
    Tool("compare_blueprints", "Compare Blueprints",
         "Semantic round-trip comparison of two Blueprints (uploaded files or live exports), "
         "ignoring generated ids and timestamps. Use it to prove an installed Workspace matches "
         "the source, or to see what changed against the committed file.",
         compare_blueprints,
         properties={"left": _SIDE, "right": _SIDE},
         required=("left", "right"), read_only=True, idempotent=True),
    Tool("promote_workspace", "Promote Workspace",
         "Promote a simulation Workspace to live after its preflight passes. preflight_only=true "
         "lists unmet requirements; force=true promotes despite them.",
         promote_workspace,
         properties={"workspace_id": string("Simulation Workspace id."),
                     "preflight_only": boolean("Only list unmet requirements.", False),
                     "force": boolean("Promote despite unmet requirements.", False)},
         required=("workspace_id",), destructive=True),
    *_UPGRADE_TOOLS,
]
