"""Code project rules: profiles, folder layout, asset sources, and validation.

A code project is a folder with a durable id (``CodeProject``). Each project
declares a profile that decides which files it may hold, how large it may
grow, which checks run after every write, and where it can be published.
Every validator and publish target named here must have a real
implementation; ``tests/test_code_project_tool.py`` keeps that 1:1.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Callable
from urllib.parse import urljoin, urlsplit

import httpx

from packages.core.services import site_publisher
from packages.core.services.file_refs import parse_file_reference

NON_WORKSPACE_PROJECTS_DIR = "Code"
WORKSPACE_PROJECTS_SUBDIR = "code"
MAX_VALIDATION_ERRORS = 50
_MAX_DOWNLOAD_REDIRECTS = 3

_TEXT_EXTENSIONS = frozenset({
    ".html", ".htm", ".css", ".scss", ".sass", ".less", ".js", ".mjs", ".cjs",
    ".ts", ".tsx", ".jsx", ".vue", ".svelte", ".json", ".webmanifest", ".map",
    ".md", ".markdown", ".txt", ".xml", ".svg", ".csv", ".tsv", ".yaml", ".yml",
    ".toml", ".ini", ".py", ".sh", ".sql",
})
_BINARY_ASSET_EXTENSIONS = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif", ".ico",
    ".woff", ".woff2", ".ttf", ".otf", ".eot",
    ".mp3", ".mp4", ".webm", ".pdf",
})


class CodeProjectError(ValueError):
    """A request that a code project rule rejects; the message is user-facing."""


def _site_link_errors(root_abs: str) -> list[str]:
    if not os.path.isdir(root_abs):
        return []
    try:
        snapshot = site_publisher.collect_snapshot(root_abs)
    except site_publisher.SitePublishError as exc:
        return [str(exc)]
    errors = []
    if not any(item.rel == "index.html" for item in snapshot.included):
        errors.append("index.html missing from project root")
    return errors + site_publisher.check_links(root_abs, snapshot)


VALIDATORS: dict[str, Callable[[str], list[str]]] = {
    "site_links": _site_link_errors,
}

# Publish target -> the agent tool that performs it.
PUBLISH_TARGET_TOOLS: dict[str, str] = {
    "site": "publish_site",
}


@dataclass(frozen=True)
class CodeProjectProfile:
    id: str
    description: str
    writable_extensions: frozenset[str]
    max_file_bytes: int
    max_total_bytes: int
    max_files: int
    validators: tuple[str, ...] = ()
    publish_targets: tuple[str, ...] = ()
    default_entry: str | None = None


PROFILES: dict[str, CodeProjectProfile] = {
    "static_site": CodeProjectProfile(
        id="static_site",
        description=(
            "Static website: browser-ready HTML/CSS/JS plus images, fonts and media. "
            "Each page gets its own address: about.html is /about, pricing/index.html is "
            "/pricing/; add 404.html to answer unknown addresses. Links are checked after "
            "every write; publish with publish_site."
        ),
        writable_extensions=frozenset(site_publisher.ALLOWED_EXTENSIONS | {".md"}),
        max_file_bytes=site_publisher.MAX_FILE_BYTES,
        max_total_bytes=site_publisher.MAX_TOTAL_BYTES,
        max_files=site_publisher.MAX_FILES,
        validators=("site_links",),
        publish_targets=("site",),
        default_entry="index.html",
    ),
    "generic": CodeProjectProfile(
        id="generic",
        description="Any source or data project: text files, data files, images, fonts, media and PDFs.",
        writable_extensions=_TEXT_EXTENSIONS | _BINARY_ASSET_EXTENSIONS,
        max_file_bytes=10 * 1024 * 1024,
        max_total_bytes=200 * 1024 * 1024,
        max_files=2000,
    ),
    "release_package": CodeProjectProfile(
        id="release_package",
        description="Music release materials with supplied master audio and cover artwork.",
        writable_extensions=_TEXT_EXTENSIONS | _BINARY_ASSET_EXTENSIONS | {
            ".wav", ".flac", ".aiff", ".aif",
        },
        max_file_bytes=250 * 1024 * 1024,
        max_total_bytes=500 * 1024 * 1024,
        max_files=100,
    ),
}


def get_profile(profile_id: str | None) -> CodeProjectProfile:
    profile = PROFILES.get(str(profile_id or "").strip())
    if profile is None:
        raise CodeProjectError(
            f"unknown profile {profile_id!r}; choose one of {sorted(PROFILES)}"
        )
    return profile


def is_text_extension(ext: str) -> bool:
    return ext.lower() in _TEXT_EXTENSIONS


def check_file_allowed(profile: CodeProjectProfile, rel_path: str, size: int) -> None:
    ext = os.path.splitext(rel_path)[1].lower()
    if ext not in profile.writable_extensions:
        raise CodeProjectError(
            f"{rel_path}: '{ext or '(no extension)'}' files are not allowed in a "
            f"{profile.id} project"
        )
    if size > profile.max_file_bytes:
        raise CodeProjectError(
            f"{rel_path}: {size} bytes exceeds the {profile.max_file_bytes} byte per-file "
            f"limit of a {profile.id} project; host large media elsewhere and link to it"
        )


def validate_project(profile: CodeProjectProfile, root_abs: str) -> dict:
    errors: list[str] = []
    for name in profile.validators:
        errors.extend(VALIDATORS[name](root_abs))
    return {
        "valid": not errors,
        "errors": errors[:MAX_VALIDATION_ERRORS],
        "error_count": len(errors),
    }


_SLUG_CLEAN_RE = re.compile(r"[^A-Za-z0-9一-鿿]+")


def project_slug(name: str) -> str:
    slug = _SLUG_CLEAN_RE.sub("-", str(name or "").strip()).strip("-").lower()
    return slug[:80].strip("-") or "project"


def project_parent_dir(workspace_home: str | None) -> str:
    """Folder that holds new projects: the Workspace home, never a Task archive."""
    if workspace_home:
        return f"{workspace_home.strip('/')}/{WORKSPACE_PROJECTS_SUBDIR}"
    return NON_WORKSPACE_PROJECTS_DIR


def parse_entity_fs_url(value: str) -> tuple[str, str] | None:
    """Split a ``/api/v1/fs/<entity>/<path>`` link (absolute or relative)."""
    ref = parse_file_reference(value)
    if ref is None or not ref.entity_id or not ref.fs_path:
        return None
    return ref.entity_id, ref.fs_path


def is_external_url(value: str) -> bool:
    return urlsplit(str(value or "").strip()).scheme.lower() in {"http", "https"}


async def download_public_https(url: str, *, max_bytes: int) -> bytes:
    """Fetch a public HTTPS file, pinned to its validated DNS answers.

    Private, loopback and link-local destinations are refused by
    ``resolve_public_https_target``; every redirect hop is re-validated.
    """
    from packages.core.services.dashboard_http import (
        DashboardHttpError,
        create_public_https_transport,
        resolve_public_https_target,
    )

    current = str(url or "").strip()
    try:
        for hop in range(_MAX_DOWNLOAD_REDIRECTS + 1):
            target = await resolve_public_https_target(current)
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(60.0, connect=15.0),
                follow_redirects=False,
                trust_env=False,
                transport=create_public_https_transport(target),
            ) as client:
                async with client.stream("GET", target.url) as response:
                    if response.is_redirect:
                        location = response.headers.get("location")
                        if hop >= _MAX_DOWNLOAD_REDIRECTS or not location:
                            raise CodeProjectError(f"{url}: too many redirects")
                        current = urljoin(current, location)
                        continue
                    if response.status_code >= 400:
                        raise CodeProjectError(f"{url}: HTTP {response.status_code}")
                    declared = int(response.headers.get("content-length") or 0)
                    if declared > max_bytes:
                        raise CodeProjectError(f"{url}: {declared} bytes exceeds the {max_bytes} byte limit")
                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in response.aiter_bytes(256 * 1024):
                        total += len(chunk)
                        if total > max_bytes:
                            raise CodeProjectError(f"{url}: exceeds the {max_bytes} byte limit")
                        chunks.append(chunk)
                    return b"".join(chunks)
    except DashboardHttpError as exc:
        raise CodeProjectError(f"{url}: {exc}") from exc
    except httpx.HTTPError as exc:
        raise CodeProjectError(f"{url}: download failed ({exc.__class__.__name__})") from exc
    raise CodeProjectError(f"{url}: too many redirects")
