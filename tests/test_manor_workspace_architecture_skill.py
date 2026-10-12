from __future__ import annotations

import re
from pathlib import Path

import pytest


pytestmark = pytest.mark.oss_smoke


ROOT = Path(__file__).parents[1]
SKILL_DIR = ROOT / ".agents/skills/manor-workspace-architecture"
SKILL = SKILL_DIR / "SKILL.md"
ARCHITECTURE = SKILL_DIR / "references/architecture.md"
CHECKLIST = SKILL_DIR / "references/node-test-checklist.md"
BLUEPRINT_SKILL = ROOT / ".agents/skills/manor-workspace-blueprint/SKILL.md"
BLUEPRINT_RUNTIME_CONTRACT = ROOT / ".agents/skills/manor-workspace-blueprint/references/runtime-contract.md"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_workspace_architecture_skill_has_required_references() -> None:
    assert SKILL.exists()
    assert ARCHITECTURE.exists()
    assert CHECKLIST.exists()
    skill = _read(SKILL)
    assert "references/architecture.md" in skill
    assert "references/node-test-checklist.md" in skill


def test_architecture_code_entries_resolve_to_repository_paths() -> None:
    """Prevent a node from documenting invented modules as its authority."""
    text = _read(ARCHITECTURE)
    paths = set(re.findall(r"`((?:packages|apps)/[^`(), ]+)`", text))
    assert paths
    missing = sorted(path for path in paths if not (ROOT / path.split("::", 1)[0]).exists())
    assert not missing, f"architecture references missing code entries: {missing}"


def test_twilio_voice_runtime_is_not_blueprint_portable() -> None:
    for portable_module in (
        "packages/core/blueprints/payload.py",
        "packages/core/blueprints/exporter.py",
    ):
        assert "TwilioVoiceCallSession" not in _read(ROOT / portable_module)


def test_blueprint_skill_references_real_contract_and_tools() -> None:
    text = _read(BLUEPRINT_SKILL)
    assert BLUEPRINT_RUNTIME_CONTRACT.exists()
    assert "references/runtime-contract.md" in text
    assert (SKILL_DIR.parent / "manor-workspace-blueprint/scripts/check_blueprint.py").exists()
    assert (SKILL_DIR.parent / "manor-workspace-blueprint/scripts/compare_roundtrip.py").exists()
