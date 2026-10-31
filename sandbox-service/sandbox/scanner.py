"""
Skill directory scanner.

Scans a skill directory and produces a SkillManifest describing its scripts,
dependencies, environment declarations, and suggested entry point.

Supports both Python (requirements.txt) and Node.js (package.json) environments.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path, PurePosixPath
from typing import Iterable

from .models import SkillManifest

logger = logging.getLogger(__name__)

SCRIPT_EXTENSIONS: set[str] = {".py", ".sh", ".bash", ".js", ".ts", ".rb"}

# Directories that should never be scanned inside a skill.
SKIP_DIRS: set[str] = {
    "__pycache__", ".git", "node_modules", ".venv", "venv", ".tox",
    ".mypy_cache", ".pytest_cache", ".ruff_cache",
}

ENTRY_CANDIDATES: list[str] = [
    "run.py", "main.py", "index.py",
    "run.sh", "main.sh",
    "index.js", "index.ts",
]


class SkillScanner:
    """Scan a local skill directory and produce a SkillManifest."""

    def scan(self, skill_dir: str) -> SkillManifest:
        root = Path(skill_dir).resolve()
        if not root.is_dir():
            raise FileNotFoundError(f"Skill directory not found: {skill_dir}")
        relative = [item.relative_to(root).as_posix() for item in self._walk(root)]
        return self.scan_listing(root.name, str(root), relative)

    def scan_listing(
        self,
        name: str,
        skill_dir: str,
        relative_paths: Iterable[str],
    ) -> SkillManifest:
        """Describe a Skill from its file list; root-level files are read from ``skill_dir``.

        ``scan`` lists a host directory; a Skill baked into the sandbox image
        is described from the image's index instead, with its root-level texts
        written to ``skill_dir`` (see ``baked_skills.BakedSkill``). Both share
        this function, so the two paths cannot describe a Skill differently.
        """
        root = Path(skill_dir)
        scripts: list[str] = []
        requirements_txt: str | None = None
        package_json: str | None = None
        env_vars: dict[str, str] = {}
        description = ""
        entry_hint: str | None = None

        for rel in relative_paths:
            parts = PurePosixPath(rel).parts
            if not parts or not self._visible(parts):
                continue
            filename = parts[-1]
            at_root = len(parts) == 1

            if PurePosixPath(filename).suffix in SCRIPT_EXTENSIONS:
                scripts.append(rel)

            if filename == "requirements.txt" and at_root:
                requirements_txt = rel

            if filename == "package.json" and at_root:
                package_json = rel

            if filename in (".env", ".env.example", "env.json") and at_root:
                env_vars.update(self._parse_env_file(root / filename))

            if filename == "SKILL.md" and at_root:
                try:
                    description = (root / filename).read_text(encoding="utf-8")[:8000]
                except OSError:
                    pass

        for candidate in ENTRY_CANDIDATES:
            if candidate in scripts:
                entry_hint = candidate
                break

        needs_sandbox = bool(scripts) or requirements_txt is not None or package_json is not None

        manifest = SkillManifest(
            name=name,
            skill_dir=str(root),
            description=description,
            scripts=sorted(scripts),
            requirements_txt=requirements_txt,
            package_json=package_json,
            env_vars=env_vars,
            entry_hint=entry_hint,
            needs_sandbox=needs_sandbox,
        )

        has_py = requirements_txt is not None or any(s.endswith(".py") for s in scripts)
        has_node = package_json is not None or any(s.endswith((".js", ".ts")) for s in scripts)
        logger.info(
            "scanner: skill=%s scripts=%d py=%s node=%s requirements=%s package_json=%s entry=%s",
            name, len(scripts), has_py, has_node,
            requirements_txt or "none",
            package_json or "none",
            entry_hint or "none",
        )
        return manifest

    # ── internals ──

    @staticmethod
    def _visible(parts: tuple[str, ...]) -> bool:
        """Skip hidden/excluded dirs and hidden files other than env declarations."""
        if any(p.startswith(".") or p in SKIP_DIRS for p in parts[:-1]):
            return False
        return not (parts[-1].startswith(".") and parts[-1] not in (".env", ".env.example"))

    @classmethod
    def _walk(cls, root: Path):
        """Yield all regular files, skipping hidden/excluded dirs."""
        for item in root.rglob("*"):
            if not item.is_file():
                continue
            if not cls._visible(item.relative_to(root).parts):
                continue
            yield item

    @staticmethod
    def _parse_env_file(path: Path) -> dict[str, str]:
        env: dict[str, str] = {}
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return env

        if path.suffix == ".json":
            try:
                data = json.loads(text)
                return {k: str(v) for k, v in data.items() if isinstance(k, str)}
            except (json.JSONDecodeError, ValueError):
                return env

        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip("'\"")
            if key:
                env[key] = value
        return env
