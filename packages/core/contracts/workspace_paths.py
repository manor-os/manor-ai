"""Default/scoping of artifact paths into a workspace's fixed storage dir."""
from __future__ import annotations

from typing import Any

from packages.core.services.generated_media_naming import scope_workspace_artifact_path


def default_fs_path_into_workspace(data: Any, *, workspace_base_dir: str) -> Any:
    if not isinstance(data, dict) or not workspace_base_dir:
        return data
    files = data.get("files")
    if not isinstance(files, list):
        return data
    out_files = []
    for f in files:
        if not isinstance(f, dict):
            out_files.append(f)
            continue
        entry = dict(f)
        name = entry.get("name") or "artifact"
        raw_path = entry.get("fs_path") or name
        entry["fs_path"] = scope_workspace_artifact_path(raw_path, workspace_base_dir)
        out_files.append(entry)
    out = dict(data)
    out["files"] = out_files
    return out
