"""The sandbox image's toolset: one manifest, installed, verified and described.

``sandbox_toolset.toml`` (next to this file) lists every tool the sandbox
image ships beyond the shipped Skills' own dependency manifests. This module
is the only reader of that file, and it serves three consumers:

* the image build (docker/Dockerfile.sandbox) runs ``export`` to turn the
  manifest into the apt / pip / npm package lists it installs;
* the release pipeline runs ``verify`` inside the built image, so a tool the
  manifest promises but the image lacks fails the release (``verify`` also
  checks the image's baked built-in Skills, see ``builtin_skill_bundle.py``);
* the Agent runtime calls :func:`sandbox_toolset_summary`, so what the model
  is told is installed is generated from the same entries the probes check.

It is standard-library only (Python >= 3.11 for ``tomllib``) because it runs
in a bare ``python:3.12-slim`` build stage and inside the sandbox image.
"""

from __future__ import annotations

import argparse
import functools
import importlib.util
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

MANIFEST_PATH = Path(__file__).with_name("sandbox_toolset.toml")

# Files ``export`` writes; the Dockerfile installs from exactly these names.
APT_LIST = "apt.txt"
PIP_REQUIREMENTS = "requirements.txt"
NPM_LIST = "npm.txt"

_ENTRY_KEYS = frozenset(
    {"id", "category", "label", "listed", "apt", "pip", "npm", "dockerfile", "probe", "import"}
)
_INSTALL_KEYS = ("apt", "pip", "npm", "dockerfile")
_PROBE_TIMEOUT_SECONDS = 180


class ToolsetError(ValueError):
    """The manifest is malformed; the message names the offending entry."""


@dataclass(frozen=True)
class ToolEntry:
    id: str
    category: str
    label: str
    listed: bool
    apt: tuple[str, ...]
    pip: tuple[str, ...]
    npm: tuple[str, ...]
    dockerfile: tuple[str, ...]
    probe: tuple[str, ...]
    imports: tuple[str, ...]


@dataclass(frozen=True)
class Toolset:
    categories: dict[str, str]
    entries: tuple[ToolEntry, ...]

    def apt_packages(self) -> list[str]:
        return _unique(package for entry in self.entries for package in entry.apt)

    def pip_requirements(self) -> list[str]:
        return _unique(requirement for entry in self.entries for requirement in entry.pip)

    def npm_packages(self) -> list[str]:
        return _unique(package for entry in self.entries for package in entry.npm)


def _unique(values: Iterable[str]) -> list[str]:
    seen: dict[str, None] = {}
    for value in values:
        seen.setdefault(value, None)
    return list(seen)


def _strings(raw: dict, key: str, entry_id: str) -> tuple[str, ...]:
    value = raw.get(key, [])
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise ToolsetError(f"tool {entry_id!r}: {key!r} must be a list of non-empty strings")
    return tuple(item.strip() for item in value)


def parse_manifest(text: str) -> Toolset:
    """Parse and validate manifest text (every entry installed and probed)."""
    data = tomllib.loads(text)
    categories = data.get("categories")
    if not isinstance(categories, dict) or not categories:
        raise ToolsetError("manifest needs a non-empty [categories] table")
    raw_entries = data.get("tool")
    if not isinstance(raw_entries, list) or not raw_entries:
        raise ToolsetError("manifest needs at least one [[tool]] entry")

    entries: list[ToolEntry] = []
    seen_ids: set[str] = set()
    for raw in raw_entries:
        entry_id = raw.get("id")
        if not isinstance(entry_id, str) or not entry_id:
            raise ToolsetError(f"tool entry without an id: {raw!r}")
        if entry_id in seen_ids:
            raise ToolsetError(f"duplicate tool id {entry_id!r}")
        seen_ids.add(entry_id)
        unknown = set(raw) - _ENTRY_KEYS
        if unknown:
            raise ToolsetError(f"tool {entry_id!r}: unknown keys {sorted(unknown)}")
        category = raw.get("category")
        if category not in categories:
            raise ToolsetError(f"tool {entry_id!r}: unknown category {category!r}")
        label = raw.get("label")
        if not isinstance(label, str) or not label.strip():
            raise ToolsetError(f"tool {entry_id!r}: needs a label")
        listed = raw.get("listed", True)
        if not isinstance(listed, bool):
            raise ToolsetError(f"tool {entry_id!r}: 'listed' must be true or false")
        entry = ToolEntry(
            id=entry_id,
            category=category,
            label=label.strip(),
            listed=listed,
            apt=_strings(raw, "apt", entry_id),
            pip=_strings(raw, "pip", entry_id),
            npm=_strings(raw, "npm", entry_id),
            dockerfile=_strings(raw, "dockerfile", entry_id),
            probe=_strings(raw, "probe", entry_id),
            imports=_strings(raw, "import", entry_id),
        )
        if not any(getattr(entry, key) for key in _INSTALL_KEYS):
            raise ToolsetError(
                f"tool {entry_id!r}: lists no install source (one of {', '.join(_INSTALL_KEYS)})"
            )
        if not entry.probe and not entry.imports:
            raise ToolsetError(f"tool {entry_id!r}: needs a probe or an import to verify it")
        if entry.pip and not entry.imports:
            raise ToolsetError(f"tool {entry_id!r}: pip packages need an import probe")
        if entry.npm and not entry.probe:
            raise ToolsetError(f"tool {entry_id!r}: npm packages need a command probe")
        entries.append(entry)
    return Toolset(categories=dict(categories), entries=tuple(entries))


def load_manifest(path: Path | str | None = None) -> Toolset:
    return parse_manifest(Path(path or MANIFEST_PATH).read_text(encoding="utf-8"))


# ── Agent-visible summary ─────────────────────────────────────────────────

RUNTIME_INSTALL_NOTE = (
    "Missing something? `pip install <pkg>` / `npm install -g <pkg>` work when the "
    "sandbox has network and land on its disk-backed install dir for the sandbox's life."
)


def render_summary(toolset: Toolset) -> str:
    """One line per category, listing every ``listed`` entry's label."""
    lines = ["Preinstalled in the sandbox:"]
    for category, title in toolset.categories.items():
        labels = [entry.label for entry in toolset.entries if entry.category == category and entry.listed]
        if labels:
            lines.append(f"- {title}: {'; '.join(labels)}")
    lines.append(RUNTIME_INSTALL_NOTE)
    return "\n".join(lines)


@functools.lru_cache(maxsize=1)
def sandbox_toolset_summary() -> str:
    """The Agent-visible list of what the sandbox image ships (cached)."""
    return render_summary(load_manifest())


# ── Image build: package lists ────────────────────────────────────────────


def export_lists(toolset: Toolset, out_dir: Path) -> None:
    """Write the apt / pip / npm lists the Dockerfile installs from.

    Each kind is its own file so the image layer that installs it is cached
    on that file's bytes alone: a pip-only manifest edit never reruns apt.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, values in (
        (APT_LIST, toolset.apt_packages()),
        (PIP_REQUIREMENTS, toolset.pip_requirements()),
        (NPM_LIST, toolset.npm_packages()),
    ):
        (out_dir / name).write_text("".join(f"{value}\n" for value in values), encoding="utf-8")


# ── Built image: verification ─────────────────────────────────────────────


def _run(command: list[str]) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=_PROBE_TIMEOUT_SECONDS,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    output = (result.stdout + result.stderr).strip()
    return result.returncode == 0, output[-600:]


def verify(toolset: Toolset, *, stream=sys.stdout) -> list[str]:
    """Run every probe and import; return one line per failure (empty = ok)."""
    failures: list[str] = []
    checks: list[tuple[ToolEntry, str, list[str]]] = []
    for entry in toolset.entries:
        for module in entry.imports:
            checks.append((entry, f"import {module}", [sys.executable, "-c", f"import {module}"]))
        for probe in entry.probe:
            checks.append((entry, probe, ["sh", "-c", probe]))
    checks_total = len(checks) + 1
    for index, (entry, name, command) in enumerate(checks, start=1):
        ok, output = _run(command)
        print(f"[{index}/{checks_total}] {'ok  ' if ok else 'FAIL'} {entry.id}: {name}", file=stream)
        if not ok:
            failures.append(f"{entry.id}: {name}: {output or '(no output)'}")
    ok, output = _run([sys.executable, "-m", "pip", "check"])
    print(f"[{checks_total}/{checks_total}] {'ok  ' if ok else 'FAIL'} pip check", file=stream)
    if not ok:
        failures.append(f"pip check: {output}")
    return failures


# The image's baked built-in Skills (docker/Dockerfile.sandbox, baked-skills).
BAKED_SKILLS_ROOT = "/opt/manor/skills"


def verify_baked_skills(root: Path | str = BAKED_SKILLS_ROOT, *, stream=sys.stdout) -> list[str]:
    """Check the baked built-in Skills with the module that baked them.

    ``builtin_skill_bundle.py`` ships next to this file (in the repository
    and in the image's /opt/manor/toolset); a missing module, index or Skill
    is a failure, since every sandbox for that Skill would fall back to
    uploading its files.
    """
    module_path = Path(__file__).with_name("builtin_skill_bundle.py")
    spec = importlib.util.spec_from_file_location("builtin_skill_bundle", module_path)
    if spec is None or spec.loader is None or not module_path.is_file():
        failures = [f"baked Skills: {module_path} is missing"]
    else:
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        failures = module.verify_baked(root)
    print(f"{'ok  ' if not failures else 'FAIL'} baked built-in Skills at {root}", file=stream)
    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--manifest", default=str(MANIFEST_PATH))
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export", help="write apt.txt / requirements.txt / npm.txt")
    export.add_argument("--out", required=True)
    verify_cmd = commands.add_parser("verify", help="run every probe in this environment")
    verify_cmd.add_argument("--baked-skills-root", default=BAKED_SKILLS_ROOT)
    commands.add_parser("describe", help="print the Agent-visible summary")
    commands.add_parser("check", help="validate the manifest only")
    args = parser.parse_args(argv)

    try:
        toolset = load_manifest(args.manifest)
    except (OSError, tomllib.TOMLDecodeError, ToolsetError) as exc:
        print(f"sandbox toolset manifest error: {exc}", file=sys.stderr)
        return 2
    if args.command == "export":
        export_lists(toolset, Path(args.out))
        return 0
    if args.command == "describe":
        print(render_summary(toolset))
        return 0
    if args.command == "check":
        return 0
    failures = verify(toolset)
    failures += verify_baked_skills(args.baked_skills_root)
    if failures:
        print(f"\n{len(failures)} sandbox toolset check(s) failed:", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1
    print("sandbox toolset verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
