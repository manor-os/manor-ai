from packages.core.contracts.envelope import Success, Failure
from packages.core.workers.internal import enforce_output_shape


def test_enforce_reshapes_drifted_result_to_success():
    raw = {"files": [{"name": "RULES.md", "path": "Workspaces/Demo/RULES.md"}]}
    result = enforce_output_shape("ArtifactResult", raw)
    assert isinstance(result, Success)
    assert result.data["files"][0]["fs_path"] == "Workspaces/Demo/RULES.md"


def test_enforce_genuine_empty_becomes_failure():
    raw = {"files": [{"name": "RULES.md", "fs_path": None}]}
    result = enforce_output_shape("ArtifactResult", raw)
    assert isinstance(result, Failure)
    assert "fs_path" in result.reason or "files" in result.reason


def test_enforce_workspace_default_path():
    raw = {"files": [{"name": "RULES.md"}]}
    result = enforce_output_shape("ArtifactResult", raw, workspace_base_dir="Workspaces/Demo")
    assert isinstance(result, Success)
    assert result.data["files"][0]["fs_path"] == "Workspaces/Demo/RULES.md"
