from __future__ import annotations

import json
import hashlib
import base64
from pathlib import Path
from types import SimpleNamespace

from packages.core.ai.runtime.capabilities import CORE_CAPABILITIES
from packages.core.ai.runtime.approvals import runtime_capability_id_for_action_key
from packages.core.ai.runtime.tool_visibility import (
    MASTER_ALWAYS_LOADED,
    runtime_tool_is_eager_for_profile,
)
from packages.core.ai.tools.video_edit_tools import (
    GSAP_VERSION,
    HYPERFRAMES_VERSION,
    VIDEO_EDIT_SCHEMA,
    _assert_workspace_reference_scope,
    _default_snapshot_times,
    _normalize_entity_relative_path,
    _normalize_project_file_path,
    _project_source_sha256,
    _upload_sandbox_sources,
    _validate_local_runtime_assets,
    _validate_project_contract,
    _video_sandbox_bundle,
)
from packages.core.ai.runtime.video_edit_sessions import (
    append_video_edit_session_event,
    assert_video_edit_session_owner,
    new_video_edit_session,
    video_edit_session_events,
)


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_video_edit_tool_contract_has_approval_gated_operations():
    function = VIDEO_EDIT_SCHEMA["function"]
    parameters = function["parameters"]

    assert function["name"] == "video_edit"
    assert parameters["properties"]["operation"]["enum"] == [
        "start",
        "prepare",
        "push_assets",
        "write_files",
        "sync",
        "status",
        "events",
        "report",
        "review",
        "render",
        "close",
    ]
    assert parameters["required"] == ["operation"]
    assert "session_id" in parameters["properties"]
    assert "files" in parameters["properties"]
    assert "review_sha256" in parameters["properties"]
    assert parameters["properties"]["quality"]["default"] == "high"


def test_professional_video_sandbox_is_a_deferred_capability_tool():
    description = VIDEO_EDIT_SCHEMA["function"]["description"]

    assert "Video Edit" in description
    assert "product demos" in description
    assert "HyperFrames" not in description
    assert "video_edit" in CORE_CAPABILITIES["sandbox.execute"].tool_names
    assert runtime_capability_id_for_action_key("video_edit") == "sandbox.execute"
    assert "video_edit" not in MASTER_ALWAYS_LOADED
    assert not runtime_tool_is_eager_for_profile(
        "video_edit",
        is_master=True,
    )
    assert not runtime_tool_is_eager_for_profile(
        "video_edit",
        is_master=False,
    )


def test_video_edit_paths_accept_entity_fs_urls_and_reject_traversal():
    assert (
        _normalize_entity_relative_path("/api/v1/fs/acme/Videos/launch/source.png", entity_id="acme")
        == "Videos/launch/source.png"
    )
    assert _normalize_entity_relative_path("Videos/launch", entity_id="acme") == "Videos/launch"

    try:
        _normalize_entity_relative_path("/api/v1/fs/another-entity/private.png", entity_id="acme")
    except ValueError as exc:
        assert "different entity" in str(exc)
    else:
        raise AssertionError("cross-entity references should be rejected")

    try:
        _normalize_entity_relative_path("../../secret", entity_id="acme")
    except ValueError as exc:
        assert "traversal" in str(exc).lower()
    else:
        raise AssertionError("path traversal should be rejected")

    assert _normalize_project_file_path("compositions/scene-01.html") == ("compositions/scene-01.html")
    for blocked in ("renders/final.mp4", "snapshots/frame.png", "hyperframes.json"):
        try:
            _normalize_project_file_path(blocked)
        except ValueError:
            pass
        else:
            raise AssertionError(f"runtime-owned source path should be blocked: {blocked}")


def test_review_hash_covers_source_assets_but_ignores_generated_outputs(tmp_path: Path):
    (tmp_path / "index.html").write_text("<main>v1</main>", encoding="utf-8")
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "screen.png").write_bytes(b"reference-v1")
    first = _project_source_sha256(tmp_path)

    snapshots = tmp_path / "snapshots"
    snapshots.mkdir()
    (snapshots / "review.png").write_bytes(b"generated-review")
    renders = tmp_path / "renders"
    renders.mkdir()
    (renders / "final.mp4").write_bytes(b"generated-render")
    assert _project_source_sha256(tmp_path) == first

    vendor = assets / "vendor"
    vendor.mkdir()
    (vendor / "gsap.min.js").write_bytes(b"sandbox-injected-runtime")
    assert _project_source_sha256(tmp_path) == first

    (assets / "screen.png").write_bytes(b"reference-v2")
    assert _project_source_sha256(tmp_path) != first


def test_workspace_references_stay_inside_the_active_workspace():
    current = "Workspaces/ws-current"
    _assert_workspace_reference_scope(
        ["Knowledge/shared.png", f"{current}/Assets/screen.png"],
        workspace_base_dir=current,
    )

    for workspace_base_dir in (current, ""):
        try:
            _assert_workspace_reference_scope(
                ["Workspaces/ws-other/Assets/private.png"],
                workspace_base_dir=workspace_base_dir,
            )
        except PermissionError as exc:
            assert "outside the active Workspace" in str(exc)
        else:
            raise AssertionError("cross-workspace references should be rejected")


def test_project_contract_requires_planning_and_motion_sidecar(tmp_path: Path):
    for name in ("BRIEF.md", "STORYBOARD.md", "video.json", "package.json"):
        (tmp_path / name).write_text("ok", encoding="utf-8")
    (tmp_path / "index.html").write_text(
        """<div id="root" data-composition-id="demo" data-start="0" data-width="1280" data-height="720" data-duration="4" data-fps="30">
<section id="scene" class="clip" data-start="0" data-duration="4" data-track-index="0"></section></div>
<script src="./assets/vendor/gsap.min.js"></script>
<script>window.__timelines=window.__timelines||{};const tl=gsap.timeline({paused:true});window.__timelines["demo"]=tl;</script>""",
        encoding="utf-8",
    )

    assert _validate_project_contract(tmp_path) == ["*.motion.json"]
    (tmp_path / "index.motion.json").write_text(
        json.dumps(
            {
                "duration": 4,
                "assertions": [{"kind": "appearsBy", "selector": "#scene", "bySec": 0.8}],
            }
        ),
        encoding="utf-8",
    )
    assert _validate_project_contract(tmp_path) == []


def test_project_contract_preflights_root_timeline_clips_and_motion_schema(tmp_path: Path):
    for name in ("BRIEF.md", "STORYBOARD.md", "video.json", "package.json"):
        (tmp_path / name).write_text("ok", encoding="utf-8")
    (tmp_path / "index.html").write_text(
        '<main data-composition-id="demo"><section data-start="0"></section></main>',
        encoding="utf-8",
    )
    (tmp_path / "index.motion.json").write_text(
        json.dumps({"assertions": [{"kind": "exists", "selector": "#scene"}]}),
        encoding="utf-8",
    )

    errors = _validate_project_contract(tmp_path)
    assert 'index.html: root missing data-start="0"' in errors
    assert "index.html: root missing data-fps" in errors
    assert any("inline exactly one paused gsap.timeline" in error for error in errors)
    assert any("timed element section missing data-duration" in error for error in errors)
    assert any("unsupported kind 'exists'" in error for error in errors)


def test_review_rejects_remote_runtime_assets(tmp_path: Path):
    index = tmp_path / "index.html"
    index.write_text(
        '<script src="https://cdn.example.invalid/gsap.js"></script>',
        encoding="utf-8",
    )
    errors = _validate_local_runtime_assets(tmp_path)
    assert any("remote runtime/media URL" in error for error in errors)

    index.write_text(
        '<script src="./assets/vendor/gsap.min.js"></script>',
        encoding="utf-8",
    )
    assert _validate_local_runtime_assets(tmp_path) == []


def test_default_review_samples_cover_the_full_timeline():
    assert _default_snapshot_times(10) == [0.6, 2.3, 4.1, 5.9, 7.7, 9.4]


def test_video_edit_session_events_are_ordered_and_owner_scoped():
    state = new_video_edit_session(
        sandbox_id="sbx-1",
        entity_id="entity-1",
        user_id="user-1",
        conversation_id="conversation-1",
        workspace_id="workspace-1",
        project_path="Videos/demo",
    )
    append_video_edit_session_event(state, "progress", message="started")
    append_video_edit_session_event(
        state,
        "asset_request",
        message="Need the billing screenshot",
        data={"kind": "screenshot"},
    )

    assert [event["seq"] for event in video_edit_session_events(state)] == [1, 2]
    assert [event["seq"] for event in video_edit_session_events(state, after_seq=1)] == [2]
    assert_video_edit_session_owner(
        state,
        entity_id="entity-1",
        user_id="user-1",
        conversation_id="conversation-1",
    )
    for owner_kwargs in (
        {"entity_id": "entity-2", "user_id": "user-1", "conversation_id": "conversation-1"},
        {"entity_id": "entity-1", "user_id": "user-2", "conversation_id": "conversation-1"},
        {"entity_id": "entity-1", "user_id": "user-1", "conversation_id": "conversation-2"},
    ):
        try:
            assert_video_edit_session_owner(state, **owner_kwargs)
        except PermissionError:
            pass
        else:
            raise AssertionError("cross-owner Video Edit session access should be rejected")


def test_sandbox_project_upload_is_bounded_and_concurrent(tmp_path: Path):
    import asyncio

    class FakeClient:
        def __init__(self):
            self.active = 0
            self.max_active = 0
            self.paths: list[str] = []

        async def write_file_base64(self, **kwargs):
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            await asyncio.sleep(0.01)
            self.paths.append(kwargs["path"])
            self.active -= 1

    files = []
    for index in range(9):
        path = tmp_path / f"asset-{index}.bin"
        path.write_bytes(f"asset-{index}".encode())
        files.append(path)
    client = FakeClient()

    asyncio.run(
        _upload_sandbox_sources(
            client=client,
            sandbox_id="sbx-test",
            project_dir=tmp_path,
            source_files=files,
        )
    )

    assert client.max_active == 4
    assert sorted(client.paths) == [f"/skill/project/asset-{index}.bin" for index in range(9)]


def test_builtin_video_edit_skill_and_sandbox_runtime_are_pinned():
    skill_dir = REPO_ROOT / "packages/core/ai/skills/video-edit"
    config = json.loads((skill_dir / "config.json").read_text(encoding="utf-8"))
    instructions = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
    sandbox_dockerfile = (REPO_ROOT / "docker/Dockerfile.sandbox").read_text(encoding="utf-8")
    api_dockerfile = (REPO_ROOT / "docker/Dockerfile.api").read_text(encoding="utf-8")
    sandbox_models = (REPO_ROOT / "sandbox-service/sandbox/models.py").read_text(encoding="utf-8")
    sandbox_backend = (REPO_ROOT / "sandbox-service/sandbox/docker_backend.py").read_text(encoding="utf-8")
    sandbox_routes = (REPO_ROOT / "sandbox-service/api/routes.py").read_text(encoding="utf-8")
    sandbox_client = (REPO_ROOT / "packages/core/services/sandbox_sdk/client.py").read_text(encoding="utf-8")

    assert config["id"] == "video-edit"
    assert config["display_name"] == "Video Edit"
    assert "video_edit" in config["tools"]
    assert "hyperframes" not in json.dumps(config).lower()
    assert "HyperFrames" not in instructions
    assert "hyperframes_project" not in instructions
    assert "approval" in instructions.lower()
    assert 'video_edit(operation="start"' in instructions
    assert 'video_edit(operation="push_assets"' in instructions
    assert 'video_edit(operation="write_files"' in instructions
    assert 'video_edit(operation="review"' in instructions
    assert 'video_edit(operation="render"' in instructions
    assert "network-isolated" in instructions
    assert "which chrome" in instructions
    assert "/usr/bin/chromium" in instructions
    assert "Only a structured" in instructions
    assert '"kind": "appearsBy"' in instructions
    assert '"kind": "keepsMoving"' in instructions
    assert "such as `exists`" in instructions
    assert f"HYPERFRAMES_VERSION={HYPERFRAMES_VERSION}" in sandbox_dockerfile
    assert f"GSAP_VERSION={GSAP_VERSION}" in sandbox_dockerfile
    assert "hyperframes browser ensure" in sandbox_dockerfile
    assert "HYPERFRAMES_BROWSER_PATH=/usr/bin/chromium" in sandbox_dockerfile
    assert "FROM node:22-bookworm-slim AS node-runtime" in sandbox_dockerfile
    assert "hyperframes" not in api_dockerfile.lower()
    assert "workdir_tmpfs_size" in sandbox_models
    assert "cfg.workdir_tmpfs_size" in sandbox_backend
    assert "app_config.IDLE_TIMEOUT_SECONDS" in sandbox_backend
    assert "def touch(self)" in sandbox_backend
    assert '"/sandbox/{sandbox_id}/touch"' in sandbox_routes
    assert "async def touch(self, sandbox_id" in sandbox_client
    assert "le=1800" in sandbox_models

    bundle = _video_sandbox_bundle()
    runner = bundle["scripts/video_runtime.py"]
    assert 'PROJECT / "video.json"' in runner
    assert 'PROJECT / "hyperframes.json"' in runner
    assert '"--strict-all"' in runner
    assert '"--no-best-effort"' in runner
    assert '"--no-low-memory-mode"' in runner
    assert "timeout=1800" in runner
    assert '"ffprobe"' in runner
    assert 'payload["sandbox_metrics"]' in (REPO_ROOT / "packages/core/ai/tools/video_edit_tools.py").read_text(
        encoding="utf-8"
    )
    assert '"--strict", "--snapshots", "--json"' in runner
    assert 'shutil.which("hyperframes")' in runner
    assert 'os.environ.get("HYPERFRAMES_BROWSER_PATH")' in runner
    assert "/skill/project" in runner
    assert 'choices=("probe", "manifest", "review", "render")' in runner
    assert 'rel.parts[:2] == ("assets", "vendor")' in runner


def test_video_edit_handler_keeps_one_sandbox_for_authoring_status_and_close(
    tmp_path: Path,
    monkeypatch,
):
    import asyncio

    from packages.core.ai.tools import video_edit_tools
    from packages.core import config as core_config
    from packages.core.services import entity_fs, sandbox_sdk

    sandboxes: dict[str, dict[str, bytes]] = {}
    calls = {"create": 0, "probe": 0, "touch": 0, "destroy": 0}

    class FakeSandboxClient:
        def __init__(self, *args, **kwargs):
            pass

        async def create_from_builtin(self, **kwargs):
            calls["create"] += 1
            sandbox_id = f"sbx-{calls['create']}"
            sandboxes[sandbox_id] = {}
            return SimpleNamespace(sandbox_id=sandbox_id)

        async def touch(self, sandbox_id):
            calls["touch"] += 1
            if sandbox_id not in sandboxes:
                from packages.core.services.sandbox_sdk.exceptions import SandboxNotFoundError

                raise SandboxNotFoundError("missing")
            return SimpleNamespace(
                status="ready",
                active_command=None,
                created_at=1.0,
                last_used_at=2.0,
                expires_at=3602.0,
            )

        async def write_file(self, sandbox_id, path, content, mkdir=True):
            sandboxes[sandbox_id][path] = content.encode("utf-8")
            return SimpleNamespace(path=path, written=True)

        async def write_file_base64(
            self,
            sandbox_id,
            path,
            content_base64,
            mkdir=True,
        ):
            sandboxes[sandbox_id][path] = base64.b64decode(content_base64)
            return SimpleNamespace(path=path, written=True)

        async def exec(self, sandbox_id, command, timeout=60, workdir=None):
            if command.endswith("video_runtime.py probe"):
                calls["probe"] += 1
                return SimpleNamespace(
                    stdout=json.dumps(
                        {
                            "status": "ok",
                            "operation": "probe",
                            "runtime": {
                                "renderer": "/usr/local/bin/hyperframes",
                                "browser": "/usr/bin/chromium",
                                "animation": "/usr/local/lib/node_modules/gsap/dist/gsap.min.js",
                            },
                        }
                    ),
                    stderr="",
                    exit_code=0,
                )
            if command.endswith("video_runtime.py manifest"):
                files = []
                for path, content in sorted(sandboxes[sandbox_id].items()):
                    if not path.startswith("/skill/project/"):
                        continue
                    rel = path.removeprefix("/skill/project/")
                    files.append(
                        {
                            "path": rel,
                            "size_bytes": len(content),
                            "sha256": hashlib.sha256(content).hexdigest(),
                        }
                    )
                payload = {
                    "status": "ok",
                    "operation": "manifest",
                    "files": files,
                }
                return SimpleNamespace(
                    stdout=json.dumps(payload),
                    stderr="",
                    exit_code=0,
                )
            if command.startswith("rm -f -- "):
                path = command.removeprefix("rm -f -- ").strip("'")
                sandboxes[sandbox_id].pop(path, None)
                return SimpleNamespace(stdout="", stderr="", exit_code=0)
            raise AssertionError(f"unexpected command: {command}")

        async def read_file_base64(self, sandbox_id, path, max_size):
            content = sandboxes[sandbox_id][path]
            return SimpleNamespace(
                content_base64=base64.b64encode(content).decode("ascii"),
                size=len(content),
            )

        async def destroy(self, sandbox_id):
            calls["destroy"] += 1
            sandboxes.pop(sandbox_id, None)

        async def close(self):
            pass

    monkeypatch.setattr(sandbox_sdk, "SandboxClient", FakeSandboxClient)
    monkeypatch.setattr(
        core_config,
        "get_settings",
        lambda: SimpleNamespace(SANDBOX_SERVICE_URL="http://sandbox"),
    )
    monkeypatch.setattr(entity_fs, "assert_entity_filesystem_ready", lambda: str(tmp_path))
    monkeypatch.setattr(entity_fs, "get_entity_root", lambda _entity_id: str(tmp_path))

    def fake_atomic_write(entity_id, rel_path, data, **kwargs):
        target = tmp_path / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return str(target)

    monkeypatch.setattr(entity_fs, "write_entity_file_atomic", fake_atomic_write)

    started = json.loads(
        asyncio.run(
            video_edit_tools._video_edit_handler(
                entity_id="entity-1",
                user_id="user-1",
                operation="start",
                project_path="Videos/session-demo",
                conversation_id="conversation-session-demo",
            )
        )
    )
    assert started["status"] == "ready"
    session_id = started["session_id"]

    updated = json.loads(
        asyncio.run(
            video_edit_tools._video_edit_handler(
                entity_id="entity-1",
                user_id="user-1",
                operation="write_files",
                session_id=session_id,
                files={"BRIEF.md": "# Demo", "index.html": "<main>demo</main>"},
                conversation_id="conversation-session-demo",
            )
        )
    )
    assert updated["status"] == "source_updated"
    assert started["authoring_contract"]["timeline"]["requirement"].startswith("Load ./assets/vendor/gsap.min.js")
    assert started["authoring_contract"]["motion_assertions"]["supported_kinds"] == [
        "appearsBy",
        "before",
        "staysInFrame",
        "keepsMoving",
    ]
    assert "STORYBOARD.md" in updated["missing_project_contract"]
    assert "*.motion.json" in updated["missing_project_contract"]
    assert (tmp_path / "Videos/session-demo/BRIEF.md").read_text() == "# Demo"
    assert calls["create"] == 1
    assert calls["probe"] == 1

    status = json.loads(
        asyncio.run(
            video_edit_tools._video_edit_handler(
                entity_id="entity-1",
                user_id="user-1",
                operation="status",
                session_id=session_id,
                after_event_seq=0,
                conversation_id="conversation-session-demo",
            )
        )
    )
    assert status["status"] == "active"
    assert status["sandbox"]["status"] == "ready"
    assert calls["create"] == 1

    sandboxes.clear()
    recovered = json.loads(
        asyncio.run(
            video_edit_tools._video_edit_handler(
                entity_id="entity-1",
                user_id="user-1",
                operation="status",
                session_id=session_id,
                after_event_seq=status["event_cursor"],
                conversation_id="conversation-session-demo",
            )
        )
    )
    assert recovered["status"] == "active"
    assert recovered["recovered"] is True
    assert calls["create"] == 2
    assert calls["probe"] == 2
    assert recovered["events"][-1]["message"].startswith("Sandbox session recovered")

    closed = json.loads(
        asyncio.run(
            video_edit_tools._video_edit_handler(
                entity_id="entity-1",
                user_id="user-1",
                operation="close",
                session_id=session_id,
                conversation_id="conversation-session-demo",
            )
        )
    )
    assert closed["status"] == "closed"
    assert calls["destroy"] == 1
