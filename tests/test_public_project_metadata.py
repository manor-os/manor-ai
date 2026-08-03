from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
PUBLIC_REPOSITORY = "https://github.com/manor-os/manor-ai"


@pytest.mark.unit
def test_public_project_metadata_uses_canonical_identity() -> None:
    with (ROOT / "pyproject.toml").open("rb") as handle:
        project = tomllib.load(handle)["project"]

    citation = yaml.safe_load((ROOT / "CITATION.cff").read_text(encoding="utf-8"))
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    llms = (ROOT / "llms.txt").read_text(encoding="utf-8")

    assert project["urls"]["Repository"] == PUBLIC_REPOSITORY
    assert citation["repository-code"] == PUBLIC_REPOSITORY
    assert PUBLIC_REPOSITORY in readme
    assert PUBLIC_REPOSITORY in llms
    assert "source-available" in readme
    assert "source-available" in llms
    assert "Sustainable Use License 1.0" in readme
    assert "Sustainable Use License 1.0" in llms
    assert "fair-code" in readme
    assert "fair-code" in llms
    assert "Manor Sustainable Use License 1.0" not in readme
    assert "Manor Sustainable Use License 1.0" not in llms
