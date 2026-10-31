"""Built-in Skills baked into the sandbox image.

The sandbox image ships every built-in Skill under ``BAKED_SKILLS_ROOT``
(docker/Dockerfile.sandbox, ``baked-skills`` stage), written by
``packages/core/ai/runtime/builtin_skill_bundle.py`` — the same module the
control plane digests its Skills with. Layout::

    /opt/manor/skills/<name>/...        the Skill's files (read-only)
    /opt/manor/skills/.digests/<name>   its content digest
    /opt/manor/skills/.links/<name>     read-only subtrees, one per line
    /opt/manor/skills/index.json        {"schema": 1, "skills": {name: {
                                          digest, files, texts, read_only_paths}}}

The runner reads ``index.json`` once per image id and advertises
``{name: digest}`` in ``/health``. The control plane then sends
``baked_skill={name, digest}`` instead of the files when its own digest is
equal, and the runner materializes the image's copy into the workdir
(``DockerSandbox.materialize_builtin_skill``). The index carries the file list
and root-level texts so the runner describes the Skill (scripts, dependency
manifests) without the files on its host.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Mapping

from .errors import BakedSkillUnavailable
from .models import SkillManifest
from .scanner import SkillScanner

logger = logging.getLogger(__name__)

# Must equal builtin_skill_bundle.BAKED_* (tests pin them).
BAKED_SKILLS_ROOT = "/opt/manor/skills"
BAKED_INDEX_FILE = "index.json"
BAKED_DIGEST_DIR = ".digests"
BAKED_LINKS_DIR = ".links"
BAKED_INDEX_SCHEMA = 1


@dataclass(frozen=True)
class BakedSkill:
    """One baked Skill as the image's index describes it."""

    name: str
    digest: str
    files: tuple[str, ...]
    texts: Mapping[str, str] = field(default_factory=dict)
    read_only_paths: tuple[str, ...] = ()

    def write_root_texts(self, directory: str) -> None:
        """Write the root-level texts (SKILL.md, manifests) into a host dir.

        The runner reads dependency manifests and SKILL.md from
        ``SkillManifest.skill_dir``; for a baked Skill that is this stub.
        """
        for rel, content in self.texts.items():
            if "/" in rel or rel in ("", ".", ".."):
                continue
            with open(os.path.join(directory, rel), "w", encoding="utf-8") as handle:
                handle.write(content)

    def manifest(self, skill_name: str, stub_dir: str) -> SkillManifest:
        return SkillScanner().scan_listing(skill_name, stub_dir, self.files)


class BakedSkillIndex:
    """The baked Skills of one sandbox image."""

    def __init__(self, skills: Mapping[str, BakedSkill] | None = None) -> None:
        self._skills = dict(skills or {})

    @classmethod
    def parse(cls, text: str) -> "BakedSkillIndex":
        data = json.loads(text)
        if not isinstance(data, dict) or data.get("schema") != BAKED_INDEX_SCHEMA:
            raise ValueError(f"unsupported baked Skills index schema: {data.get('schema') if isinstance(data, dict) else data!r}")
        skills: dict[str, BakedSkill] = {}
        for name, entry in (data.get("skills") or {}).items():
            digest = str(entry.get("digest") or "")
            files = entry.get("files")
            if not name or not digest or not isinstance(files, list):
                raise ValueError(f"malformed baked Skills index entry {name!r}")
            texts = entry.get("texts") or {}
            skills[name] = BakedSkill(
                name=name,
                digest=digest,
                files=tuple(str(path) for path in files),
                texts={str(k): str(v) for k, v in texts.items()},
                read_only_paths=tuple(str(p) for p in entry.get("read_only_paths") or ()),
            )
        return cls(skills)

    def advertised(self) -> dict[str, str]:
        return {name: skill.digest for name, skill in sorted(self._skills.items())}

    def resolve(self, name: str, digest: str) -> BakedSkill:
        """The baked Skill, only when the image holds exactly ``digest``."""
        skill = self._skills.get(name)
        if skill is None:
            raise BakedSkillUnavailable(f"The sandbox image has no baked Skill {name!r}; send its files.")
        if skill.digest != digest:
            raise BakedSkillUnavailable(
                f"The sandbox image's baked Skill {name!r} is a different version "
                f"({skill.digest[:12]} != {digest[:12]}); send its files."
            )
        return skill

    def __len__(self) -> int:
        return len(self._skills)


# Exit status of the materialize program when the container's image lacks the
# requested digest; it is checked before the target is touched.
BAKED_SKILL_MISMATCH_EXIT = 65

# Runs inside the container as the sandbox user:
#   python3 -c MATERIALIZE_PROGRAM <root> <name> <digest> <target> <replace 0|1>
# Copies the Skill into <target> as real, writable files (Skills write into
# their own tree, e.g. pptx rewrites templates/layouts/layouts_index.json),
# except the declared read-only subtrees, which become one symlink each into
# the image. ``replace=1`` clears <target> first (another Skill was loaded);
# ``replace=0`` overlays the packaged files and keeps the rest (same Skill
# reloaded: session work such as projects/ survives).
MATERIALIZE_PROGRAM = r"""
import os, shutil, sys
root, name, want, target, replace = sys.argv[1:6]
src = os.path.join(root, name)
try:
    with open(os.path.join(root, ".digests", name), encoding="utf-8") as handle:
        have = handle.read().strip()
except OSError:
    have = ""
if not os.path.isdir(src) or have != want:
    sys.exit(65)
try:
    with open(os.path.join(root, ".links", name), encoding="utf-8") as handle:
        links = [line.strip() for line in handle if line.strip()]
except OSError:
    links = []
os.makedirs(target, exist_ok=True)

def remove(path):
    if os.path.isdir(path) and not os.path.islink(path):
        shutil.rmtree(path)
    elif os.path.lexists(path):
        os.unlink(path)

if replace == "1":
    for entry in os.listdir(target):
        remove(os.path.join(target, entry))
linked = set(links)

def ignore(directory, names):
    rel = os.path.relpath(directory, src)
    return [n for n in names if os.path.normpath(os.path.join(rel, n)) in linked]

def copy(source, destination):
    if os.path.islink(destination):
        os.unlink(destination)
    return shutil.copy(source, destination)

# Entry by entry: copytree would copystat <target> itself, and the workdir
# tmpfs belongs to root (mode 1777), so the sandbox user cannot chmod it.
skipped = set(ignore(src, os.listdir(src)))
for entry in sorted(os.listdir(src)):
    if entry in skipped:
        continue
    source, destination = os.path.join(src, entry), os.path.join(target, entry)
    if os.path.isdir(source):
        if os.path.lexists(destination) and not os.path.isdir(destination):
            remove(destination)
        shutil.copytree(source, destination, ignore=ignore, dirs_exist_ok=True, copy_function=copy)
    else:
        if os.path.isdir(destination) and not os.path.islink(destination):
            shutil.rmtree(destination)
        copy(source, destination)
for rel in links:
    destination = os.path.join(target, rel)
    remove(destination)
    os.makedirs(os.path.dirname(destination), exist_ok=True)
    os.symlink(os.path.join(src, rel), destination)
"""
