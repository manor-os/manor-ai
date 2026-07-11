"""Skill service — CRUD, listing, and invocation."""
from __future__ import annotations

import logging
import os
import json
import hashlib
import posixpath
import re
from pathlib import Path
from typing import Any, Optional

from sqlalchemy import and_, case, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.ai.runtime import (
    RUNTIME_SANDBOX_IDLE_THRESHOLD,
    runtime_init_sandbox_context,
    runtime_load_sandbox_context,
    runtime_binding_owner_matches,
    runtime_skill_binding_ref,
)
from packages.core.models.base import generate_ulid
from packages.core.models.skill import Skill, AgentSkillBinding

logger = logging.getLogger(__name__)

# Script extensions that indicate a sandbox skill when found in skill_dir
_SCRIPT_EXTENSIONS = {".py", ".sh", ".bash", ".js", ".ts", ".rb"}


def _sandbox_skill_error_response(skill: Skill, message: str) -> dict:
    """Return a structured sandbox-skill failure that the tool layer can surface."""
    return {
        "skill": skill.name,
        "content": message,
        "usage": {},
        "tools_used": [],
        "rounds": 0,
        "stop_reason": "error",
        "error": message,
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
        "- `sandbox_exec` is available for sandbox commands, but the expected path is to run bundled skill scripts or explicit helper files.",
        "- Before saving a final artifact, verify that the artifact path, intermediate evidence, and quality gates match `/skill/SKILL.md`.",
        "- If a required dependency, script, or workflow gate is missing, stop and report the blocker instead of inventing a shortcut.",
        "",
    ]
    lines.extend([
        "## Sandbox Environment",
        "- All skill files are mounted at `/skill/` (working directory).",
        (
            "- The entity filesystem is mounted read-only at `/workspace/`. "
            "Uploaded chat files are available under `/workspace/uploads/chat/...`."
            if expected_workspace_volume
            else "- No entity filesystem mount is available in this sandbox; use attachment text already provided in the chat context."
        ),
        "- Never use `/mnt/user-data` for Manor uploads. It is not the upload mount.",
        "- Read skill files with `sandbox_read_file(path=\"/skill/SKILL.md\")` or targeted `sandbox_exec` commands such as `sed -n '1,160p' /skill/SKILL.md`.",
        "- Write new files with `sandbox_write_file`; do not use `cat >`, `echo >`, `printf >`, `tee >`, or heredoc writes.",
        "- Do not use host shell/file tools such as `bash`, `read_file`, or root filesystem searches to inspect sandbox or workspace files.",
        "- `/tmp/` is writable for temporary files and npm cache.",
        "",
        "## Response Language",
        "- Match the user's original language for all user-visible narration, status updates, and the final answer.",
        "- If the user wrote in Chinese, keep progress messages and summaries in Chinese even when skill files are written in English.",
        "- Tool commands, code, filenames, and API identifiers may remain in their required language.",
        "",
        "## Skill Input",
        skill_input or "Use the latest user request and conversation context as the skill input.",
        "",
        "## Skill Instructions",
        "The following is the complete `/skill/SKILL.md` loaded for this run. Follow it exactly.",
        "",
        skill_instructions,
        "",
        "## Next Tool Guidance",
        f"- Use `sandbox_exec(sandbox_id=\"{sandbox_id}\", command=\"...\")` or `sandbox_read_file` for additional targeted inspection when needed.",
        "- Run the bundled scripts/workflow required by `/skill/SKILL.md`.",
        "- After the final artifact exists, call `sandbox_save_result`; then call `sandbox_destroy` once to release the sandbox.",
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


def _skill_bundle_file_json(path: str, content: str) -> str:
    source_sha = hashlib.sha256(content.encode("utf-8")).hexdigest()
    lines = content.splitlines(keepends=True)
    return json.dumps({
        "path": path,
        "source": "skill_bundle",
        "size": len(content.encode("utf-8")),
        "source_sha256": source_sha,
        "slice_sha256": source_sha,
        "total_lines": len(lines),
        "total_chars": len(content),
        "mode": "line",
        "offset": 0,
        "char_offset": 0,
        "lines_returned": len(lines),
        "next_offset": None,
        "next_char_offset": None,
        "partial_line": False,
        "truncated": False,
        "char_truncated": False,
        "line_truncated": False,
        "content": content,
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
        return _skill_bundle_file_json(rel_path, extra_files[rel_path])
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
    """Load prompt skill bundled files, preferring MinIO with config fallback."""
    extra_files: dict[str, str] = {}
    raw_extra = config.get("extra_files") or {}
    if isinstance(raw_extra, dict):
        extra_files.update({
            str(path).replace("\\", "/"): str(content)
            for path, content in raw_extra.items()
            if str(path).strip()
        })

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


def _append_skill_bundle_manifest(prompt: str, extra_files: dict[str, str]) -> str:
    if not extra_files:
        return prompt
    paths = sorted(extra_files)
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


# ── List ──

async def _seed_builtin_skills(db: AsyncSession) -> None:
    """Seed platform built-in skills (idempotent, best-effort)."""
    from packages.core.services.builtin_skill_loader import seed_builtin_skills
    try:
        await seed_builtin_skills(db)
        await db.commit()
    except Exception:
        await db.rollback()
        logger.warning("Built-in skill seed skipped", exc_info=True)


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
        select(Skill).where(*conditions).order_by(Skill.created_at.desc())
    )
    return list(result.scalars().all())


async def _workspace_operation_skill_ids(
    db: AsyncSession,
    *,
    entity_id: str,
    agent_id: str,
    workspace_id: str | None,
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
    service_keys = {
        str(sub.service_key or "").strip()
        for sub in subs
        if sub.agent_id == agent_id and str(sub.service_key or "").strip()
    }
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
    db: AsyncSession, entity_id: str, agent_id: str, *, workspace_id: str | None = None,
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
        .order_by(Skill.created_at.desc())
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
    conditions = [Skill.slug == slug, Skill.status == "active"]
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
        .order_by(priority.asc(), Skill.created_at.desc(), Skill.id.desc())
        .limit(1)
    )
    return result.scalars().first()


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
            # Persist the computed dir name so loads/deletes can resolve it
            skill.config = {**cfg, "minio_dir": minio_dir}
            await db.flush()

    return skill


# ── Update ──

async def update_skill(
    db: AsyncSession, skill_id: str, entity_id: str, **kwargs,
) -> Skill | None:
    """Update a skill owned by entity_id — DB record + MinIO files."""
    skill = await get_skill(db, skill_id)
    if not skill or skill.entity_id != entity_id:
        return None

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

    for key, value in kwargs.items():
        if value is not None and hasattr(skill, key):
            setattr(skill, key, value)

    # Auto-update slug if name changed
    if "name" in kwargs and kwargs["name"] is not None and "slug" not in kwargs:
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
        return None


# ── Skill type detection ──

def _determine_skill_type(skill: Skill) -> str:
    """Return 'sandbox' or 'prompt' based on skill config and on-disk files.

    Priority order:
      1. config.type explicitly set to 'sandbox' or 'prompt'
      2. config.scripts or config.requirements present  → sandbox
      3. builtin skill_dir contains script files        → sandbox
      4. default                                        → prompt
    """
    cfg = skill.config or {}
    declared = str(cfg.get("type", "")).strip().lower()
    if declared in ("sandbox", "prompt"):
        return declared

    if cfg.get("scripts") or cfg.get("requirements"):
        return "sandbox"

    skill_dir_str = cfg.get("skill_dir")
    if skill_dir_str:
        skill_dir = Path(skill_dir_str)
        if skill_dir.is_dir():
            for f in skill_dir.rglob("*"):
                if f.is_file() and (
                    f.suffix in _SCRIPT_EXTENSIONS or f.name == "requirements.txt"
                ):
                    return "sandbox"

    return "prompt"


async def _invoke_sandbox_skill(
    skill: Skill,
    entity_id: str,
    user_id: Optional[str],
    input_text: str,
    *,
    conversation_id: Optional[str] = None,
    on_sub_tool_start=None,
    on_sub_tool_end=None,
) -> dict:
    """Invoke a sandbox skill via the Sandbox Service.

    Flow:
      1. Collect all skill files from MinIO (or on-disk for platform skills).
      2. Load skill credentials from MinIO credentials.json.
      3. Try to reuse an existing sandbox for this conversation; otherwise create
         a new one via SandboxClient.create_from_builtin() for platform skills
         or SandboxClient.create_from_files() for entity skills.
      4. Return a compact execution contract (sandbox_id + skill index) as
         `content` so the parent LLM can drive execution via sandbox tools.
    """
    from packages.core.config import get_settings as _get_settings
    sandbox_url = _get_settings().SANDBOX_SERVICE_URL.strip()
    if not sandbox_url:
        return _sandbox_skill_error_response(
            skill,
            f"Sandbox service not configured (SANDBOX_SERVICE_URL unset). "
            f"Cannot run sandbox skill '{skill.name}'.",
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
        # Platform built-in skill: read from on-disk skill_dir
        skill_dir_str = cfg.get("skill_dir")
        if skill_dir_str:
            skill_dir_path = Path(skill_dir_str)
            if skill_dir_path.is_dir():
                for p in skill_dir_path.rglob("*"):
                    if p.is_file():
                        rel = str(p.relative_to(skill_dir_path))
                        try:
                            files[rel] = p.read_text(encoding="utf-8")
                        except Exception:
                            pass
        if not files:
            files["SKILL.md"] = instructions
        elif files.get("SKILL.md"):
            instructions = files["SKILL.md"]

    # Require at least one executable script for sandbox execution
    script_keys = [k for k in files if k not in ("SKILL.md", "requirements.txt", "credentials.json")]
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
    allowed_keys = list(env.keys())

    # Entity-filesystem mount (read-only)
    config_overrides = None
    expected_workspace_volume: str | None = None
    manor_fs_root = os.getenv("MANOR_FS_ROOT", "")
    if manor_fs_root and entity_id and os.getenv("MANOR_FS_ENABLED", "").lower() in ("true", "1"):
        entity_path = os.path.join(manor_fs_root, entity_id)
        if os.path.isdir(entity_path):
            expected_workspace_volume = f"{entity_path}:/workspace:ro"
            config_overrides = {"volumes": [expected_workspace_volume]}

    # ── 4. Create or reuse sandbox ──────────────────────────────────
    from packages.core.services.sandbox_sdk import SandboxClient
    from packages.core.services.sandbox_sdk.exceptions import SandboxError
    client = SandboxClient(base_url=sandbox_url, timeout=180.0)
    result_sandbox_id: str
    skill_info_parts: list[str] = []

    try:
        health = await client.health()
        if health.get("sandbox_image_available") is False:
            image = str(health.get("sandbox_image") or "sandbox-skill:latest")
            detail = str(health.get("sandbox_image_error") or "").strip()
            extra = f"\nDetails: {detail}" if detail else ""
            await client.close()
            return _sandbox_skill_error_response(
                skill,
                "Sandbox skill execution failed before the skill could run.\n"
                f"Sandbox runtime image '{image}' is not available on the "
                f"Sandbox Service Docker host.{extra}\n\n"
                f"Build/load the image with `docker build -t {image} "
                "-f docker/Dockerfile.sandbox .` or deploy the tagged "
                "sandbox skill image and set SANDBOX_IMAGE to that tag.",
            )
    except SandboxError as exc:
        logger.debug("[skill_service] sandbox health check failed before create: %s", exc)

    def _sandbox_has_expected_workspace_mount(info) -> bool:
        if not expected_workspace_volume:
            return True
        config = getattr(info, "config", {}) or {}
        volumes = config.get("volumes") if isinstance(config, dict) else None
        if not isinstance(volumes, list):
            return False
        expected_parts = expected_workspace_volume.split(":")
        for volume in volumes:
            if not isinstance(volume, str):
                continue
            parts = volume.split(":")
            if len(parts) >= 2 and parts[0] == expected_parts[0] and parts[1] == "/workspace":
                return True
        return False

    try:
        existing_ctx = await runtime_load_sandbox_context(conversation_id or "")
        existing_sandbox_id = (existing_ctx or {}).get("sandbox_id")

        if existing_sandbox_id:
            try:
                if expected_workspace_volume:
                    status = await client.status(existing_sandbox_id)
                    if not _sandbox_has_expected_workspace_mount(status):
                        logger.info(
                            "[skill_service] sandbox reuse skipped: missing /workspace mount skill=%s sandbox=%s",
                            skill.name, existing_sandbox_id,
                        )
                        existing_sandbox_id = None
                        raise RuntimeError("sandbox missing expected /workspace mount")
                load_result = await client.load_skill(
                    sandbox_id=existing_sandbox_id,
                    skill_name=skill.slug or skill.name,
                    files=files,
                    auto_install=True,
                    idle_threshold=RUNTIME_SANDBOX_IDLE_THRESHOLD,
                    source="workspace" if skill.entity_id else "builtin",
                )
                result_sandbox_id = existing_sandbox_id
                skill_info_parts.append(f"sandbox_id: {result_sandbox_id}  *(reused)*")
                if load_result.skill.scripts:
                    skill_info_parts.append(
                        f"scripts ({len(load_result.skill.scripts)}): "
                        f"{_compact_skill_items(load_result.skill.scripts)}"
                    )
                if load_result.skill.requirements_txt:
                    skill_info_parts.append("dependencies: re-installed from requirements.txt")
                logger.info(
                    "[skill_service] sandbox reused: skill=%s sandbox=%s",
                    skill.name, existing_sandbox_id,
                )
            except RuntimeError as reuse_skip:
                if str(reuse_skip) != "sandbox missing expected /workspace mount":
                    raise
            except SandboxError as busy_exc:
                if busy_exc.status_code == 409:
                    await client.close()
                    return _sandbox_skill_error_response(
                        skill,
                        f"The active sandbox is still busy. "
                        f"Complete the current task before invoking skill '{skill.name}', "
                        f"or wait a moment and retry.\nDetails: {busy_exc}",
                    )
                # Sandbox gone (404) or other error → create fresh
                logger.info(
                    "[skill_service] sandbox reuse failed (%s), creating new: skill=%s",
                    busy_exc, skill.name,
                )
                existing_sandbox_id = None

        if not existing_sandbox_id:
            try:
                create_kwargs = {
                    "skill_name": skill.slug or skill.name,
                    "files": files,
                    "env": env,
                    "allowed_sensitive_keys": allowed_keys,
                    "auto_install": True,
                    "config": config_overrides,
                }
                if skill.entity_id:
                    create_result = await client.create_from_files(**create_kwargs)
                else:
                    create_result = await client.create_from_builtin(**create_kwargs)
            except SandboxError as exc:
                logger.warning(
                    "[skill_service] sandbox create failed skill=%s status=%s error=%s",
                    skill.name, getattr(exc, "status_code", None), exc,
                )
                return _sandbox_skill_error_response(
                    skill,
                    "Sandbox skill execution failed before the skill could run.\n"
                    f"Details: {exc}\n\n"
                    "This skill is script-backed and must run through the Sandbox "
                    "Service. Check the sandbox service health, image availability, "
                    "and the built-in/workspace skill creation route.",
                )
            result_sandbox_id = create_result.sandbox_id
            skill_info_parts.append(f"sandbox_id: {result_sandbox_id}")
            if create_result.skill.entry_hint:
                skill_info_parts.append(f"entry_hint: {create_result.skill.entry_hint}")
            if create_result.skill.scripts:
                skill_info_parts.append(
                    f"scripts ({len(create_result.skill.scripts)}): "
                    f"{_compact_skill_items(create_result.skill.scripts)}"
                )
            if create_result.skill.requirements_txt:
                skill_info_parts.append("dependencies: installed from requirements.txt")
            if env:
                skill_info_parts.append(
                    f"credentials_injected ({len(env)}): {_compact_skill_items(set(env.keys()))}"
                )
            if create_result.env_blocked:
                skill_info_parts.append(
                    f"env_blocked ({len(create_result.env_blocked)}): "
                    f"{_compact_skill_items(create_result.env_blocked)}"
                )
            if conversation_id:
                await runtime_init_sandbox_context(
                    conversation_id,
                    result_sandbox_id,
                    skill.slug or skill.name,
                )
            logger.info(
                "[skill_service] sandbox created: skill=%s sandbox=%s entity=%s",
                skill.name, result_sandbox_id, entity_id or "(none)",
            )
    finally:
        await client.close()

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
    }


# ── Invoke ──

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
    task_id: Optional[str] = None,
    manual_skill_selected: bool = False,
    legacy_tool_profile: Optional[str] = None,
    allowed_tool_names: Optional[set[str]] = None,
    runtime_envelope: Any | None = None,
    metadata: Optional[dict] = None,
    model: Optional[str] = None,
) -> dict:
    """Invoke a skill — prompt or sandbox depending on skill type.

    1. Load skill by ID or slug
    2. Detect skill type (prompt vs sandbox)
    3a. Sandbox skill → create/reuse a Sandbox Service sandbox via SandboxClient; return a
        context block (sandbox_id + SKILL.md) for the parent LLM to drive via
        sandbox_exec / sandbox_destroy tool calls.
    3b. Prompt skill  → agentic_loop with skill's declared tools only.
    4. Return {content, usage, tools_used, rounds}
    """
    skill = await get_skill(db, skill_id_or_slug)
    if not skill:
        skill = await get_skill_by_slug(db, skill_id_or_slug, entity_id)
    if not skill:
        return {"error": f"Skill not found: {skill_id_or_slug}"}

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

            if not runtime_skill_allowed_on_surface(
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

        if is_public_customer_surface and agent_id:
            try:
                available = await list_agent_skill_bindings(db, agent_id, entity_id)
                if skill.id not in {item.id for item in available}:
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

        if is_public_customer_surface:
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

    if agent_id and enforce_agent_access:
        available = await list_skills_for_agent(
            db,
            entity_id,
            agent_id,
            workspace_id=workspace_id,
        )
        if skill.id not in {item.id for item in available}:
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

    skill_type = _determine_skill_type(skill)
    logger.info(
        "[skill_service] invoke skill=%s type=%s entity=%s",
        skill.name, skill_type, entity_id,
    )

    if skill_type == "sandbox":
        return await _invoke_sandbox_skill(
            skill, entity_id, user_id, input_text,
            conversation_id=conversation_id,
            on_sub_tool_start=on_sub_tool_start,
            on_sub_tool_end=on_sub_tool_end,
        )

    # Prompt skill — load system_prompt from MinIO when available, else fall back to DB
    minio_prompt: str | None = None
    if skill.entity_id:
        try:
            from packages.core.services.skill_file_storage import load_skill_prompt as _load_minio_prompt
            _cfg = skill.config or {}
            minio_prompt = _load_minio_prompt(
                skill.entity_id, skill.id,
                skill_dir=_cfg.get("minio_dir") or None,
                config=_cfg,
            )
        except Exception as exc:
            logger.debug("[skill_service] MinIO prompt load failed skill=%s: %s", skill.id, exc)

    config = skill.config or {}
    skill_extra_files = _load_prompt_skill_extra_files(skill, config)
    effective_prompt = minio_prompt if minio_prompt else skill.system_prompt
    effective_prompt = _append_skill_bundle_manifest(effective_prompt, skill_extra_files)
    from packages.core.ai.runtime import (
        runtime_execute_skill_agent_loop,
        runtime_prepare_prompt_skill_tool_surface,
        runtime_prompt_skill_registered_tool_executor,
        runtime_prompt_skill_tool_executor,
        runtime_prompt_skill_tool_schema_resolver,
        runtime_terminal_tool_result_policy_for_skill,
    )

    tool_surface = runtime_prepare_prompt_skill_tool_surface(
        skill,
        allowed_tool_names=allowed_tool_names,
        runtime_envelope=runtime_envelope,
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
    max_rounds = config.get("max_rounds", 100)
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
            active_user_message=input_text,
            manual_skill_selected=manual_skill_selected,
            legacy_tool_profile=legacy_tool_profile,
            allowed_tool_names=skill_allowed_tool_names,
            runtime_envelope=skill_runtime_envelope,
            skill_slug=(skill.slug or skill.name or ""),
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
    )

    runtime_policy = runtime_terminal_tool_result_policy_for_skill(skill)

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
        active_user_message=input_text,
        legacy_tool_profile=legacy_tool_profile,
        allowed_tool_names=skill_allowed_tool_names,
        tool_executor=skill_tool_executor,
        max_rounds=max_rounds,
        temperature=temperature,
        model=config.get("model") or model,
        metadata=metadata,
        on_tool_start=on_sub_tool_start,
        on_tool_end=on_sub_tool_end,
        tool_schema_resolver=skill_tool_schema_resolver,
        terminal_tool_result_policy=runtime_policy,
    )
    control = getattr(result, "control", None) or {}

    return {
        "skill": skill.name,
        "content": result.content,
        "usage": result.usage,
        "tools_used": result.tool_calls_made,
        "rounds": result.rounds,
        "stop_reason": result.stop_reason,
        "stop_parent": bool(control.get("stop_parent")),
        "notice_key": control.get("notice_key"),
        "replace_visible_text": bool(control.get("replace_visible_text")),
        "control": control,
        "error": getattr(result, "error", None),
        "limit_detail": getattr(result, "error_detail", None),
    }


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
