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

import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

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
_PLATFORM_GUIDE_SKILL_NAMES = ("cloud-intro", "intro")
_RETIRED_BUILTIN_SKILL_SLUGS = frozenset({
    "mcp_desktop_recording",
    # Retired until packages/core/ai/mcp/discord.py exists — the pack
    # instructed agents to call mcp__discord__* tools that had no
    # dispatchable module behind them.
    "mcp_discord",
})

# Parent tools that script-backed built-in skills need after `invoke_skill`
# prepares the sandbox and returns a sandbox_id.
_DEFAULT_SKILL_TOOLS = [
    "invoke_skill",
    "search_tools",
    "generate_file",
    "sandbox_exec",
    "sandbox_read_file",
    "sandbox_write_file",
    "sandbox_save_result",
    "sandbox_destroy",
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
    return skill_dir.is_dir() and (skill_dir / "SKILL.md").is_file()


def _builtin_skill_dirs(
    *,
    skills_root: Path | None = None,
    agent_skills_root: Path | None = None,
) -> list[Path]:
    """Return built-ins plus the guide for the current product edition.

    Cloud source trees contain ``cloud-intro`` while OSS exports remove that
    directory. Prefer the Cloud guide when it exists; otherwise register the
    OSS ``intro`` guide. Other repository-maintenance Skills under ``.agents``
    are intentionally never exposed to Manor users.
    """

    package_root = skills_root or _SKILLS_ROOT
    project_root = agent_skills_root or _AGENT_SKILLS_ROOT
    directories = (
        sorted(
            path
            for path in package_root.iterdir()
            if path.is_dir() and path.name not in _RETIRED_BUILTIN_SKILL_SLUGS
        )
        if package_root.exists()
        else []
    )
    for name in _PLATFORM_GUIDE_SKILL_NAMES:
        candidate = project_root / name
        if _has_skill_manifest(candidate):
            directories.append(candidate)
            break
    return directories


async def _retire_removed_builtin_skills(db: AsyncSession) -> None:
    """Deactivate packaged Skills retained only for migration compatibility."""
    for slug in _RETIRED_BUILTIN_SKILL_SLUGS:
        existing = (
            await db.execute(
                select(Skill).where(
                    Skill.entity_id.is_(None),
                    Skill.slug == slug,
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            continue
        existing.status = "inactive"
        existing.is_public = False


def _skill_source_sha256(skill_dir: Path) -> str:
    """Fingerprint the complete repository-controlled Skill bundle."""

    digest = hashlib.sha256()
    for path in sorted(skill_dir.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(skill_dir)
        if (
            "__pycache__" in relative.parts
            or path.suffix == ".pyc"
            or path.name == ".DS_Store"
        ):
            continue
        digest.update(relative.as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


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


async def seed_builtin_skills(db: AsyncSession) -> list[Skill]:
    """Read packaged built-ins and the edition-specific guide into `skills`.

    Called lazily (on first list_skills for an entity) and at app startup.
    Idempotent — re-running is a no-op unless the SKILL.md version changed.
    """
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
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.exists():
            continue

        try:
            text = skill_md.read_text(encoding="utf-8")
        except Exception as e:
            logger.warning("Failed to read %s: %s", skill_md, e)
            continue

        fm, body = _parse_frontmatter(text)
        slug = (fm.get("name") or skill_dir.name).strip()
        description = (fm.get("description") or "").strip()
        version = (fm.get("version") or "1.0.0").strip()
        skill_config = _read_skill_config(skill_dir)
        display_name = str(skill_config.get("display_name") or slug).strip()
        is_runtime_guidance = skill_config.get("type") == "runtime_guidance"
        expected_config = {
            "source": "builtin",
            "skill_dir": str(skill_dir),
            "source_sha256": _skill_source_sha256(skill_dir),
        }
        for key in (
            "bundle_roots",
            "allowed_surfaces",
            "public_allowed_surfaces",
            "runtime_surfaces",
            "invocation_policy",
            "max_rounds",
            "temperature",
            "model",
        ):
            if key in skill_config:
                if key == "invocation_policy":
                    try:
                        policy = SkillInvocationPolicy.from_config(skill_config[key])
                    except ValueError as exc:
                        raise ValueError(
                            f"Invalid invocation_policy in {skill_dir / 'config.json'}: {exc}"
                        ) from exc
                    expected_config[key] = policy.to_dict()
                else:
                    expected_config[key] = skill_config[key]
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
            desired_tools = _DEFAULT_SKILL_TOOLS
            desired_output_format = "file"
            desired_category = "document-generation"
            desired_tags = [slug, "builtin"]

        # Locate existing platform row (entity_id = NULL)
        existing: Optional[Skill] = (
            await db.execute(
                select(Skill).where(
                    Skill.entity_id.is_(None),
                    Skill.slug == slug,
                )
            )
        ).scalar_one_or_none()

        existing_config = existing.config if existing and isinstance(existing.config, dict) else {}
        existing_tools = list(existing.tools or []) if existing else []
        system_prompt = body.strip()
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
            results.append(skill)
            logger.info("Built-in skill seeded: %s (v%s)", slug, version)

    await db.flush()
    return results
