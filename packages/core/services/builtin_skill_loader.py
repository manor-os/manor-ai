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

import json
import logging
import re
from pathlib import Path
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.base import generate_ulid
from packages.core.models.skill import Skill


logger = logging.getLogger(__name__)

# packages/core/ai/skills/<name>/SKILL.md
_SKILLS_ROOT = (
    Path(__file__).resolve().parent.parent / "ai" / "skills"
)

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


async def seed_builtin_skills(db: AsyncSession) -> list[Skill]:
    """Read every packages/core/ai/skills/*/SKILL.md and upsert into `skills`.

    Called lazily (on first list_skills for an entity) and at app startup.
    Idempotent — re-running is a no-op unless the SKILL.md version changed.
    """
    if not _SKILLS_ROOT.exists():
        logger.debug("Built-in skills root missing: %s", _SKILLS_ROOT)
        return []

    results: list[Skill] = []
    for skill_dir in sorted(p for p in _SKILLS_ROOT.iterdir() if p.is_dir()):
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
        is_runtime_guidance = skill_config.get("type") == "runtime_guidance"
        expected_config = {"source": "builtin", "skill_dir": str(skill_dir)}
        if is_runtime_guidance:
            expected_config["type"] = "runtime_guidance"
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
        if (
            existing
            and existing.version == version
            and existing_tools == desired_tools
            and existing_config.get("source") == expected_config["source"]
            and existing_config.get("skill_dir") == expected_config["skill_dir"]
            and existing_config.get("type") == expected_config.get("type")
        ):
            results.append(existing)
            continue

        system_prompt = body.strip()

        if existing:
            existing.name = slug
            existing.display_name = slug
            existing.description = description
            existing.system_prompt = system_prompt
            existing.tools = desired_tools
            existing.version = version
            existing.is_public = True
            existing.config = expected_config
            existing.output_format = desired_output_format
            existing.category = desired_category
            existing.tags = desired_tags
            results.append(existing)
            logger.info("Built-in skill updated: %s (v%s)", slug, version)
        else:
            skill = Skill(
                id=generate_ulid(),
                entity_id=None,
                name=slug,
                slug=slug,
                display_name=slug,
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
