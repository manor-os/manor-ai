"""Read immutable GitHub Skill bundles and install them without executing code."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import quote, unquote, urlsplit

import httpx
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore[assignment]
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.skill import Skill
from packages.core.services.skill_bundle import SANDBOX_SKILL_TOOLS

logger = logging.getLogger(__name__)
GITHUB_API_BASE = "https://api.github.com/repos"
GITHUB_RAW_BASE = "https://raw.githubusercontent.com"
_TEXT_FILE_EXTS = {
    ".md", ".txt", ".json", ".json5", ".yaml", ".yml", ".py", ".sh", ".bash",
    ".js", ".ts", ".sql", ".csv", ".xml", ".html", ".htm", ".css", ".svg",
    ".toml", ".ini", ".cfg", ".rb", ".mjs", ".cjs",
}
_SCRIPT_EXTS = {".py", ".sh", ".bash", ".js", ".ts", ".rb", ".mjs", ".cjs"}
_LICENSE_NAMES = {"LICENSE", "LICENCE", "COPYING", "NOTICE"}
_MAX_FILES = 128
_MAX_DIRECTORIES = 32
_MAX_DEPTH = 8
_MAX_FILE_BYTES = 512 * 1024
_MAX_BUNDLE_BYTES = 4 * 1024 * 1024
_MAX_API_BYTES = 2 * 1024 * 1024
_FETCH_TIMEOUT_SECONDS = 90


def _safe_path(path: str, *, allow_empty: bool = False) -> str:
    if not path and allow_empty:
        return ""
    if (
        not path or path.startswith("/") or "\\" in path or ".." in path
        or any(ord(char) < 32 for char in path)
        or any(part in ("", ".", "..") for part in path.split("/"))
    ):
        raise ValueError("GitHub Skill contains an unsafe path")
    return path


def _parse_github_url(url: str) -> tuple[str, str, str, str]:
    """Return owner/repo/path/ref for GitHub tree, blob, raw or shorthand URLs."""
    value = url.strip()
    if "://" in value:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password:
            raise ValueError("Expected a public GitHub URL")
        if parsed.netloc not in {"github.com", "raw.githubusercontent.com"}:
            raise ValueError("Expected a public GitHub URL")
        parts = unquote(parsed.path).strip("/").split("/")
        if len(parts) < 2:
            raise ValueError("GitHub URL must identify a repository")
        owner, repo = parts[:2]
        if parsed.netloc == "raw.githubusercontent.com":
            if len(parts) < 4:
                raise ValueError("Raw GitHub URL must identify a Skill file")
            ref, path = parts[2], "/".join(parts[3:])
        elif len(parts) == 2:
            ref, path = "main", ""
        elif len(parts) >= 4 and parts[2] in {"tree", "blob"}:
            ref, path = parts[3], "/".join(parts[4:])
        else:
            raise ValueError("Expected a GitHub tree or blob URL")
    else:
        parts = value.strip("/").split("/")
        if len(parts) < 2:
            raise ValueError("Expected owner/repository[/skill-directory]")
        owner, repo = parts[:2]
        ref, path = "main", "/".join(parts[2:])
    if not all(
        re.fullmatch(r"[A-Za-z0-9_.-]+", item) and item not in {".", ".."}
        for item in (owner, repo, ref)
    ):
        raise ValueError("Invalid GitHub repository or ref")
    if path == "SKILL.md":
        path = ""
    elif path.endswith("/SKILL.md"):
        path = path[:-len("/SKILL.md")]
    _safe_path(path, allow_empty=True)
    return owner, repo, path, ref


def _parse_skill_frontmatter(content: str) -> dict[str, Any]:
    """Parse YAML frontmatter, retaining the body (also used by Marketplace)."""
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", content, re.DOTALL)
    if not match:
        return {"body": content}
    try:
        metadata = yaml.safe_load(match.group(1)) if yaml else None
        if isinstance(metadata, dict):
            return {**metadata, "body": match.group(2).strip()}
    except Exception:
        pass
    return {"body": match.group(2).strip()}


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        value = value.split(",")
    return [str(item).strip() for item in value if str(item).strip()] if isinstance(value, list) else []


@dataclass(frozen=True)
class GitHubSkillBundle:
    source_url: str
    commit_sha: str
    files: dict[str, str]
    metadata: dict[str, Any]
    system_prompt: str
    content_sha256: str

    def to_spec(self) -> dict[str, Any]:
        """Review input preserving source paths, not a basename-normalized bundle."""
        scripts = {
            path: content for path, content in self.files.items()
            if PurePosixPath(path).suffix.lower() in _SCRIPT_EXTS
        }
        return {
            "name": self.metadata["name"],
            "description": self.metadata.get("description", ""),
            "system_prompt": self.system_prompt,
            "tools": _string_list(self.metadata.get("tools")),
            "input_schema": self.metadata.get("input_schema") or {},
            "output_format": self.metadata.get("output_format") or "text",
            "scripts": scripts,
            "references": {
                path: content for path, content in self.files.items()
                if path not in scripts and path not in {"SKILL.md", "requirements.txt"}
            },
            "requirements": self.files.get("requirements.txt", ""),
        }


async def _fetch_bytes(client: httpx.AsyncClient, url: str, *, limit: int) -> bytes:
    chunks: list[bytes] = []
    size = 0
    async with client.stream("GET", url) as response:
        response.raise_for_status()
        async for chunk in response.aiter_bytes():
            size += len(chunk)
            if size > limit:
                raise ValueError("GitHub Skill download exceeds the size limit")
            chunks.append(chunk)
    return b"".join(chunks)


async def _fetch_json(client: httpx.AsyncClient, url: str) -> Any:
    return json.loads(await _fetch_bytes(client, url, limit=_MAX_API_BYTES))


async def _fetch_github_contents(
    client: httpx.AsyncClient, owner: str, repo: str, path: str, ref: str,
) -> list[dict]:
    url = f"{GITHUB_API_BASE}/{owner}/{repo}/contents/{quote(path, safe='/')}?ref={ref}"
    data = await _fetch_json(client, url)
    if not isinstance(data, list) or len(data) >= 1000:
        raise ValueError("GitHub Skill directory is unavailable or may be truncated")
    return data


async def fetch_github_skill_bundle(url: str) -> GitHubSkillBundle:
    """Read a complete bounded text bundle at one immutable commit; no writes."""
    owner, repo, path, ref = _parse_github_url(url)
    files: dict[str, str] = {}
    total_bytes = 0
    directories = 0
    async with asyncio.timeout(_FETCH_TIMEOUT_SECONDS), httpx.AsyncClient(
        timeout=20.0, follow_redirects=False,
    ) as client:
        commit = await _fetch_json(client, f"{GITHUB_API_BASE}/{owner}/{repo}/commits/{quote(ref, safe='')}")
        sha = commit.get("sha") if isinstance(commit, dict) else None
        if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", sha):
            raise ValueError("GitHub did not resolve an immutable commit")
        sha = sha.lower()
        pending = [(path, 0)]
        while pending:
            directory, depth = pending.pop()
            directories += 1
            if directories > _MAX_DIRECTORIES or depth > _MAX_DEPTH:
                raise ValueError("GitHub Skill exceeds the directory limit")
            entries = await _fetch_github_contents(client, owner, repo, directory, sha)
            for entry in sorted(entries, key=lambda item: str(item.get("path", ""))):
                entry_path = _safe_path(str(entry.get("path") or ""))
                if str(PurePosixPath(entry_path).parent) != (directory or "."):
                    raise ValueError("GitHub Skill directory entry escaped its source")
                if entry.get("target") or entry.get("submodule_git_url"):
                    raise ValueError("GitHub Skill symlinks and submodules require manual adaptation")
                if entry.get("type") == "dir":
                    pending.append((entry_path, depth + 1))
                    continue
                if entry.get("type") != "file":
                    raise ValueError("GitHub Skill contains an unsupported entry")
                relative = entry_path[len(path) + 1:] if path else entry_path
                name = PurePosixPath(relative).name
                if relative in {"config.json", "credentials.json"}:
                    raise ValueError(f"GitHub Skill contains reserved runtime file: {relative}")
                if (
                    PurePosixPath(name).suffix.lower() not in _TEXT_FILE_EXTS
                    and name.upper() not in _LICENSE_NAMES | {"MAKEFILE", "DOCKERFILE"}
                ):
                    raise ValueError(f"Unsupported GitHub Skill file requires adaptation: {relative}")
                if len(files) >= _MAX_FILES or relative in files:
                    raise ValueError("GitHub Skill exceeds the file limit or repeats a path")
                declared_size = entry.get("size")
                if not isinstance(declared_size, int) or declared_size < 0 or declared_size > _MAX_FILE_BYTES:
                    raise ValueError(f"GitHub Skill file exceeds the size limit: {relative}")
                raw_url = f"{GITHUB_RAW_BASE}/{owner}/{repo}/{sha}/{quote(entry_path, safe='/')}"
                raw = await _fetch_bytes(client, raw_url, limit=_MAX_FILE_BYTES)
                blob_hash = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
                if len(raw) != declared_size or entry.get("sha") != blob_hash:
                    raise ValueError(f"GitHub Skill file integrity check failed: {relative}")
                total_bytes += len(raw)
                if total_bytes > _MAX_BUNDLE_BYTES:
                    raise ValueError("GitHub Skill exceeds the total size limit")
                try:
                    content = raw.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise ValueError(f"GitHub Skill file is not UTF-8 text: {relative}") from exc
                if "\0" in content:
                    raise ValueError(f"GitHub Skill file contains binary data: {relative}")
                files[relative] = content
    if not files.get("SKILL.md", "").strip():
        raise FileNotFoundError("A non-empty SKILL.md is required at the source directory root")
    metadata = _parse_skill_frontmatter(files["SKILL.md"])
    body = metadata.pop("body")
    if not body.strip():
        raise ValueError("GitHub Skill instructions are empty")
    frontmatter = re.match(r"^---\s*\n(.*?)\n---\s*\n", files["SKILL.md"], re.DOTALL)
    if frontmatter:
        try:
            parsed_metadata = yaml.safe_load(frontmatter.group(1)) if yaml else None
        except Exception as exc:
            raise ValueError("GitHub Skill frontmatter is invalid") from exc
        if not isinstance(parsed_metadata, dict):
            raise ValueError("GitHub Skill frontmatter must be a YAML object")
    schema = metadata.get("input_schema", {})
    if not isinstance(schema, dict):
        raise ValueError("GitHub Skill input_schema must be an object")
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise ValueError("GitHub Skill input_schema is invalid") from exc
    metadata["name"] = str(metadata.get("name") or PurePosixPath(path).name or repo)
    source_url = f"https://github.com/{owner}/{repo}/tree/{sha}" + (f"/{quote(path, safe='/')}" if path else "")
    digest = hashlib.sha256(json.dumps(files, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return GitHubSkillBundle(source_url, sha, files, metadata, body, digest)


async def install_from_github(url: str, entity_id: str, db: AsyncSession) -> Skill:
    """Install only after immutable fetch and verified storage, never run scripts."""
    from packages.core.services.skill_service import create_skill
    from packages.core.services.skill_file_storage import (
        delete_skill_files, load_skill_extra_files, load_skill_prompt, save_skill_files,
    )

    bundle = await fetch_github_skill_bundle(url)
    extra_files = {path: body for path, body in bundle.files.items() if path != "SKILL.md"}
    executable_bundle = any(
        path != "requirements.txt"
        and PurePosixPath(path).name.upper().split(".")[0] not in _LICENSE_NAMES
        for path in extra_files
    )
    if "requirements.txt" in extra_files and not executable_bundle:
        raise ValueError("GitHub Skill dependencies require a packaged script or resource")
    config: dict[str, Any] = {
        "source": "github", "source_url": bundle.source_url,
        "source_commit": bundle.commit_sha, "content_sha256": bundle.content_sha256,
        "file_sha256": {path: hashlib.sha256(body.encode()).hexdigest() for path, body in bundle.files.items()},
        "source_license": {
            "declared": bundle.metadata.get("license"),
            "files": [path for path in extra_files if PurePosixPath(path).name.upper().split(".")[0] in _LICENSE_NAMES],
        },
        "type": "sandbox" if executable_bundle else "prompt",
        "extra_file_paths": sorted(extra_files),
    }
    env_vars = bundle.metadata.get("env_vars") or bundle.metadata.get("environment_variables")
    if env_vars:
        config["env_vars"] = env_vars
    tools = list(dict.fromkeys([
        *_string_list(bundle.metadata.get("tools")),
        *(SANDBOX_SKILL_TOOLS if executable_bundle else []),
    ]))
    directory = None
    try:
        async with db.begin_nested():
            skill = await create_skill(
                db, entity_id=entity_id, name=bundle.metadata["name"],
                system_prompt=bundle.system_prompt,
                description=bundle.metadata.get("description") or "", tools=tools,
                tags=_string_list(bundle.metadata.get("tags")) or ["github-import"],
                input_schema=bundle.metadata.get("input_schema") or {},
                output_format=bundle.metadata.get("output_format") or "text",
                config=config, category=bundle.metadata.get("category"),
            )
            # Only this newly allocated Skill's prefix; source config cannot control it.
            directory = skill.id
            if not save_skill_files(
                entity_id, directory, prompt=bundle.files["SKILL.md"], extra_files=extra_files,
                config_snapshot=config, skill_dir=directory,
            ):
                raise ValueError("GitHub Skill bundle could not be saved completely")
            if (
                load_skill_prompt(entity_id, directory, skill_dir=directory) != bundle.files["SKILL.md"]
                or load_skill_extra_files(entity_id, directory, skill_dir=directory) != extra_files
            ):
                raise ValueError("GitHub Skill storage verification failed")
            skill.config = {**config, "minio_dir": directory}
            await db.flush()
    except BaseException:
        if directory:
            try:
                delete_skill_files(entity_id, directory, skill_dir=directory)
            except Exception:
                logger.warning("Failed to clean incomplete GitHub Skill bundle", exc_info=True)
        raise
    return skill
