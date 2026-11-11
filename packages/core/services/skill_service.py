"""Skill service — CRUD, listing, and invocation."""
from __future__ import annotations

import asyncio
import logging
import json
import hashlib
import posixpath
import re
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.ai.runtime import (
    runtime_binding_owner_matches,
    runtime_public_failure_payload,
    runtime_skill_binding_ref,
)
from packages.core.ai.runtime.sandbox_toolset import sandbox_toolset_summary
from packages.core.database_runtime import release_read_transaction
from packages.core.models.base import generate_ulid
from packages.core.models.permission import Visibility
from packages.core.models.skill import Skill, AgentSkillBinding
from packages.core.services.reusable_resource_locks import (
    lock_agent_skill_binding_references,
)
from packages.core.services.sandbox_session_service import (
    SandboxBinding,
    SandboxBundle,
    SandboxOwner,
    SandboxSession,
    SandboxSessionError,
    acquire_sandbox_session,
    attach_durable_reservation,
)

logger = logging.getLogger(__name__)

DEFAULT_SKILL_MAX_ROUNDS = 200
INSTRUCTIONS_ONLY_SKILL_EXECUTION_MODE = "instructions_only"

# Script extensions that indicate a sandbox skill when found in skill_dir
_SCRIPT_EXTENSIONS = {".py", ".sh", ".bash", ".js", ".ts", ".rb"}


def _sandbox_skill_error_response(
    skill: Skill,
    message: str,
    *,
    code: str | None = None,
    retryable: bool = False,
) -> dict:
    """Return a structured sandbox-skill failure that the tool layer can surface."""
    response = {
        "skill": skill.name,
        "content": message,
        "usage": {},
        "tools_used": [],
        "rounds": 0,
        "stop_reason": "error",
        "error": message,
        "retryable": bool(retryable),
    }
    if code:
        response["code"] = code
    return response




SKILL_DEPENDENCIES_UNAVAILABLE = "skill_dependencies_unavailable"


def skill_tools_optional(skill: Skill) -> bool:
    """``config.runtime.tools_optional``: the Skill may run without any declared tool."""
    config = getattr(skill, "config", None) or {}
    runtime = config.get("runtime") if isinstance(config, dict) else None
    return isinstance(runtime, dict) and runtime.get("tools_optional") is True


def _skill_dependency_refusal(
    skill: Skill, tool_surface: Any, *, runtime_envelope: Any | None = None,
) -> dict | None:
    """Refuse a prompt Skill none of whose declared tools can execute in this scope.

    A child loop without any of its tools can only describe work it never did.
    Skills without declared tools, instructions-only guides (handled before
    this) and ``tools_optional`` Skills are unaffected. Bundle readers added
    for extra files are not the Skill's declared work tools.
    """
    from packages.core.ai.runtime.integration_setup_links import runtime_integration_setup_link
    from packages.core.ai.runtime.skills import runtime_skill_executability_from_surface
    from packages.core.ai.runtime.tool_discovery import runtime_mcp_provider_from_tool_name

    if skill_tools_optional(skill):
        return None
    executability = runtime_skill_executability_from_surface(
        skill, tool_surface, runtime_envelope=runtime_envelope,
    )
    if executability.executable:
        return None
    missing = list(executability.missing_tools)
    providers = list(dict.fromkeys(
        provider for name in missing
        if name.startswith("mcp__") and (provider := runtime_mcp_provider_from_tool_name(name))
    ))
    setup = runtime_integration_setup_link(providers[0]) if providers else {}
    slug = skill.slug or skill.name
    steps = []
    if setup.get("setup_url"):
        steps.append(f"ask the user to connect the integration at {setup['setup_url']}")
    steps += [
        "acquire the tools with acquire_capabilities if this run offers it",
        "delegate to an agent that has them",
        "or tell the user this capability is unavailable here",
    ]
    refusal = {
        "error": (
            f"Skill '{slug}' was not run: none of its declared tools can execute "
            "in this agent's current scope."
        ),
        "code": SKILL_DEPENDENCIES_UNAVAILABLE,
        "missing_tools": missing,
        "instruction": (
            "Nothing was executed. To proceed, " + "; ".join(steps)
            + ". Do not claim this Skill's work was done."
        ),
    }
    if setup.get("setup_url"):
        refusal["setup_url"] = setup["setup_url"]
    logger.info(
        "skill.dependencies_unavailable %s",
        {"skill": slug, "missing_tools": missing, "providers": providers},
    )
    return refusal


def _instructions_only_skill_response(skill: Skill, instructions: str) -> dict:
    """Return guidance to the calling Agent without starting a child executor."""

    return {
        "skill": skill.name,
        "content": str(instructions or "").strip(),
        "usage": {},
        "tools_used": [],
        "rounds": 0,
        "stop_reason": "instructions_loaded",
        "instructions_only": True,
    }


def _compact_skill_items(items: list[str] | tuple[str, ...] | set[str], *, limit: int = 12) -> str:
    values = [str(item) for item in items if str(item).strip()]
    if not values:
        return "(none)"
    values = sorted(values)
    shown = values[:limit]
    suffix = f", ... (+{len(values) - limit} more)" if len(values) > limit else ""
    return ", ".join(shown) + suffix


def _sandbox_skill_runtime_contract(
    *,
    skill: Skill,
    sandbox_id: str,
    skill_info_parts: list[str],
    expected_workspace_volume: str | None,
    skill_instructions: str,
    skill_input: str,
) -> str:
    lines = [
        f"✓ Sandbox ready for skill **{skill.name}**",
        "",
        *skill_info_parts,
        "All required credentials are pre-injected as environment variables.",
        "",
        "## Runtime Skill Execution Contract",
        "- The complete `/skill/SKILL.md` is provided below and is authoritative for this skill execution.",
        "- Follow the packaged skill workflow, scripts, templates, references, gates, and quality checks described by `/skill/SKILL.md`.",
        "- Do not replace the skill workflow with an ad-hoc generator or direct output script unless `/skill/SKILL.md` explicitly instructs that route.",
        "- Helper files may only support the workflow described by `/skill/SKILL.md`; they must not substitute a different output pipeline.",
        "- `sandbox` action `exec` is available for commands, but the expected path is to run bundled skill scripts or explicit helper files.",
        "- Before saving a final artifact, verify that the artifact path, intermediate evidence, and quality gates match `/skill/SKILL.md`.",
        "- If a required dependency, script, or workflow gate is missing, stop and report the blocker instead of inventing a shortcut.",
        "- This handoff is for you, not the user: complete the workflow and save the final artifact with `sandbox` action `save_result` in this same turn before replying. Do not answer the user with a progress or \"started\" message in between.",
        "- Think step by step: before the first command, work out every step `/skill/SKILL.md` requires from setup through the saved final artifact (`save_result`), then run them in order and check each step's output before starting the next. Keep this reasoning private; user-visible progress stays one short sentence.",
        "",
    ]
    lines.extend([
        "## Sandbox Environment",
        "- All skill files are mounted at `/skill/` (working directory). `/skill` is this Skill's own "
        "directory in the user's shared sandbox: other Skills and other conversations of the user may "
        "run beside it, but only your commands see this Skill and its credentials.",
        (
            "- The entity filesystem is mounted read-only at `/workspace/`. "
            "Uploaded chat files are available under `/workspace/uploads/chat/...`."
            if expected_workspace_volume
            else (
                "- No entity filesystem mount is available in this sandbox: there is no "
                "`/workspace/` directory and the root filesystem is read-only. "
                "Use `sandbox` action `write_file` with `workspace_path` or direct content "
                "to inject required inputs before execution."
            )
        ),
        "- Write every output under `/skill/` (the working directory); `/tmp/` is the only other writable path.",
        "- Never use `/mnt/user-data` for Manor uploads. It is not the upload mount.",
        "- Read skill files with `sandbox(action='read_file', params={'sandbox_id': ..., 'path':'/skill/SKILL.md'})` or targeted `exec` commands.",
        "- Write new files with `sandbox` action `write_file`; do not use `cat >`, `echo >`, `printf >`, `tee >`, or heredoc writes.",
        "- Do not use host shell/file tools such as `bash`, `read_file`, or root filesystem searches to inspect sandbox or workspace files.",
        "- `/tmp/` is writable for temporary files.",
        # Generated from the image's toolset manifest, which the release verifies.
        sandbox_toolset_summary(),
        "",
        "## Response Language",
        "- Match the user's original language for all user-visible narration, status updates, and the final answer.",
        "- If the user wrote in Chinese, keep progress messages and summaries in Chinese even when skill files are written in English.",
        "- Tool commands, code, filenames, and API identifiers may remain in their required language.",
        "",
        "## Skill Input",
        skill_input or "Use the latest user request and conversation context as the skill input.",
        "",
        "## Next Tool Guidance",
        f"- Use `sandbox(action='exec', params={{'sandbox_id':'{sandbox_id}','command':'...'}})` or action `read_file` for additional targeted inspection when needed.",
        "- For long text files, paginate `read_file` with `offset` and `limit`; continue from `next_offset` instead of rereading the whole file.",
        "- Run the bundled scripts/workflow required by `/skill/SKILL.md`.",
        (
            "- After the final artifact exists, call `sandbox` action `save_result` with "
            "`artifact_role=\"final\"` so Chat receives a clickable file card; use "
            "`artifact_role=\"intermediate\"` for supporting files that should stay hidden. "
            "Deliver the file with the `markdown_link` of the returned `file`; a `/skill/` "
            "path is not a delivered file."
        ),
        "- Call `sandbox` action `destroy` once after the final artifact has been saved to release the sandbox for reuse; the container stays warm for this user's next request.",
        "",
        "## Skill Instructions",
        "The following is the complete `/skill/SKILL.md` loaded for this run. Follow it exactly.",
        "",
        skill_instructions,
    ])
    return "\n".join(lines)


def _slugify(name: str) -> str:
    """Convert a skill name to a URL-safe slug."""
    slug = name.lower().strip()
    slug = re.sub(r"[^a-z0-9]+", "_", slug)
    return slug.strip("_")


def _normalize_requested_slug(value: object) -> str:
    """Normalize an explicit slug while preserving URL-safe hyphens."""
    slug = str(value or "").lower().strip()
    slug = re.sub(r"[^a-z0-9_-]+", "-", slug)
    slug = re.sub(r"-+", "-", slug)
    slug = re.sub(r"_+", "_", slug)
    return slug.strip("-_")


def skill_slug_lookup_key(value: object) -> str:
    """Return the stable lookup key shared by hyphenated and underscored slugs."""
    return _normalize_requested_slug(value).replace("-", "_")


_PLACEHOLDER_SKILL_IDENTIFIERS = {
    "unknown",
    "unnamed",
    "unnamed_skill",
    "untitled",
    "untitled_skill",
    "general_assistant",
    "placeholder",
    "placeholder_skill",
}


def _normalize_skill_identifier(value: object) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"[^a-z0-9]+", "_", text)
    return text.strip("_")


def is_placeholder_skill_identifier(value: object) -> bool:
    """Return true for model/import placeholders that should not become slugs."""
    return _normalize_skill_identifier(value) in _PLACEHOLDER_SKILL_IDENTIFIERS


def _fallback_skill_name(
    *,
    skill_id: str,
    name: object,
    description: object = "",
    system_prompt: object = "",
    category: object = "",
) -> str:
    raw_name = str(name or "").strip()
    if raw_name and not is_placeholder_skill_identifier(raw_name):
        return raw_name[:100]

    candidates = [description, system_prompt, category]
    for value in candidates:
        text = str(value or "").strip()
        if not text:
            continue
        for line in text.splitlines():
            cleaned = line.strip().lstrip("#-*0123456789. )\t").strip()
            if not cleaned or is_placeholder_skill_identifier(cleaned):
                continue
            if cleaned.lower().startswith(("you are ", "use this skill when ")):
                continue
            if len(cleaned) > 80:
                cleaned = cleaned[:80].rsplit(" ", 1)[0].strip() or cleaned[:80]
            if cleaned:
                return cleaned[:100]

    return f"Generated Skill {skill_id[-6:]}"


async def _unique_skill_slug(
    db: AsyncSession,
    *,
    entity_id: Optional[str],
    base_slug: str,
    skill_id: str,
    exclude_id: Optional[str] = None,
    explicit_slug: bool = False,
) -> str:
    slug = (
        _normalize_requested_slug(base_slug)
        if explicit_slug
        else _slugify(base_slug)
    ) or f"skill_{skill_id[-8:].lower()}"
    if is_placeholder_skill_identifier(slug):
        slug = f"skill_{skill_id[-8:].lower()}"

    conditions = [Skill.slug == slug, Skill.status == "active"]
    if entity_id:
        conditions.append(Skill.entity_id == entity_id)
    else:
        conditions.append(Skill.entity_id.is_(None))
    existing = await db.execute(select(Skill.id).where(*conditions).limit(1))
    found = existing.scalar_one_or_none()
    if found is None or (exclude_id and found == exclude_id):
        return slug

    base = slug
    for idx in range(2, 100):
        candidate = f"{base}_{idx}"
        conditions = [Skill.slug == candidate, Skill.status == "active"]
        if entity_id:
            conditions.append(Skill.entity_id == entity_id)
        else:
            conditions.append(Skill.entity_id.is_(None))
        result = await db.execute(select(Skill.id).where(*conditions).limit(1))
        found = result.scalar_one_or_none()
        if found is None or (exclude_id and found == exclude_id):
            return candidate
    return f"{base}_{skill_id[-6:].lower()}"


def _normalize_skill_bundle_path(path: object) -> str | None:
    raw = str(path or "").strip().replace("\\", "/")
    if not raw:
        return ""
    if ".." in raw.lstrip("/").split("/"):
        return None
    rel = posixpath.normpath(raw.lstrip("/"))
    if rel in ("", "."):
        return ""
    if rel == ".." or rel.startswith("../") or "/../" in f"/{rel}/":
        return None
    return rel


def _skill_bundle_claims_path(extra_files: dict[str, str], rel_path: str | None) -> bool:
    if not extra_files or rel_path is None:
        return False
    if not rel_path:
        return False
    top = rel_path.split("/", 1)[0]
    return any(path == top or path.startswith(f"{top}/") for path in extra_files)


def _skill_bundle_file_json(path: str, content: str, args: dict) -> str:
    source_sha = hashlib.sha256(content.encode("utf-8")).hexdigest()
    expected_sha = str(args.get("expected_sha256") or "").strip()
    if expected_sha and expected_sha != source_sha:
        return json.dumps({
            "error": "source_changed", "path": path,
            "expected_sha256": expected_sha, "source_sha256": source_sha,
        })
    lines = content.splitlines(keepends=True)
    try:
        offset = min(len(lines), max(0, int(args.get("offset") or 0)))
        limit = max(1, int(args["limit"])) if args.get("limit") is not None else len(lines)
        max_chars = (
            min(200_000, max(1, int(args["max_chars"])))
            if args.get("max_chars") is not None else len(content)
        )
        if args.get("char_offset") is not None:
            mode = "char"
            start = min(len(content), max(0, int(args["char_offset"])))
            scope_end = len(content)
        else:
            mode = "line"
            start = sum(len(line) for line in lines[:offset])
            scope_end = start + sum(len(line) for line in lines[offset:offset + limit])
    except (TypeError, ValueError, OverflowError):
        return json.dumps({"error": "File slice offsets and limits must be integers", "path": path})
    end = min(scope_end, start + max_chars)
    selected = content[start:end]
    partial_line = bool(selected) and end < len(content) and not selected.endswith("\n")
    next_offset = (
        offset + len(selected.splitlines())
        if mode == "line" and not partial_line and end < len(content) else None
    )
    return json.dumps({
        "path": path,
        "source": "skill_bundle",
        "size": len(content.encode("utf-8")),
        "source_sha256": source_sha,
        "slice_sha256": hashlib.sha256(selected.encode("utf-8")).hexdigest(),
        "total_lines": len(lines),
        "total_chars": len(content),
        "mode": mode,
        "offset": offset,
        "char_offset": start,
        "lines_returned": len(selected.splitlines()),
        "next_offset": next_offset,
        "next_char_offset": end if end < len(content) else None,
        "partial_line": partial_line,
        "truncated": end < len(content),
        "char_truncated": end < scope_end,
        "line_truncated": scope_end < len(content),
        "content": selected,
    }, ensure_ascii=False)


def _merge_agent_skill_binding_config(
    current: dict[str, Any] | None,
    incoming: dict[str, Any] | None,
) -> dict[str, Any]:
    """Merge binding provenance without losing earlier workspace contexts."""
    merged: dict[str, Any] = dict(current or {})
    payload = dict(incoming or {})
    if not payload:
        return merged

    incoming_contexts = payload.pop("contexts", None)
    if incoming_contexts is None:
        incoming_contexts = [payload]
    elif isinstance(incoming_contexts, dict):
        incoming_contexts = [incoming_contexts]
    elif not isinstance(incoming_contexts, list):
        incoming_contexts = []

    contexts: list[dict[str, Any]] = [
        dict(item)
        for item in (merged.get("contexts") or [])
        if isinstance(item, dict)
    ]
    seen = {
        json.dumps(item, sort_keys=True, default=str)
        for item in contexts
    }
    for raw_context in incoming_contexts:
        if not isinstance(raw_context, dict):
            continue
        context = {k: v for k, v in raw_context.items() if v not in (None, "", [], {})}
        if not context:
            continue
        key = json.dumps(context, sort_keys=True, default=str)
        if key in seen:
            continue
        contexts.append(context)
        seen.add(key)
    if contexts:
        merged["contexts"] = contexts[-20:]

    for key, value in payload.items():
        if value not in (None, "", [], {}):
            merged[key] = value
    return merged


def _try_read_skill_bundle_file(extra_files: dict[str, str], args: dict) -> str | None:
    rel_path = _normalize_skill_bundle_path(args.get("path", ""))
    if rel_path is None:
        return json.dumps({"error": "Path traversal detected"})
    if not rel_path:
        return None
    if rel_path in extra_files:
        return _skill_bundle_file_json(rel_path, extra_files[rel_path], args)
    if _skill_bundle_claims_path(extra_files, rel_path):
        return json.dumps({"error": f"File not found in skill bundle: {args.get('path', '')}"})
    return None


def _try_list_skill_bundle_files(extra_files: dict[str, str], args: dict) -> str | None:
    rel_path = _normalize_skill_bundle_path(args.get("path", ""))
    if rel_path is None:
        return json.dumps({"error": "Path traversal detected"})
    if not rel_path:
        return None
    prefix = f"{rel_path}/"
    if rel_path in extra_files or not any(path.startswith(prefix) for path in extra_files):
        if _skill_bundle_claims_path(extra_files, rel_path):
            return json.dumps({"error": f"Directory not found in skill bundle: {args.get('path', '')}"})
        return None

    recursive = bool(args.get("recursive", False))
    entries_by_path: dict[str, dict] = {}
    for path, content in sorted(extra_files.items()):
        if not path.startswith(prefix):
            continue
        rest = path[len(prefix):]
        if not rest:
            continue
        if recursive:
            entries_by_path[path] = {
                "path": path,
                "type": "file",
                "size": len(content.encode("utf-8")),
                "source": "skill_bundle",
            }
            continue
        first, sep, _remaining = rest.partition("/")
        child_path = f"{rel_path}/{first}"
        if sep:
            entries_by_path.setdefault(child_path, {
                "path": child_path,
                "type": "dir",
                "size": None,
                "source": "skill_bundle",
            })
        else:
            entries_by_path[child_path] = {
                "path": child_path,
                "type": "file",
                "size": len(content.encode("utf-8")),
                "source": "skill_bundle",
            }

    entries = list(entries_by_path.values())
    return json.dumps({
        "count": len(entries),
        "limit": len(entries),
        "offset": 0,
        "next_offset": None,
        "has_more": False,
        "total": len(entries),
        "source": "skill_bundle",
        "entries": entries,
    }, ensure_ascii=False)


def _load_prompt_skill_extra_files(skill: Skill, config: dict) -> dict[str, str]:
    """Load prompt skill bundled files from config, disk, or MinIO."""
    extra_files: dict[str, str] = {}
    raw_extra = config.get("extra_files") or {}
    if isinstance(raw_extra, dict):
        extra_files.update({
            str(path).replace("\\", "/"): str(content)
            for path, content in raw_extra.items()
            if str(path).strip()
        })

    if not skill.entity_id:
        skill_dir_value = str(config.get("skill_dir") or "").strip()
        raw_roots = config.get("bundle_roots") or []
        bundle_roots = (
            list(raw_roots)
            if isinstance(raw_roots, (list, tuple, set))
            else [raw_roots]
        )
        if skill_dir_value and bundle_roots:
            from packages.core.services.builtin_skill_loader import resolve_builtin_skill_dir

            skill_dir = resolve_builtin_skill_dir(skill_dir_value).resolve()
            for raw_root in bundle_roots:
                rel_root = _normalize_skill_bundle_path(raw_root)
                if not rel_root:
                    continue
                source_root = (skill_dir / rel_root).resolve()
                if skill_dir not in source_root.parents or not source_root.exists():
                    continue
                candidates = (
                    [source_root]
                    if source_root.is_file()
                    else sorted(path for path in source_root.rglob("*") if path.is_file())
                )
                for path in candidates:
                    resolved = path.resolve()
                    if skill_dir not in resolved.parents:
                        continue
                    rel_path = resolved.relative_to(skill_dir).as_posix()
                    try:
                        extra_files[rel_path] = resolved.read_text(encoding="utf-8")
                    except (OSError, UnicodeDecodeError):
                        logger.debug(
                            "[skill_service] Skipped unreadable built-in bundle file skill=%s path=%s",
                            skill.id,
                            rel_path,
                        )

    if skill.entity_id:
        try:
            from packages.core.services.skill_file_storage import load_skill_extra_files
            minio_extra = load_skill_extra_files(
                skill.entity_id,
                skill.id,
                skill_dir=config.get("minio_dir") or None,
                config=config,
            )
            if minio_extra:
                extra_files.update(minio_extra)
        except Exception as exc:
            logger.debug("[skill_service] MinIO extra file load failed skill=%s: %s", skill.id, exc)
    return extra_files


def _append_skill_bundle_manifest(
    prompt: str,
    extra_files: dict[str, str],
    *,
    instructions_only: bool = False,
) -> str:
    if not extra_files:
        return prompt
    paths = sorted(extra_files)
    if instructions_only:
        # These instructions are handed back to the caller, whose read_file
        # serves only the caller's own bundle. Paths the instructions already
        # name are not repeated: the hand-off must stay within one nested
        # tool result.
        unnamed = [path for path in paths if f"`{path}`" not in prompt]
        lines = [
            "",
            "## Bundled Skill Files",
            "This skill's bundled files are not workspace files and `read_file` "
            "cannot open them. To load one, call `invoke_skill` for this skill "
            "again with `bundle_file` set to its exact relative path"
            + (" (listed above)." if not unnamed else ":"),
            *[f"- `{path}`" for path in unnamed[:80]],
        ]
        if len(unnamed) > 80:
            lines.append(f"- ... {len(unnamed) - 80} more bundled file(s)")
        return prompt.rstrip() + "\n\n" + "\n".join(lines)
    listed = paths[:80]
    lines = [
        "",
        "## Bundled Skill Files",
        "The following files are bundled with this skill. When you need them, "
        "call `read_file` or `list_files` with these exact relative paths; "
        "they are not user workspace files.",
        *[f"- `{path}`" for path in listed],
    ]
    if len(paths) > len(listed):
        lines.append(f"- ... {len(paths) - len(listed)} more bundled file(s)")
    return prompt.rstrip() + "\n\n" + "\n".join(lines)


def _instructions_only_reference_response(
    skill: Skill,
    extra_files: dict[str, str],
    reference: object,
    char_offset: object = None,
) -> dict:
    """Hand one bundled file of an instructions-only skill back to the caller.

    Only files already loaded into the skill's bundle (its ``bundle_roots``
    for built-ins) are addressable; the path is normalised and traversal is
    refused. The payload is a paged text read, so a nested loop that compacts
    it keeps a truthful ``next_char_offset`` the caller can pass back.
    """
    name = skill.slug or skill.name
    available = ", ".join(f"`{path}`" for path in sorted(extra_files)) or "(none)"
    rel_path = _normalize_skill_bundle_path(reference)
    if rel_path is None or not rel_path:
        return {
            "error": (
                f"Invalid bundle_file {str(reference or '')!r} for skill '{name}'. "
                f"Use an exact bundled path: {available}"
            ),
            "code": "skill_reference_invalid",
        }
    if rel_path not in extra_files:
        return {
            "error": (
                f"Skill '{name}' has no bundled file {rel_path!r}. "
                f"Available files: {available}"
            ),
            "code": "skill_reference_not_found",
        }
    content = extra_files[rel_path]
    # Like an empty reference, an unset, empty or negative offset means the start.
    try:
        offset = max(0, int(char_offset)) if char_offset not in (None, "") else 0
    except (TypeError, ValueError):
        offset = 0
    if offset > len(content):
        return {
            "error": (
                f"char_offset must be between 0 and {len(content)} for {rel_path!r}."
            ),
            "code": "skill_reference_invalid",
        }
    payload = {
        "skill": name,
        "bundle_file": rel_path,
        "source": "skill_bundle",
        "total_chars": len(content),
        "char_offset": offset,
        "next_char_offset": None,
        "content": content[offset:],
    }
    return _instructions_only_skill_response(skill, json.dumps(payload, ensure_ascii=False))


# ── List ──

def builtin_skill_copy_sources(skills: list[Skill]) -> dict[str, str]:
    """Identify legacy installs by exact built-in provenance, never by name.

    Catalogs project these rows onto the platform Skill. Keep the persisted
    copies and their binding IDs available to authorization and old references.
    """
    builtin_ids = {
        skill.id for skill in skills
        if skill.entity_id is None and skill.status == "active"
        and (getattr(skill, "config", None) or {}).get("source") == "builtin"
    }
    return {
        skill.id: source_id
        for skill in skills
        if skill.entity_id is not None
        and skill.status == "active"
        and (getattr(skill, "config", None) or {}).get("source") == "marketplace"
        and isinstance(source_id := (getattr(skill, "config", None) or {}).get("source_skill_id"), str)
        and source_id in builtin_ids
    }


# Built-in Skills ship inside the image; the post-deploy publish job (or API
# startup on single-owner deployments) writes them. Listing Skills runs on every chat turn, and
# a full seed walks ~13k bundle files and issues one query per built-in (~100
# round trips). The packaged files cannot change during a process, so verify
# once per process and database, then never again.
_builtin_seed_verified: set[int] = set()


def reset_builtin_skill_seed_cache() -> None:
    _builtin_seed_verified.clear()


def _builtin_seed_cache_key(db: AsyncSession) -> int:
    bind = db.get_bind()
    return id(getattr(bind, "engine", bind))


async def _seed_builtin_skills(db: AsyncSession) -> None:
    """Seed platform built-in skills (idempotent, best-effort)."""
    from packages.core.services.builtin_skill_loader import (
        BUILTIN_SKILL_SEED_WROTE_KEY,
        seed_builtin_skills,
    )

    key = _builtin_seed_cache_key(db)
    if key in _builtin_seed_verified:
        return
    try:
        # A failed best-effort seed must not roll back the request transaction.
        # Session.rollback() expires authenticated ORM rows and can trigger
        # MissingGreenlet when the route later reads their scalar attributes.
        async with db.begin_nested():
            await seed_builtin_skills(db)
    except Exception:
        logger.warning("Built-in skill seed skipped", exc_info=True)
        return
    # Rows written here are not durable until the caller commits; verify again
    # next time instead of trusting a write that may still roll back.
    if not db.info.pop(BUILTIN_SKILL_SEED_WROTE_KEY, False):
        _builtin_seed_verified.add(key)


async def list_skills(
    db: AsyncSession, entity_id: str, *, category: Optional[str] = None,
) -> list[Skill]:
    """List all skills visible to an entity: platform built-ins + entity's own.

    Seeds built-in platform skills (docx/pdf/pptx/xlsx) from disk if they
    aren't already present. Idempotent — subsequent calls skip re-seeding
    when on-disk version matches the DB row.
    """
    await _seed_builtin_skills(db)

    conditions = [
        or_(Skill.entity_id == entity_id, Skill.entity_id.is_(None)),
        Skill.status == "active",
    ]
    if category:
        conditions.append(Skill.category == category)
    result = await db.execute(
        select(Skill).where(*conditions).order_by(Skill.created_at.desc(), Skill.id)
    )
    return list(result.scalars().all())


async def _workspace_operation_skill_ids(
    db: AsyncSession,
    *,
    entity_id: str,
    agent_id: str,
    workspace_id: str | None,
    agent_subscription_id: str | None = None,
) -> set[str]:
    if not workspace_id:
        return set()
    from packages.core.constants.agents import is_master_agent
    from packages.core.models.workspace import AgentSubscription, Workspace

    workspace = (await db.execute(
        select(Workspace).where(
            Workspace.id == workspace_id,
            Workspace.entity_id == entity_id,
        )
    )).scalar_one_or_none()
    if not workspace:
        return set()

    operating_model = workspace.operating_model if isinstance(workspace.operating_model, dict) else {}
    raw_bindings = operating_model.get("skill_bindings") or []
    bindings = [
        dict(row)
        for row in raw_bindings
        if isinstance(row, dict) and row.get("enabled") is not False
    ]
    if not bindings:
        return set()

    subs = list((await db.execute(
        select(AgentSubscription).where(
            AgentSubscription.workspace_id == workspace_id,
            AgentSubscription.status == "active",
        )
    )).scalars().all())
    current_subscription = next(
        (
            sub for sub in subs
            if agent_subscription_id
            and sub.id == agent_subscription_id
            and sub.agent_id == agent_id
        ),
        None,
    )
    subscription_scope_supplied = bool(agent_subscription_id)
    service_keys = (
        {str(current_subscription.service_key).strip()}
        if current_subscription
        and str(current_subscription.service_key or "").strip()
        else set()
        if subscription_scope_supplied
        else {
            str(sub.service_key or "").strip()
            for sub in subs
            if sub.agent_id == agent_id and str(sub.service_key or "").strip()
        }
    )
    subscription_agent_ids_by_id = {sub.id: sub.agent_id for sub in subs}
    is_master = is_master_agent(agent_id, None)

    refs: set[str] = set()
    for binding in bindings:
        if runtime_binding_owner_matches(
            binding,
            agent_id=agent_id,
            is_master=is_master,
            current_service_keys=service_keys,
            task_service_keys=None,
            subscription_agent_ids_by_id=subscription_agent_ids_by_id,
            current_subscription_id=(
                current_subscription.id
                if current_subscription
                else ""
                if subscription_scope_supplied
                else None
            ),
        ):
            ref = runtime_skill_binding_ref(binding)
            if ref:
                refs.add(ref)
    if not refs:
        return set()

    rows = list((await db.execute(
        select(Skill).where(
            Skill.status == "active",
            or_(Skill.entity_id == entity_id, Skill.entity_id.is_(None)),
            or_(Skill.id.in_(refs), Skill.slug.in_(refs), Skill.name.in_(refs)),
        )
    )).scalars().all())
    return {skill.id for skill in rows}


async def list_skills_for_agent(
    db: AsyncSession,
    entity_id: str,
    agent_id: str,
    *,
    workspace_id: str | None = None,
    agent_subscription_id: str | None = None,
) -> list[Skill]:
    """Skills the given agent may use.

    Includes:
    - Platform built-ins (entity_id IS NULL, is_public=True)
    - Entity's public skills (entity_id=entity_id, is_public=True)
    - Entity's private skills explicitly bound to this agent via AgentSkillBinding

    This is the correct set to inject into the agent's context prompt AND to
    validate at invoke_skill time.
    """
    await _seed_builtin_skills(db)

    # Collect the agent's explicit private-skill bindings
    bound_result = await db.execute(
        select(AgentSkillBinding.skill_id).where(
            AgentSkillBinding.agent_id == agent_id,
            AgentSkillBinding.status == "active",
        )
    )
    bound_ids = set(bound_result.scalars().all())
    bound_ids.update(await _workspace_operation_skill_ids(
        db,
        entity_id=entity_id,
        agent_id=agent_id,
        workspace_id=workspace_id,
        agent_subscription_id=agent_subscription_id,
    ))

    # Build the combined filter
    accessibility = or_(
        Skill.entity_id.is_(None),                                          # platform built-ins
        and_(Skill.entity_id == entity_id, Skill.is_public.is_(True)),      # public entity skills
        and_(Skill.entity_id == entity_id, Skill.id.in_(bound_ids)),        # agent-bound/workspace-bound private
    )
    result = await db.execute(
        select(Skill)
        .where(accessibility, Skill.status == "active")
        .order_by(Skill.created_at.desc(), Skill.id.asc())
    )
    return list(result.scalars().all())


async def list_public_skills(
    db: AsyncSession, *, category: Optional[str] = None,
) -> list[Skill]:
    """List public skills (skill store) — excludes platform built-ins (entity_id IS NULL)."""
    conditions = [
        Skill.is_public == True,  # noqa: E712
        Skill.status == "active",
        Skill.entity_id.isnot(None),  # exclude platform built-ins
    ]
    if category:
        conditions.append(Skill.category == category)
    result = await db.execute(
        select(Skill).where(*conditions).order_by(Skill.created_at.desc())
    )
    return list(result.scalars().all())


# ── Get ──

async def get_skill(db: AsyncSession, skill_id: str) -> Skill | None:
    """Get a skill by ID."""
    result = await db.execute(select(Skill).where(Skill.id == skill_id))
    return result.scalar_one_or_none()


async def get_skill_by_slug(
    db: AsyncSession, slug: str, entity_id: Optional[str] = None,
) -> Skill | None:
    """Get a skill by slug, preferring entity-owned skills over built-ins.

    Slugs are not globally unique today: a workspace can define an entity skill
    with the same slug as a platform skill, and older seeders may leave
    duplicate active rows. Runtime invocation must choose deterministically
    instead of raising MultipleResultsFound in the middle of a plan.
    """
    requested_slug = _normalize_requested_slug(slug)
    if not requested_slug:
        return None
    alias_key = skill_slug_lookup_key(requested_slug)
    conditions = [
        func.pg_catalog.replace(func.pg_catalog.lower(Skill.slug), "-", "_") == alias_key,
        Skill.status == "active",
    ]
    exact_priority = case((Skill.slug == requested_slug, 0), else_=1)
    if entity_id:
        conditions.append(
            or_(Skill.entity_id == entity_id, Skill.entity_id.is_(None))
        )
        priority = case(
            (Skill.entity_id == entity_id, 0),
            (Skill.entity_id.is_(None), 1),
            else_=2,
        )
    else:
        conditions.append(Skill.entity_id.is_(None))
        priority = case((Skill.entity_id.is_(None), 0), else_=1)
    result = await db.execute(
        select(Skill)
        .where(*conditions)
        .order_by(
            priority.asc(),
            exact_priority.asc(),
            Skill.created_at.desc(),
            Skill.id.desc(),
        )
        .limit(1)
    )
    return result.scalars().first()


def _select_available_skill_by_slug(
    skills: list[Skill],
    slug: str,
    *,
    entity_id: str,
    workspace_id: str | None,
) -> Skill | None:
    requested_slug = _normalize_requested_slug(slug)
    alias_key = skill_slug_lookup_key(requested_slug)
    if not alias_key:
        return None

    matches = [
        skill
        for skill in skills
        if skill_slug_lookup_key(getattr(skill, "slug", None)) == alias_key
    ]
    if not matches:
        return None

    def priority(skill: Skill) -> tuple[int, int]:
        skill_workspace_id = str(getattr(skill, "workspace_id", None) or "")
        if workspace_id and skill_workspace_id == workspace_id:
            scope_priority = 0
        elif getattr(skill, "entity_id", None) == entity_id and not skill_workspace_id:
            scope_priority = 1
        elif getattr(skill, "entity_id", None) == entity_id:
            scope_priority = 2
        else:
            scope_priority = 3
        return (
            scope_priority,
            0 if getattr(skill, "slug", None) == requested_slug else 1,
        )

    return min(matches, key=priority)


# ── Create ──

async def create_skill(
    db: AsyncSession,
    entity_id: Optional[str],
    name: str,
    system_prompt: str,
    **kwargs,
) -> Skill:
    """Create a new skill — DB record + MinIO files for entity-owned skills."""
    skill_id = generate_ulid()
    cfg = dict(kwargs.get("config", {}) or {})
    effective_name = _fallback_skill_name(
        skill_id=skill_id,
        name=name,
        description=kwargs.get("description") or "",
        system_prompt=system_prompt,
        category=kwargs.get("category") or "",
    )
    requested_slug = str(kwargs.get("slug") or "").strip()
    has_explicit_slug = bool(requested_slug and not is_placeholder_skill_identifier(requested_slug))
    base_slug = (
        requested_slug
        if has_explicit_slug
        else effective_name
    )
    slug = await _unique_skill_slug(
        db,
        entity_id=entity_id,
        base_slug=base_slug,
        skill_id=skill_id,
        explicit_slug=has_explicit_slug,
    )
    requested_display_name = str(kwargs.get("display_name") or "").strip()
    display_name = (
        requested_display_name
        if requested_display_name and not is_placeholder_skill_identifier(requested_display_name)
        else effective_name
    )
    skill = Skill(
        id=skill_id,
        entity_id=entity_id,
        # Ownership triple for resource_access.py. Platform skills (no entity)
        # never carry an owner or workspace.
        owner_user_id=kwargs.get("owner_user_id") if entity_id else None,
        workspace_id=kwargs.get("workspace_id") if entity_id else None,
        visibility=kwargs.get("visibility") or Visibility.ENTITY,
        name=effective_name,
        slug=slug,
        display_name=display_name,
        description=kwargs.get("description"),
        system_prompt=system_prompt,
        tools=kwargs.get("tools", []),
        input_schema=kwargs.get("input_schema", {}),
        output_format=kwargs.get("output_format", "text"),
        category=kwargs.get("category"),
        tags=kwargs.get("tags", []),
        is_public=kwargs.get("is_public", False),
        version=kwargs.get("version", "1.0.0"),
        config=cfg,
        status=kwargs.get("status", "active"),
    )
    db.add(skill)
    await db.flush()
    await db.refresh(skill)

    # Save files to MinIO for entity-owned skills (platform skills stay DB-only)
    if entity_id:
        cfg = skill.config or {}
        minio_dir = _save_skill_files_to_minio(skill, system_prompt, cfg)
        if minio_dir:
            try:
                # Persist the computed dir name so loads/deletes can resolve it.
                # If this DB write fails, the caller never receives ``skill``
                # and therefore cannot clean the already-written object prefix.
                skill.config = {**cfg, "minio_dir": minio_dir}
                await db.flush()
            except Exception:
                _delete_skill_files_best_effort(
                    skill,
                    {**cfg, "minio_dir": minio_dir},
                    context="create rollback",
                )
                raise

    return skill


# ── Update ──

async def update_skill(
    db: AsyncSession, skill_id: str, entity_id: str, *, expected_edit_version: int | None = None, **kwargs,
) -> Skill | None:
    """Update a skill owned by entity_id — DB record + MinIO files.

    With ``expected_edit_version`` the row is locked and the save is refused
    (``StaleRevisionError``) when anyone saved it since that version."""
    if expected_edit_version is None:
        skill = await get_skill(db, skill_id)
    else:
        skill = (await db.execute(
            select(Skill).where(Skill.id == skill_id).with_for_update().execution_options(populate_existing=True)
        )).scalar_one_or_none()
    if not skill or skill.entity_id != entity_id:
        return None
    if expected_edit_version is not None and int(skill.edit_version or 1) != int(expected_edit_version):
        from packages.core.revisions import StaleRevisionError

        raise StaleRevisionError(
            target_kind="skill", target_id=skill.id,
            expected=int(expected_edit_version), actual=int(skill.edit_version or 1),
        )

    if "name" in kwargs and kwargs["name"] is not None:
        kwargs["name"] = _fallback_skill_name(
            skill_id=skill.id,
            name=kwargs.get("name"),
            description=kwargs.get("description") or skill.description or "",
            system_prompt=kwargs.get("system_prompt") or skill.system_prompt or "",
            category=kwargs.get("category") or skill.category or "",
        )
    if "display_name" in kwargs and kwargs["display_name"] is not None:
        if is_placeholder_skill_identifier(kwargs["display_name"]):
            kwargs["display_name"] = kwargs.get("name") or skill.name
    if "slug" in kwargs and kwargs["slug"] is not None:
        if is_placeholder_skill_identifier(kwargs["slug"]):
            kwargs.pop("slug", None)
        else:
            kwargs["slug"] = await _unique_skill_slug(
                db,
                entity_id=entity_id,
                base_slug=str(kwargs["slug"]),
                skill_id=skill.id,
                exclude_id=skill.id,
                explicit_slug=True,
            )
    name_changed = "name" in kwargs and kwargs["name"] != skill.name

    # M11: diff BEFORE applying — only real changes to behavior-affecting
    # fields bump the config revision. Cosmetic edits (name/display_name/
    # description/slug/tags/category/version label) and same-value writes
    # (idempotent marketplace re-imports) must not move it.
    from packages.core.revisions import (
        SKILL_CONTENT_REVISION_FIELDS,
        bump_revision,
        content_patch_for,
    )
    content_patch = content_patch_for(skill, kwargs, SKILL_CONTENT_REVISION_FIELDS)

    changed = False
    for key, value in kwargs.items():
        if value is not None and hasattr(skill, key):
            changed = changed or getattr(skill, key) != value
            setattr(skill, key, value)
    if changed:
        skill.edit_version = int(skill.edit_version or 1) + 1

    if content_patch:
        await bump_revision(db, skill, patch=content_patch)

    # Auto-update slug if name changed
    if name_changed and "slug" not in kwargs:
        skill.slug = await _unique_skill_slug(
            db,
            entity_id=entity_id,
            base_slug=kwargs["name"],
            skill_id=skill.id,
            exclude_id=skill.id,
        )

    await db.flush()
    await db.refresh(skill)

    # Persist updated files to MinIO
    if entity_id:
        cfg = skill.config or {}
        minio_dir = _save_skill_files_to_minio(skill, skill.system_prompt, cfg)
        if minio_dir and cfg.get("minio_dir") != minio_dir:
            skill.config = {**cfg, "minio_dir": minio_dir}
            await db.flush()

    return skill


# ── Delete ──

async def delete_skill(db: AsyncSession, skill_id: str, entity_id: str) -> bool:
    """Delete a skill owned by entity_id — removes DB record and MinIO files."""
    skill = await get_skill(db, skill_id)
    if not skill or skill.entity_id != entity_id:
        return False
    await db.delete(skill)
    await db.flush()

    # Remove MinIO files (best-effort)
    if entity_id:
        try:
            from packages.core.services.skill_file_storage import delete_skill_files
            delete_skill_files(
                entity_id, skill_id,
                skill_dir=skill.config.get("minio_dir") if skill.config else None,
            )
        except Exception as exc:
            logger.warning("[skill_service] MinIO delete failed skill=%s: %s", skill_id, exc)

    return True


# ── MinIO file sync helper ──

def _save_skill_files_to_minio(skill: Skill, prompt: str, cfg: dict) -> Optional[str]:
    """Best-effort write of skill files to MinIO.

    Returns the resolved ``minio_dir`` on success, or None on failure.
    Callers should persist the returned dir name into ``skill.config["minio_dir"]``.
    """
    if not skill.entity_id:
        return None
    try:
        from packages.core.services.skill_file_storage import save_skill_files
        config_snapshot = {
            "id": skill.id,
            "name": skill.name,
            "slug": skill.slug,
            "version": skill.version or "1.0.0",
            "description": skill.description or "",
            "tags": skill.tags or [],
            "is_public": skill.is_public,
            "entity_id": skill.entity_id,
            "type": cfg.get("type") or "",
            # Pass owner/skill_name so compute_skill_dir can build the human-readable path
            "owner": cfg.get("owner") or "",
            "skill_name": cfg.get("skill_name") or "",
        }
        # extra_files covers github-install and openclaw imports
        extra_files: dict = {}
        raw_extra = cfg.get("extra_files") or {}
        if isinstance(raw_extra, dict):
            extra_files = raw_extra

        minio_dir = save_skill_files(
            skill.entity_id,
            skill.id,
            prompt=prompt or "",
            scripts=cfg.get("scripts") or {},
            requirements=cfg.get("requirements") or "",
            extra_files=extra_files,
            config_snapshot=config_snapshot,
        )
        return minio_dir or None
    except Exception as exc:
        logger.warning(
            "[skill_service] MinIO file save failed skill=%s entity=%s: %s",
            skill.id, skill.entity_id, exc,
        )
        _delete_skill_files_best_effort(
            skill,
            cfg,
            context="partial save",
        )
        return None


def _delete_skill_files_best_effort(
    skill: Skill,
    cfg: dict,
    *,
    context: str,
) -> None:
    """Remove one uncommitted Skill prefix without hiding the root error."""

    try:
        from packages.core.services.skill_file_storage import delete_skill_files

        delete_skill_files(
            skill.entity_id,
            skill.id,
            skill_dir=cfg.get("minio_dir") or None,
            config=cfg,
        )
    except Exception:
        logger.warning(
            "[skill_service] %s MinIO cleanup failed skill=%s",
            context,
            skill.id,
            exc_info=True,
        )


# ── Skill type detection ──

def _determine_skill_type(skill: Skill) -> str:
    """Return 'sandbox' or 'prompt' based on skill config and on-disk files.

    Priority order:
      1. config.type explicitly set to 'sandbox', 'prompt', or 'runtime_guidance'
      2. config.scripts or config.requirements present  → sandbox
      3. builtin skill_dir contains script files        → sandbox
      4. default                                        → prompt
    """
    cfg = skill.config or {}
    declared = str(cfg.get("type", "")).strip().lower()
    if declared == "runtime_guidance":
        # Runtime guidance uses the prompt/tool loop. Bundled helper scripts
        # belong to its tools, not to a standalone Sandbox Skill entry point.
        return "prompt"
    if declared in ("sandbox", "prompt"):
        return declared

    if cfg.get("scripts") or cfg.get("requirements"):
        return "sandbox"

    skill_dir_str = cfg.get("skill_dir")
    if skill_dir_str:
        from packages.core.services.builtin_skill_loader import resolve_builtin_skill_dir

        skill_dir = resolve_builtin_skill_dir(skill_dir_str)
        if skill_dir is not None and skill_dir.is_dir():
            for f in skill_dir.rglob("*"):
                if f.is_file() and (
                    f.suffix in _SCRIPT_EXTENSIONS or f.name == "requirements.txt"
                ):
                    return "sandbox"

    return "prompt"


async def skill_credentials_withheld(skill: Skill, *, entity_id: str, user_id: Optional[str]) -> bool:
    """Whether a sandbox skill runs without its stored credentials.

    The run drives the sandbox itself (exec, file reads), so whatever the
    sandbox env holds is readable by whoever steers the run. When an external
    collaborator acts -- their own run, or a run of a Task they steered --
    the customer's skill credentials stay out of the sandbox; only a skill
    every acting collaborator owns (they uploaded its credentials) gets its
    env. Any other skill then runs without credentials and may fail; that is
    intended.
    """
    from packages.core.services.collaborator_steering import collaborators_acting_now
    from packages.core.services.runtime_authorization.collaborator import is_runtime_collaborator

    acting = set(collaborators_acting_now(entity_id))
    if user_id and await is_runtime_collaborator(str(user_id), entity_id):
        acting.add(str(user_id))
    return bool(acting - {str(getattr(skill, "owner_user_id", None) or "")})


async def _invoke_sandbox_skill(
    skill: Skill,
    entity_id: str,
    user_id: Optional[str],
    input_text: str,
    *,
    agent_id: Optional[str] = None,
    conversation_id: Optional[str] = None,
    sandbox_binding_key: Optional[str] = None,
    task_id: Optional[str] = None,
    on_sub_tool_start=None,
    on_sub_tool_end=None,
    runtime_tool_context: Optional[Mapping[str, Any]] = None,
    db: AsyncSession | None = None,
) -> Any:
    """Invoke a sandbox skill via the Sandbox Service.

    Flow:
      1. Collect all skill files from MinIO (or on-disk for platform skills).
      2. Load skill credentials from MinIO credentials.json.
      3. Admit through ``sandbox_session_service.acquire_sandbox_session``:
         reuse the (entity, user) owner's warm sandbox when one is alive,
         otherwise create one (local direct, or durable reservation).
      4. Return a compact execution contract (sandbox_id + skill index) as
         `content` so the parent LLM can drive execution via sandbox tools.
    """
    from packages.core.config import get_settings as _get_settings
    settings = _get_settings()
    sandbox_url = settings.SANDBOX_SERVICE_URL.strip()
    durable_external = (
        settings.MANOR_RUNTIME_EXECUTION_MODE == "durable"
        and settings.SANDBOX_COORDINATION_MODE == "external-runner"
    )
    if not sandbox_url and not (durable_external and settings.SANDBOX_RUNNERS_JSON.strip()):
        return _sandbox_skill_error_response(
            skill,
            f"Sandbox service not configured (SANDBOX_SERVICE_URL unset). "
            f"Cannot run sandbox skill '{skill.name}'.",
        )
    if not str(sandbox_binding_key or conversation_id or "").strip():
        # Every sandbox operation is checked against the caller's binding
        # slot, and a run with neither a conversation nor a task has none: a
        # container admitted here could never be operated. Refuse before
        # allocating one.
        return _sandbox_skill_error_response(
            skill,
            f"Cannot open a sandbox for skill '{skill.name}': this run has no sandbox "
            "binding scope (neither a conversation nor a task). Sandbox skills can only "
            "run from a conversation or task run.",
            code="not_configured",
        )

    cfg = skill.config or {}

    # ── 1. Load system prompt ───────────────────────────────────────
    instructions = skill.system_prompt
    if skill.entity_id:
        try:
            from packages.core.services.skill_file_storage import load_skill_prompt as _load_minio_prompt
            _mdir = cfg.get("minio_dir") or None
            minio_prompt = _load_minio_prompt(skill.entity_id, skill.id, skill_dir=_mdir, config=cfg)
            if minio_prompt:
                instructions = minio_prompt
        except Exception as exc:
            logger.debug("[skill_service] MinIO prompt load failed skill=%s: %s", skill.id, exc)

    if not instructions:
        return _sandbox_skill_error_response(
            skill,
            f"Skill '{skill.name}' has no instructions (SKILL.md missing).",
        )

    # ── 2. Collect skill files ──────────────────────────────────────
    files: dict[str, str] = {}
    # Set for a packaged built-in Skill (see sandbox_baked_skills).
    baked_skill = None
    bundle_paths: list[str] = []

    if skill.entity_id:
        # Entity-owned skill: load everything from MinIO
        try:
            from packages.core.services.skill_file_storage import (
                load_skill_extra_files,
                load_skill_scripts,
                load_skill_requirements,
            )
            _mdir = cfg.get("minio_dir") or None
            files["SKILL.md"] = instructions
            scripts = load_skill_scripts(skill.entity_id, skill.id, skill_dir=_mdir, config=cfg)
            if scripts:
                files.update(scripts)
            reqs = load_skill_requirements(skill.entity_id, skill.id, skill_dir=_mdir, config=cfg)
            if reqs:
                files["requirements.txt"] = reqs
            extra = load_skill_extra_files(skill.entity_id, skill.id, skill_dir=_mdir, config=cfg)
            if extra:
                files.update(extra)
            script_files = [k for k in files if k != "SKILL.md" and k != "requirements.txt"]
            logger.info(
                "[skill_service] sandbox files loaded skill=%s minio_dir=%s scripts=%d extra_keys=%s",
                skill.id, _mdir, len(script_files), list(files.keys()),
            )
        except Exception as exc:
            logger.warning("[skill_service] MinIO file collect failed skill=%s: %s", skill.id, exc)
            # Always include SKILL.md so the sandbox has instructions even if scripts are missing
            if "SKILL.md" not in files:
                files["SKILL.md"] = instructions
    else:
        # Platform built-in skill: read from on-disk skill_dir. A packaged
        # built-in ships in the sandbox image too: its files are read only if
        # the runner turns out not to hold this exact version.
        skill_dir_str = cfg.get("skill_dir")
        if skill_dir_str:
            from packages.core.services.builtin_skill_loader import resolve_builtin_skill_dir
            from packages.core.services.sandbox_baked_skills import (
                baked_skill_ref,
                packaged_skill_file_paths,
                read_skill_upload_files,
            )

            skill_dir_path = resolve_builtin_skill_dir(skill_dir_str)
            if skill_dir_path is not None and skill_dir_path.is_dir():
                baked_skill = baked_skill_ref(skill_dir_path)
                if baked_skill is not None:
                    bundle_paths = packaged_skill_file_paths(baked_skill.name)
                    try:
                        instructions = (skill_dir_path / "SKILL.md").read_text(encoding="utf-8") or instructions
                    except OSError:
                        pass
                else:
                    files = read_skill_upload_files(skill_dir_path)
        if baked_skill is None:
            if not files:
                files["SKILL.md"] = instructions
            elif files.get("SKILL.md"):
                instructions = files["SKILL.md"]

    # Require at least one executable script for sandbox execution
    script_keys = [
        k for k in (bundle_paths if baked_skill is not None else files)
        if k not in ("SKILL.md", "requirements.txt", "credentials.json")
    ]
    if not script_keys:
        logger.warning(
            "[skill_service] sandbox skill has no scripts skill=%s entity=%s minio_dir=%s files=%s",
            skill.id, skill.entity_id, cfg.get("minio_dir"), list(files.keys()),
        )
        return _sandbox_skill_error_response(
            skill,
            f"Skill '{skill.name}' has no executable script files in storage. "
            "Please upload skill scripts (e.g. run.py) before invoking this skill.",
        )

    # ── 3. Load credentials ─────────────────────────────────────────
    env: dict[str, str] = {}
    if skill.entity_id:
        try:
            from packages.core.services.skill_file_storage import load_skill_credentials as _load_creds
            _mdir = cfg.get("minio_dir") or None
            env = _load_creds(skill.entity_id, skill.id, skill_dir=_mdir, config=cfg) or {}
        except Exception:
            pass
    if env and await skill_credentials_withheld(skill, entity_id=entity_id, user_id=user_id):
        logger.info(
            "[skill_service] credentials withheld from a collaborator run skill=%s entity=%s",
            skill.id, entity_id,
        )
        env = {}
    allowed_keys = list(env.keys())

    config_overrides = None
    if not skill.entity_id:
        from packages.core.services.builtin_plugin_service import (
            builtin_plugin_definition,
            builtin_plugin_key_for_skill,
        )

        plugin_key = builtin_plugin_key_for_skill(skill)
        plugin_definition = (
            builtin_plugin_definition(plugin_key) if plugin_key else None
        )
        if plugin_definition and plugin_definition.sandbox_config:
            # Only platform-owned plugin metadata may tune container resources.
            # Entity skill config is intentionally excluded from this path.
            config_overrides = dict(plugin_definition.sandbox_config)
    expected_workspace_volume: str | None = None


    context = dict(runtime_tool_context or {})
    runtime_run_id = str(context.get("_runtime_run_id_from_context") or "")
    runtime_tool_call_id = str(context.get("_runtime_tool_call_id_from_context") or "")
    # The sandbox scope. A task-worker run has no conversation, so the runtime
    # hands over a synthetic binding key instead; it is used ONLY to bind,
    # scope, and later re-find this sandbox. Everything else in this function
    # keeps the run's real (possibly absent) conversation.
    binding_conversation = sandbox_binding_key or conversation_id
    try:
        owner = SandboxOwner(entity_id=entity_id, user_id=str(user_id or ""))
    except ValueError as exc:
        # A missing or ``:``-bearing identity is a caller error, not a crash
        # inside the skill: surface it the way every other admission refusal
        # is surfaced.
        return _sandbox_skill_error_response(
            skill,
            f"Invalid sandbox owner: {exc}",
            code="not_configured",
        )
    bundle = SandboxBundle(
        skill_name=skill.slug or skill.name,
        files=files,
        env=env,
        allowed_sensitive_keys=tuple(allowed_keys),
        source="workspace" if skill.entity_id else "builtin",
        config=config_overrides,
        auto_install=True,
        baked_skill=baked_skill,
        baked_file_paths=tuple(bundle_paths),
    )
    binding = SandboxBinding(
        conversation_id=binding_conversation or None,
        agent_id=agent_id,
        skill_id=skill.id,
        runtime_run_id=runtime_run_id or None,
        runtime_tool_call_id=runtime_tool_call_id or None,
        # A task worker has no durable run: admission reserves against the
        # task instead and waits for the allocation itself.
        task_id=task_id or None,
        workflow_run_id=str(context.get("_workflow_run_id_from_context") or "") or None,
        # A prompt skill's nested loop shares the run's identity but cannot
        # be parked on a reservation; admission must not queue a WARM one.
        parent_skill=str(context.get("_runtime_parent_skill_from_context") or "") or None,
        credentials_owner_user_id=(str(getattr(skill, "owner_user_id", None) or "") or None) if env else None,
    )
    if durable_external and runtime_run_id and runtime_tool_call_id:
        if db is None:
            return _sandbox_skill_error_response(
                skill,
                "Durable Runtime database context is unavailable for Sandbox admission.",
            )
        # A durable run resumes by re-executing this call: attach the
        # container the scheduler allocated, or keep waiting on the ticket.
        attached = await attach_durable_reservation(
            owner=owner, bundle=bundle, binding=binding, db=db,
        )
        if isinstance(attached, SandboxSessionError):
            return _sandbox_skill_error_response(
                skill, attached.message, code=attached.code, retryable=attached.retryable,
            )
        if isinstance(attached, SandboxSession):
            result_sandbox_id = attached.sandbox_id
            skill_info_parts = [f"sandbox_id: {result_sandbox_id}  *(allocated)*"]
            if script_keys:
                skill_info_parts.append(
                    f"scripts ({len(script_keys)}): {_compact_skill_items(script_keys)}"
                )
            if files.get("requirements.txt") or "requirements.txt" in bundle_paths:
                skill_info_parts.append("dependencies: installed from requirements.txt")
            if env:
                skill_info_parts.append(
                    f"credentials_injected ({len(env)}): {_compact_skill_items(set(env.keys()))}"
                )
            return {
                "skill": skill.name,
                "content": _sandbox_skill_runtime_contract(
                    skill=skill,
                    sandbox_id=result_sandbox_id,
                    skill_info_parts=skill_info_parts,
                    expected_workspace_volume=expected_workspace_volume,
                    skill_input=str(input_text or "").strip(),
                    skill_instructions=instructions,
                ),
                "usage": {},
                "tools_used": [],
                "rounds": 0,
                "stop_reason": "sandbox_ready",
                "sandbox_id": result_sandbox_id,
                # The runner's name for this Skill: its /skill view in the
                # user's shared sandbox (staging writes through it).
                "sandbox_skill": bundle.skill_name,
            }
        if attached is not None:
            # RuntimeToolSuspension: the ticket is still waiting for a runner.
            return attached

    # ── 4. Admission through the owner-scoped session service ──────
    session = await acquire_sandbox_session(
        owner=owner,
        bundle=bundle,
        binding=binding,
        db=db,
    )
    if isinstance(session, SandboxSessionError):
        return _sandbox_skill_error_response(
            skill,
            session.message,
            code=session.code,
            retryable=session.retryable,
        )
    if not isinstance(session, SandboxSession):
        # RuntimeToolSuspension: a durable reservation was queued.
        return session

    result_sandbox_id = session.sandbox_id
    skill_info_parts = [
        f"sandbox_id: {result_sandbox_id}" + ("  *(reused)*" if session.reused else "")
    ]
    if session.entry_hint:
        skill_info_parts.append(f"entry_hint: {session.entry_hint}")
    if session.scripts:
        skill_info_parts.append(
            f"scripts ({len(session.scripts)}): {_compact_skill_items(session.scripts)}"
        )
    if session.requirements_installed:
        skill_info_parts.append(
            "dependencies: re-installed from requirements.txt"
            if session.reused
            else "dependencies: installed from requirements.txt"
        )
    if env:
        skill_info_parts.append(
            f"credentials_injected ({len(env)}): {_compact_skill_items(set(env.keys()))}"
        )
    if session.env_blocked:
        skill_info_parts.append(
            f"env_blocked ({len(session.env_blocked)}): "
            f"{_compact_skill_items(session.env_blocked)}"
        )

    # ── 5. Build execution contract for the parent LLM ─────────────
    skill_input = str(input_text or "").strip()
    content = _sandbox_skill_runtime_contract(
        skill=skill,
        sandbox_id=result_sandbox_id,
        skill_info_parts=skill_info_parts,
        expected_workspace_volume=expected_workspace_volume,
        skill_input=skill_input,
        skill_instructions=instructions,
    )

    return {
        "skill": skill.name,
        "content": content,
        "usage": {},
        "tools_used": [],
        "rounds": 0,
        "stop_reason": "sandbox_ready",
        "sandbox_id": result_sandbox_id,
        "sandbox_skill": bundle.skill_name,
    }


# ── Invoke ──

@dataclass(frozen=True)
class RunningSkillInvocation:
    """A prompt Skill whose child loop is running in this async context."""

    ids: frozenset[str]
    slug: str
    # Slugs this Skill may invoke from its loop (config ``invocable_skills``); None = any.
    invocable: frozenset[str] | None


_running_skill_invocations: ContextVar[tuple[RunningSkillInvocation, ...]] = ContextVar(
    "running_skill_invocations", default=(),
)


def running_skill_invocations() -> tuple[RunningSkillInvocation, ...]:
    return _running_skill_invocations.get()


def _skill_slug(skill: Skill) -> str:
    return str(skill.slug or skill.name or "").strip().lower()


async def _task_grants_skill(
    db: AsyncSession,
    skill: Skill,
    *,
    entity_id: str,
    workspace_id: str | None,
    user_id: str | None,
    task_id: str | None,
) -> bool:
    """A Task's own declared Skill is an execution grant for that Task only.

    A scheduled Skill job runs as an agent Task that must invoke the job's
    Skill, which its owner chose but never bound to the agent. The Task's
    ``required_skills`` carries that choice; the acting user must still be
    able to read the Skill, as without an agent binding.
    """
    if not task_id:
        return False
    from packages.core.models.task import Task

    required = await db.scalar(select(Task.required_skills).where(
        Task.id == task_id,
        Task.entity_id == entity_id,
    ))
    declared = {str(value) for value in (required or []) if str(value or "").strip()}
    if not declared & {str(skill.id), str(skill.slug or "")}:
        return False
    from packages.core.ai.runtime.skills import runtime_skill_is_eligible

    eligible, _ = await runtime_skill_is_eligible(
        db,
        skill,
        entity_id=entity_id,
        workspace_id=workspace_id,
        user_id=user_id,
        enforce_user_access=bool(user_id),
    )
    return eligible


def _skill_invocation_stack_error(skill: Skill, bound_skill_id: str) -> dict | None:
    """Refuse re-entering a running Skill or invoking one the parent Skill does not list."""
    stack = _running_skill_invocations.get()
    if not stack:
        return None
    slug = _skill_slug(skill)
    for running in stack:
        if {str(skill.id), str(bound_skill_id)} & running.ids or slug == running.slug:
            chain = " -> ".join(item.slug for item in stack)
            return {
                "error": (
                    f"Skill '{slug}' is already running in this invocation chain ({chain}); "
                    "a Skill cannot invoke itself or the Skill that invoked it. Nothing was run. "
                    "Continue with your own tools and report the result to your caller."
                ),
                "code": "skill_invocation_cycle",
            }
    parent = stack[-1]
    if parent.invocable is not None and slug not in parent.invocable:
        allowed = ", ".join(sorted(parent.invocable)) or "(none)"
        return {
            "error": (
                f"Skill '{parent.slug}' may only invoke: {allowed}. '{slug}' was not run."
            ),
            "code": "skill_not_invocable",
        }
    return None


@contextmanager
def _running_skill_invocation_scope(skill: Skill, bound_skill_id: str) -> Iterator[None]:
    raw = (skill.config or {}).get("invocable_skills")
    invocable = (
        frozenset(str(item).strip().lower() for item in raw if str(item or "").strip())
        if isinstance(raw, (list, tuple))
        else None
    )
    frame = RunningSkillInvocation(
        ids=frozenset({str(skill.id), str(bound_skill_id)}), slug=_skill_slug(skill), invocable=invocable,
    )
    token = _running_skill_invocations.set((*_running_skill_invocations.get(), frame))
    try:
        yield
    finally:
        _running_skill_invocations.reset(token)


async def invoke_skill(
    db: AsyncSession,
    skill_id_or_slug: str,
    entity_id: str,
    input_text: str,
    *,
    agent_id: Optional[str] = None,
    enforce_agent_access: bool = True,
    user_id: Optional[str] = None,
    workspace_id: Optional[str] = None,
    conversation_id: Optional[str] = None,
    sandbox_binding_key: Optional[str] = None,
    task_id: Optional[str] = None,
    manual_skill_selected: bool = False,
    tool_profile: Optional[str] = None,
    allowed_tool_names: Optional[set[str]] = None,
    runtime_envelope: Any | None = None,
    metadata: Optional[dict] = None,
    model: Optional[str] = None,
    active_user_message: Optional[str] = None,
    runtime_tool_context: Optional[Mapping[str, Any]] = None,
    output_schema: Optional[dict[str, Any]] = None,
    forced_tool_calls: Optional[list[dict[str, Any]]] = None,
    room_file_skill_scope: bool = False,
    bundle_reference: Optional[str] = None,
    bundle_char_offset: Optional[int] = None,
    hitl_resume_checkpoint: bool = False,
    runtime_checkpoint: Optional[dict[str, Any]] = None,
) -> dict:
    """Invoke a skill — prompt or sandbox depending on skill type.

    1. Load skill by ID or slug
    2. Detect skill type (prompt vs sandbox)
    3a. Sandbox skill → create/reuse a Sandbox Service sandbox via SandboxClient; return a
        context block (sandbox_id + SKILL.md) for the parent LLM to drive via
        sandbox exec / destroy actions.
    3b. Prompt skill  → agentic_loop with skill's declared tools only.
    4. Return {content, usage, tools_used, rounds}

    ``sandbox_binding_key`` is the sandbox-only scope for a run without a
    conversation (a task worker). It is deliberately separate from
    ``conversation_id``: the prompt-skill loop forwards ``conversation_id``
    into nested tool calls, so substituting the key there would spread it far
    past sandbox admission. Only ``_invoke_sandbox_skill`` reads it.

    ``bundle_reference`` names one bundled file of an instructions-only skill
    (optionally from ``bundle_char_offset``). The caller's own ``read_file``
    only serves the caller's bundle, so the invoked skill hands its
    references back through this channel instead of starting a loop.
    """
    runtime_metadata = (
        getattr(runtime_envelope, "metadata", None)
        if runtime_envelope is not None
        else None
    )
    agent_subscription_id = (
        runtime_metadata.get("agent_subscription_id")
        if isinstance(runtime_metadata, dict)
        else None
    )
    from packages.core.ai.runtime.skills import (
        runtime_master_uses_owner_chat_skill_scope,
        runtime_skill_is_eligible,
    )

    owner_chat_master_scope = bool(
        runtime_envelope is not None
        and getattr(runtime_envelope, "entity_id", None) == entity_id
        and getattr(runtime_envelope, "agent_id", None) == agent_id
        and getattr(runtime_envelope, "user_id", None) == user_id
        and getattr(runtime_envelope, "workspace_id", None) == workspace_id
        and runtime_master_uses_owner_chat_skill_scope(
            agent_id=agent_id,
            user_id=user_id,
            surface=getattr(runtime_envelope, "surface", None),
        )
    )
    enforce_bound_agent_access = bool(
        agent_id and enforce_agent_access and not owner_chat_master_scope
    )
    available_agent_skills: list[Skill] | None = None
    skill = await get_skill(db, skill_id_or_slug)
    if not skill:
        if enforce_bound_agent_access:
            available_agent_skills = await list_skills_for_agent(
                db,
                entity_id,
                agent_id,
                workspace_id=workspace_id,
                agent_subscription_id=agent_subscription_id,
            )
            skill = _select_available_skill_by_slug(
                available_agent_skills,
                skill_id_or_slug,
                entity_id=entity_id,
                workspace_id=workspace_id,
            )
    if not skill:
        skill = await get_skill_by_slug(db, skill_id_or_slug, entity_id)
    if not skill:
        return {"error": f"Skill not found: {skill_id_or_slug}"}

    room_builtin_file_skill = False
    if room_file_skill_scope:
        from packages.core.services.room_file_skill_policy import ROOM_FILE_SKILL_SLUGS

        envelope_metadata = getattr(runtime_envelope, "metadata", None)
        room_builtin_file_skill = bool(
            skill.entity_id is None
            and skill.slug in ROOM_FILE_SKILL_SLUGS
            and (skill.config or {}).get("source") == "builtin"
            and (skill.config or {}).get("type") == "sandbox"
            and user_id
            and isinstance(envelope_metadata, dict)
            and envelope_metadata.get("room_id")
            and envelope_metadata.get("room_agent_run_id")
            and not workspace_id and not conversation_id and not task_id
        )
        if not room_builtin_file_skill:
            return {"error": "group_file_skill_unavailable", "code": "group_file_skill_unavailable"}

    from packages.core.ai.runtime.skills import runtime_skill_is_eligible

    eligible, eligibility_code = await runtime_skill_is_eligible(
        db,
        skill,
        entity_id=entity_id,
        workspace_id=workspace_id,
        user_id=user_id,
        # An enforced Agent binding is an explicit execution grant. Direct and
        # manually selected Skill calls still require the actor to read the
        # entity-owned Skill itself.
        enforce_user_access=bool(
            manual_skill_selected
            or (user_id and not enforce_bound_agent_access)
        ),
    )
    if not eligible:
        if eligibility_code == "skill_plugin_disabled":
            return {
                "error": (
                    f"Skill plugin for '{skill.slug or skill.name}' is disabled. "
                    "Enable it under Settings → Integrations first."
                ),
                "code": eligibility_code,
            }
        if eligibility_code == "skill_workspace_contract_missing":
            return {
                "error": (
                    f"Skill '{skill.slug or skill.name}' is not available in the "
                    "current Workspace."
                ),
                "code": eligibility_code,
            }
        return {
            "error": f"Skill not found: {skill_id_or_slug}",
            "code": eligibility_code,
        }

    # Legacy Marketplace installs copied built-in rows but not their sandbox
    # files. Authorize the original row, then apply current platform runtime
    # policy and execute the source while retaining the original binding ID.
    bound_skill_id = skill.id
    skill_config = skill.config or {}
    source_skill_id = skill_config.get("source_skill_id")
    if (
        skill.entity_id == entity_id
        and skill_config.get("source") == "marketplace"
        and isinstance(source_skill_id, str)
        and source_skill_id
    ):
        source_skill = await get_skill(db, source_skill_id)
        if (
            source_skill is not None
            and source_skill.entity_id is None
            and source_skill.status == "active"
            and (source_skill.config or {}).get("source") == "builtin"
        ):
            source_eligible, source_code = await runtime_skill_is_eligible(
                db,
                source_skill,
                entity_id=entity_id,
                workspace_id=workspace_id,
                user_id=user_id,
            )
            if not source_eligible:
                return {
                    "error": f"Skill not found: {skill_id_or_slug}",
                    "code": source_code,
                }
            skill = source_skill

    stack_error = _skill_invocation_stack_error(skill, bound_skill_id)
    if stack_error is not None:
        return stack_error

    if runtime_envelope is not None:
        surface = getattr(runtime_envelope, "surface", None)
        surface_value = getattr(surface, "value", surface)
        from packages.core.ai.runtime.surfaces import ChatSurface

        is_external_surface = str(surface_value or "") in {
            ChatSurface.PUBLIC_CUSTOMER_CHAT.value,
            ChatSurface.EXTERNAL_CHANNEL_CHAT.value,
        }
        is_public_customer_surface = (
            str(surface_value or "") == ChatSurface.PUBLIC_CUSTOMER_CHAT.value
        )
        try:
            from packages.core.ai.runtime.skills import runtime_skill_allowed_on_surface

            if not room_builtin_file_skill and not runtime_skill_allowed_on_surface(
                skill,
                surface,
            ):
                return {
                    "error": (
                        f"Skill '{skill.slug or skill.name}' is not available "
                        "on this runtime surface."
                    ),
                    "code": "skill_surface_not_allowed",
                }
        except Exception:
            if is_external_surface:
                logger.warning("Runtime skill surface check failed", exc_info=True)
                return {
                    "error": (
                        f"Skill '{skill.slug or skill.name}' could not be verified "
                        "for this runtime surface."
                    ),
                    "code": "skill_surface_policy_error",
                }
            logger.debug("Runtime skill surface check failed", exc_info=True)

        if is_public_customer_surface and agent_id and not room_builtin_file_skill:
            try:
                available = await list_agent_skill_bindings(db, agent_id, entity_id)
                bound_ids = {item.id for item in available}
                bound_ids.update(builtin_skill_copy_sources([skill, *available]).values())
                if bound_skill_id not in bound_ids:
                    return {
                        "error": (
                            f"Skill '{skill.slug or skill.name}' is not bound to this "
                            "agent for this runtime surface."
                        ),
                        "code": "skill_not_bound_to_agent",
                    }
            except Exception:
                logger.warning("Runtime skill agent binding check failed", exc_info=True)
                return {
                    "error": (
                        f"Skill '{skill.slug or skill.name}' could not be verified "
                        "for this agent."
                    ),
                    "code": "skill_agent_binding_policy_error",
                }

        if is_public_customer_surface and not room_builtin_file_skill:
            runtime_allowed = set(allowed_tool_names or ())
            envelope_allowed = set(getattr(runtime_envelope, "allowed_tool_names", None) or ())
            runtime_allowed.update(envelope_allowed)
            declared_tools = {
                str(tool_name)
                for tool_name in (getattr(skill, "tools", None) or ())
                if str(tool_name or "").strip()
            }
            missing_tools = declared_tools - runtime_allowed
            if missing_tools:
                return {
                    "error": (
                        f"Skill '{skill.slug or skill.name}' requires tools that "
                        "are not available on this runtime surface."
                    ),
                    "code": "skill_declared_tools_not_allowed",
                    "missing_tools": sorted(missing_tools),
                }

        if (
            is_external_surface
            and not room_builtin_file_skill
            and _determine_skill_type(skill) == "sandbox"
        ):
            # External principals never use the Sandbox (product decision
            # 2026-10-06): their tool policy and authorization refuse every
            # sandbox action. Refuse before admission instead of allocating a
            # container they could never drive; the room file-skill receipt
            # above is the only external Sandbox Skill path.
            return {
                "error": (
                    f"Skill '{skill.slug or skill.name}' runs in the Sandbox, which "
                    "is not available on this runtime surface."
                ),
                "code": "skill_surface_not_allowed",
            }

    if enforce_bound_agent_access:
        available = (
            available_agent_skills
            if available_agent_skills is not None
            else await list_skills_for_agent(
                db,
                entity_id,
                agent_id,
                workspace_id=workspace_id,
                agent_subscription_id=agent_subscription_id,
            )
        )
        if bound_skill_id not in {item.id for item in available} and not (
            await _task_grants_skill(
                db,
                skill,
                entity_id=entity_id,
                workspace_id=workspace_id,
                user_id=user_id,
                task_id=task_id,
            )
        ):
            return {
                "error": (
                    f"Skill '{skill.slug or skill.name}' is not available to this "
                    "agent in the current workspace."
                ),
                "code": "skill_not_allowed",
            }

    # Wire up runtime stream forwarding so sub-tool calls appear in the parent run.
    on_sub_tool_start = None
    on_sub_tool_end = None
    try:
        from packages.core.ai.runtime import runtime_skill_nested_tool_callbacks

        on_sub_tool_start, on_sub_tool_end = runtime_skill_nested_tool_callbacks(
            skill_name=skill.slug or skill.name,
            invoke_skill_args={"skill": skill_id_or_slug or skill.slug or skill.name},
        )
    except Exception:
        pass

    # An empty bundle_file is an omitted one (see the invoke_skill handler).
    bundle_reference = str(bundle_reference or "").strip() or None
    if (
        bundle_reference
        and (skill.config or {}).get("execution_mode") != INSTRUCTIONS_ONLY_SKILL_EXECUTION_MODE
    ):
        return {
            "error": (
                f"Skill '{skill.slug or skill.name}' runs its own tools and reads its "
                "bundled files itself; `bundle_file` only applies to instructions-only skills."
            ),
            "code": "skill_reference_unsupported",
        }

    skill_type = _determine_skill_type(skill)
    logger.info(
        "[skill_service] invoke skill=%s type=%s entity=%s",
        skill.name, skill_type, entity_id,
    )

    if skill_type == "sandbox":
        await release_read_transaction(db)
        return await _invoke_sandbox_skill(
            skill, entity_id, user_id, input_text,
            agent_id=agent_id,
            conversation_id=conversation_id,
            sandbox_binding_key=sandbox_binding_key,
            task_id=task_id,
            on_sub_tool_start=on_sub_tool_start,
            on_sub_tool_end=on_sub_tool_end,
            runtime_tool_context=runtime_tool_context,
            db=db,
        )

    from packages.core.ai.runtime.skill_routing import is_chrome_skill
    from packages.core.ai.runtime.nested_agent import runtime_nested_skill_resume_active

    fresh_chrome_task = (
        is_chrome_skill(skill.slug, skill.name)
        and runtime_checkpoint is None
        and not runtime_nested_skill_resume_active()
    )

    # The tool surface needs only the Skill row and the caller scope: refuse an
    # unrunnable Skill before any prompt or bundle storage I/O. Instructions-only
    # guides return their text without a child loop and are never gated.
    tool_surface = None
    withheld_tools: tuple[str, ...] = ()
    if (skill.config or {}).get("execution_mode") != INSTRUCTIONS_ONLY_SKILL_EXECUTION_MODE:
        from packages.core.ai.runtime import runtime_prepare_prompt_skill_tool_surface
        from packages.core.ai.runtime.skills import runtime_skill_tools_withheld_from_surface

        tool_surface = runtime_prepare_prompt_skill_tool_surface(
            skill,
            allowed_tool_names=allowed_tool_names,
            runtime_envelope=runtime_envelope,
            fresh_chrome_task=fresh_chrome_task,
        )
        dependency_refusal = _skill_dependency_refusal(
            skill, tool_surface, runtime_envelope=runtime_envelope,
        )
        if dependency_refusal is not None:
            return dependency_refusal
        # A partly withheld Skill still runs, but the gap must be visible: the
        # child is told which declared tools this scope keeps from it, and the
        # caller's result names them, instead of a vague "tools unavailable".
        withheld_tools = runtime_skill_tools_withheld_from_surface(skill, tool_surface)
        if withheld_tools:
            logger.info(
                "skill.declared_tools_withheld %s",
                {"skill": skill.slug or skill.name, "withheld_tools": list(withheld_tools)},
            )

    # Bundle loading performs blocking storage I/O before the Agent loop.
    await release_read_transaction(db)

    # Prompt skill — load system_prompt from MinIO when available, else fall back to DB
    minio_prompt: str | None = None
    if skill.entity_id:
        try:
            from packages.core.services.skill_file_storage import load_skill_prompt as _load_minio_prompt
            _cfg = skill.config or {}
            minio_prompt = await asyncio.to_thread(
                _load_minio_prompt,
                skill.entity_id, skill.id,
                skill_dir=_cfg.get("minio_dir") or None,
                config=_cfg,
            )
        except Exception as exc:
            logger.debug("[skill_service] MinIO prompt load failed skill=%s: %s", skill.id, exc)

    config = skill.config or {}
    skill_extra_files = await asyncio.to_thread(_load_prompt_skill_extra_files, skill, config)
    instructions_only = config.get("execution_mode") == INSTRUCTIONS_ONLY_SKILL_EXECUTION_MODE
    if instructions_only and bundle_reference:
        return _instructions_only_reference_response(
            skill, skill_extra_files, bundle_reference, bundle_char_offset,
        )
    effective_prompt = minio_prompt if minio_prompt else skill.system_prompt
    effective_prompt = _append_skill_bundle_manifest(
        effective_prompt, skill_extra_files, instructions_only=instructions_only,
    )
    if instructions_only:
        if (
            skill.entity_id is None
            and config.get("source") == "builtin"
            and skill.category == "workspace-operations"
        ):
            from packages.core.ai.runtime.workspace_operation_skills import (
                load_workspace_operation_guide, workspace_operation_guide_configs,
                workspace_operation_guide_index,
            )

            effective_prompt, _ = load_workspace_operation_guide(skill.slug)
            if skill.slug == "workspace-operations":
                scope = runtime_envelope.effective_allowed_tool_names() if runtime_envelope else allowed_tool_names
                effective_prompt += "\n\n" + workspace_operation_guide_index(workspace_operation_guide_configs(scope))
        return _instructions_only_skill_response(skill, effective_prompt)
    authorization_user_message = (
        input_text if active_user_message is None else active_user_message
    )
    from packages.core.ai.runtime import (
        runtime_execute_skill_agent_loop,
        runtime_prepare_prompt_skill_tool_surface,
        runtime_prompt_skill_execution_contract,
        runtime_prompt_skill_execution_guidance,
        runtime_prompt_skill_registered_tool_executor,
        runtime_prompt_skill_required_arguments,
        runtime_prompt_skill_tool_executor,
        runtime_prompt_skill_tool_schema_resolver,
        runtime_skill_continuation_from_content,
        runtime_skill_continuation_prompt,
        runtime_skill_continuation_schema,
        runtime_skill_continuation_stages,
        runtime_terminal_tool_result_policy_for_skill,
    )

    execution_contract = runtime_prompt_skill_execution_contract(skill)
    execution_guidance = runtime_prompt_skill_execution_guidance(
        execution_contract,
        runtime_tool_context,
    )
    if execution_guidance:
        effective_prompt = f"{effective_prompt}\n\n{execution_guidance}"
    if withheld_tools:
        effective_prompt = (
            f"{effective_prompt}\n\nThis caller's tool scope does not grant these tools this Skill declares: "
            f"{', '.join(withheld_tools)}. Do not plan steps that need them. If the work requires one, "
            "stop and report it by name as the blocker."
        )
    # A staged Skill (one that stops at a declared stage for its caller to
    # invoke it again) reports its stage as validated JSON, so the caller's
    # runtime, not prose, decides whether the turn may end. A caller-supplied
    # output contract keeps precedence.
    continuation_stages = (
        runtime_skill_continuation_stages(config) if output_schema is None else ()
    )
    if continuation_stages:
        output_schema = runtime_skill_continuation_schema(continuation_stages)
        effective_prompt = f"{effective_prompt}\n\n{runtime_skill_continuation_prompt(continuation_stages)}"

    if tool_surface is None:
        tool_surface = runtime_prepare_prompt_skill_tool_surface(
            skill,
            allowed_tool_names=allowed_tool_names,
            runtime_envelope=runtime_envelope,
            fresh_chrome_task=fresh_chrome_task,
        )
    skill_tools = tool_surface.tools
    skill_allowed_tool_names = (
        set(tool_surface.allowed_tool_names)
        if tool_surface.allowed_tool_names is not None
        else (set(allowed_tool_names) if allowed_tool_names is not None else None)
    )
    skill_runtime_envelope = (
        tool_surface.harness.envelope
        if tool_surface.harness is not None
        else runtime_envelope
    )

    # A skill is real work — generating documents, running scripts,
    # iterating on errors — and cutting it off mid-task produces worse
    # outcomes than letting it finish (the parent agent re-does the
    # work, doubling cost). 20 rounds is a safety ceiling for genuinely
    # stuck loops, not a deadline. Skills that converge in 3-5 rounds
    # cost nothing extra; the cap only bites on pathological retries.
    # Override per-skill via DB ``config.max_rounds`` when a skill needs
    # more (or less).
    max_rounds = config.get("max_rounds", DEFAULT_SKILL_MAX_ROUNDS)
    temperature = config.get("temperature", 0.7)

    skill_tool_executor = runtime_prompt_skill_tool_executor(
        harness=tool_surface.harness,
        execute_tool=runtime_prompt_skill_registered_tool_executor(
            entity_id=entity_id,
            user_id=user_id,
            agent_id=agent_id,
            workspace_id=workspace_id,
            conversation_id=conversation_id,
            task_id=task_id,
            active_user_message=authorization_user_message,
            manual_skill_selected=manual_skill_selected,
            tool_profile=tool_profile,
            allowed_tool_names=skill_allowed_tool_names,
            runtime_envelope=skill_runtime_envelope,
            skill_slug=(skill.slug or skill.name or ""),
            # Every tool the nested loop runs is marked with this skill so
            # sandbox admission knows it cannot suspend the durable run here.
            runtime_tool_context={
                **dict(runtime_tool_context or {}),
                "_runtime_parent_skill_from_context": (skill.slug or skill.name or ""),
            },
            execution_contract=execution_contract,
        ),
        read_bundle_file=lambda tool_args: _try_read_skill_bundle_file(
            skill_extra_files,
            tool_args,
        ),
        list_bundle_files=lambda tool_args: _try_list_skill_bundle_files(
            skill_extra_files,
            tool_args,
        ),
    )
    skill_tool_schema_resolver = runtime_prompt_skill_tool_schema_resolver(
        declared_tool_names=tool_surface.declared_tool_names,
        allowed_tool_names=tool_surface.allowed_tool_names,
        required_arguments_by_tool=runtime_prompt_skill_required_arguments(
            execution_contract,
        ),
    )

    runtime_policy = runtime_terminal_tool_result_policy_for_skill(skill)

    from packages.core.ai.runtime.nested_agent import runtime_nested_skill_scope
    from packages.core.ai.runtime.skills import RuntimeSkillToolObservations

    tool_observations = RuntimeSkillToolObservations()
    from packages.core.ai.runtime import runtime_skill_progress_callbacks

    on_sub_tool_start, on_sub_stream_event = runtime_skill_progress_callbacks(on_sub_tool_start)

    def observe_tool_end(*callback_args):
        # Capture independently of the presentation hook. The loop retains
        # its existing callback exception policy and the hook's original args.
        try:
            tool_observations.record(callback_args[0], callback_args[1])
        finally:
            if on_sub_tool_end is not None:
                on_sub_tool_end(*callback_args)

    # Only a durable host installs this frame. Chat's ordinary Skill execution
    # keeps its existing behavior, while workers retain the parent call tree.
    await release_read_transaction(db)
    with (
        _running_skill_invocation_scope(skill, bound_skill_id),
        runtime_nested_skill_scope(str(skill.id)) as nested_frame,
    ):
        if nested_frame is not None and nested_frame.completed_response is not None:
            return nested_frame.completed_response
        nested_kwargs = nested_frame.loop_kwargs() if nested_frame is not None else {}
        if hitl_resume_checkpoint:
            nested_kwargs["hitl_resume_checkpoint"] = True
        if runtime_checkpoint is not None:
            nested_kwargs["runtime_checkpoint"] = runtime_checkpoint
            nested_kwargs["forced_tool_calls"] = None
        if "resume_checkpoint" in nested_kwargs:
            nested_kwargs["runtime_checkpoint"] = nested_kwargs.pop("resume_checkpoint")
        if nested_frame is not None:
            nested_kwargs.update(
                on_round_end=nested_frame.on_round_end,
                on_round_end_strict=True,
            )
        result = await runtime_execute_skill_agent_loop(
            runtime_envelope=skill_runtime_envelope,
            system_prompt=effective_prompt,
            user_message=input_text,
            tools=skill_tools,
            entity_id=entity_id,
            agent_id=agent_id,
            user_id=user_id,
            workspace_id=workspace_id,
            conversation_id=conversation_id,
            task_id=task_id,
            active_user_message=authorization_user_message,
            tool_profile=tool_profile,
            allowed_tool_names=skill_allowed_tool_names,
            tool_executor=skill_tool_executor,
            max_rounds=max_rounds,
            temperature=temperature,
            model=config.get("model") or model,
            metadata=metadata,
            on_tool_start=on_sub_tool_start,
            on_tool_end=observe_tool_end,
            stream_handler=on_sub_stream_event,
            tool_schema_resolver=skill_tool_schema_resolver,
            terminal_tool_result_policy=runtime_policy,
            output_schema=output_schema,
            **{"forced_tool_calls": forced_tool_calls, **nested_kwargs},
        )
    control = getattr(result, "control", None) or {}

    response = {
        "skill": skill.name,
        "content": result.content,
        "usage": result.usage,
        "tools_used": result.tool_calls_made,
        "rounds": result.rounds,
        "tool_observations": tool_observations.snapshot(getattr(result, "messages", None) or []),
        "tool_evidence": getattr(result, "tool_evidence", None) or {},
        "stop_reason": result.stop_reason,
        "stop_parent": bool(control.get("stop_parent")),
        "notice_key": control.get("notice_key"),
        "replace_visible_text": bool(control.get("replace_visible_text")),
        "control": control,
        "error": getattr(result, "error", None),
        "limit_detail": runtime_public_failure_payload(
            getattr(result, "error_detail", None)
        ),
    }
    if withheld_tools:
        response["withheld_tools"] = list(withheld_tools)
    continuation = (
        runtime_skill_continuation_from_content(continuation_stages, result.content)
        if result.stop_reason == "completed" else None
    )
    if continuation is not None:
        response["continuation"] = continuation
    if is_chrome_skill(skill.slug, skill.name):
        from packages.core.ai.runtime.chrome_run import (
            chrome_outcome_for_result,
            commit_chrome_runtime_state,
        )

        child_metadata = getattr(skill_runtime_envelope, "metadata", None)
        outcome = chrome_outcome_for_result(
            child_metadata,
            skill=str(skill.slug or skill.name or "chrome"),
            goal=authorization_user_message,
            content=result.content,
            stop_reason=result.stop_reason,
            error=getattr(result, "error", None),
            control=control,
        )
        commit_chrome_runtime_state(
            getattr(runtime_envelope, "metadata", None),
            child_metadata,
        )
        response["chrome_outcome"] = outcome.to_dict()
    if nested_frame is not None:
        # Re-enter the exact frame while publishing its final checkpoint. The
        # child loop's result, not Skill prose, owns all receipt evidence.
        with runtime_nested_skill_scope(str(skill.id)) as completed_frame:
            await completed_frame.complete(response, result.messages)
    return response


# ── Agent Skill Bindings ──

async def list_agent_skill_bindings(
    db: AsyncSession, agent_id: str, entity_id: str,
) -> list[Skill]:
    """List skills bound to a specific agent (private binding) plus entity skills."""
    result = await db.execute(
        select(AgentSkillBinding).where(
            AgentSkillBinding.agent_id == agent_id,
            AgentSkillBinding.status == "active",
        )
    )
    bindings = list(result.scalars().all())
    if not bindings:
        return []
    skill_ids = [b.skill_id for b in bindings]
    skills_result = await db.execute(
        select(Skill).where(
            Skill.id.in_(skill_ids),
            Skill.status == "active",
            or_(Skill.entity_id == entity_id, Skill.entity_id.is_(None)),
        )
    )
    return list(skills_result.scalars().all())


async def list_available_skills_for_agent(
    db: AsyncSession, agent_id: str, entity_id: str,
) -> list[Skill]:
    """List entity skills NOT yet bound to the agent (available to bind)."""
    bound_result = await db.execute(
        select(AgentSkillBinding.skill_id).where(
            AgentSkillBinding.agent_id == agent_id,
            AgentSkillBinding.status == "active",
        )
    )
    bound_ids = {row[0] for row in bound_result.all()}

    skills_result = await db.execute(
        select(Skill).where(
            Skill.entity_id == entity_id,
            Skill.status == "active",
        )
    )
    all_entity_skills = list(skills_result.scalars().all())
    return [s for s in all_entity_skills if s.id not in bound_ids]


async def bind_skill_to_agent(
    db: AsyncSession,
    agent_id: str,
    skill_id: str,
    entity_id: str,
    *,
    config: dict[str, Any] | None = None,
) -> AgentSkillBinding | None:
    """Bind a skill to an agent. Idempotent — returns existing binding if present."""
    skill = await get_skill(db, skill_id)
    if not skill:
        return None
    if skill.entity_id is not None and skill.entity_id != entity_id:
        return None

    await lock_agent_skill_binding_references(
        db,
        entity_id=entity_id,
        agent_id=agent_id,
        skill_id=skill_id,
    )
    existing = await db.execute(
        select(AgentSkillBinding).where(
            AgentSkillBinding.agent_id == agent_id,
            AgentSkillBinding.skill_id == skill_id,
        )
    )
    binding = existing.scalar_one_or_none()
    if binding:
        binding.status = "active"
        binding.config = _merge_agent_skill_binding_config(binding.config, config)
        await db.flush()
        return binding

    binding = AgentSkillBinding(
        id=generate_ulid(),
        agent_id=agent_id,
        skill_id=skill_id,
        config=_merge_agent_skill_binding_config({}, config),
        status="active",
    )
    db.add(binding)
    await db.flush()
    return binding


async def unbind_skill_from_agent(
    db: AsyncSession, agent_id: str, skill_id: str,
) -> bool:
    """Remove a skill binding from an agent."""
    result = await db.execute(
        select(AgentSkillBinding).where(
            AgentSkillBinding.agent_id == agent_id,
            AgentSkillBinding.skill_id == skill_id,
        )
    )
    binding = result.scalar_one_or_none()
    if not binding:
        return False
    await db.delete(binding)
    await db.flush()
    return True
