"""Built-in Skill bundle paths must not depend on where the seeding process runs.

A seed run from another checkout stored ``/tmp/pd/packages/core/ai/skills/...``
on staging; the image (``/app``) then rewrote ~100 rows on every chat turn and
could not read bundle files under the foreign path.
"""
from __future__ import annotations

from packages.core.services import builtin_skill_loader as loader


def test_bundle_paths_are_stored_relative_to_the_repository():
    docx = loader._SKILLS_ROOT / "docx"

    portable = loader.portable_builtin_skill_dir(docx)

    assert portable == "packages/core/ai/skills/docx"
    assert loader.resolve_builtin_skill_dir(portable) == loader._REPO_ROOT / portable
    assert (loader.resolve_builtin_skill_dir(portable) / "SKILL.md").is_file()


def test_foreign_absolute_paths_resolve_into_this_checkout():
    for foreign in (
        "/tmp/pd/packages/core/ai/skills/docx",
        "/app/packages/core/ai/skills/docx",
        "/somewhere/else/.agents/skills/cloud-intro",
    ):
        resolved = loader.resolve_builtin_skill_dir(foreign)
        assert resolved is not None
        assert resolved.is_relative_to(loader._REPO_ROOT)
        assert loader.portable_builtin_skill_dir(foreign) in str(resolved)

    assert loader.resolve_builtin_skill_dir("/tmp/pd/packages/core/ai/skills/docx") == (
        loader._SKILLS_ROOT / "docx"
    )


def test_unrelated_absolute_paths_are_kept_and_empty_values_are_none(tmp_path):
    assert loader.resolve_builtin_skill_dir(str(tmp_path)) == tmp_path
    assert loader.resolve_builtin_skill_dir("") is None
    assert loader.resolve_builtin_skill_dir(None) is None
