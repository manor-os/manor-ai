"""The workspace storage layout is declared in one place.

It used to exist only as string literals in the tools that write files —
``"images"`` in one, ``"videos"`` in three, ``"documents"`` in five more —
so nothing could answer "what IS the layout?" and a new writer could invent
another bucket without anything noticing. A staging workspace accumulated
five parallel layouts that way.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from packages.core.services.workspace_layout import (
    ArtifactRole,
    WorkspaceArtifactDir,
)

#: Modules that choose where generated output lands.
WRITER_GLOBS = (
    "packages/core/ai/tools/*.py",
    "packages/core/ai/tools/generate_file/*.py",
    "packages/core/ai/runtime/generated_files.py",
    "packages/core/tasks/media_tasks.py",
    "packages/core/workers/internal.py",
)


def _writer_sources() -> list[tuple[str, str]]:
    seen: dict[str, str] = {}
    for pattern in WRITER_GLOBS:
        base = Path(pattern).parent
        for path in sorted(base.glob(Path(pattern).name)):
            seen[str(path)] = path.read_text(encoding="utf-8")
    return sorted(seen.items())


def test_the_layout_is_a_closed_set():
    assert WorkspaceArtifactDir.values() == {
        "images", "videos", "audio", "documents",
        "presentations", "spreadsheets", "code", "artifacts",
    }


def test_no_writer_hardcodes_a_bucket_name():
    """Every destination must name a WorkspaceArtifactDir member."""
    literal_default_subdir = re.compile(r'default_subdir\s*=\s*["\']')
    literal_default_dir = re.compile(r'workspace_base_dir\s*,\s*["\']')

    offenders: list[str] = []
    for path, source in _writer_sources():
        for pattern in (literal_default_subdir, literal_default_dir):
            for match in pattern.finditer(source):
                line = source[: match.start()].count("\n") + 1
                offenders.append(f"{path}:{line}")

    assert not offenders, (
        "these writers hardcode an artifact directory instead of naming a "
        f"WorkspaceArtifactDir member: {offenders}"
    )


def test_writers_that_choose_a_bucket_import_the_declaration():
    for path, source in _writer_sources():
        if "WorkspaceArtifactDir." not in source:
            continue
        assert "from packages.core.services.workspace_layout import" in source, (
            f"{path} uses the layout without importing its declaration"
        )


# ── Deliverables are visible without moving files ─────────────────────


def test_artifact_roles_distinguish_deliverable_from_working_material():
    """Every writer used to stamp 'final', so a task's 14 storyboard frames
    and its one finished video were indistinguishable."""
    assert ArtifactRole.DELIVERABLE.value == "final"  # unchanged on disk
    assert ArtifactRole.INTERMEDIATE.value == "intermediate"
    assert ArtifactRole.REFERENCE.value == "reference"
    assert len(ArtifactRole.values()) == 3


def test_deliverable_role_keeps_its_stored_value():
    """The stored word stays 'final' so existing rows and any consumer that
    already filters on it keep working — renaming it would have been a data
    migration for zero benefit."""
    assert ArtifactRole.DELIVERABLE.value == "final"


@pytest.mark.parametrize("member", list(WorkspaceArtifactDir))
def test_bucket_names_are_safe_path_segments(member):
    assert member.value.isalnum() or member.value.isalpha()
    assert "/" not in member.value and ".." not in member.value
