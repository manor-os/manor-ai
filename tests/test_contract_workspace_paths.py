from packages.core.contracts.workspace_paths import default_fs_path_into_workspace


def test_missing_path_defaults_under_workspace_base():
    out = default_fs_path_into_workspace(
        {"files": [{"name": "RULES.md"}]},
        workspace_base_dir="Workspaces/Demo",
    )
    assert out["files"][0]["fs_path"] == "Workspaces/Demo/RULES.md"


def test_existing_path_is_scoped_under_workspace():
    out = default_fs_path_into_workspace(
        {"files": [{"name": "a.md", "fs_path": "a.md"}]},
        workspace_base_dir="Workspaces/Demo",
    )
    assert out["files"][0]["fs_path"].startswith("Workspaces/Demo/")


def test_no_workspace_base_is_unchanged():
    raw = {"files": [{"name": "a.md", "fs_path": "a.md"}]}
    out = default_fs_path_into_workspace(raw, workspace_base_dir="")
    assert out["files"][0]["fs_path"] == "a.md"
