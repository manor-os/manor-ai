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
    delete_video_edit_session,
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
        "patch_files",
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


@pytest.fixture
def local_runner(monkeypatch):
    """A fake runner behind every client, in local coordination mode."""
    from packages.core.config import get_settings
    from packages.core.services import sandbox_sdk
    from sandbox_entry_fakes import FakeRunner

    runner = FakeRunner()
    settings = get_settings()
    monkeypatch.setattr(sandbox_sdk, "SandboxClient", runner.client_class)
    monkeypatch.setattr(settings, "SANDBOX_SERVICE_URL", "http://sandbox.test")
    monkeypatch.setattr(settings, "SANDBOX_COORDINATION_MODE", "local")
    monkeypatch.setattr(settings, "SANDBOX_API_TOKEN", "runner-secret")
    return runner


def _video_context(**overrides):
    values = {
        "entity_id": "entity-1", "user_id": "user-1", "agent_id": "agent-1",
        "conversation_id": "conversation-1", "sandbox_binding_key": "conversation-1",
        "runtime_run_id": None, "runtime_tool_call_id": None, "task_id": None,
        "parent_skill": None, "workspace_id": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "host_cpus,reports_config,requested,expected",
    [
        # The runner's effective config: the 4-CPU ask clamped to its host.
        (1, True, 2, "1"),  # staging runner: 1 vCPU
        (2, True, 1, "1"),  # prod runner: 2 vCPU
        (2, True, 99, "2"),
        (16, True, 2, "2"),  # a large host is capped at two workers
        # A runner that reports only its host on /health.
        (1, False, 2, "1"),
        (2, False, 2, "2"),
        # A runner that reports neither gets the smallest runner's share.
        (None, False, 2, "1"),
    ],
)
async def test_render_worker_limit_keeps_fresh_isolated_sandbox(
    tmp_path, local_runner, host_cpus, reports_config, requested, expected,
):
    """Regression: render workers never exceed the CPUs the container got (#1128)."""
    from packages.core.ai.tools import video_edit_tools

    local_runner.host_cpus = host_cpus
    local_runner.reports_config = reports_config
    local_runner.exec_handler = lambda sandbox_id, command, container: SimpleNamespace(
        exit_code=0, stdout='{"status":"ok","files":[]}', stderr="",
    )
    await video_edit_tools._run_video_sandbox(
        project_dir=tmp_path, operation="render", sandbox_id="authoring-session",
        upload_sources=False, render_workers=requested, context=_video_context(),
    )
    [created] = local_runner.created
    [execution] = [call for name, call in local_runner.calls if name == "exec"]
    assert execution["sandbox_id"] == created["sandbox_id"] != "authoring-session"
    # Render trusts the review that ran the same strict check on this hash.
    assert execution["command"].endswith("--workers " + expected + " --reviewed")
    # Named, so Stop can kill the render itself.
    assert execution["execution_id"].startswith("manor-")
    assert created["config"]["network"] == "none"
    assert created["config"]["pids_limit"] == 512
    # The same ceilings everywhere: the runner fits memory and CPUs to its
    # host (Docker refuses --cpus above the host's count).
    assert created["config"]["memory"] == "4g"
    assert created["config"]["cpus"] == 4.0
    assert created["config"]["workdir_tmpfs_size"] == "2g"
    assert created["config"]["exec_timeout"] == 1800
    assert local_runner.destroyed == [created["sandbox_id"]]
    assert local_runner.live() == set()


# ── A failed call is settled by its write trace, never by its error text ──


@pytest.fixture
def render_call(tmp_path, monkeypatch):
    """The real handler on a legacy session (no source sync), sandbox faked."""
    from packages.core.ai.tools import video_edit_tools
    from packages.core.services import entity_fs

    project_dir = tmp_path / "Videos" / "promo"
    project_dir.mkdir(parents=True)
    (project_dir / "index.html").write_text("<main>video</main>", encoding="utf-8")
    state = {"session_id": "s1", "sandbox_id": "sb", "project_path": "Videos/promo",
             "legacy_external_authoring": True, "events": [], "last_event_seq": 0}

    async def session(**_kwargs):
        return state

    async def nothing(*_args, **_kwargs):
        return None

    monkeypatch.setattr(entity_fs, "assert_entity_filesystem_ready", lambda: None)
    monkeypatch.setattr(entity_fs, "get_entity_root", lambda _entity: str(tmp_path))
    monkeypatch.setattr(video_edit_tools, "_resolve_owned_video_session", session)
    monkeypatch.setattr(video_edit_tools, "_ensure_video_session_sandbox", nothing)
    monkeypatch.setattr(video_edit_tools, "save_video_edit_session", nothing)

    async def call(run_sandbox, **arguments):
        monkeypatch.setattr(video_edit_tools, "_run_video_sandbox", run_sandbox)
        state["last_review_sha256"] = _project_source_sha256(project_dir)
        arguments = {
            "operation": "render", "session_id": "s1", "project_path": "Videos/promo",
            "review_sha256": state["last_review_sha256"], **arguments,
        }
        return arguments, await video_edit_tools._video_edit_handler(entity_id="entity-1", **arguments)

    return SimpleNamespace(call=call, project_dir=project_dir)


def _state(arguments, result) -> str:
    from packages.core.ai.runtime.receipts import runtime_tool_effect_state

    return runtime_tool_effect_state("video_edit", arguments, result).value


async def _no_mp4(**_kwargs):
    return 1, {"status": "error", "code": "render_failed"}, {}, "boom"


async def _mp4(**_kwargs):
    return 0, {"status": "ok"}, {"renders/final.mp4": b"mp4 bytes"}, ""


@pytest.mark.asyncio
async def test_a_failure_before_the_first_write_is_none(render_call):
    from packages.core.ai.tools import video_edit_tools

    arguments, result = await render_call.call(_no_mp4, review_sha256="0" * 64)
    assert json.loads(result)["error"] == "review_required"
    assert _state(arguments, result) == "none"
    refused = await video_edit_tools._video_edit_handler(entity_id="entity-1", operation="explode")
    assert _state({"operation": "explode"}, refused) == "none"


@pytest.mark.asyncio
async def test_a_failure_after_completed_writes_only_is_confirmed_with_them_listed(render_call):
    arguments, result = await render_call.call(_no_mp4)
    payload = json.loads(result)
    assert payload["error"] == "render_failed"
    assert not (render_call.project_dir / "renders/final.mp4").exists()
    # The renders directory it created is the only change, and it completed.
    assert payload["details"]["written_files"] == [{"fs_path": "Videos/promo/renders"}]
    assert "uncertain_targets" not in payload["details"]
    assert _state(arguments, result) == "confirmed"
    # Identical JSON from anywhere else proves nothing.
    assert _state(arguments, str(result)) == "unconfirmed"


@pytest.mark.asyncio
async def test_a_write_cut_off_mid_way_stays_unconfirmed_until_a_host_read_reconciles_it(
    render_call, monkeypatch,
):
    import hashlib

    from packages.core.ai.runtime.tool_evidence import RuntimeToolEvidence
    from packages.core.ai.tools import video_edit_tools

    async def register(**_kwargs):
        raise RuntimeError("Knowledge projection unavailable")

    monkeypatch.setattr(video_edit_tools, "_register_artifact", register)
    arguments, result = await render_call.call(_mp4)
    digest = hashlib.sha256(b"mp4 bytes").hexdigest()
    payload = json.loads(result)
    assert payload["error"] == "operation_failed"
    assert payload["details"]["uncertain_targets"] == [
        {"fs_path": "Videos/promo/renders/final.mp4", "sha256": digest},
    ]
    assert _state(arguments, result) == "unconfirmed"

    ledger = RuntimeToolEvidence()
    ledger.observe_tool_result("video_edit", "render", result, arguments)
    assert ledger.unconfirmed_calls[0]["targets"] == [
        {"fs_path": "Videos/promo/renders/final.mp4", "sha256": digest},
    ]
    ledger.observe_tool_result("read_file", "read", json.dumps({
        "path": "Videos/promo/renders/final.mp4", "resolved_path": "Videos/promo/renders/final.mp4",
        "source_sha256": digest, "size": 9,
    }), {"path": "Videos/promo/renders/final.mp4"})
    assert ledger.unconfirmed_calls == [] and ledger.effect_state == "confirmed"


def test_every_video_edit_write_is_inside_the_write_trace():
    """Guard the class: a write outside _tracked_write would let a failure after
    it be settled as "none". Every write primitive in the module sits in a
    ``with _tracked_write(...)`` block (or is the tracked mkdir helper itself)."""
    import ast

    from packages.core.ai.tools import video_edit_tools

    source = Path(video_edit_tools.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    writes = {
        "write_bytes", "write_text", "mkdir", "copy2", "copy", "move", "rmtree", "unlink",
        "rename", "write_entity_file_atomic", "write_file", "_register_artifact",
    }
    # Session-record and Sandbox-mirror helpers that are not Workspace effects.
    exempt_functions = {"_tracked_mkdir", "_upload_sandbox_sources"}
    untracked = []

    def visit(node, function: str, tracked: bool) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            function, tracked = node.name, False
        if isinstance(node, (ast.With, ast.AsyncWith)) and any(
            isinstance(item.context_expr, ast.Call)
            and getattr(item.context_expr.func, "id", "") == "_tracked_write"
            for item in node.items
        ):
            tracked = True
        if isinstance(node, ast.Call):
            name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
            if name in writes and not tracked and function not in exempt_functions:
                untracked.append(f"{function}:{node.lineno}:{name}")
        for child in ast.iter_child_nodes(node):
            visit(child, function, tracked)

    visit(tree, "<module>", False)
    assert untracked == []


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

    # A public URL is not an entity file: it used to become the made-up path
    # "google/fonts/main/..." (local eval 2026-10-17). It is refused with the
    # supported route instead.
    with pytest.raises(ValueError, match="does not download URLs") as refused:
        _normalize_entity_relative_path(
            "https://raw.githubusercontent.com/google/fonts/main/ofl/longcang/LongCang-Regular.ttf",
            entity_id="acme",
        )
    assert "manor push" in str(refused.value)

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
        agent_id="agent-1",
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
    owner = {"entity_id": "entity-1", "user_id": "user-1", "conversation_id": "conversation-1"}
    assert_video_edit_session_owner(state, **owner)
    # The session is the user's, in any of their conversations: conversation
    # and acting agent are recorded for attribution, not checked.
    assert state["agent_id"] == "agent-1" and state["conversation_id"] == "conversation-1"
    assert_video_edit_session_owner(state, **{**owner, "conversation_id": "conversation-2"})
    assert_video_edit_session_owner(state, **{**owner, "conversation_id": None})
    for owner_kwargs in (
        {**owner, "entity_id": "entity-2"},
        {**owner, "user_id": "user-2"},
        # An empty side is never a wildcard: a user-less caller owns nothing
        # of a user's, and nobody without an entity owns anything.
        {**owner, "user_id": None},
        {**owner, "entity_id": ""},
    ):
        try:
            assert_video_edit_session_owner(state, **owner_kwargs)
        except PermissionError:
            pass
        else:
            raise AssertionError("cross-owner Video Edit session access should be rejected")

    # A user-less session is owned only by the same user-less principal.
    userless = new_video_edit_session(
        sandbox_id="sbx-2", entity_id="entity-1", user_id=None, conversation_id="U01TASK",
        agent_id="agent-1", project_path="Videos/demo",
    )
    # A user-less session is owned per task by its workspace entity.
    assert_video_edit_session_owner(userless, entity_id="entity-1", user_id="", conversation_id="U01TASK")
    for caller in (
        {"entity_id": "entity-1", "user_id": "user-1", "conversation_id": "U01TASK"},
        {"entity_id": "entity-1", "user_id": "", "conversation_id": "U01OTHER"},
        {"entity_id": "entity-1", "user_id": "", "conversation_id": ""},
    ):
        with pytest.raises(PermissionError):
            assert_video_edit_session_owner(userless, **caller)
    # A record from before the strict owner binding (it allowed an empty user
    # or conversation) cannot prove its owner.
    legacy = {key: value for key, value in state.items() if key != "agent_id"}
    legacy["version"] = 1
    with pytest.raises(PermissionError, match="call start again"):
        assert_video_edit_session_owner(legacy, **owner)
    # A session needs an owner: a user, or (user-less) a task.
    with pytest.raises(PermissionError):
        new_video_edit_session(
            sandbox_id="sbx-3", entity_id="entity-1", user_id=None, conversation_id="",
            agent_id="agent-1", project_path="Videos/demo",
        )


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
    # W1a (2026-10-14): a used-up review budget still exports the passed revision
    # with exceptions; it is never escalated to someone who could raise the cap.
    assert "delivered with exceptions" in instructions
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
    assert "le=3600" in sandbox_models

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
    assert 'str(max(1, min(workers, 4)))' in runner
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


def _video_runtime_exec(dead_containers: set[str], counts: dict[str, int]):
    """The video runtime's commands, answered from the fake container's files."""

    def handle(sandbox_id, command, container):
        files = container["files"]
        if command == "test -d /skill/project":
            dead = sandbox_id in dead_containers
            # The directory exists once created, or once a file was put in it.
            exists = container.get("project_dir") or any(path.startswith("/skill/project/") for path in files)
            return SimpleNamespace(
                stdout="", stderr="Error response from daemon: No such container" if dead else "",
                exit_code=1 if dead or not exists else 0,
            )
        if command == "mkdir -p /skill/project":
            container["project_dir"] = True
            return SimpleNamespace(stdout="", stderr="", exit_code=0)
        if command.endswith("video_runtime.py probe"):
            counts["probe"] += 1
            return SimpleNamespace(stdout=json.dumps({
                "status": "ok", "operation": "probe",
                "runtime": {"renderer": "/usr/local/bin/hyperframes", "browser": "/usr/bin/chromium"},
            }), stderr="", exit_code=0)
        if command.endswith("video_runtime.py manifest"):
            manifest = [
                {"path": path.removeprefix("/skill/project/"), "size_bytes": len(content),
                 "sha256": hashlib.sha256(content).hexdigest()}
                for path, content in sorted(files.items()) if path.startswith("/skill/project/")
            ]
            return SimpleNamespace(stdout=json.dumps({"status": "ok", "operation": "manifest", "files": manifest}),
                                   stderr="", exit_code=0)
        if command.startswith("rm -f -- "):
            files.pop(command.removeprefix("rm -f -- ").strip("'"), None)
            return SimpleNamespace(stdout="", stderr="", exit_code=0)
        raise AssertionError(f"unexpected command: {command}")

    return handle


@pytest.mark.asyncio
async def test_video_edit_handler_keeps_one_sandbox_for_authoring_status_and_close(
    tmp_path: Path,
    monkeypatch,
    local_runner,
):
    from packages.core.ai.tools import video_edit_tools
    from packages.core.services import entity_fs

    dead_containers: set[str] = set()
    counts = {"probe": 0}
    local_runner.exec_handler = _video_runtime_exec(dead_containers, counts)
    monkeypatch.setattr(entity_fs, "assert_entity_filesystem_ready", lambda: str(tmp_path))
    monkeypatch.setattr(entity_fs, "get_entity_root", lambda _entity_id: str(tmp_path))

    def fake_atomic_write(entity_id, rel_path, data, **kwargs):
        target = tmp_path / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return str(target)

    monkeypatch.setattr(entity_fs, "write_entity_file_atomic", fake_atomic_write)

    async def call(**arguments):
        return json.loads(await video_edit_tools._video_edit_handler(
            entity_id="entity-1", user_id="user-1",
            conversation_id="conversation-session-demo", **arguments,
        ))

    started = await call(operation="start", project_path="Videos/session-demo")
    assert started["status"] == "ready"
    session_id = started["session_id"]
    [first] = local_runner.created
    # Created through the entry point with the video runtime's resources.
    assert first["skill_name"] == "video-edit-runtime"
    assert first["config"]["memory"] == "4g" and first["config"]["network"] == "none"

    updated = await call(
        operation="write_files", session_id=session_id,
        files={"BRIEF.md": "# Demo", "index.html": "<main>demo</main>"},
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
    assert len(local_runner.created) == 1
    assert counts["probe"] == 1

    unchanged_contract = await call(
        operation="write_files", session_id=session_id,
        files={"hook.html": "<section>Another scene edit</section>"},
    )
    assert unchanged_contract["source_saved"] is True
    assert unchanged_contract["contract_valid"] is False
    assert "repair_strategy" not in unchanged_contract
    assert unchanged_contract["contract_violations"] == updated["contract_violations"]
    assert (tmp_path / "Videos/session-demo/hook.html").is_file()

    status = await call(operation="status", session_id=session_id, after_event_seq=0)
    assert status["status"] == "active"
    assert status["sandbox"]["status"] == "ready"
    assert status["authoring_contract"] == started["authoring_contract"]
    assert status["contract_violations"] == updated["contract_violations"]
    assert len(local_runner.created) == 1

    # The service still remembers the session, but Docker killed its container.
    dead_containers.add(first["sandbox_id"])
    recovered = await call(operation="status", session_id=session_id, after_event_seq=status["event_cursor"])
    assert recovered["status"] == "active"
    assert recovered["recovered"] is True
    assert recovered["authoring_contract"] == started["authoring_contract"]
    assert recovered["contract_violations"] == updated["contract_violations"]
    assert len(local_runner.created) == 2
    assert counts["probe"] == 2
    # What was left of the dead container is destroyed, not abandoned.
    assert local_runner.destroyed == [first["sandbox_id"]]
    assert recovered["events"][-1]["message"].startswith("Sandbox session recovered")
    assert recovered["events"][-1]["data"]["authoring_contract_version"] == started["authoring_contract"]["version"]
    assert "authoring_contract" not in recovered["events"][-1]["data"]

    synchronized = await call(operation="sync", session_id=session_id)
    assert synchronized["status"] == "synchronized"
    assert synchronized["authoring_contract"] == started["authoring_contract"]
    assert synchronized["contract_valid"] is False
    assert synchronized["contract_violations"] == updated["contract_violations"]

    # The user's session is theirs from any conversation, without its id.
    elsewhere = json.loads(await video_edit_tools._video_edit_handler(
        entity_id="entity-1", user_id="user-1", conversation_id="conversation-elsewhere",
        operation="status", after_event_seq=0,
    ))
    assert elsewhere["status"] == "active" and elsewhere["session_id"] == session_id

    closed = await call(operation="close", session_id=session_id)
    assert closed["status"] == "closed"
    assert len(local_runner.destroyed) == 2
    assert local_runner.live() == set()


@pytest.mark.asyncio
async def test_patch_files_is_all_or_nothing_and_invalidates_the_review(tmp_path: Path, monkeypatch, local_runner):
    from packages.core.ai.tools import video_edit_tools
    from packages.core.services import entity_fs

    local_runner.exec_handler = _video_runtime_exec(set(), {"probe": 0})
    monkeypatch.setattr(entity_fs, "assert_entity_filesystem_ready", lambda: str(tmp_path))
    monkeypatch.setattr(entity_fs, "get_entity_root", lambda _entity_id: str(tmp_path))

    def fake_atomic_write(entity_id, rel_path, data, **kwargs):
        target = tmp_path / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return str(target)

    monkeypatch.setattr(entity_fs, "write_entity_file_atomic", fake_atomic_write)

    async def raw_call(**arguments):
        return await video_edit_tools._video_edit_handler(
            entity_id="entity-1", user_id="user-1", conversation_id="conversation-patch", **arguments,
        )

    started = json.loads(await raw_call(operation="start", project_path="Videos/patch-demo"))
    session_id = started["session_id"]
    project = tmp_path / "Videos/patch-demo"
    html = "<main>\n<h1 id='t'>Old title</h1>\n<p class='c'>red</p><p class='c'>red</p>\n</main>"
    storyboard = "# Storyboard\n\n00:00 Hook - old copy\n"
    written = json.loads(await raw_call(
        operation="write_files", session_id=session_id,
        files={"index.html": html, "STORYBOARD.md": storyboard},
    ))
    [container] = [local_runner.containers[c["sandbox_id"]] for c in local_runner.created]

    def sandbox_text(rel_path: str) -> str:
        return container["files"][f"/skill/project/{rel_path}"].decode("utf-8")

    # A missing match in the second file fails the call before anything is written.
    arguments = {"operation": "patch_files", "session_id": session_id, "patches": [
        {"path": "index.html", "edits": [{"old_text": "Old title", "new_text": "New title"}]},
        {"path": "STORYBOARD.md", "edits": [{"old_text": "no such text", "new_text": "x"}]},
    ]}
    missing = await raw_call(**arguments)
    payload = json.loads(missing)
    assert payload["error"] == "patch_text_not_found"
    assert payload["details"]["path"] == "STORYBOARD.md" and payload["details"]["edit_index"] == 0
    assert _state(arguments, missing) == "none"
    assert sandbox_text("index.html") == html and (project / "index.html").read_text() == html

    ambiguous = json.loads(await raw_call(operation="patch_files", session_id=session_id, patches=[
        {"path": "index.html", "edits": [{"old_text": "red", "new_text": "blue"}]},
    ]))
    assert ambiguous["error"] == "patch_text_ambiguous" and ambiguous["details"]["match_count"] == 2
    absent = json.loads(await raw_call(operation="patch_files", session_id=session_id, patches=[
        {"path": "scene.css", "edits": [{"old_text": "a", "new_text": "b"}]},
    ]))
    assert absent["error"] == "patch_target_not_found"
    assert sandbox_text("index.html") == html

    # The current source was reviewed and approved.
    state = await video_edit_tools.load_video_edit_session(session_id)
    approved = written["source_sha256"]
    state["last_review_sha256"] = approved
    await video_edit_tools.save_video_edit_session(state)

    arguments = {"operation": "patch_files", "session_id": session_id, "patches": [
        {"path": "index.html", "edits": [
            {"old_text": "Old title", "new_text": "New title"},
            {"old_text": "red", "new_text": "blue", "replace_all": True},
        ]},
        {"path": "STORYBOARD.md", "edits": [{"old_text": "old copy", "new_text": "new copy"}]},
    ]}
    patched_result = await raw_call(**arguments)
    patched = json.loads(patched_result)
    expected_html = html.replace("Old title", "New title").replace("red", "blue")
    expected_storyboard = storyboard.replace("old copy", "new copy")
    assert patched["status"] == "source_updated" and patched["written_files"] == ["index.html", "STORYBOARD.md"]
    assert _state(arguments, patched_result) == _state(
        {"operation": "write_files", "session_id": session_id, "files": {}}, json.dumps(written),
    )
    for rel_path, text, replacements in (
        ("index.html", expected_html, 3), ("STORYBOARD.md", expected_storyboard, 1),
    ):
        assert sandbox_text(rel_path) == text
        assert (project / rel_path).read_text() == text
        [entry] = [item for item in patched["patched_files"] if item["path"] == rel_path]
        assert entry["replacements"] == replacements
        assert entry["size_bytes"] == len(text.encode())
        assert entry["sha256"] == hashlib.sha256(text.encode()).hexdigest()
    assert patched["source_sha256"] != approved
    assert patched["source_revision"] == written["source_revision"] + 1

    # The approval was for the pre-patch source: render requires a new review.
    rendered = json.loads(await raw_call(operation="render", session_id=session_id, review_sha256=approved))
    assert rendered["error"] == "source_changed_after_review"
    assert rendered["details"]["current_sha256"] == patched["source_sha256"]


@pytest.mark.asyncio
async def test_a_replaced_video_session_never_takes_the_owners_container_back(tmp_path: Path, monkeypatch, local_runner):
    """One session container per user: a start replaces the previous session,
    and a record of it that survived (a failed delete, a stale cache entry)
    must not rebuild a container on the user's scope -- that would destroy
    the current session's container and the two would take turns."""

    from packages.core.ai.tools import video_edit_tools
    from packages.core.services import entity_fs

    local_runner.exec_handler = _video_runtime_exec(set(), {"probe": 0})
    monkeypatch.setattr(entity_fs, "assert_entity_filesystem_ready", lambda: str(tmp_path))
    monkeypatch.setattr(entity_fs, "get_entity_root", lambda _entity_id: str(tmp_path))

    async def call(conversation_id, **arguments):
        return json.loads(await video_edit_tools._video_edit_handler(
            entity_id="entity-1", user_id="user-1", conversation_id=conversation_id, **arguments,
        ))

    first = await call("conversation-a", operation="start", project_path="Videos/one")
    stale = await video_edit_tools.load_video_edit_session(first["session_id"])
    second = await call("conversation-b", operation="start", project_path="Videos/two")
    [old_container, current_container] = [created["sandbox_id"] for created in local_runner.created]
    # Start in another conversation replaced the user's session.
    assert local_runner.live() == {current_container}

    # The record survived (a failed delete, a stale cache entry) and is even
    # written again: saving it never makes it the owner's session again.
    await video_edit_tools.save_video_edit_session(stale)
    revived = await call("conversation-a", operation="status", session_id=first["session_id"])
    assert "replaced by a newer session" in revived["message"]
    assert local_runner.live() == {current_container}
    assert len(local_runner.created) == 2

    current = await call("conversation-a", operation="status", session_id=second["session_id"])
    assert current["status"] == "active" and current["recovered"] is False

    # Even with no record left to retire (expired after a day), the user's
    # one session container is replaced by their next start, wherever it is.
    await delete_video_edit_session(
        await video_edit_tools.load_video_edit_session(second["session_id"])
    )
    third = await call("conversation-c", operation="start", project_path="Videos/three")
    third_container = local_runner.created[-1]["sandbox_id"]
    assert local_runner.live() == {third_container}
    await call("conversation-c", operation="close", session_id=third["session_id"])
    assert local_runner.live() == set()


@pytest.mark.asyncio
async def test_video_edit_start_retries_only_container_loss_during_initial_injection(
    tmp_path: Path, local_runner,
):
    from packages.core.ai.tools import video_edit_tools
    from packages.core.services.sandbox_sdk.exceptions import SandboxRuntimeError

    local_runner.create_failures.append(SandboxRuntimeError("File injection failed: container is not running"))
    local_runner.exec_handler = lambda sandbox_id, command, container: SimpleNamespace(
        exit_code=0, stderr="",
        stdout=json.dumps({"status": "ok", "runtime": {"browser": "/usr/bin/chromium"}}),
    )
    sandbox_id, metrics = await video_edit_tools._create_video_session_sandbox(tmp_path, _video_context())
    assert [call["source"] for name, call in local_runner.calls if name == "create"] == ["builtin", "builtin"]
    assert local_runner.live() == {sandbox_id}
    assert metrics["source_files"] == 0

    # Any other create failure is reported, never retried.
    local_runner.create_failures.append(SandboxRuntimeError("image pull failed"))
    with pytest.raises(video_edit_tools.VideoEditRuntimeUnavailable, match="image pull failed"):
        await video_edit_tools._create_video_session_sandbox(tmp_path, _video_context(agent_id="agent-2"))


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
        conversation_id="conversation-1", agent_id=None, workspace_id="ws-1", project_path=project,
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
    # write_files proves a no-op only for its own read-only source_paths preflight.
    assert proof.matches_call("video_edit", {"operation": "write_files", "source_paths": {"index.html": "a.html"}})
    assert not proof.matches_call("video_edit", {"operation": "write_files", "files": {"index.html": "x"}})


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
            sandbox_id="sbx-1", entity_id="entity-1", user_id="", conversation_id="U01TASKREF",
            agent_id=None, workspace_id="ws-1", project_path=project,
        )

        async def load(session_id):
            return state if session_id == state["session_id"] else None

        monkeypatch.setattr(video_edit_tools, "load_video_edit_session", load)
        arguments["session_id"] = state["session_id"]
    else:
        arguments["project_path"] = project
    before = sorted(str(path) for path in tmp_path.rglob("*"))
    result = await video_edit_tools._video_edit_handler(
        entity_id="entity-1", user_id="", workspace_id="ws-1", conversation_id="U01TASKREF", **arguments,
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
    assert set(effects) == {"_start_video_session", "_push_video_session_assets", "_read_video_source_paths"}
    for owner, lines in effects.items():
        if owner == "_read_video_source_paths":
            # write_files(source_paths) reads before its caller's first effect.
            assert "first_effect" not in lines, owner
            continue
        assert lines["preflight"] < lines["first_effect"], owner
    # Nothing else in the codebase raises the carriers.
    for module in (REPO_ROOT / "packages").rglob("*.py"):
        if module != path:
            text = module.read_text(encoding="utf-8")
            assert not any(f"raise {carrier}" in text for carrier in carriers), module


def test_video_containers_are_admitted_in_the_callers_per_user_sandbox_scope() -> None:
    """Video Edit uses the one per-user scope resolver (sandbox-user-conversation):
    a human user's authoring session is their dedicated ``user:video-edit``
    role container in every conversation; a group room run and user-less work
    keep containers of their own scope; a render is never registered."""

    from packages.core.ai.runtime.sandbox import runtime_room_binding_key
    from packages.core.ai.tools.video_edit_tools import _video_sandbox_identity
    from packages.core.services.sandbox_session_service import SandboxLifetime, sandbox_reuse_scope

    def scope_of(context, lifetime=SandboxLifetime.SESSION, purpose="video-session"):
        owner, bundle, binding = _video_sandbox_identity(context, lifetime=lifetime, purpose=purpose)
        return owner, sandbox_reuse_scope(bundle, binding, owner)

    human = SimpleNamespace(entity_id="ent", user_id="user-1", conversation_id="conv-a", sandbox_binding_key=None)
    assert scope_of(human)[1] == (True, "user:video-edit")
    assert scope_of(SimpleNamespace(**{**vars(human), "conversation_id": "conv-b"}))[1] == (True, "user:video-edit")
    assert scope_of(human, SandboxLifetime.ONE_SHOT, "video-render")[1] == (False, None)

    room_key = runtime_room_binding_key("01ROOMRUN0000000000000000A")
    room = SimpleNamespace(entity_id="ent", user_id="user-1", conversation_id="room-conv", sandbox_binding_key=room_key)
    assert scope_of(room)[1] == (True, f"conversation:{room_key}:video-edit")

    userless = SimpleNamespace(
        entity_id="ent", user_id="", conversation_id=None, sandbox_binding_key="task-key-1",
        runtime_envelope=SimpleNamespace(workspace_id="ws-9"),
    )
    owner, scope = scope_of(userless)
    assert owner.workspace_id == "ws-9"
    assert scope == (True, "conversation:task-key-1:video-edit")

    # A scheduled Workflow has neither a chat nor a Task row. Its trusted
    # run identity must reach the same sandbox admission and owner checks.
    from packages.core.ai.runtime.tool_context import runtime_tool_call_context_from_kwargs

    for user_id in ("user-1", None):
        workflow = runtime_tool_call_context_from_kwargs(
            {"_workflow_run_id_from_context": "01M51P1YJSCBPQ2EYY261CJDMY"},
            entity_id="ent", user_id=user_id,
            runtime_envelope=SimpleNamespace(workspace_id="ws-9"),
        )
        owner, scope = scope_of(workflow)
        assert workflow.conversation_id is None and workflow.task_id is None
        assert owner.user_id == (user_id or "")
        if user_id:
            assert scope == (True, "user:video-edit")
        else:
            assert owner.workspace_id == "ws-9"
            assert scope == (True, f"conversation:{workflow.sandbox_binding_key}:video-edit")


@pytest.mark.asyncio
async def test_write_files_source_paths_copy_generated_text_under_reference_acl(monkeypatch, tmp_path):
    """A builder Skill's saved page is copied by path: re-typing a 40 KB page
    through the model truncates it and left EP.04 without a project source."""
    from packages.core.ai.tools import video_edit_tools

    page = "<!doctype html>" + "x" * 40_000
    (tmp_path / "Workspaces").mkdir()
    (tmp_path / "Workspaces" / "page.html").write_text(page, encoding="utf-8")
    (tmp_path / "Workspaces" / "frame.png").write_bytes(b"\x89PNG\xff\xfe")
    checked = []

    async def preflight(*, raw_paths, **_kwargs):
        checked.extend(raw_paths)
        return [str(value) for value in raw_paths]

    monkeypatch.setattr(video_edit_tools, "_preflight_reference_paths", preflight)
    context = SimpleNamespace(user_id="user-1", workspace_id="ws-1")
    kwargs = dict(entity_id="entity-1", entity_root=tmp_path, context=context, workspace_base_dir="")

    copied = await video_edit_tools._read_video_source_paths({"index.html": "Workspaces/page.html"}, **kwargs)
    assert copied == {"index.html": page}
    assert checked == ["Workspaces/page.html"]

    refused = await video_edit_tools._read_video_source_paths({"index.html": "Workspaces/frame.png"}, **kwargs)
    assert "UTF-8" in json.loads(refused)["message"]


@pytest.mark.asyncio
async def test_render_releases_the_authoring_sandbox_before_its_own(tmp_path, monkeypatch):
    """Both sandboxes are sized to the whole runner: on a 2 GB staging runner
    the pair starved Chromium and EP.04's render died in its preflight check."""
    from packages.core.ai.tools import video_edit_tools

    project_dir = tmp_path / "Videos" / "promo"
    project_dir.mkdir(parents=True)
    (project_dir / "index.html").write_text("<main>video</main>", encoding="utf-8")
    approved = video_edit_tools._project_source_sha256(project_dir)
    order = []

    async def ensure(state, **_kwargs):
        order.append(("ensure", state["sandbox_id"]))
        return {"status": "ready"}, False

    async def sync(**_kwargs):
        order.append(("sync",))

    async def release(state, context):
        order.append(("release", state["sandbox_id"]))

    async def run_sandbox(**kwargs):
        order.append(("render", kwargs["operation"]))
        return 0, {"status": "ok"}, {"renders/final.mp4": b"mp4"}, ""

    async def register(**_kwargs):
        return {}

    monkeypatch.setattr(video_edit_tools, "_ensure_video_session_sandbox", ensure)
    monkeypatch.setattr(video_edit_tools, "_sync_video_session_to_entity", sync)
    monkeypatch.setattr(video_edit_tools, "_release_video_session_sandbox", release)
    monkeypatch.setattr(video_edit_tools, "_run_video_sandbox", run_sandbox)
    async def save(_state):
        return None

    monkeypatch.setattr(video_edit_tools, "_register_artifact", register)
    monkeypatch.setattr(video_edit_tools, "save_video_edit_session", save)
    monkeypatch.setattr(video_edit_tools, "append_video_edit_session_event", lambda *_a, **_k: None)
    await video_edit_tools._render_project(
        project_dir=project_dir, project_rel="Videos/promo", entity_root=tmp_path,
        entity_id="entity-1", context=SimpleNamespace(user_id="user-1"),
        review_sha256=approved, output_name="final.mp4", quality="high",
        session_state={"sandbox_id": "authoring", "last_review_sha256": approved, "session_id": "ved_1", "events": [{}], "last_event_seq": 0},
    )

    assert order == [("ensure", "authoring"), ("sync",), ("release", "authoring"), ("render", "render")]


@pytest.mark.asyncio
async def test_releasing_the_authoring_sandbox_discards_it_through_the_session_service(monkeypatch):
    """The release is the scope's own discard (registry entry and container),
    never a direct client: the next session operation rebuilds it."""
    from packages.core.ai.tools import video_edit_tools

    discarded = []

    async def discard(context, sandbox_id):
        discarded.append((context.user_id, sandbox_id))
        return "destroyed"

    monkeypatch.setattr(video_edit_tools, "_discard_video_session_sandbox", discard)
    context = SimpleNamespace(user_id="user-1")
    await video_edit_tools._release_video_session_sandbox({"sandbox_id": "authoring"}, context)
    await video_edit_tools._release_video_session_sandbox({"sandbox_id": ""}, context)
    assert discarded == [("user-1", "authoring")]


def test_render_uploads_only_the_assets_the_page_names(tmp_path):
    """EP.04 carried 282 MB of unused staged references into a 1.5 GB render sandbox.

    Picture book 01M58KZRHR9WJG0GMFZJEFPTTY built its narration and one scene's
    path in script; the render left them out and its audio mix failed.
    """
    from packages.core.ai.tools import video_edit_tools

    refs = tmp_path / "assets" / "references"
    refs.mkdir(parents=True)
    for name in (
        "used.mp4", "unused-old-take.mp4", "Noto.ttf", "海报.png", "dup-1234abcd.mp4",
        "norm-zh-s01.wav", "norm-zh-s06.wav", "s04-bg.png", "music-all-s01-s06.wav",
    ):
        (refs / name).write_bytes(b"x")
    (tmp_path / "index.html").write_text(
        '<video src="assets/references/used.mp4"></video><img src="assets/references/%E6%B5%B7%E6%8A%A5.png">'
        "<script>for(let i=1;i<=6;i++){vo.src='assets/references/norm-zh-s0'+i+'.wav';}"
        "bg.src=`./assets/references/s0${n}-bg.png`;</script>",
        encoding="utf-8",
    )
    (tmp_path / "style.css").write_text("@font-face{src:url('assets/references/Noto.ttf')}", encoding="utf-8")
    (tmp_path / "BRIEF.md").write_text("notes", encoding="utf-8")

    files = video_edit_tools._project_source_files(tmp_path)
    kept = {p.relative_to(tmp_path).as_posix() for p in video_edit_tools._render_source_files(tmp_path, files)}

    assert kept == {
        "BRIEF.md", "index.html", "style.css",
        "assets/references/used.mp4", "assets/references/Noto.ttf", "assets/references/海报.png",
        "assets/references/norm-zh-s01.wav", "assets/references/norm-zh-s06.wav",
        "assets/references/s04-bg.png",
    }


@pytest.mark.asyncio
async def test_restaging_identical_bytes_reuses_the_reference(tmp_path, monkeypatch):
    """The same clip staged under another path spelling must not become a -hash copy."""
    from packages.core.ai.tools import video_edit_tools

    monkeypatch.setattr(video_edit_tools, "_tracked_mkdir", lambda path: path.mkdir(parents=True, exist_ok=True))
    entity = tmp_path / "entity"
    (entity / "a").mkdir(parents=True)
    (entity / "b").mkdir(parents=True)
    (entity / "a" / "clip.mp4").write_bytes(b"same")
    (entity / "b" / "clip.mp4").write_bytes(b"same")
    project = tmp_path / "project"
    project.mkdir()

    first = await video_edit_tools._stage_references(entity_root=entity, project_dir=project, rel_paths=["a/clip.mp4"])
    again = await video_edit_tools._stage_references(entity_root=entity, project_dir=project, rel_paths=["b/clip.mp4"])
    assert first[0]["project_path"] == again[0]["project_path"] == "assets/references/clip.mp4"
    assert sorted(p.name for p in (project / "assets" / "references").iterdir()) == ["clip.mp4"]

    (entity / "b" / "clip.mp4").write_bytes(b"different")
    other = await video_edit_tools._stage_references(entity_root=entity, project_dir=project, rel_paths=["b/clip.mp4"])
    assert other[0]["project_path"] != "assets/references/clip.mp4"


def _composition(tmp_path, *, duration: float, width: int = 1080, height: int = 1920):
    (tmp_path / "index.html").write_text(
        f'<main data-composition-id="r" data-start="0" data-duration="{duration}" '
        f'data-width="{width}" data-height="{height}"></main>',
        encoding="utf-8",
    )
    return tmp_path


@pytest.mark.parametrize(
    ("duration", "width", "height", "memory", "cpus"),
    [
        (109.1, 1080, 1920, "10776m", 8.0),  # measured: 10.7 GB peak at 4 workers
        (15, 1080, 1920, "4096m", 4.0),  # short clips get the floor
        (109.1, 720, 1280, "5927m", 4.0),  # fewer pixels, less memory
        (900, 1080, 1920, "14336m", 8.0),  # capped at VIDEO_RENDER_MAX_MEMORY_MB
    ],
)
def test_render_size_request_follows_the_composition(tmp_path, duration, width, height, memory, cpus):
    from packages.core.ai.tools import video_edit_tools

    request = video_edit_tools._render_size_request(_composition(tmp_path, duration=duration, width=width, height=height))
    assert (request["memory"], request["cpus"], request["pids_limit"]) == (memory, cpus, 1536)


@pytest.mark.asyncio
async def test_render_pool_is_used_only_when_it_can_serve(monkeypatch):
    from contextlib import asynccontextmanager
    from packages.core import database
    from packages.core.ai.tools import video_edit_tools
    from packages.core.config import get_settings

    found = {"runner": None}

    class Session:
        async def scalar(self, _query):
            return found["runner"]

    @asynccontextmanager
    async def session():
        yield Session()

    monkeypatch.setattr(database, "async_session", session)
    monkeypatch.setattr(get_settings(), "VIDEO_RENDER_RUNNER_POOL", "", raising=False)
    assert await video_edit_tools._render_runner_pool() == "default"

    monkeypatch.setattr(get_settings(), "VIDEO_RENDER_RUNNER_POOL", "render", raising=False)
    monkeypatch.setattr(get_settings(), "SANDBOX_AUTOSCALE_FLEETS_JSON", "", raising=False)
    # Nothing would ever serve a ticket there: stay on the default pool.
    assert await video_edit_tools._render_runner_pool() == "default"


def _finished(*, stdout: str, exit_code: int = 0) -> SimpleNamespace:
    """A started command's status when it has already ended."""

    return SimpleNamespace(
        status="completed" if exit_code == 0 else "failed", terminal=True, exit_code=exit_code,
        stdout=stdout, stderr="", events=[], next_sequence=0, outcome=None,
    )


@pytest.mark.asyncio
async def test_a_render_pool_render_asks_for_its_size_and_runs_four_workers_on_eight_cpus(tmp_path, monkeypatch):
    from packages.core.ai.tools import video_edit_tools

    project = _composition(tmp_path, duration=109.1)
    admitted_with = {}

    async def admit(context, *, session, pool=None, config_overrides=None):
        admitted_with.update(session=session, pool=pool, config=config_overrides)
        return SimpleNamespace(sandbox_id="render-1", container_config={"cpus": 8.0})

    commands = []

    class Client:
        async def start_exec(self, **kwargs):
            commands.append(kwargs["command"])
            return _finished(stdout='{"status":"ok","files":[]}')

        async def close(self):
            pass

    async def client_for(_sandbox_id, *, timeout):
        return Client()

    async def pool():
        return "render"

    async def discard(*_args, **_kwargs):
        return None

    monkeypatch.setattr(video_edit_tools, "_render_runner_pool", pool)
    monkeypatch.setattr(video_edit_tools, "_admit_video_sandbox", admit)
    monkeypatch.setattr(video_edit_tools, "_video_sandbox_client", client_for)
    monkeypatch.setattr(video_edit_tools, "_upload_sandbox_sources", discard)
    import packages.core.services.sandbox_session_service as sessions
    monkeypatch.setattr(sessions, "discard_one_shot_sandbox", discard, raising=False)

    await video_edit_tools._run_video_sandbox_once(
        project_dir=project, operation="render", context=_video_context(), render_workers=4,
    )
    assert admitted_with == {
        "session": False,
        "pool": "render",
        "config": {"memory": "10776m", "cpus": 8.0, "pids_limit": 1536, "workdir_tmpfs_size": "10776m"},
    }
    assert "--workers 4 --reviewed" in commands[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("render_pool,one_shot", [("render", True), ("default", False)])
async def test_a_review_leaves_the_authoring_session_only_for_a_render_pool(
    tmp_path, monkeypatch, render_pool, one_shot,
):
    """2026-10-19 staging: a 109 s review timed out (900 s) in the 1-CPU session."""

    from packages.core.ai.tools import video_edit_tools

    project = _composition(tmp_path, duration=109.1)
    admitted, executed, discarded = [], [], []

    async def admit(context, *, session, pool=None, config_overrides=None):
        admitted.append((session, pool, config_overrides))
        return SimpleNamespace(sandbox_id="review-1", container_config={"cpus": 8.0})

    class Client:
        async def start_exec(self, **kwargs):
            executed.append((kwargs["sandbox_id"], kwargs["command"]))
            return _finished(stdout='{"status":"ok","files":[]}')

        async def touch(self, _sandbox_id):
            pass

        async def close(self):
            pass

    async def client_for(_sandbox_id, *, timeout):
        return Client()

    async def pool():
        return render_pool

    async def upload(**_kwargs):
        return None

    async def discard(sandbox_id, *_args, **_kwargs):
        discarded.append(sandbox_id)

    monkeypatch.setattr(video_edit_tools, "_render_runner_pool", pool)
    monkeypatch.setattr(video_edit_tools, "_admit_video_sandbox", admit)
    monkeypatch.setattr(video_edit_tools, "_video_sandbox_client", client_for)
    monkeypatch.setattr(video_edit_tools, "_upload_sandbox_sources", upload)
    import packages.core.services.sandbox_session_service as sessions
    monkeypatch.setattr(sessions, "discard_one_shot_sandbox", discard, raising=False)

    await video_edit_tools._run_video_sandbox_once(
        project_dir=project, operation="review", context=_video_context(),
        sandbox_id="session-1", upload_sources=False, snapshot_times=[1.0, 5.0],
    )

    assert "review --snapshot-times 1,5" in executed[0][1]
    if one_shot:
        assert admitted == [(False, "render", {"memory": "10776m", "cpus": 8.0, "pids_limit": 1536, "workdir_tmpfs_size": "10776m"})]
        assert executed[0][0] == "review-1" and discarded == ["review-1"]
    else:
        assert admitted == [] and discarded == []
        assert executed[0][0] == "session-1"


@pytest.mark.asyncio
async def test_a_closed_session_reopens_to_the_reviewed_hash_and_still_renders(tmp_path: Path, monkeypatch, local_runner):
    """2026-10-19 eval 01M5AQ5KWVG3DFPYY6VC7NKTNF: produce reviewed and closed its
    session; render then found no session, and start + review of the untouched
    durable copy returned another hash, so the approved hash could never render.

    The durable copy after close must be exactly the reviewed source, and a
    closed session's last reviewed hash still renders from it.
    """
    from packages.core.ai.tools import video_edit_tools
    from packages.core.services import entity_fs

    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "video_runtime_under_test", REPO_ROOT / "packages/core/ai/skills/video-edit/scripts/video_runtime.py",
    )
    video_runtime = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(video_runtime)
    base = _video_runtime_exec(set(), {"probe": 0})

    def runtime(sandbox_id, command, container):
        files = container["files"]
        project = "/skill/project/"
        if command.endswith("video_runtime.py manifest"):
            # The runtime's manifest: its own source rules, over the container's files.
            listed = {
                path: content for path, content in files.items()
                if not path.startswith(project) or (
                    path.removeprefix(project) not in video_runtime.SOURCE_EXCLUDED_FILES
                    and not set(Path(path.removeprefix(project)).parts) & video_runtime.SOURCE_EXCLUDED_DIRS
                    and Path(path.removeprefix(project)).parts[:2] != ("assets", "vendor")
                )
            }
            return base(sandbox_id, command, {**container, "files": listed})
        operation = next((name for name in ("review", "render") if f"video_runtime.py {name}" in command), "")
        if not operation:
            return base(sandbox_id, command, container)
        # What a run writes into the project besides its output: the
        # engine config and vendored GSAP, and HyperFrames' own history.
        files[project + "hyperframes.json"] = files.get(project + "video.json", b"{}")
        files[project + "assets/vendor/gsap.min.js"] = b"gsap"
        files[project + ".hyperframes/history-id"] = sandbox_id.encode()
        output = "snapshots/manor-review/frame-1.png" if operation == "review" else "renders/final.mp4"
        files[project + output] = b"output"
        return SimpleNamespace(stdout=json.dumps({
            "status": "ok", "operation": operation, "review_passed": True, "files": [output],
        }), stderr="", exit_code=0)

    local_runner.exec_handler = runtime
    monkeypatch.setattr(entity_fs, "assert_entity_filesystem_ready", lambda: str(tmp_path))
    monkeypatch.setattr(entity_fs, "get_entity_root", lambda _entity_id: str(tmp_path))

    def fake_atomic_write(entity_id, rel_path, data, **kwargs):
        target = tmp_path / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return str(target)

    async def default_pool():
        return "default"

    async def register(*, abs_path, **_kwargs):
        return {"file": abs_path.name}

    async def no_probe(_path):
        return {}

    async def readable(*, raw_paths, **_kwargs):
        return [str(value) for value in raw_paths]

    monkeypatch.setattr(video_edit_tools, "_preflight_reference_paths", readable)
    monkeypatch.setattr(entity_fs, "write_entity_file_atomic", fake_atomic_write)
    monkeypatch.setattr(video_edit_tools, "_render_runner_pool", default_pool)
    monkeypatch.setattr(video_edit_tools, "_register_artifact", register)
    monkeypatch.setattr(video_edit_tools, "_probe_render", no_probe)
    monkeypatch.setattr(video_edit_tools, "_video_project_feedback", lambda _dir: {
        "missing_project_contract": [], "contract_valid": True, "contract_violations": [],
    })
    (tmp_path / "Inputs").mkdir()
    (tmp_path / "Inputs/music.mp3").write_bytes(b"music")

    async def call(**arguments):
        return json.loads(await video_edit_tools._video_edit_handler(
            entity_id="entity-1", user_id="user-1", conversation_id="conversation-reopen", **arguments,
        ))

    started = await call(operation="start", project_path="Videos/reopen", reference_paths=["Inputs/music.mp3"])
    session_id = started["session_id"]
    await call(operation="write_files", session_id=session_id, files={
        "BRIEF.md": "# Brief", "STORYBOARD.md": "# Board", "video.json": '{"fps": 30}',
        "package.json": "{}", "index.html": "<main></main>", "index.motion.json": "[]",
    })
    reviewed = await call(operation="review", session_id=session_id)
    approved = reviewed["review_sha256"]
    closed = await call(operation="close", session_id=session_id)
    assert closed["status"] == "closed", json.dumps(closed)

    # Exactly the reviewed source is durable, under the runtime's own source rules.
    project_dir = tmp_path / "Videos/reopen"
    assert video_edit_tools._project_source_sha256(project_dir) == approved
    assert video_edit_tools._HASH_EXCLUDED_DIRS == video_runtime.SOURCE_EXCLUDED_DIRS
    assert video_edit_tools._HASH_EXCLUDED_FILES == video_runtime.SOURCE_EXCLUDED_FILES
    (project_dir / "hyperframes.json").write_text('{"stale": true}')
    assert video_edit_tools._project_source_sha256(project_dir) == approved

    # Reopened from the durable copy with nothing changed: the same hash, even
    # when the step passes its inputs again -- the original reference and the
    # review frame the previous step returned.
    frame = "Videos/reopen/snapshots/manor-review/frame-1.png"
    again = await call(
        operation="start", project_path="Videos/reopen", reference_paths=["Inputs/music.mp3", frame],
    )
    assert (await call(operation="review", session_id=again["session_id"]))["review_sha256"] == approved

    # Source another tool saved straight into the durable project reaches the
    # session before its review, so the reviewed sandbox holds what is hashed.
    (project_dir / "assets/bg.png").write_bytes(b"background")
    changed = await call(operation="review", session_id=again["session_id"])
    assert changed["review_sha256"] != approved
    [live] = [container for container in local_runner.containers.values()]
    assert live["files"]["/skill/project/assets/bg.png"] == b"background"
    (project_dir / "assets/bg.png").unlink()
    await call(operation="write_files", session_id=again["session_id"], delete_paths=["assets/bg.png"])
    assert (await call(operation="review", session_id=again["session_id"]))["review_sha256"] == approved
    await call(operation="close", session_id=again["session_id"])

    # The closed session's approved hash renders from the durable copy.
    rendered = await call(operation="render", session_id=session_id, review_sha256=approved)
    assert rendered["status"] == "completed", rendered
    assert rendered["source_sha256"] == approved
