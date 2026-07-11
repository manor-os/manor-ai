"""GitHub skill installer — fetch a skill from a GitHub repo and persist it."""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Optional

import json

import httpx

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore[assignment]
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.skill import Skill

logger = logging.getLogger(__name__)

GITHUB_API_BASE = "https://api.github.com/repos"
GITHUB_RAW_BASE = "https://raw.githubusercontent.com"

_TEXT_FILE_EXTS = {
    ".md", ".txt", ".json", ".json5", ".yaml", ".yml",
    ".py", ".sh", ".bash", ".js", ".ts",
    ".sql", ".csv", ".xml", ".html", ".htm", ".css", ".svg",
    ".toml", ".ini", ".cfg",
}


# ---------------------------------------------------------------------------
# URL parsing
# ---------------------------------------------------------------------------

def _parse_github_url(url: str) -> tuple[str, str, str, str]:
    """Parse a GitHub URL into (owner, repo, path, ref).

    Supported formats:
      - https://github.com/owner/repo/tree/branch/path/to/skill
      - https://github.com/owner/repo/blob/branch/path/to/SKILL.md
      - https://raw.githubusercontent.com/owner/repo/branch/path/to/SKILL.md
      - owner/repo/path/to/skill  (assumes ``main`` branch)
      - https://github.com/owner/repo  (repo root, ``main``)

    Raises :class:`ValueError` if the URL cannot be parsed.
    """
    url = url.strip()

    # raw.githubusercontent.com/owner/repo/branch/path/...
    m = re.match(
        r"https?://raw\.githubusercontent\.com/([^/]+)/([^/]+)/([^/]+)/(.+)", url,
    )
    if m:
        owner, repo, ref, path = m.groups()
        if path.endswith("/SKILL.md") or path == "SKILL.md":
            path = path.rsplit("/SKILL.md", 1)[0] or ""
        return owner, repo, path, ref

    # github.com/owner/repo/tree|blob/branch/path
    m = re.match(
        r"https?://github\.com/([^/]+)/([^/]+)/(tree|blob)/([^/]+)(?:/(.*))?", url,
    )
    if m:
        owner, repo, _, ref, path = m.groups()
        path = (path or "").rstrip("/")
        if path.endswith("/SKILL.md") or path.endswith("SKILL.md"):
            path = path.rsplit("/SKILL.md", 1)[0] or ""
        return owner, repo, path, ref

    # github.com/owner/repo (repo root)
    m = re.match(r"https?://github\.com/([^/]+)/([^/]+)/?$", url)
    if m:
        owner, repo = m.groups()
        return owner, repo, "", "main"

    # Shorthand: owner/repo or owner/repo/path
    m = re.match(r"^([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)(?:/(.+))?$", url)
    if m:
        owner, repo, path = m.groups()
        path = (path or "").rstrip("/")
        if path.endswith("/SKILL.md") or path == "SKILL.md":
            path = path.rsplit("/SKILL.md", 1)[0] or ""
        return owner, repo, path, "main"

    raise ValueError(f"Could not parse GitHub URL: {url}")


# ---------------------------------------------------------------------------
# GitHub fetchers
# ---------------------------------------------------------------------------

async def _fetch_github_contents(
    client: httpx.AsyncClient,
    owner: str,
    repo: str,
    path: str,
    ref: str,
) -> list[dict]:
    """Fetch a directory listing from the GitHub Contents API.

    Returns a list of file/dir dicts, or an empty list on failure.
    """
    contents_path = path or ""
    url = f"{GITHUB_API_BASE}/{owner}/{repo}/contents/{contents_path}?ref={ref}"
    resp = await client.get(url)
    resp.raise_for_status()
    data = resp.json()
    return data if isinstance(data, list) else []


# ---------------------------------------------------------------------------
# Frontmatter parser
# ---------------------------------------------------------------------------

def _parse_skill_frontmatter(content: str) -> dict[str, Any]:
    """Parse YAML frontmatter from SKILL.md content.

    Returns the parsed dict (may be empty if no frontmatter found).
    The ``body`` key contains the markdown body after the frontmatter.
    """
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", content, re.DOTALL)
    if not m:
        return {"body": content}
    fm_raw, body = m.group(1), m.group(2)
    try:
        fm = yaml.safe_load(fm_raw)
        if isinstance(fm, dict):
            fm["body"] = body.strip()
            return fm
    except Exception:
        pass
    return {"body": body.strip()}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def install_from_github(
    url: str,
    entity_id: str,
    db: AsyncSession,
) -> Skill:
    """Install a skill from a GitHub repository URL.

    The target directory must contain a ``SKILL.md`` file with optional
    YAML frontmatter (name, description, tags, env_vars, tools).
    Sibling text files are stored in the skill's ``config`` dict.

    Returns the created :class:`Skill`.
    """
    from packages.core.services.skill_service import create_skill

    owner, repo, path, ref = _parse_github_url(url)

    skill_md_path = f"{path}/SKILL.md" if path else "SKILL.md"
    raw_url = f"{GITHUB_RAW_BASE}/{owner}/{repo}/{ref}/{skill_md_path}"

    extra_files: dict[str, str] = {}

    async with httpx.AsyncClient(timeout=20.0) as client:
        # 1. Fetch SKILL.md
        resp = await client.get(raw_url)
        if resp.status_code == 404:
            raise FileNotFoundError(f"SKILL.md not found at {raw_url}")
        resp.raise_for_status()
        skill_md_content = resp.text

        # 2. Parse frontmatter
        meta = _parse_skill_frontmatter(skill_md_content)
        body = meta.pop("body", skill_md_content)

        name = meta.get("name") or (path.split("/")[-1] if path else repo)
        description = meta.get("description") or ""
        tags = meta.get("tags") or []
        if isinstance(tags, str):
            tags = [t.strip() for t in tags.split(",") if t.strip()]
        tools = meta.get("tools") or []
        if isinstance(tools, str):
            tools = [t.strip() for t in tools.split(",") if t.strip()]

        # 3. Fetch sibling files concurrently
        try:
            dir_items = await _fetch_github_contents(client, owner, repo, path, ref)
            sibling_paths = [
                item["path"]
                for item in dir_items
                if item.get("type") == "file"
                and item.get("name") not in ("SKILL.md", "config.json")
                and any(item["name"].lower().endswith(ext) for ext in _TEXT_FILE_EXTS)
            ]

            async def _fetch_file(fp: str) -> Optional[tuple[str, str]]:
                file_url = f"{GITHUB_RAW_BASE}/{owner}/{repo}/{ref}/{fp}"
                r = await client.get(file_url)
                if r.status_code == 200:
                    base_prefix = f"{path}/" if path else ""
                    rel = fp[len(base_prefix):]
                    return (rel, r.text) if rel else None
                return None

            results = await asyncio.gather(
                *[_fetch_file(fp) for fp in sibling_paths[:20]],
                return_exceptions=True,
            )
            for result in results:
                if isinstance(result, tuple):
                    extra_files[result[0]] = result[1]
        except Exception as e:
            logger.debug("Failed to fetch sibling files from %s/%s: %s", owner, repo, e)

    # 4. Build source URL
    source_url = (
        f"https://github.com/{owner}/{repo}/tree/{ref}/{path}"
        if path
        else f"https://github.com/{owner}/{repo}"
    )

    # 5. Build a lean DB config — no raw file content embedded in JSONB.
    #    Actual file content lives in MinIO; only record paths here.
    config: dict[str, Any] = {
        "source": "github",
        "source_url": source_url,
        "extra_file_paths": sorted(extra_files.keys()),
    }
    env_vars = meta.get("env_vars") or meta.get("environment_variables")
    if env_vars:
        config["env_vars"] = env_vars

    # 6. Persist DB record via skill_service
    skill = await create_skill(
        db,
        entity_id=entity_id,
        name=name,
        system_prompt=body,
        description=description,
        tools=tools,
        tags=tags or ["github-import"],
        config=config,
        category=meta.get("category"),
    )

    # 7. Write auxiliary files (scripts, requirements.txt, …) to the same
    #    MinIO directory that create_skill already opened for SKILL.md.
    if extra_files:
        try:
            from packages.core.services.skill_file_storage import save_skill_files
            cfg_now = skill.config or {}
            minio_dir = cfg_now.get("minio_dir") or None
            save_skill_files(
                entity_id,
                skill.id,
                prompt=body,              # re-write SKILL.md for consistency
                extra_files=extra_files,
                skill_dir=minio_dir,
            )
            logger.info(
                "install_from_github: saved %d extra file(s) to MinIO dir=%s skill=%s entity=%s",
                len(extra_files), minio_dir, skill.id, entity_id,
            )
        except Exception as exc:
            logger.warning(
                "install_from_github: MinIO extra-files save failed skill=%s entity=%s: %s",
                skill.id, entity_id, exc,
            )

    logger.info(
        "Installed skill from GitHub: %s/%s -> %s (entity=%s, files=%d)",
        owner, repo, skill.id, entity_id, len(extra_files),
    )
    return skill
