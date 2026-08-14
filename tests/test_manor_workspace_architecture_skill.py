from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).parents[1]
SKILL_DIR = ROOT / ".agents/skills/manor-workspace-architecture"
SKILL = SKILL_DIR / "SKILL.md"
ARCHITECTURE = SKILL_DIR / "references/architecture.md"
CHECKLIST = SKILL_DIR / "references/node-test-checklist.md"
BLUEPRINT_SKILL = ROOT / ".agents/skills/manor-workspace-blueprint/SKILL.md"
BLUEPRINT_RUNTIME_CONTRACT = (
    ROOT / ".agents/skills/manor-workspace-blueprint/references/runtime-contract.md"
)

EXPECTED_NODES = {
    "WS-01",  # create/install/configure
    "WS-02",  # runtime scope and chat
    "WS-03",  # readiness and context
    "WS-04",  # Strategist and Proposal
    "WS-05",  # proposal governance
    "WS-06",  # Task and planning
    "WS-07",  # Plan executor and dispatcher
    "WS-08",  # HITL and resume
    "WS-09",  # Workflow runtime
    "WS-10",  # Artifact and Knowledge projection
    "WS-11",  # events, goals, evaluation
    "WS-12",  # credits and usage
    "WS-13",  # deletion lifecycle
    "WS-14",  # Blueprint portability
}


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_workspace_architecture_skill_has_required_references() -> None:
    assert SKILL.exists()
    assert ARCHITECTURE.exists()
    assert CHECKLIST.exists()
    skill = _read(SKILL)
    assert "references/architecture.md" in skill
    assert "references/node-test-checklist.md" in skill


def test_architecture_has_diagrams_flow_and_code_anchored_nodes() -> None:
    text = _read(ARCHITECTURE)
    assert "```mermaid" in text
    assert "End-to-end" in text
    assert EXPECTED_NODES <= set(re.findall(r"WS-\d{2}", text))
    for phrase in ("Code entry", "State", "Success invariant", "Failure boundary"):
        assert phrase in text


def test_architecture_code_entries_resolve_to_repository_paths() -> None:
    """Prevent a node from documenting invented modules as its authority."""
    text = _read(ARCHITECTURE)
    paths = set(re.findall(r"`((?:packages|apps)/[^`(), ]+)`", text))
    assert paths
    missing = sorted(
        path for path in paths
        if not (ROOT / path.split("::", 1)[0]).exists()
    )
    assert not missing, f"architecture references missing code entries: {missing}"


def test_every_architecture_node_has_an_independent_test_entry() -> None:
    text = _read(CHECKLIST)
    checklist_nodes = set(re.findall(r"WS-\d{2}", text))
    assert EXPECTED_NODES <= checklist_nodes
    for phrase in ("Automated", "Integration", "Manual", "Pass criteria"):
        assert phrase in text


def test_blueprint_skill_requires_workspace_architecture_first() -> None:
    text = _read(BLUEPRINT_SKILL)
    assert "manor-workspace-architecture" in text
    assert text.index("manor-workspace-architecture") < text.index("export_workspace()")


def test_blueprint_skill_references_real_contract_and_tools() -> None:
    text = _read(BLUEPRINT_SKILL)
    assert BLUEPRINT_RUNTIME_CONTRACT.exists()
    assert "references/runtime-contract.md" in text
    assert (SKILL_DIR.parent / "manor-workspace-blueprint/scripts/check_blueprint.py").exists()
    assert (SKILL_DIR.parent / "manor-workspace-blueprint/scripts/compare_roundtrip.py").exists()
