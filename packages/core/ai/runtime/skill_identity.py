from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Protocol

from sqlalchemy import case, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.skill import Skill


class SkillCatalogKey(StrEnum):
    """Portable keys for platform Skills.

    Catalog keys are valid in packaged configuration and import/export payloads.
    Runtime invocation payloads must use the resolved entity-local ``Skill.id``.
    """

    CODING_BASED_VIDEO = "coding-based-video"
    VIDEO_EDIT = "video-edit"
    DOCX = "docx"
    PDF = "pdf"
    PPTX = "pptx"
    RESEARCH = "research"
    XLSX = "xlsx"


class SkillIdentityResolutionError(LookupError):
    pass


class SkillIdentityResolver(Protocol):
    async def resolve_catalog_ids(
        self,
        keys: Iterable[SkillCatalogKey | str],
    ) -> dict[str, str]: ...


@dataclass(frozen=True)
class DatabaseSkillIdentityResolver:
    """Resolve portable platform catalog keys to stable database IDs."""

    db: AsyncSession

    async def resolve_catalog_ids(
        self,
        keys: Iterable[SkillCatalogKey | str],
    ) -> dict[str, str]:
        requested = {
            str(key.value if isinstance(key, SkillCatalogKey) else key).strip()
            for key in keys
            if str(key.value if isinstance(key, SkillCatalogKey) else key).strip()
        }
        if not requested:
            return {}

        rows = list(
            (
                await self.db.execute(
                    select(Skill).where(
                        Skill.entity_id.is_(None),
                        Skill.status == "active",
                        Skill.slug.in_(requested),
                    )
                )
            )
            .scalars()
            .all()
        )
        resolved = {str(skill.slug): skill.id for skill in rows if skill.slug}
        missing = requested - set(resolved)
        if missing:
            # Built-ins are normally seeded at startup. Keep direct chat modes
            # reliable in fresh/self-hosted databases without committing the
            # caller's transaction from inside the resolver.
            from packages.core.services.builtin_skill_loader import seed_builtin_skills

            await seed_builtin_skills(self.db)
            rows = list(
                (
                    await self.db.execute(
                        select(Skill).where(
                            Skill.entity_id.is_(None),
                            Skill.status == "active",
                            Skill.slug.in_(requested),
                        )
                    )
                )
                .scalars()
                .all()
            )
            resolved = {str(skill.slug): skill.id for skill in rows if skill.slug}
            missing = requested - set(resolved)
        if missing:
            raise SkillIdentityResolutionError(
                "Required platform Skill(s) are not installed: " + ", ".join(sorted(missing))
            )
        return resolved


@dataclass(frozen=True)
class SkillInvocationFactory:
    """Build executable ``invoke_skill`` calls from already-resolved IDs."""

    skill_ids_by_catalog_key: Mapping[str, str]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "skill_ids_by_catalog_key",
            MappingProxyType(dict(self.skill_ids_by_catalog_key)),
        )

    def build(
        self,
        catalog_key: SkillCatalogKey | str,
        input_text: str,
        *,
        params: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        key = str(catalog_key.value if isinstance(catalog_key, SkillCatalogKey) else catalog_key).strip()
        skill_id = str(self.skill_ids_by_catalog_key.get(key) or "").strip()
        if not skill_id:
            raise SkillIdentityResolutionError(f"Skill catalog key was not resolved: {key}")
        arguments: dict[str, Any] = {
            "skill_id": skill_id,
            "input": input_text,
        }
        if params is not None:
            arguments["params"] = dict(params)
        return {"name": "invoke_skill", "arguments": arguments}


async def build_skill_invocation_factory(
    db: AsyncSession,
    keys: Iterable[SkillCatalogKey | str],
) -> SkillInvocationFactory:
    resolver: SkillIdentityResolver = DatabaseSkillIdentityResolver(db)
    return SkillInvocationFactory(await resolver.resolve_catalog_ids(keys))


async def materialize_portable_skill_id(
    db: AsyncSession,
    *,
    entity_id: str,
    skill_ref: str,
) -> str:
    """Resolve an import/config reference before it crosses into Runtime.

    This compatibility boundary is intentionally separate from ``invoke_skill``:
    user/model tool calls are ID-only, while portable Workflow and Blueprint
    definitions may use a slug because database IDs differ across installations.
    """

    ref = str(skill_ref or "").strip()
    if not ref:
        raise SkillIdentityResolutionError("Portable Skill reference is empty")
    priority = case((Skill.entity_id == entity_id, 0), else_=1)
    skill = (
        await db.execute(
            select(Skill)
            .where(
                Skill.status == "active",
                or_(Skill.entity_id == entity_id, Skill.entity_id.is_(None)),
                or_(Skill.id == ref, Skill.slug == ref),
            )
            .order_by(priority.asc(), Skill.created_at.desc(), Skill.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if skill is None:
        raise SkillIdentityResolutionError(f"Portable Skill reference was not found: {ref}")
    return skill.id


async def materialize_portable_skill_tool_calls(
    db: AsyncSession,
    *,
    entity_id: str,
    calls: Iterable[Mapping[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Convert portable Workflow tool calls to executable ID-only calls."""

    materialized: list[dict[str, Any]] = []
    for raw_call in calls or ():
        call = dict(raw_call)
        if str(call.get("name") or "") != "invoke_skill":
            materialized.append(call)
            continue
        arguments = dict(call.get("arguments") or {})
        legacy_ref = arguments.pop("skill", "")
        ref = str(arguments.get("skill_id") or legacy_ref or "").strip()
        arguments["skill_id"] = await materialize_portable_skill_id(
            db,
            entity_id=entity_id,
            skill_ref=ref,
        )
        call["arguments"] = arguments
        materialized.append(call)
    return materialized
