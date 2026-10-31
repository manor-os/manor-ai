"""Built-in Skills the sandbox image already holds: send a reference, not the files.

The sandbox image bakes every built-in Skill (docker/Dockerfile.sandbox,
``baked-skills``) with the content digest
``builtin_skill_bundle.skill_source_sha256`` computes, and the runner
advertises ``{name: digest}`` in ``/health`` (``baked_skills``). A sandbox
for a packaged built-in Skill is created (or loaded) with
``baked_skill={name, digest}`` instead of its files only when the runner's
digest equals this process's; otherwise — an old runner, an image built from
other Skill files, a runner that answers ``baked_skill_unavailable`` — the
files are read and uploaded exactly as before.

Only packaged built-in directories (the loader's set) ever get a reference:
workspace, custom and edited Skills always upload their own files.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

logger = logging.getLogger(__name__)

#: Runner error code: the image lacks the Skill at that digest; send the files.
BAKED_SKILL_UNAVAILABLE = "baked_skill_unavailable"


@dataclass(frozen=True)
class BakedSkillRef:
    """A packaged built-in Skill by name and content digest."""

    name: str
    digest: str

    def payload(self) -> dict[str, str]:
        return {"name": self.name, "digest": self.digest}

    @classmethod
    def from_payload(cls, raw: Any) -> "BakedSkillRef | None":
        if not isinstance(raw, Mapping):
            return None
        name, digest = str(raw.get("name") or ""), str(raw.get("digest") or "")
        return cls(name=name, digest=digest) if name and digest else None


def _packaged_skill_dirs() -> list[Path]:
    # The loader's own selection, so the set matches what it seeds (and what
    # the image bakes: both call builtin_skill_bundle.builtin_skill_dirs).
    from packages.core.services.builtin_skill_loader import _builtin_skill_dirs

    return _builtin_skill_dirs()


def packaged_skill_dir(name: str) -> Path | None:
    """The packaged built-in directory named ``name``, if this process ships one."""

    for skill_dir in _packaged_skill_dirs():
        if skill_dir.name == name:
            return skill_dir
    return None


def baked_skill_ref(skill_dir: Path | None) -> BakedSkillRef | None:
    """Reference for ``skill_dir`` when it is one of the packaged built-ins.

    The digest is this process's view of the files (cached on their stat
    signature), so a runner holding the same digest holds the same bytes.
    """

    if skill_dir is None:
        return None
    try:
        resolved = skill_dir.resolve()
    except OSError:
        return None
    for candidate in _packaged_skill_dirs():
        try:
            if candidate.resolve() != resolved:
                continue
        except OSError:
            continue
        from packages.core.ai.runtime.builtin_skill_bundle import skill_source_sha256

        return BakedSkillRef(name=candidate.name, digest=skill_source_sha256(candidate))
    return None


def baked_skill_ref_for_name(name: str) -> BakedSkillRef | None:
    return baked_skill_ref(packaged_skill_dir(name))


def read_skill_upload_files(skill_dir: Path) -> dict[str, str]:
    """The files a built-in Skill uploads: every UTF-8 file under ``skill_dir``.

    The pre-baking upload path, unchanged: binary files are left out.
    """

    files: dict[str, str] = {}
    if not skill_dir.is_dir():
        return files
    for path in skill_dir.rglob("*"):
        if path.is_file():
            rel = str(path.relative_to(skill_dir))
            try:
                files[rel] = path.read_text(encoding="utf-8")
            except Exception:  # noqa: BLE001 - binary or unreadable: not uploaded
                pass
    return files


def packaged_skill_file_paths(name: str) -> list[str]:
    """Relative paths of a packaged Skill's files (no contents read)."""

    from packages.core.ai.runtime.builtin_skill_bundle import skill_bundle_files

    skill_dir = packaged_skill_dir(name)
    return [rel for rel, _ in skill_bundle_files(skill_dir)] if skill_dir is not None else []


def runner_baked_digest(health: Mapping[str, Any] | None, name: str) -> str | None:
    baked = (health or {}).get("baked_skills")
    if not isinstance(baked, Mapping):
        return None
    digest = baked.get(name)
    return digest if isinstance(digest, str) and digest else None


def runner_holds(health: Mapping[str, Any] | None, ref: BakedSkillRef | None) -> bool:
    """Whether the runner advertises exactly ``ref`` (an old runner never does)."""

    return ref is not None and runner_baked_digest(health, ref.name) == ref.digest


async def _health(client) -> Mapping[str, Any]:
    try:
        health = await client.health()
    except Exception as exc:  # noqa: BLE001 - unknown runner: upload
        logger.info("[sandbox.baked] runner health unavailable; uploading Skill files: %s", exc)
        return {}
    return health if isinstance(health, Mapping) else {}


def _is_unavailable(exc: BaseException) -> bool:
    return getattr(exc, "code", None) == BAKED_SKILL_UNAVAILABLE


def _upload_files(files: Mapping[str, str] | None, baked: BakedSkillRef | None) -> dict[str, str]:
    if files:
        return dict(files)
    if baked is not None:
        skill_dir = packaged_skill_dir(baked.name)
        if skill_dir is not None:
            return read_skill_upload_files(skill_dir)
    return dict(files or {})


async def create_builtin_sandbox(
    client,
    *,
    skill_name: str,
    files: Mapping[str, str] | None,
    baked: BakedSkillRef | None,
    health: Mapping[str, Any] | None = None,
    **kwargs: Any,
):
    """``client.create_from_builtin`` that sends ``baked`` instead of files when it can.

    ``files=None`` (or empty) with a reference reads the packaged files only
    if they must be uploaded. ``health`` is the runner's ``/health`` when the
    caller already has it.
    """

    if baked is not None:
        if health is None:
            health = await _health(client)
        if runner_holds(health, baked):
            try:
                return await client.create_from_builtin(
                    skill_name=skill_name, files={}, baked_skill=baked.payload(), **kwargs
                )
            except Exception as exc:
                if not _is_unavailable(exc):
                    raise
                logger.info(
                    "[sandbox.baked] runner refused baked Skill %s@%s; uploading: %s",
                    baked.name, baked.digest[:12], exc,
                )
        else:
            logger.info(
                "[sandbox.baked] runner does not hold %s@%s (has %s); uploading",
                baked.name, baked.digest[:12], (runner_baked_digest(health, baked.name) or "none")[:12],
            )
    return await client.create_from_builtin(
        skill_name=skill_name, files=_upload_files(files, baked), **kwargs
    )


async def load_builtin_skill(
    client,
    *,
    sandbox_id: str,
    skill_name: str,
    files: Mapping[str, str] | None,
    baked: BakedSkillRef | None,
    health: Mapping[str, Any] | None = None,
    **kwargs: Any,
):
    """``client.load_skill`` with the same baked-or-upload choice as create."""

    if baked is not None:
        if health is None:
            health = await _health(client)
        if runner_holds(health, baked):
            try:
                return await client.load_skill(
                    sandbox_id=sandbox_id,
                    skill_name=skill_name,
                    files={},
                    baked_skill=baked.payload(),
                    **kwargs,
                )
            except Exception as exc:
                if not _is_unavailable(exc):
                    raise
                logger.info(
                    "[sandbox.baked] runner refused baked Skill %s@%s on load; uploading: %s",
                    baked.name, baked.digest[:12], exc,
                )
    return await client.load_skill(
        sandbox_id=sandbox_id,
        skill_name=skill_name,
        files=_upload_files(files, baked),
        **kwargs,
    )
