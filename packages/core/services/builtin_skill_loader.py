"""Built-in skill loader.

Seeds platform-level skills from SKILL.md files on disk into the `skills`
DB table. Each subdirectory under `packages/core/ai/skills/` is a built-in
skill. Scripts/requirements in that directory make the skill execute through
the Sandbox Service after the agent invokes it via `invoke_skill(...)`.

A skill row is (re)seeded if its DB `version` differs from the `version`
in the SKILL.md frontmatter, so edits to SKILL.md propagate on next
startup or first listing.

Platform skills live with `entity_id = NULL` + `is_public = True` so every
entity sees them without duplicating rows per tenant.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from functools import lru_cache
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.ai.runtime import builtin_skill_bundle
from packages.core.ai.runtime.skill_capability_companion import (
    SkillCapabilityCompanion,
)
from packages.core.ai.runtime.skill_invocation_policy import SkillInvocationPolicy
from packages.core.models.base import generate_ulid
from packages.core.models.skill import Skill


logger = logging.getLogger(__name__)

# packages/core/ai/skills/<name>/SKILL.md
_SKILLS_ROOT = (
    Path(__file__).resolve().parent.parent / "ai" / "skills"
)
_REPO_ROOT = Path(__file__).resolve().parents[3]
_AGENT_SKILLS_ROOT = _REPO_ROOT / ".agents" / "skills"
_PLATFORM_GUIDE_SKILL_NAMES = builtin_skill_bundle.PLATFORM_GUIDE_SKILL_NAMES
# Path markers that locate a built-in bundle inside any checkout or image.
_BUILTIN_SKILL_PATH_MARKERS = ("packages/core/ai/skills/", ".agents/skills/")


def portable_builtin_skill_dir(skill_dir: Path | str) -> str:
    """The bundle path relative to the repository root.

    ``config["skill_dir"]`` used to store this process's absolute path, so a
    seed run from another checkout (e.g. ``/tmp/pd``) and the image (``/app``)
    rewrote every built-in on each other's next turn, and readers in the image
    could not find files under the foreign path.
    """

    path = Path(skill_dir)
    try:
        return path.resolve().relative_to(_REPO_ROOT.resolve()).as_posix()
    except ValueError:
        text = path.as_posix()
        for marker in _BUILTIN_SKILL_PATH_MARKERS:
            index = text.find(marker)
            if index >= 0:
                return text[index:]
        return text


def resolve_builtin_skill_dir(value: str | None) -> Path | None:
    """Locate a stored built-in bundle path in this process's checkout."""

    text = str(value or "").strip()
    if not text:
        return None
    portable = portable_builtin_skill_dir(text) if Path(text).is_absolute() else text
    if Path(portable).is_absolute():
        return Path(portable)
    return _REPO_ROOT / portable

_RETIRED_BUILTIN_SKILL_SLUGS = builtin_skill_bundle.RETIRED_BUILTIN_SKILL_SLUGS

# Parent tools that script-backed built-in skills need after `invoke_skill`
# prepares the sandbox and returns a sandbox_id.
_DEFAULT_SKILL_TOOLS = [
    "invoke_skill",
    "search_tools",
    "generate_file",
    "sandbox",
]


def _parse_frontmatter(md_text: str) -> tuple[dict, str]:
    """Parse YAML-ish frontmatter block (simple key: value lines)."""
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", md_text, re.DOTALL)
    if not m:
        return {}, md_text

    fm_block, body = m.group(1), m.group(2)
    fm: dict = {}
    # Minimal parser — handles `key: value` and `key: "value"` (no nesting).
    for line in fm_block.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        v = v.strip().strip('"').strip("'")
        fm[k.strip()] = v
    return fm, body


def _read_skill_config(skill_dir: Path) -> dict:
    config_path = skill_dir / "config.json"
    if not config_path.exists():
        return {}
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except Exception as exc:
        logger.warning("Failed to read skill config %s: %s", config_path, exc)
        return {}


def _has_skill_manifest(skill_dir: Path) -> bool:
    return builtin_skill_bundle.has_skill_manifest(skill_dir)


def _builtin_skill_dirs(
    *,
    skills_root: Path | None = None,
    agent_skills_root: Path | None = None,
) -> list[Path]:
    """Return built-ins plus the guide for the current product edition.

    The selection lives in ``builtin_skill_bundle`` because the sandbox image
    bakes exactly this set (docker/Dockerfile.sandbox, ``baked-skills``).
    """

    return builtin_skill_bundle.builtin_skill_dirs(
        skills_root=skills_root or _SKILLS_ROOT,
        agent_skills_root=agent_skills_root or _AGENT_SKILLS_ROOT,
    )


# Set on ``db.info`` when a seed changed rows; callers that memoize a "seeded"
# state must not do so until those writes are durable.
BUILTIN_SKILL_SEED_WROTE_KEY = "builtin_skill_seed_wrote"


async def _retire_removed_builtin_skills(db: AsyncSession) -> None:
    """Deactivate packaged Skills retained only for migration compatibility."""
    for slug in _RETIRED_BUILTIN_SKILL_SLUGS:
        existing_rows = list((
            await db.execute(
                select(Skill).where(
                    Skill.entity_id.is_(None),
                    Skill.slug == slug,
                )
            )
        ).scalars().all())
        for existing in existing_rows:
            if existing.status != "inactive" or existing.is_public is not False:
                db.info[BUILTIN_SKILL_SEED_WROTE_KEY] = True
            existing.status = "inactive"
            existing.is_public = False


def _skill_source_sha256(skill_dir: Path) -> str:
    """Fingerprint the complete repository-controlled Skill bundle.

    The same function digests the sandbox image's baked copy, so equal
    digests mean the image holds exactly these files.
    """

    return builtin_skill_bundle.skill_source_sha256(skill_dir)


def _read_builtin_skill_extra_files(skill_dir: Path) -> dict[str, str]:
    """Read bundled reference files next to a built-in prompt skill."""
    extra_files: dict[str, str] = {}
    for dirname in ("references",):
        root = skill_dir / dirname
        if not root.exists() or not root.is_dir():
            continue
        for path in sorted(p for p in root.rglob("*") if p.is_file()):
            rel = path.relative_to(skill_dir).as_posix()
            try:
                extra_files[rel] = path.read_text(encoding="utf-8")
            except Exception as exc:
                logger.warning("Failed to read skill bundled file %s: %s", path, exc)
    return extra_files


def read_skill_dir_files(
    skill_dir: Path,
    *,
    max_file_bytes: int = 131072,
    max_total_bytes: int = 2_000_000,
) -> tuple[dict[str, str], list[dict]]:
    """Read a full on-disk skill bundle for display purposes.

    Returns ``(files, skipped)`` where ``files`` maps relative paths (with
    ``SKILL.md`` at the root) to text content, and ``skipped`` lists entries
    that were left out — binary files, files over ``max_file_bytes``, and
    anything past the ``max_total_bytes`` budget — as
    ``{"path": ..., "reason": "binary" | "too_large" | "omitted", "size": ...}``.
    Bundles like ``docx`` ship megabytes of XML schemas, so the caps keep the
    API response small while the viewer can still list every file.
    """
    files: dict[str, str] = {}
    skipped: list[dict] = []
    if not skill_dir.is_dir():
        return files, skipped

    exclude_names = {"config.json", "credentials.json"}
    total = 0
    paths = sorted(p for p in skill_dir.rglob("*") if p.is_file())
    for path in paths:
        rel = path.relative_to(skill_dir).as_posix()
        if rel in exclude_names:
            continue
        parts = rel.split("/")
        if any(part.startswith(".") or part == "__pycache__" for part in parts):
            continue
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size > max_file_bytes:
            skipped.append({"path": rel, "reason": "too_large", "size": size})
            continue
        if total + size > max_total_bytes:
            skipped.append({"path": rel, "reason": "omitted", "size": size})
            continue
        try:
            content = path.read_bytes().decode("utf-8")
        except (UnicodeDecodeError, OSError):
            skipped.append({"path": rel, "reason": "binary", "size": size})
            continue
        files[rel] = content
        total += size
    return files, skipped


def _read_builtin_skill_spec(skill_dir: Path) -> dict | None:
    """Build the trusted packaged behavior shared by seeding and portability."""
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.exists():
        return None

    try:
        text = skill_md.read_text(encoding="utf-8")
    except Exception as e:
        logger.warning("Failed to read %s: %s", skill_md, e)
        return None

    fm, body = _parse_frontmatter(text)
    slug = (fm.get("name") or skill_dir.name).strip()
    description = (fm.get("description") or "").strip()
    version = (fm.get("version") or "1.0.0").strip()
    skill_config = _read_skill_config(skill_dir)
    display_name = str(skill_config.get("display_name") or slug).strip()
    is_runtime_guidance = skill_config.get("type") == "runtime_guidance"
    expected_config = {
        "source": "builtin",
        "skill_dir": portable_builtin_skill_dir(skill_dir),
        "source_sha256": _skill_source_sha256(skill_dir),
    }
    for key in (
        "bundle_roots",
        "allowed_surfaces",
        "public_allowed_surfaces",
        "runtime_surfaces",
        "workspace_operations",
        "ledger_contracts",
        "execution_mode",
        "invocable_skills",
        "continuation_stages",
        "discoverable_provider_keys",
        "discoverable_tool_prefixes",
        "capability_companion",
        "invocation_policy",
        "plugin_key",
        "type",
        "max_rounds",
        "temperature",
        "model",
        "search",
        "groups",
    ):
        if key in skill_config:
            if key == "capability_companion":
                try:
                    companion = SkillCapabilityCompanion.from_config(
                        skill_config[key]
                    )
                except ValueError as exc:
                    raise ValueError(
                        f"Invalid capability_companion in "
                        f"{skill_dir / 'config.json'}: {exc}"
                    ) from exc
                expected_config[key] = companion.to_dict()
            elif key == "invocation_policy":
                try:
                    policy = SkillInvocationPolicy.from_config(skill_config[key])
                except ValueError as exc:
                    raise ValueError(
                        f"Invalid invocation_policy in {skill_dir / 'config.json'}: {exc}"
                    ) from exc
                expected_config[key] = policy.to_dict()
            elif key == "ledger_contracts":
                raw_contracts = skill_config[key]
                if (
                    not isinstance(raw_contracts, list)
                    or not raw_contracts
                    or not all(
                        isinstance(contract_id, str) and contract_id.strip()
                        for contract_id in raw_contracts
                    )
                ):
                    raise ValueError(
                        f"ledger_contracts in {skill_dir / 'config.json'} "
                        "must be an array of non-empty contract ids"
                    )
                expected_config[key] = list(dict.fromkeys(
                    contract_id.strip() for contract_id in raw_contracts
                ))
            elif key == "execution_mode":
                execution_mode = str(skill_config[key] or "").strip()
                if execution_mode != "instructions_only":
                    raise ValueError(
                        f"execution_mode in {skill_dir / 'config.json'} "
                        "must be 'instructions_only'"
                    )
                expected_config[key] = execution_mode
            elif key == "invocable_skills":
                slugs = skill_config[key]
                if not isinstance(slugs, list) or not all(
                    isinstance(slug, str) and slug.strip() for slug in slugs
                ):
                    raise ValueError(
                        f"invocable_skills in {skill_dir / 'config.json'} "
                        "must be an array of skill slugs"
                    )
                expected_config[key] = [slug.strip() for slug in slugs]
            elif key == "continuation_stages":
                from packages.core.ai.runtime.skills import runtime_skill_continuation_stages

                stages = runtime_skill_continuation_stages(skill_config)
                if not stages or list(stages) != skill_config[key]:
                    raise ValueError(
                        f"continuation_stages in {skill_dir / 'config.json'} must be a "
                        "non-empty array of unique snake_case stage names other than "
                        "'delivered' and 'blocked'"
                    )
                expected_config[key] = list(stages)
            else:
                expected_config[key] = skill_config[key]
    runtime_config = skill_config.get("runtime")
    if isinstance(runtime_config, dict) and isinstance(runtime_config.get("tools_optional"), bool):
        # Only the declared-tools opt-out; other runtime policy stays code-owned.
        expected_config["runtime"] = {"tools_optional": runtime_config["tools_optional"]}
    if skill_config.get("category") == "workspace-operations":
        from packages.core.ai.runtime.workspace_operation_skills import (
            workspace_operation_tool_contract_digest,
        )

        expected_config["tool_contract_sha256"] = workspace_operation_tool_contract_digest(skill_config)
    if is_runtime_guidance:
        expected_config["type"] = "runtime_guidance"
        bundled_extra_files = _read_builtin_skill_extra_files(skill_dir)
        if bundled_extra_files:
            expected_config["extra_files"] = bundled_extra_files
        if isinstance(skill_config.get("extra_files"), dict):
            configured_extra_files = {
                str(path).replace("\\", "/"): str(content)
                for path, content in skill_config.get("extra_files", {}).items()
                if str(path).strip()
            }
            expected_config["extra_files"] = {
                **dict(expected_config.get("extra_files") or {}),
                **configured_extra_files,
            }
        description = str(skill_config.get("description") or description).strip()
        desired_tools = list(skill_config.get("tools") or [])
        desired_output_format = str(skill_config.get("output_format") or "guidance")
        desired_category = str(skill_config.get("category") or "browser-automation")
        desired_tags = list(skill_config.get("tags") or [slug, "builtin", "runtime-guidance"])
    else:
        desired_tools = list(skill_config.get("tools") or _DEFAULT_SKILL_TOOLS)
        if not skill_config.get("tools"):
            # The default parent tools are a loader convenience for script
            # Skills, not the Skill's declaration of its own work.
            expected_config["tools_defaulted"] = True
        desired_output_format = str(skill_config.get("output_format") or "file")
        desired_category = str(
            skill_config.get("category") or "document-generation"
        )
        desired_tags = list(skill_config.get("tags") or [slug, "builtin"])

    return {
        "slug": slug, "name": slug, "display_name": display_name,
        "description": description, "version": version,
        "system_prompt": body.strip(), "tools": desired_tools,
        "input_schema": {"type": "object", "properties": {"input": {"type": "string"}}},
        "output_format": desired_output_format, "category": desired_category,
        "tags": desired_tags, "config": expected_config,
    }


def _packaged_builtin_skill_spec(slug: str) -> dict | None:
    # Resolve only repository-controlled directories, never a row's skill_dir.
    matches = []
    for skill_dir in _builtin_skill_dirs():
        manifest = skill_dir / "SKILL.md"
        if not manifest.is_file():
            continue
        frontmatter, _ = _parse_frontmatter(manifest.read_text(encoding="utf-8"))
        if str(frontmatter.get("name") or skill_dir.name).strip() == slug:
            matches.append(skill_dir)
    return _read_builtin_skill_spec(matches[0]) if len(matches) == 1 else None


@lru_cache(maxsize=1)
def _packaged_builtin_skill_dirs_by_slug() -> dict[str, Path]:
    found: dict[str, list[Path]] = {}
    for skill_dir in _builtin_skill_dirs():
        manifest = skill_dir / "SKILL.md"
        if manifest.is_file():
            frontmatter, _ = _parse_frontmatter(manifest.read_text(encoding="utf-8"))
            found.setdefault(str(frontmatter.get("name") or skill_dir.name).strip(), []).append(skill_dir)
    return {slug: dirs[0] for slug, dirs in found.items() if len(dirs) == 1}


@lru_cache(maxsize=None)
def packaged_builtin_skill_declaration(slug: str) -> SimpleNamespace | None:
    """A packaged built-in's tool declaration, read from config.json only.

    Skill-shaped (``slug``, ``tools``, ``config``) for the runtime's tool
    surface rules, without hashing the bundle or touching the database, so
    authoring checks can ask what a Skill needs wherever it is invoked.
    """
    from packages.core.services.skill_service import _determine_skill_type

    skill_dir = _packaged_builtin_skill_dirs_by_slug().get(str(slug or "").strip())
    if skill_dir is None:
        return None
    raw = _read_skill_config(skill_dir)
    guidance = raw.get("type") == "runtime_guidance"
    config: dict = {
        key: raw[key]
        for key in (
            "type", "execution_mode", "invocable_skills", "scripts", "requirements",
            "discoverable_provider_keys", "discoverable_tool_prefixes",
        )
        if key in raw
    }
    config.update(source="builtin", skill_dir=portable_builtin_skill_dir(skill_dir))
    if not guidance and not raw.get("tools"):
        config["tools_defaulted"] = True
    declaration = SimpleNamespace(
        id=None, slug=slug, name=slug, entity_id=None, config=config,
        tools=list(raw.get("tools") or ([] if guidance else _DEFAULT_SKILL_TOOLS)),
    )
    if not guidance and _determine_skill_type(declaration) == "sandbox":
        config["type"] = "sandbox"
    return declaration


async def resolve_packaged_builtin_skill(db: AsyncSession, slug: str) -> Skill:
    """Read the unique current packaged identity without repairing catalog rows.

    Historical duplicate IDs may remain callable and explicitly bound. Neither
    their slug nor a claimed digest proves equivalent portable behavior.
    """
    spec = _packaged_builtin_skill_spec(slug)
    if spec is None:
        raise ValueError("Built-in Skill is missing or ambiguous in this deployment")
    rows = (await db.execute(select(Skill).where(
        Skill.slug == slug, Skill.entity_id.is_(None),
        Skill.owner_user_id.is_(None), Skill.workspace_id.is_(None),
        Skill.is_public.is_(True), Skill.status == "active",
        Skill.config["source"].as_string() == "builtin",
        Skill.config["source_sha256"].as_string() == spec["config"]["source_sha256"],
    ))).scalars().all()
    behavior_fields = ("system_prompt", "tools", "input_schema", "output_format", "config", "version")
    matches = [row for row in rows if all(getattr(row, key) == spec[key] for key in behavior_fields)]
    if len(matches) != 1:
        raise ValueError("Built-in Skill is missing or ambiguous in this deployment")
    return matches[0]


async def seed_builtin_skills(db: AsyncSession) -> list[Skill]:
    """Read packaged built-ins and the edition-specific guide into `skills`.

    Called lazily (on first list_skills for an entity) and at app startup.
    Idempotent — re-running is a no-op unless the SKILL.md version changed.
    """
    db.info.pop(BUILTIN_SKILL_SEED_WROTE_KEY, None)
    await _retire_removed_builtin_skills(db)
    skill_dirs = _builtin_skill_dirs()
    if not skill_dirs:
        logger.debug(
            "Built-in skills roots missing: %s, %s",
            _SKILLS_ROOT,
            _AGENT_SKILLS_ROOT,
        )
        return []

    results: list[Skill] = []
    for skill_dir in skill_dirs:
        # A cold read hashes thousands of bundle files; other requests keep running.
        spec = await asyncio.to_thread(_read_builtin_skill_spec, skill_dir)
        if spec is None:
            continue
        slug = spec["slug"]
        description = spec["description"]
        version = spec["version"]
        display_name = spec["display_name"]
        system_prompt = spec["system_prompt"]
        desired_tools = spec["tools"]
        desired_output_format = spec["output_format"]
        desired_category = spec["category"]
        desired_tags = spec["tags"]
        expected_config = spec["config"]

        # Locate existing platform row (entity_id = NULL)
        existing: Optional[Skill] = (
            await db.execute(
                select(Skill).where(
                    Skill.entity_id.is_(None),
                    Skill.slug == slug,
                ).order_by(Skill.created_at.asc().nulls_last(), Skill.id.asc()).limit(1)
            )
        ).scalar_one_or_none()

        existing_config = existing.config if existing and isinstance(existing.config, dict) else {}
        existing_tools = list(existing.tools or []) if existing else []
        if (
            existing
            and existing.version == version
            and existing.display_name == display_name
            and existing.description == description
            and existing.system_prompt == system_prompt
            and existing_tools == desired_tools
            and existing_config.get("source") == expected_config["source"]
            and existing_config.get("skill_dir") == expected_config["skill_dir"]
            and existing_config.get("type") == expected_config.get("type")
            and existing_config.get("extra_files") == expected_config.get("extra_files")
            and existing_config.get("source_sha256") == expected_config["source_sha256"]
            and existing_config.get("tool_contract_sha256") == expected_config.get("tool_contract_sha256")
            and existing_config.get("tools_defaulted") == expected_config.get("tools_defaulted")
            and existing_config.get("runtime") == expected_config.get("runtime")
            and existing.is_public is True
            and existing.status == "active"
        ):
            results.append(existing)
            continue

        if existing:
            # M11: diff the behavior-affecting fields BEFORE overwriting so a
            # reseed that only refreshes cosmetics keeps the revision put.
            # (The equality guard above already short-circuits fully identical
            # rows; this catches the mixed case.)
            from packages.core.revisions import (
                SKILL_CONTENT_REVISION_FIELDS,
                bump_revision,
                content_patch_for,
            )
            content_patch = content_patch_for(
                existing,
                {
                    "system_prompt": system_prompt,
                    "tools": desired_tools,
                    "output_format": desired_output_format,
                    "config": expected_config,
                    "status": "active",
                },
                SKILL_CONTENT_REVISION_FIELDS,
            )
            existing.name = slug
            existing.display_name = display_name
            existing.description = description
            existing.system_prompt = system_prompt
            existing.tools = desired_tools
            existing.version = version
            existing.is_public = True
            existing.config = expected_config
            existing.output_format = desired_output_format
            existing.category = desired_category
            existing.tags = desired_tags
            existing.status = "active"
            if content_patch:
                await bump_revision(db, existing, patch=content_patch)
            db.info[BUILTIN_SKILL_SEED_WROTE_KEY] = True
            results.append(existing)
            logger.info("Built-in skill updated: %s (v%s)", slug, version)
        else:
            skill = Skill(
                id=generate_ulid(),
                entity_id=None,
                name=slug,
                slug=slug,
                display_name=display_name,
                description=description,
                system_prompt=system_prompt,
                tools=desired_tools,
                input_schema={"type": "object", "properties": {"input": {"type": "string"}}},
                output_format=desired_output_format,
                category=desired_category,
                tags=desired_tags,
                is_public=True,
                version=version,
                config=expected_config,
                status="active",
            )
            db.add(skill)
            db.info[BUILTIN_SKILL_SEED_WROTE_KEY] = True
            results.append(skill)
            logger.info("Built-in skill seeded: %s (v%s)", slug, version)

    await db.flush()
    return results
