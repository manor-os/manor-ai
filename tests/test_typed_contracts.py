from packages.core.workers.internal import enforce_output_shape
from packages.core.contracts.envelope import Success


def test_governance_files_production_payload_now_succeeds():
    # exact drifted shape from the real write_workspace_governance_files failure
    raw = {
        "files": [
            {"name": "RULES.md", "path": "Workspaces/15秒短剧创作工作室/documents/RULES.md"},
            {"name": "LEARNINGS.md", "path": "Workspaces/15秒短剧创作工作室/documents/LEARNINGS.md"},
            {"name": "MEMORY.md", "path": "Workspaces/15秒短剧创作工作室/documents/MEMORY.md"},
        ],
    }
    result = enforce_output_shape("ArtifactResult", raw)
    assert isinstance(result, Success)
    assert result.data["files"][0]["fs_path"].endswith("RULES.md")
