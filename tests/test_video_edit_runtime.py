from __future__ import annotations

import json
import hashlib
import base64
from pathlib import Path
from types import SimpleNamespace

import pytest

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
    _review_project,
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


@pytest.mark.asyncio
@pytest.mark.parametrize("requested,expected", [(1, "1"), (2, "2"), (99, "2")])
async def test_render_worker_limit_keeps_fresh_isolated_sandbox(tmp_path, monkeypatch, requested, expected):
    from packages.core.ai.tools import video_edit_tools
    from packages.core.config import get_settings

    calls = {}

    class Client:
        async def create_from_builtin(self, **kwargs):
            calls["create"] = kwargs
            return SimpleNamespace(sandbox_id="fresh-render")

        async def exec(self, **kwargs):
            calls["exec"] = kwargs
            return SimpleNamespace(exit_code=0, stdout='{"status":"ok","files":[]}', stderr="")

        async def destroy(self, sandbox_id):
            calls["destroy"] = sandbox_id

        async def close(self):
            pass

    monkeypatch.setattr(get_settings(), "SANDBOX_SERVICE_URL", "http://sandbox.test")
    monkeypatch.setattr(video_edit_tools, "_video_sandbox_client", lambda **_kwargs: Client())
    await video_edit_tools._run_video_sandbox(
        project_dir=tmp_path, operation="render", sandbox_id="authoring-session",
        upload_sources=False, render_workers=requested,
    )
    assert calls["exec"]["sandbox_id"] == "fresh-render"
    assert calls["exec"]["command"].endswith("--workers " + expected)
    assert calls["create"]["config"]["network"] == "none"
    assert calls["create"]["config"]["pids_limit"] == 512
    assert calls["destroy"] == "fresh-render"


@pytest.mark.asyncio
async def test_review_reports_passed_gate_when_preview_registration_hits_quota(tmp_path, monkeypatch):
    from packages.core.ai.tools import video_edit_tools
    from packages.core.services.document_service import StorageLimitExceeded

    project_dir = tmp_path / "Videos" / "promo"
    project_dir.mkdir(parents=True)
    (project_dir / "index.html").write_text("<main>video</main>", encoding="utf-8")
    monkeypatch.setattr(video_edit_tools, "_project_contract_violations", lambda _path: [])
    monkeypatch.setattr(video_edit_tools, "_validate_local_runtime_assets", lambda _path: [])

    async def run_sandbox(**_kwargs):
        return 0, {"status": "ok"}, {
            "snapshots/manor-review/frame.png": b"preview",
        }, ""

    async def register(**_kwargs):
        raise StorageLimitExceeded("Knowledge full", plan="free", limit=100, current=100)

    monkeypatch.setattr(video_edit_tools, "_run_video_sandbox", run_sandbox)
    monkeypatch.setattr(video_edit_tools, "_register_artifact", register)
    result = json.loads(await _review_project(
        project_dir=project_dir,
        project_rel="Videos/promo",
        entity_root=tmp_path,
        entity_id="entity-1",
        context=SimpleNamespace(user_id="user-1"),
        snapshot_times=[0],
    ))
    assert result["status"] == "blocked"
    assert result["code"] == "knowledge_storage_limit"
    assert result["quality_gate"] == "passed"
    assert result["preview_frames_written"] == 1
    assert (project_dir / "snapshots/manor-review/frame.png").read_bytes() == b"preview"


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


@pytest.mark.parametrize("execution_archive", ["workflow/run-1", "runs/run-1", "tasks/task1"])
def test_execution_archive_references_cover_the_whole_workspace_home(execution_archive):
    # Inside a Workflow or Task the resolved base is the run archive, while
    # generate_file(storage_scope='workspace') writes the shared Workspace home.
    home = "Workspaces/_by_id/folder-current"
    archive = f"{home}/{execution_archive}"
    _assert_workspace_reference_scope(
        [
            f"{home}/Videos/kinetic-quote-zh/CANVAS.md",
            f"{archive}/documents/notes.md",
            "Knowledge/shared.png",
        ],
        workspace_base_dir=archive,
    )

    rejected = [
        "Workspaces/_by_id/folder-other/Videos/private/CANVAS.md",
        "Workspaces/_by_id/folder-current-sibling/Videos/CANVAS.md",
        "Workspaces/_by_id",
        "Workspaces",
        _normalize_entity_relative_path(
            f"{home}/../folder-other/Videos/CANVAS.md", entity_id="acme",
        ),
    ]
    for rel_path in rejected:
        with pytest.raises(PermissionError, match="outside the active Workspace"):
            _assert_workspace_reference_scope([rel_path], workspace_base_dir=archive)
    with pytest.raises(ValueError, match="traversal"):
        _normalize_entity_relative_path(f"{home}/../../../../secret", entity_id="acme")


@pytest.mark.parametrize("archive", ["workflow/run-1", "runs/lineage-1", "tasks/task-1"])
def test_execution_archive_can_read_the_same_workspace_home(archive):
    home = "Workspaces/_by_id/folder-current"
    _assert_workspace_reference_scope(
        [f"{home}/Assets/original.mp4", f"{home}/workflow/previous/audio.wav"],
        workspace_base_dir=f"{home}/{archive}",
    )
    for other in ("folder-other", "folder-current-shadow"):
        with pytest.raises(PermissionError, match="outside the active Workspace"):
            _assert_workspace_reference_scope(
                [f"Workspaces/_by_id/{other}/Assets/private.mp4"],
                workspace_base_dir=f"{home}/{archive}",
            )


@pytest.mark.asyncio
async def test_same_workspace_reference_still_requires_document_read_permission(tmp_path, monkeypatch):
    from packages.core import database
    from packages.core.services import document_access
    from packages.core.ai.tools.video_edit_tools import _preflight_reference_paths

    home = "Workspaces/_by_id/folder-current"
    source_path = f"{home}/Assets/private.mp4"
    source = tmp_path / source_path
    source.parent.mkdir(parents=True)
    source.write_bytes(b"private source")
    project = tmp_path / home / "workflow/run-1/project"
    project.mkdir(parents=True)

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

    checked = []

    async def deny(_db, **kwargs):
        checked.append(kwargs)
        return [source_path]

    monkeypatch.setattr(database, "async_session", Session)
    monkeypatch.setattr(document_access, "unreadable_document_paths", deny)
    with pytest.raises(PermissionError, match="Reference access denied"):
        await _preflight_reference_paths(
            entity_id="entity-current", entity_root=tmp_path,
            raw_paths=[source_path], user_id="user-current",
            workspace_id="workspace-current",
            workspace_base_dir=f"{home}/workflow/run-1",
        )
    assert checked[0]["rel_paths"] == [source_path]
    assert checked[0]["user_id"] == "user-current"
    assert checked[0]["actor_type"] == "agent"
    assert not (project / "assets/references").exists()


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

    assert client.max_active == 2
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
    assert '"--no-low-memory-mode"' not in runner
    # Worker admission is exercised by test_video_runtime_resources; the
    # sandbox must receive that exact runtime rather than a stale bundled copy.
    assert runner == (skill_dir / "scripts/video_runtime.py").read_text(encoding="utf-8")
    assert 'str(max(1, min(workers, 2)))' in runner
    assert "deadline = time.monotonic() + 1740" in runner
    assert "timeout=max(1, int(deadline - time.monotonic() - 60))" in runner
    assert '"ffprobe"' in runner
    assert 'payload["sandbox_metrics"]' in (REPO_ROOT / "packages/core/ai/tools/video_edit_tools.py").read_text(
        encoding="utf-8"
    )
    assert '"--strict", "--no-browser-gpu", "--json"' in runner
    assert '"snapshot"' in runner and 'REVIEW_DIR.as_posix()' in runner
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
    dead_containers: set[str] = set()
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
            if command == "test -d /skill/project":
                return SimpleNamespace(
                    stdout="",
                    stderr=(
                        "Error response from daemon: No such container"
                        if sandbox_id in dead_containers else ""
                    ),
                    exit_code=1 if sandbox_id in dead_containers else 0,
                )
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
            dead_containers.discard(sandbox_id)

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
    assert updated["source_saved"] is True
    assert updated["contract_valid"] is False
    assert updated["authoring_contract"] == started["authoring_contract"]
    assert any(issue["file"] == "index.html" and issue["expected"] for issue in updated["contract_violations"])
    assert "error" not in updated
    assert "repair_strategy" not in updated
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

    unchanged_contract = json.loads(asyncio.run(video_edit_tools._video_edit_handler(
        entity_id="entity-1", user_id="user-1", operation="write_files", session_id=session_id,
        files={"hook.html": "<section>Another scene edit</section>"},
        conversation_id="conversation-session-demo",
    )))
    assert unchanged_contract["source_saved"] is True
    assert unchanged_contract["contract_valid"] is False
    assert "repair_strategy" not in unchanged_contract
    assert unchanged_contract["contract_violations"] == updated["contract_violations"]
    assert (tmp_path / "Videos/session-demo/hook.html").is_file()

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
    assert status["authoring_contract"] == started["authoring_contract"]
    assert status["contract_violations"] == updated["contract_violations"]
    assert calls["create"] == 1

    # The service still remembers the session, but Docker killed its container.
    dead_containers.add("sbx-1")
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
    assert recovered["authoring_contract"] == started["authoring_contract"]
    assert recovered["contract_violations"] == updated["contract_violations"]
    assert calls["create"] == 2
    assert calls["probe"] == 2
    assert recovered["events"][-1]["message"].startswith("Sandbox session recovered")
    assert recovered["events"][-1]["data"]["authoring_contract_version"] == started["authoring_contract"]["version"]
    assert "authoring_contract" not in recovered["events"][-1]["data"]

    synchronized = json.loads(asyncio.run(video_edit_tools._video_edit_handler(
        entity_id="entity-1", user_id="user-1", operation="sync", session_id=session_id,
        conversation_id="conversation-session-demo",
    )))
    assert synchronized["status"] == "synchronized"
    assert synchronized["authoring_contract"] == started["authoring_contract"]
    assert synchronized["contract_valid"] is False
    assert synchronized["contract_violations"] == updated["contract_violations"]

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
    assert calls["destroy"] == 2


@pytest.mark.asyncio
async def test_video_edit_start_retries_only_container_loss_during_initial_injection(
    tmp_path: Path, monkeypatch,
):
    from packages.core.ai.tools import video_edit_tools
    from packages.core import config as core_config
    from packages.core.services.sandbox_sdk.exceptions import SandboxRuntimeError

    attempts = 0

    class FakeClient:
        async def create_from_builtin(self, **_kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise SandboxRuntimeError(
                    "File injection failed: container is not running"
                )
            return SimpleNamespace(sandbox_id="new-sandbox")

        async def exec(self, **_kwargs):
            return SimpleNamespace(
                exit_code=0,
                stderr="",
                stdout=json.dumps({"status": "ok", "runtime": {"browser": "/usr/bin/chromium"}}),
            )

        async def close(self):
            pass

    monkeypatch.setattr(
        core_config, "get_settings",
        lambda: SimpleNamespace(SANDBOX_SERVICE_URL="http://sandbox"),
    )
    monkeypatch.setattr(video_edit_tools, "_video_sandbox_client", lambda **_kwargs: FakeClient())
    sandbox_id, metrics = await video_edit_tools._create_video_session_sandbox(tmp_path)
    assert sandbox_id == "new-sandbox"
    assert attempts == 2
    assert metrics["source_files"] == 0


_FOREIGN_CANVAS = "Workspaces/_by_id/folder-other/Videos/private/CANVAS.md"


def _deny_before_any_video_effect(monkeypatch, tmp_path: Path):
    from unittest.mock import AsyncMock

    from packages.core.ai.tools import video_edit_tools
    from packages.core.services import entity_fs

    monkeypatch.setattr(entity_fs, "assert_entity_filesystem_ready", lambda: str(tmp_path))
    monkeypatch.setattr(entity_fs, "get_entity_root", lambda _entity_id: str(tmp_path))
    monkeypatch.setattr(
        "packages.core.services.generated_media_naming.resolve_workspace_artifact_base_dir",
        AsyncMock(return_value="Workspaces/_by_id/folder-current/workflow/run-1"),
    )

    async def no_effect(*args, **kwargs):
        raise AssertionError("a rejected reference must stop before any Video Edit effect")

    monkeypatch.setattr(video_edit_tools, "_create_video_session_sandbox", no_effect)
    monkeypatch.setattr(video_edit_tools, "_ensure_video_session_sandbox", no_effect)
    monkeypatch.setattr(video_edit_tools, "save_video_edit_session", no_effect)
    return video_edit_tools


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["start", "prepare"])
async def test_out_of_workspace_reference_is_a_proven_no_effect_rejection(monkeypatch, tmp_path, operation):
    from packages.core.ai.runtime.tool_results import RuntimeNoEffectToolResult

    video_edit_tools = _deny_before_any_video_effect(monkeypatch, tmp_path)
    arguments = {
        "operation": operation,
        "project_path": "Videos/kinetic-quote-zh/source-zh",
        "reference_paths": [_FOREIGN_CANVAS],
    }
    result = await video_edit_tools._video_edit_handler(
        entity_id="entity-1", user_id="user-1", workspace_id="ws-1",
        conversation_id="conversation-1", **arguments,
    )

    assert isinstance(result, RuntimeNoEffectToolResult)
    payload = json.loads(result)
    assert payload["error"] == "permission_denied"
    assert "outside the active Workspace" in payload["message"]
    assert payload["details"] == {"applied": False}
    assert result.matches_call("video_edit", arguments)
    # Nothing was staged: not even the project directory.
    assert not (tmp_path / "Workspaces").exists()
    # The proof is bound to this exact pre-effect rejection.
    assert not result.matches_call("video_edit", {**arguments, "operation": "render"})
    assert not result.matches_call("generate_file", arguments)


@pytest.mark.asyncio
async def test_push_assets_reference_rejection_precedes_the_sandbox(monkeypatch, tmp_path):
    from packages.core.ai.runtime.tool_results import RuntimeNoEffectToolResult

    video_edit_tools = _deny_before_any_video_effect(monkeypatch, tmp_path)
    project = "Workspaces/_by_id/folder-current/Videos/demo/source-zh"
    (tmp_path / project).mkdir(parents=True)
    state = new_video_edit_session(
        sandbox_id="sbx-1", entity_id="entity-1", user_id="user-1",
        conversation_id="conversation-1", workspace_id="ws-1", project_path=project,
    )

    async def load(session_id):
        return state if session_id == state["session_id"] else None

    monkeypatch.setattr(video_edit_tools, "load_video_edit_session", load)
    arguments = {"operation": "push_assets", "session_id": state["session_id"],
                 "reference_paths": [_FOREIGN_CANVAS]}
    result = await video_edit_tools._video_edit_handler(
        entity_id="entity-1", user_id="user-1", workspace_id="ws-1",
        conversation_id="conversation-1", **arguments,
    )

    assert isinstance(result, RuntimeNoEffectToolResult)
    assert result.matches_call("video_edit", arguments)
    assert list((tmp_path / project).iterdir()) == []


def test_reference_proof_requires_an_unapplied_rejection_and_is_lost_by_serialization():
    from packages.core.ai.runtime.tool_results import (
        RuntimeNoEffectReason,
        RuntimeNoEffectToolResult,
    )

    text = json.dumps({"contract": "tool_error", "status": "error", "tool": "video_edit",
                       "error": "permission_denied", "message": "x", "details": {"applied": False}})
    proof = RuntimeNoEffectToolResult.video_reference_denied(text)
    assert proof.reason is RuntimeNoEffectReason.VIDEO_REFERENCE_DENIED
    assert not isinstance(json.loads(json.dumps(proof)), RuntimeNoEffectToolResult)
    with pytest.raises(ValueError):
        RuntimeNoEffectToolResult.video_reference_denied(
            json.dumps({"error": "operation_failed", "details": {"applied": False}}))
    with pytest.raises(ValueError):
        RuntimeNoEffectToolResult.video_reference_denied(
            json.dumps({"error": "permission_denied", "details": {}}))
    unavailable = RuntimeNoEffectToolResult.video_reference_unavailable(json.dumps(
        {"error": "reference_unavailable", "message": "x", "details": {"applied": False}}))
    assert unavailable.reason is RuntimeNoEffectReason.VIDEO_REFERENCE_UNAVAILABLE
    # The two reasons stay distinct: neither constructor accepts the other's error code.
    with pytest.raises(ValueError):
        RuntimeNoEffectToolResult.video_reference_unavailable(text)
    with pytest.raises(ValueError):
        RuntimeNoEffectToolResult.video_reference_denied(str(unavailable))


_DENIAL_JSON = json.dumps({
    "contract": "tool_error", "status": "error", "tool": "video_edit",
    "error": "permission_denied", "message": "Workspace reference is outside the active Workspace",
    "details": {"applied": False},
})


@pytest.mark.asyncio
async def test_agent_step_honours_blocked_output_after_a_proven_no_effect_rejection(monkeypatch):
    """The produce step's honest blocked JSON is its result, not a failed step."""
    import packages.core.ai.agentic_loop as loop_module
    from packages.core.ai.runtime.tool_results import RuntimeNoEffectToolResult

    rejection = RuntimeNoEffectToolResult.video_reference_denied(_DENIAL_JSON)
    blocked = json.dumps({"status": "blocked", "summary": "The storyboard is outside this Workspace."})
    calls = []

    async def execute(name, arguments):
        calls.append(name)
        return rejection

    async def model(messages, tools, **kwargs):
        if not calls:
            return "", [
                {"id": "start", "name": "video_edit", "arguments": {
                    "operation": "start", "project_path": "Videos/demo/source-zh",
                    "reference_paths": [_FOREIGN_CANVAS]}},
                {"id": "suffix", "name": "video_edit", "arguments": {"operation": "review"}},
            ], {}
        return blocked, [], {}

    async def summary(*args, **kwargs):
        raise AssertionError("a proven no-effect rejection must not force an unconfirmed-outcome summary")

    monkeypatch.setattr(loop_module, "runtime_execute_agentic_round_tool_completion", model)
    monkeypatch.setattr(loop_module, "runtime_execute_agentic_round_text_completion", summary)
    result = await loop_module.agentic_loop(
        system_prompt="system", user_message="produce",
        tools=[VIDEO_EDIT_SCHEMA], tool_executor=execute, max_rounds=4,
    )

    assert calls == ["video_edit"]
    assert (result.stop_reason, result.error) == ("completed", None)
    assert result.content == blocked
    assert not (result.control or {}).get("error_reason")


@pytest.mark.asyncio
async def test_identical_rejection_json_without_host_proof_is_observed_without_replay(monkeypatch):
    """A plain str (provider/tool JSON) carrying the same text is only evidence for the model."""
    import packages.core.ai.agentic_loop as loop_module

    calls, model_rounds = [], []

    async def execute(name, arguments):
        calls.append(name)
        return str(_DENIAL_JSON)

    async def model(messages, tools, **kwargs):
        model_rounds.append(json.dumps(messages))
        if not calls:
            return "", [
                {"id": "start", "name": "video_edit", "arguments": {
                    "operation": "start", "project_path": "Videos/demo/source-zh",
                    "reference_paths": [_FOREIGN_CANVAS]}},
                {"id": "suffix", "name": "video_edit", "arguments": {"operation": "review"}},
            ], {}
        # The original error reaches the model unchanged; the dependent suffix
        # was dropped, and the model keeps its tools to choose its next call.
        assert "permission_denied" in model_rounds[-1]
        assert '"suffix"' not in model_rounds[-1]
        assert [tool["function"]["name"] for tool in tools] == ["video_edit"]
        return "The Video Edit outcome was not verified.", [], {}

    async def summary(*args, **kwargs):
        raise AssertionError("a tool error no longer forces a tool-free summary")

    monkeypatch.setattr(loop_module, "runtime_execute_agentic_round_tool_completion", model)
    monkeypatch.setattr(loop_module, "runtime_execute_agentic_round_text_completion", summary)
    result = await loop_module.agentic_loop(
        system_prompt="system", user_message="produce",
        tools=[VIDEO_EDIT_SCHEMA], tool_executor=execute, max_rounds=4,
    )

    assert calls == ["video_edit"]
    assert len(model_rounds) == 2
    assert (result.stop_reason, result.error) == ("completed", None)
    assert result.content == "The Video Edit outcome was not verified."


_HOME_CANVAS = "Workspaces/_by_id/folder-current/Videos/kinetic-quote-zh/CANVAS.md"
_ARCHIVE = "Workspaces/_by_id/folder-current/workflow/run-1"


def _reference_reads(monkeypatch, *, readable_workspace="ws-1"):
    """Real ACL routing; only the database rows behind the system-agent rule are faked."""
    from contextlib import asynccontextmanager

    from packages.core.services import document_access

    seen = []

    @asynccontextmanager
    async def session():
        yield object()

    async def system_agent(db, *, entity_id, wanted, wanted_dirs, workspace_id):
        seen.append(workspace_id)
        return set() if workspace_id == readable_workspace else wanted | wanted_dirs

    monkeypatch.setattr("packages.core.database.async_session", session)
    monkeypatch.setattr(document_access, "_system_agent_unreadable_paths", system_agent)
    return seen


@pytest.mark.asyncio
@pytest.mark.parametrize("workspace_id", ["ws-1", None])
async def test_userless_workflow_reference_reads_its_workspace_store(monkeypatch, tmp_path, workspace_id):
    """Scheduler/Strategist runs have no user; the ACL must still see the active Workspace."""
    from packages.core.ai.tools import video_edit_tools

    seen = _reference_reads(monkeypatch)
    (tmp_path / _HOME_CANVAS).parent.mkdir(parents=True)
    (tmp_path / _HOME_CANVAS).write_text("# Canvas", encoding="utf-8")
    preflight = video_edit_tools._preflight_reference_paths(
        entity_id="entity-1", entity_root=tmp_path, raw_paths=[_HOME_CANVAS], user_id=None,
        workspace_id=workspace_id, workspace_base_dir=_ARCHIVE,
    )
    if workspace_id:
        assert await preflight == [_HOME_CANVAS]
    else:
        with pytest.raises(video_edit_tools.VideoReferenceAccessDenied, match="Reference access denied"):
            await preflight
    assert seen == [workspace_id]


@pytest.mark.asyncio
@pytest.mark.parametrize("reference, message", [
    (_HOME_CANVAS.replace("CANVAS.md", "MISSING.md"), "Reference file not found"),
    ("Workspaces/_by_id/folder-current/Videos", "Reference file not found"),
    ("../../secret.png", "traversal"),
    (".ai/memory.json", "Hidden system paths"),
    ("/api/v1/fs/another-entity/Videos/x.png", "different entity"),
    (_HOME_CANVAS, "1 GB staging limit"),
])
@pytest.mark.parametrize("operation", ["start", "push_assets"])
async def test_unusable_reference_is_a_proven_no_effect_rejection(monkeypatch, tmp_path, reference, message, operation):
    from packages.core.ai.runtime.tool_results import RuntimeNoEffectReason, RuntimeNoEffectToolResult

    video_edit_tools = _deny_before_any_video_effect(monkeypatch, tmp_path)
    _reference_reads(monkeypatch)
    canvas = tmp_path / _HOME_CANVAS
    canvas.parent.mkdir(parents=True)
    canvas.write_bytes(b"0123456789")
    monkeypatch.setattr(video_edit_tools, "_MAX_REFERENCE_BYTES", 4)
    project = "Workspaces/_by_id/folder-current/Videos/demo/source-zh"
    arguments = {"operation": operation, "reference_paths": [reference]}
    if operation == "push_assets":
        (tmp_path / project).mkdir(parents=True)
        state = new_video_edit_session(
            sandbox_id="sbx-1", entity_id="entity-1", user_id="", conversation_id=None,
            workspace_id="ws-1", project_path=project,
        )

        async def load(session_id):
            return state if session_id == state["session_id"] else None

        monkeypatch.setattr(video_edit_tools, "load_video_edit_session", load)
        arguments["session_id"] = state["session_id"]
    else:
        arguments["project_path"] = project
    before = sorted(str(path) for path in tmp_path.rglob("*"))
    result = await video_edit_tools._video_edit_handler(
        entity_id="entity-1", user_id="", workspace_id="ws-1", **arguments,
    )

    after = sorted(str(path) for path in tmp_path.rglob("*"))
    assert after == before, "no project directory, staged copy or session may appear"
    assert isinstance(result, RuntimeNoEffectToolResult), result
    payload = json.loads(result)
    assert result.reason is RuntimeNoEffectReason.VIDEO_REFERENCE_UNAVAILABLE
    assert payload["error"] == "reference_unavailable" and message in payload["message"]
    assert payload["details"] == {"applied": False}
    assert result.matches_call("video_edit", arguments)
    assert not result.matches_call("video_edit", {**arguments, "operation": "render"})
    assert not result.matches_call("video_edit", {k: v for k, v in arguments.items() if k != "reference_paths"})


def _function_spans(source: str):
    import ast

    tree = ast.parse(source)
    spans = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            spans.append((node.lineno, node.end_lineno, node.name))
    return tree, spans


def _enclosing(spans, line):
    owners = [span for span in spans if span[0] <= line <= span[1]]
    return min(owners, key=lambda span: span[1] - span[0])[2] if owners else None


def test_no_effect_reference_rejections_come_only_from_the_read_only_preflight():
    import ast

    path = REPO_ROOT / "packages/core/ai/tools/video_edit_tools.py"
    source = path.read_text(encoding="utf-8")
    tree, spans = _function_spans(source)
    carriers = {"VideoReferenceAccessDenied", "VideoReferenceUnavailable"}
    preflight = {"_preflight_reference_paths", "_assert_workspace_reference_scope"}
    raised, scope_calls, effects = [], [], {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Raise) and node.exc is not None:
            call = node.exc.func if isinstance(node.exc, ast.Call) else node.exc
            if isinstance(call, ast.Name) and call.id in carriers:
                raised.append(_enclosing(spans, node.lineno))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id == "_assert_workspace_reference_scope":
                scope_calls.append(_enclosing(spans, node.lineno))
            if node.func.id == "_preflight_reference_paths":
                effects.setdefault(_enclosing(spans, node.lineno), {})["preflight"] = node.lineno
    assert raised and set(raised) <= preflight, raised
    assert scope_calls == ["_preflight_reference_paths"]
    # Every caller runs the preflight before its first effect.
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
            if name in {"mkdir", "_create_video_session_sandbox", "_ensure_video_session_sandbox",
                        "save_video_edit_session", "_stage_references"}:
                owner = _enclosing(spans, node.lineno)
                if owner in effects:
                    effects[owner].setdefault("first_effect", node.lineno)
                    effects[owner]["first_effect"] = min(effects[owner]["first_effect"], node.lineno)
    assert set(effects) == {"_start_video_session", "_push_video_session_assets"}
    for owner, lines in effects.items():
        assert lines["preflight"] < lines["first_effect"], owner
    # Nothing else in the codebase raises the carriers.
    for module in (REPO_ROOT / "packages").rglob("*.py"):
        if module != path:
            text = module.read_text(encoding="utf-8")
            assert not any(f"raise {carrier}" in text for carrier in carriers), module
