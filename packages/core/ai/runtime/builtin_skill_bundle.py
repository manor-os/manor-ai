"""Built-in Skill bundles: which directories ship, their file set and digest.

This module is the single source of truth for three consumers:

* the control plane (``builtin_skill_loader``) seeds the built-in Skills from
  :func:`builtin_skill_dirs` and fingerprints each with
  :func:`skill_source_sha256`;
* the sandbox image build (docker/Dockerfile.sandbox, ``baked-skills`` stage)
  runs ``bake`` to copy exactly those directories, with exactly that digest,
  into ``BAKED_SKILLS_ROOT`` so a sandbox for a built-in Skill links the
  image's copy instead of receiving the files over HTTP;
* the release verification (``sandbox_toolset.py verify``) runs
  :func:`verify_baked` inside the built image.

A sandbox uses the baked copy only when the runner's digest for the Skill
equals the control plane's, so a Skill edited after the image was built falls
back to the upload path instead of running stale files.

It is standard-library only because it runs in a bare ``python:3.12-slim``
build stage and inside the sandbox image.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat as stat_module
import sys
from pathlib import Path

# packages/core/ai/runtime/builtin_skill_bundle.py -> repository root. The
# image ships this file without the repository (see verify), so it is only a
# default for the in-repo callers.
_HERE = Path(__file__).resolve()
_REPO_ROOT = _HERE.parents[4] if len(_HERE.parents) > 4 else _HERE.parent
SKILLS_SUBDIR = Path("packages/core/ai/skills")
AGENT_SKILLS_SUBDIR = Path(".agents/skills")

RETIRED_BUILTIN_SKILL_SLUGS = frozenset({
    "mcp_desktop_recording",
    "remotion",
})
PLATFORM_GUIDE_SKILL_NAMES = ("cloud-intro", "intro")

# Layout of the baked tree in the sandbox image. The sandbox runner keeps its
# own copy of these names (sandbox-service/sandbox/docker_backend.py); a test
# pins them equal.
BAKED_SKILLS_ROOT = "/opt/manor/skills"
BAKED_INDEX_FILE = "index.json"
BAKED_DIGEST_DIR = ".digests"
BAKED_LINKS_DIR = ".links"
BAKED_INDEX_SCHEMA = 1

# Skill config.json key: subtrees the sandbox links read-only from the image
# instead of copying into the memory-backed workdir. Only for bulky reference
# assets the Skill never writes (e.g. pptx's 11k icon SVGs).
READ_ONLY_PATHS_CONFIG_KEY = "sandbox_read_only_paths"

# Scripts get the executable bit in the baked tree, as the upload path did
# after injecting them (docker_backend._inject_files).
_EXECUTABLE_SUFFIXES = (".sh", ".bash", ".py")
# Script files never belong inside a read-only subtree: a script resolves its
# own directory (``Path(__file__).resolve()``) and would land in the image.
_SCRIPT_SUFFIXES = (".py", ".sh", ".bash", ".js", ".ts", ".rb")
# Root-level text files the runner reads to describe a Skill (SKILL.md,
# dependency manifests, env declarations) without the files on its host.
_INDEX_TEXT_MAX_BYTES = 262144


def has_skill_manifest(skill_dir: Path) -> bool:
    return skill_dir.is_dir() and (skill_dir / "SKILL.md").is_file()


def builtin_skill_dirs(
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

    package_root = skills_root or (_REPO_ROOT / SKILLS_SUBDIR)
    project_root = agent_skills_root or (_REPO_ROOT / AGENT_SKILLS_SUBDIR)
    directories = (
        sorted(
            path
            for path in package_root.iterdir()
            if path.is_dir() and path.name not in RETIRED_BUILTIN_SKILL_SLUGS
        )
        if package_root.exists()
        else []
    )
    for name in PLATFORM_GUIDE_SKILL_NAMES:
        candidate = project_root / name
        if has_skill_manifest(candidate):
            directories.append(candidate)
            break
    return directories


def _bundle_entries(skill_dir: Path) -> list[tuple[tuple[str, ...], str, os.stat_result]]:
    entries: list[tuple[tuple[str, ...], str, os.stat_result]] = []
    root = str(skill_dir)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        rel_dir = os.path.relpath(dirpath, root)
        parts = () if rel_dir == "." else tuple(rel_dir.split(os.sep))
        for name in filenames:
            if name.endswith(".pyc") or name == ".DS_Store":
                continue
            full = os.path.join(dirpath, name)
            try:
                stat = os.stat(full)
            except OSError:
                continue
            if not stat_module.S_ISREG(stat.st_mode):
                continue
            entries.append(((*parts, name), full, stat))
    # Path ordering compares component-wise; keep it so digests are unchanged.
    entries.sort(key=lambda entry: entry[0])
    return entries


def skill_bundle_files(skill_dir: Path) -> list[tuple[str, Path]]:
    """``(relative posix path, absolute path)`` of every file the digest covers."""

    return [("/".join(parts), Path(full)) for parts, full, _ in _bundle_entries(skill_dir)]


# skill_dir -> (stat signature, digest). seed_builtin_skills runs on every
# list_skills call; re-reading ~12k bundle files each time cost seconds of
# event-loop time, while a stat walk is ~30x cheaper. Keys are built-in Skill
# directories, so the map is bounded by the number of shipped Skills.
_SOURCE_SHA256_CACHE: dict[str, tuple[tuple[tuple[str, int, int], ...], str]] = {}


def skill_source_sha256(skill_dir: Path) -> str:
    """Fingerprint the complete repository-controlled Skill bundle.

    Covers every file's relative path and bytes (see :func:`skill_bundle_files`).
    """

    entries = _bundle_entries(skill_dir)
    signature = tuple(
        ("/".join(parts), stat.st_size, stat.st_mtime_ns) for parts, _, stat in entries
    )
    key = str(skill_dir)
    cached = _SOURCE_SHA256_CACHE.get(key)
    if cached is not None and cached[0] == signature:
        return cached[1]

    digest = hashlib.sha256()
    for parts, full, _ in entries:
        digest.update("/".join(parts).encode("utf-8"))
        digest.update(b"\0")
        digest.update(Path(full).read_bytes())
        digest.update(b"\0")
    value = digest.hexdigest()
    _SOURCE_SHA256_CACHE[key] = (signature, value)
    return value


class BundleError(ValueError):
    """A Skill bundle cannot be baked; the message names the Skill."""


def read_only_paths(skill_dir: Path, files: list[str] | None = None) -> list[str]:
    """The Skill's declared read-only subtrees, validated against its files."""

    config_path = skill_dir / "config.json"
    if not config_path.is_file():
        return []
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BundleError(f"{skill_dir.name}: unreadable config.json: {exc}") from exc
    raw = config.get(READ_ONLY_PATHS_CONFIG_KEY) if isinstance(config, dict) else None
    if raw is None:
        return []
    if not isinstance(raw, list) or not all(isinstance(item, str) and item.strip() for item in raw):
        raise BundleError(f"{skill_dir.name}: {READ_ONLY_PATHS_CONFIG_KEY} must be a list of paths")
    rels = files if files is not None else [rel for rel, _ in skill_bundle_files(skill_dir)]
    paths: list[str] = []
    for item in raw:
        rel = item.strip().strip("/")
        parts = rel.split("/")
        if not rel or any(part in ("", ".", "..") for part in parts):
            raise BundleError(f"{skill_dir.name}: read-only path {item!r} is not a plain relative path")
        if not (skill_dir / rel).is_dir():
            raise BundleError(f"{skill_dir.name}: read-only path {rel!r} is not a directory")
        inside = [path for path in rels if path.startswith(rel + "/")]
        if not inside:
            raise BundleError(f"{skill_dir.name}: read-only path {rel!r} holds no files")
        scripts = [path for path in inside if path.endswith(_SCRIPT_SUFFIXES)]
        if scripts:
            raise BundleError(
                f"{skill_dir.name}: read-only path {rel!r} holds scripts ({scripts[:3]}); "
                "a script resolving its own path would run from the image"
            )
        if any(rel.startswith(other + "/") or other.startswith(rel + "/") for other in paths):
            raise BundleError(f"{skill_dir.name}: read-only paths {rel!r} overlap")
        paths.append(rel)
    return sorted(paths)


def _index_texts(files: list[tuple[str, Path]]) -> dict[str, str]:
    texts: dict[str, str] = {}
    for rel, path in files:
        if "/" in rel:
            continue
        try:
            if path.stat().st_size > _INDEX_TEXT_MAX_BYTES:
                continue
            texts[rel] = path.read_bytes().decode("utf-8")
        except (OSError, UnicodeDecodeError):
            continue
    return texts


# ── Image build: bake ─────────────────────────────────────────────────────


def bake(out: Path, *, skills_root: Path | None = None, agent_skills_root: Path | None = None) -> dict:
    """Copy every built-in Skill bundle into ``out`` and write the index.

    ``out/<name>/`` holds exactly the files :func:`skill_bundle_files` lists,
    ``out/.digests/<name>`` the :func:`skill_source_sha256` digest,
    ``out/.links/<name>`` the declared read-only subtrees (one per line), and
    ``out/index.json`` everything the runner needs to describe a Skill
    without its files: digest, file list, root-level texts, read-only paths.
    """

    skills: dict[str, dict] = {}
    (out / BAKED_DIGEST_DIR).mkdir(parents=True, exist_ok=True)
    (out / BAKED_LINKS_DIR).mkdir(parents=True, exist_ok=True)
    for skill_dir in builtin_skill_dirs(skills_root=skills_root, agent_skills_root=agent_skills_root):
        name = skill_dir.name
        if name in skills:
            raise BundleError(f"two built-in Skill directories are named {name!r}")
        files = skill_bundle_files(skill_dir)
        rels = [rel for rel, _ in files]
        links = read_only_paths(skill_dir, rels)
        target = out / name
        for rel, source in files:
            destination = target / rel
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
            mode = 0o755 if rel.endswith(_EXECUTABLE_SUFFIXES) else 0o644
            os.chmod(destination, mode)
        digest = skill_source_sha256(skill_dir)
        if skill_source_sha256(target) != digest:
            raise BundleError(f"{name}: baked copy does not reproduce the source digest")
        (out / BAKED_DIGEST_DIR / name).write_text(digest, encoding="utf-8")
        (out / BAKED_LINKS_DIR / name).write_text("".join(f"{rel}\n" for rel in links), encoding="utf-8")
        skills[name] = {
            "digest": digest,
            "files": rels,
            "texts": _index_texts(files),
            "read_only_paths": links,
        }
    index = {"schema": BAKED_INDEX_SCHEMA, "skills": skills}
    (out / BAKED_INDEX_FILE).write_text(json.dumps(index, sort_keys=True), encoding="utf-8")
    return index


# ── Built image: verification ─────────────────────────────────────────────


def verify_baked(root: Path | str = BAKED_SKILLS_ROOT) -> list[str]:
    """Check the baked tree against its index; one line per failure."""

    root = Path(root)
    try:
        index = json.loads((root / BAKED_INDEX_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [f"baked Skills index unreadable at {root / BAKED_INDEX_FILE}: {exc}"]
    skills = index.get("skills") if isinstance(index, dict) else None
    if index.get("schema") != BAKED_INDEX_SCHEMA or not isinstance(skills, dict) or not skills:
        return [f"baked Skills index at {root} lists no Skills (schema {index.get('schema')!r})"]
    failures: list[str] = []
    for name, entry in sorted(skills.items()):
        skill_dir = root / name
        if not has_skill_manifest(skill_dir):
            failures.append(f"baked Skill {name}: missing or has no SKILL.md")
            continue
        digest = skill_source_sha256(skill_dir)
        recorded = (root / BAKED_DIGEST_DIR / name).read_text(encoding="utf-8").strip() if (
            root / BAKED_DIGEST_DIR / name
        ).is_file() else ""
        if digest != entry.get("digest") or digest != recorded:
            failures.append(f"baked Skill {name}: content digest {digest[:12]} does not match its index")
        if [rel for rel, _ in skill_bundle_files(skill_dir)] != entry.get("files"):
            failures.append(f"baked Skill {name}: file list does not match its index")
    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    bake_cmd = commands.add_parser("bake", help="copy the built-in Skills and write the index")
    bake_cmd.add_argument("--repo-root", required=True)
    bake_cmd.add_argument("--out", required=True)
    verify_cmd = commands.add_parser("verify", help="check a baked tree against its index")
    verify_cmd.add_argument("--root", default=BAKED_SKILLS_ROOT)
    digest_cmd = commands.add_parser("digest", help="print a Skill directory's digest")
    digest_cmd.add_argument("skill_dir")
    args = parser.parse_args(argv)

    if args.command == "bake":
        repo = Path(args.repo_root)
        try:
            index = bake(
                Path(args.out),
                skills_root=repo / SKILLS_SUBDIR,
                agent_skills_root=repo / AGENT_SKILLS_SUBDIR,
            )
        except BundleError as exc:
            print(f"baking built-in Skills failed: {exc}", file=sys.stderr)
            return 2
        for name, entry in index["skills"].items():
            print(f"{name} {entry['digest'][:16]} files={len(entry['files'])} links={entry['read_only_paths']}")
        return 0
    if args.command == "digest":
        print(skill_source_sha256(Path(args.skill_dir)))
        return 0
    failures = verify_baked(args.root)
    for failure in failures:
        print(f"  - {failure}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
