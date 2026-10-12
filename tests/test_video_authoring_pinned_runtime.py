"""Opt-in, network-free proof that the supplied authoring example passes review.

MANOR_VIDEO_RUNTIME_SMOKE_IMAGE=sandbox-skill:<local-tag> pytest -q this_file
No model/provider call, user project, approval or media-generation API is used.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

import pytest

from packages.core.ai.tools.video_edit_tools import (
    _validate_project_contract,
    _video_authoring_contract,
)


@pytest.mark.docker
def test_minimal_authoring_example_passes_pinned_strict_review(tmp_path):
    image = os.environ.get("MANOR_VIDEO_RUNTIME_SMOKE_IMAGE")
    if not image:
        pytest.skip("Set MANOR_VIDEO_RUNTIME_SMOKE_IMAGE to an already-built local Sandbox image")
    contract = _video_authoring_contract()
    files = {
        "BRIEF.md": "Isolated four-second video authoring contract regression.",
        "STORYBOARD.md": "0–4 seconds: a headline moves 24px while remaining inside the frame.",
        "frame.md": "1280×720, white 64px sans-serif headline on a dark green background.",
        "package.json": {"name": "manor-contract-regression", "private": True, "type": "module"},
        "video.json": {
            "paths": {"blocks": "compositions", "components": "compositions/components", "assets": "assets"},
            "media": {"autoProxy": True}, "authoringSkill": "video-edit",
        },
        "index.html": contract["minimal_example"]["index.html"],
        "index.motion.json": contract["minimal_example"]["index.motion.json"],
    }
    tmp_path.chmod(0o777)
    for name, value in files.items():
        path = tmp_path / name
        path.write_text(value if isinstance(value, str) else json.dumps(value), encoding="utf-8")
        path.chmod(0o666)
    assert _validate_project_contract(tmp_path) == []
    helper = Path(__file__).resolve().parents[1] / "packages/core/ai/skills/video-edit/scripts/video_runtime.py"
    result = subprocess.run(
        [
            "docker", "run", "--rm", "--pull=never", "--network=none", "--shm-size=256m",
            "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges",
            "--tmpfs", "/tmp", "--tmpfs", "/var/tmp", "--pids-limit=512",
            "--tmpfs", "/home/sandbox:exec,size=512m,uid=1000,gid=1000,mode=0755",
            "--volume", f"{tmp_path}:/skill/project",
            "--volume", f"{helper}:/skill/video_runtime.py:ro",
            "--entrypoint", "python3", image,
            "/skill/video_runtime.py", "review", "--snapshot-times", "0,2,3.8",
        ], capture_output=True, text=True, timeout=180, check=False,
    )
    if result.returncode != 0:
        pytest.fail(result.stdout[-12000:] + result.stderr[-4000:], pytrace=False)
    payload = json.loads(result.stdout)
    assert payload["status"] == "ok"
    assert payload["operation"] == "review"
    assert len(payload["files"]) >= 3
    for filename in payload["files"]:
        assert (tmp_path / filename).stat().st_size > 1000
