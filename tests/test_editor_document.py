"""AI edit on documents too large for the prompt."""
from __future__ import annotations

import asyncio
import importlib
import json
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from starlette.datastructures import Headers, UploadFile

from apps.api.routers.chat import _parse_editor_context_request
from packages.core.ai.runtime.editor_document import (
    EditorDocumentRoundProcessor,
    EditorDocumentSnapshotRef,
    EditorPatchError,
    apply_editor_patch_operation,
    build_editor_document_map,
    create_editor_document_snapshot,
    read_editor_snapshot,
    record_editor_snapshot_read,
)
from packages.core.ai.runtime.file_context import (
    EDITOR_CURRENT_DOCUMENT_MAX_CHARS,
    FILE_CONTEXT_METADATA_KEY,
    editor_context_without_inline_document_content,
    editor_document_snapshot_for_path,
    render_editor_current_document_user_context,
)
from packages.core.ai.runtime.prompt_guidance import runtime_file_editor_live_edit_guidance
from packages.core.ai.tools import file_tools
from packages.core.config import get_settings

loop = importlib.import_module("packages.core.ai.agentic_loop")

PATCH_CASES = json.loads(
    (Path(__file__).parent / "fixtures" / "editor_patch_cases.json").read_text(encoding="utf-8")
)
WORKFLOW = json.dumps({
    "format": "manor-workflow-v1",
    "name": "Flow",
    "steps": [
        {"id": "start", "type": "trigger", "name": "Start", "config": {"prompt": "p" * 400}, "next": ["plan"]},
        {"id": "plan", "type": "agent", "name": "Plan", "config": {"prompt": "q" * 400}, "next": []},
    ],
}, indent=2)


def _patch(*operations) -> str:
    return f"<manor-live-patch>{json.dumps(list(operations))}</manor-live-patch>"


async def _process(root, ref, text, *, finish_reason="stop", has_tool_calls=False, messages=None):
    return await EditorDocumentRoundProcessor(str(root), ref)(
        text, finish_reason=finish_reason, has_tool_calls=has_tool_calls, messages=messages or [],
    )


@pytest.mark.parametrize("case", PATCH_CASES, ids=[case["name"] for case in PATCH_CASES])
def test_patch_rules_match_the_browser_cases(case):
    if case.get("fails"):
        with pytest.raises(EditorPatchError):
            apply_editor_patch_operation(case["content"], case["op"])
    else:
        assert apply_editor_patch_operation(case["content"], case["op"])[0] == case["expected"]


def test_map_gives_read_coordinates_for_each_document_kind():
    workflow_map = build_editor_document_map(WORKFLOW, file_type="workflow Flow")
    assert workflow_map["kind"] == "json"
    plan = next(row for row in workflow_map["map"].splitlines() if "id=plan" in row)
    assert "type=agent" in plan and "name=Plan" in plan
    first_line = int(plan.split(" offset ")[1].split("-")[0])
    assert '"id": "plan"' in "".join(WORKFLOW.splitlines(keepends=True)[first_line:first_line + 3])

    sections = "".join(f"## Section {i}\n\nbody\n\n" for i in range(5))
    markdown_map = build_editor_document_map(f"# Title\n\n{sections}", file_type="md notes.md")
    assert markdown_map["kind"] == "markdown"
    assert "offset 2 ## Section 0" in markdown_map["map"]

    # One huge line is still reachable: its windows are character ranges.
    minified_map = build_editor_document_map("x" * 200_000)
    assert minified_map["kind"] == "lines" and "char_offset 0-" in minified_map["map"]


def test_map_of_many_headings_stays_small_and_spans_the_document():
    document = "# Title\n\n" + "".join(f"## Part {i}\n\n{'word ' * 200}\n\n" for i in range(1_100))
    document_map = build_editor_document_map(document, file_type="md")
    assert len(document_map["map"]) < 30_000
    assert "# Title" in document_map["map"] and "## Part 1 " in document_map["map"]
    assert int(document_map["map"].splitlines()[-1].split()[1]) > document.count("\n") * 0.9


def test_applied_patch_changes_what_later_reads_see(tmp_path):
    ref = create_editor_document_snapshot(str(tmp_path), "one\ntwo\n")
    outcome = asyncio.run(_process(tmp_path, ref, "Done." + _patch({"op": "replace", "find": "two", "replace": "2"})))
    assert read_editor_snapshot(str(tmp_path), ref) == "one\n2\n"
    assert outcome.feedback is None and not outcome.discard
    assert "two" not in outcome.context_text and "manor-live-patch" not in outcome.context_text


def test_failed_operation_goes_back_to_the_model_and_the_rest_still_apply(tmp_path):
    ref = create_editor_document_snapshot(str(tmp_path), "a a b")
    outcome = asyncio.run(_process(tmp_path, ref, _patch(
        {"op": "replace", "find": "a", "replace": "x"},
        {"op": "replace", "find": "b", "replace": "y"},
    )))
    assert read_editor_snapshot(str(tmp_path), ref) == "a a y"
    assert not outcome.discard
    failure = json.loads(outcome.feedback.split("\n", 1)[1])[0]
    assert failure["operation"] == 0 and failure["reason"] == "not_unique"
    assert failure["found_at_offsets"] == [0, 0]
    assert outcome.accepted_operations == ({"op": "replace", "find": "b", "replace": "y"},)
    assert '"find": "a"' not in outcome.reply_text


def test_edit_that_breaks_json_is_refused(tmp_path):
    ref = create_editor_document_snapshot(str(tmp_path), WORKFLOW)
    outcome = asyncio.run(_process(tmp_path, ref, _patch({"op": "replace", "find": '"steps": [', "replace": '"steps": '})))
    assert read_editor_snapshot(str(tmp_path), ref) == WORKFLOW
    assert "breaks_json" in outcome.feedback
    assert not outcome.accepted_operations


def test_replace_all_checks_the_expanded_byte_size_before_changing_the_snapshot(monkeypatch, tmp_path):
    from packages.core.ai.runtime import editor_document

    monkeypatch.setattr(editor_document, "EDITOR_DOCUMENT_UPLOAD_MAX_BYTES", 16)
    ref = create_editor_document_snapshot(str(tmp_path), "aaaaaaaa")
    outcome = asyncio.run(_process(tmp_path, ref, _patch(
        {"op": "replace", "find": "a", "replace": "汉", "all": True},
    )))
    assert read_editor_snapshot(str(tmp_path), ref) == "aaaaaaaa"
    assert "document_too_large" in outcome.feedback
    assert not outcome.accepted_operations


@pytest.mark.parametrize("text", [
    'Partly <manor-live-patch>[{"op":"replace","find":"one","rep',
    'Partly <manor-live-patch>[{"op":"replace"]</manor-live-patch>',
])
def test_cut_off_or_malformed_response_applies_nothing(tmp_path, text):
    ref = create_editor_document_snapshot(str(tmp_path), "one")
    valid = _patch({"op": "replace", "find": "one", "replace": "1"})
    outcome = asyncio.run(_process(tmp_path, ref, valid + text, finish_reason="length"))
    assert outcome.discard and outcome.feedback
    assert read_editor_snapshot(str(tmp_path), ref) == "one"


def test_repeated_failure_stops_and_tells_the_model_to_say_so(tmp_path):
    ref = create_editor_document_snapshot(str(tmp_path), "one")
    bad = _patch({"op": "replace", "find": "missing", "replace": "x"})
    outcomes = [asyncio.run(_process(tmp_path, ref, bad)) for _ in range(5)]
    assert all("did not apply" in outcome.feedback for outcome in outcomes[:3])
    assert "Tell the user" in outcomes[3].feedback
    assert outcomes[4].feedback is None


def test_used_reads_collapse_and_progress_follows_length_changes(tmp_path):
    document = "".join(f"line {i}\n" for i in range(100))
    ref = create_editor_document_snapshot(str(tmp_path), document)
    record_editor_snapshot_read(str(tmp_path), ref, document.index("line 10\n"))
    messages = [
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "read", "function": {"name": "read_file"}},
            {"id": "other", "function": {"name": "inspect_file_engine"}},
        ]},
        {"role": "tool", "tool_call_id": "read", "content": "x" * 5_000},
        {"role": "tool", "tool_call_id": "other", "content": "y" * 5_000},
    ]
    outcome = asyncio.run(_process(
        tmp_path, ref,
        _patch({"op": "replace", "find": "line 3\n", "replace": "three\nextra\n"}),
        has_tool_calls=True, messages=messages,
    ))
    assert len(messages[1]["content"]) < 200 and messages[2]["content"] == "y" * 5_000
    # Ten lines were read and one line was added before that point.
    assert "offset 11;" in outcome.feedback

    messages.append({"role": "user", "content": outcome.feedback})
    asyncio.run(_process(
        tmp_path, ref, _patch({"op": "replace", "find": "line 4\n", "replace": "four\n"}),
        has_tool_calls=True, messages=messages,
    ))
    assert not any(outcome.feedback == message["content"] for message in messages)


@pytest.fixture
def entity_root(tmp_path):
    settings = get_settings()
    old_enabled, old_root = settings.MANOR_FS_ENABLED, settings.MANOR_FS_ROOT
    settings.MANOR_FS_ENABLED = True
    settings.MANOR_FS_ROOT = str(tmp_path)
    root = tmp_path / "entity"
    root.mkdir()
    try:
        yield root
    finally:
        settings.MANOR_FS_ENABLED = old_enabled
        settings.MANOR_FS_ROOT = old_root


def _envelope(ref, *, path="docs/saved.md"):
    return SimpleNamespace(
        user_id="user", conversation_id=None, task_id=None, workspace_id=None, agent_id=None,
        metadata={FILE_CONTEXT_METADATA_KEY: [{
            "kind": "current_editor_file",
            "path": path,
            "readable": True,
            "writable": False,
            "patch_only": True,
            "metadata": {"document_snapshot": ref.to_context()},
        }]},
    )


@pytest.mark.parametrize("path", ["current_editor_file", "docs/saved.md"])
def test_file_tools_read_the_unsaved_snapshot_for_the_mounted_path(entity_root, path):
    (entity_root / "docs").mkdir()
    (entity_root / "docs" / "saved.md").write_text("old saved bytes\n", encoding="utf-8")
    document = "".join(f"unsaved {i}\r\n" for i in range(3_000))
    ref = create_editor_document_snapshot(str(entity_root), document)
    envelope = _envelope(ref)

    page = json.loads(asyncio.run(file_tools._read_file(
        "entity", path=path, offset=2_000, limit=2, _runtime_envelope_from_context=envelope,
    )))
    assert page["content"] == "unsaved 2000\r\nunsaved 2001\r\n"
    assert page["next_offset"] == 2_002 and page["total_lines"] == 3_000

    whole = json.loads(asyncio.run(file_tools._read_file(
        "entity", path=path, _runtime_envelope_from_context=envelope,
    )))
    assert 8_000 < len(whole["content"]) <= 9_000 and whole["next_offset"]

    found = json.loads(asyncio.run(file_tools._grep_files(
        "entity", path=path, pattern=r"unsaved 299\d", max_matches=2, _runtime_envelope_from_context=envelope,
    )))
    assert found["total_matches"] == 10 and found["matches"][0]["offset"] == 2_990
    assert found["next_match_offset"] == 2


def test_other_paths_and_turns_without_a_snapshot_use_the_saved_file(entity_root):
    ref = create_editor_document_snapshot(str(entity_root), "unsaved\n")
    assert editor_document_snapshot_for_path(_envelope(ref), "current_editor_file") == ref
    assert editor_document_snapshot_for_path(_envelope(ref), "other.md") is None
    assert editor_document_snapshot_for_path(None, "current_editor_file") is None
    plain = SimpleNamespace(metadata={FILE_CONTEXT_METADATA_KEY: [{
        "kind": "current_editor_file", "path": "docs/saved.md", "metadata": {},
    }]})
    assert editor_document_snapshot_for_path(plain, "docs/saved.md") is None
    # A forged reference cannot point reads at another file.
    forged = SimpleNamespace(metadata={FILE_CONTEXT_METADATA_KEY: [{
        "kind": "current_editor_file", "path": "docs/saved.md",
        "metadata": {"document_snapshot": {"path": "../secrets.txt"}},
    }]})
    assert editor_document_snapshot_for_path(forged, "docs/saved.md") is None


def test_snapshot_grep_returns_bounded_previews_for_long_lines(entity_root):
    ref = create_editor_document_snapshot(str(entity_root), ("x" * 20_000 + "\n") * 40)
    found = json.loads(asyncio.run(file_tools._grep_files(
        "entity", path="current_editor_file", pattern=r".+", max_matches=100,
        _runtime_envelope_from_context=_envelope(ref),
    )))
    assert found["total_matches"] == 40
    assert found["next_match_offset"] == len(found["matches"])
    assert sum(len(match["text"]) for match in found["matches"]) <= 9_000


def test_upload_becomes_a_snapshot_and_its_text_stays_out_of_the_context(entity_root):
    document = "# Title\n\n## Secret heading\n\n" + "正文。" * 400_000
    upload = UploadFile(
        BytesIO(document.encode("utf-8")), headers=Headers({"content-type": "text/plain"}),
    )
    context = asyncio.run(_parse_editor_context_request(
        json.dumps({"documentName": "book.md", "path": "docs/book.md", "currentDocumentContent": "stale"}),
        entity_id="entity", document=upload,
    ))
    snapshot = context["document_snapshot"]
    assert (entity_root / snapshot["path"]).read_text(encoding="utf-8") == document
    assert snapshot["chars"] == len(document)
    assert "current_document_content" not in context

    prompt_text = render_editor_current_document_user_context(context)
    assert "正文" not in prompt_text and "Secret heading" in prompt_text

    trace_safe = json.dumps(editor_context_without_inline_document_content(context), ensure_ascii=False)
    assert "Secret heading" not in trace_safe and "正文" not in trace_safe


def test_field_over_the_cap_is_routed_like_an_upload_and_small_ones_stay_inline(entity_root):
    big = "x\n" * (EDITOR_CURRENT_DOCUMENT_MAX_CHARS // 2 + 1)
    routed = asyncio.run(_parse_editor_context_request(
        {"currentDocumentContent": big}, entity_id="entity",
    ))
    assert routed["document_snapshot"]["chars"] == len(big)

    small = asyncio.run(_parse_editor_context_request(
        {"currentDocumentContent": "small"}, entity_id="entity",
    ))
    assert small["current_document_content"] == "small" and "document_snapshot" not in small


def test_small_document_guidance_is_unchanged_by_the_large_document_protocol():
    small = runtime_file_editor_live_edit_guidance({"documentName": "a.md"})
    large = runtime_file_editor_live_edit_guidance({
        "documentName": "a.md", "document_snapshot": {"path": "x", "chars": 1},
    })
    assert "current_editor_file" not in small and "replace_range" not in small
    assert "current_editor_file" in large and "replace_range" in large


def _editor_envelope(editor_context):
    from packages.core.ai.runtime.entrypoints import runtime_request_for_surface_turn
    from packages.core.ai.runtime.resolver import RuntimeResolver
    from packages.core.ai.runtime.surfaces import ChatSurface

    schemas = [
        {"type": "function", "function": {"name": name}} for name in ("read_file", "grep_files")
    ]
    return RuntimeResolver().resolve_tool_surface(
        runtime_request_for_surface_turn(
            surface=ChatSurface.FILE_EDITOR_CHAT, entity_id="entity", user_id="user",
            editor_context=editor_context,
        ),
        tool_profile="file_editor",
        tool_schemas=schemas,
        allowed_tool_names={"read_file", "grep_files"},
    ).envelope


@pytest.mark.parametrize(("path", "reaches_snapshot"), [
    ("current_editor_file", True),
    ("docs/saved.md", True),
    ("other.md", False),
])
def test_reads_of_the_mounted_document_reach_the_snapshot_and_others_stay_blocked(path, reaches_snapshot):
    # The envelope tells the file tools about the snapshot. A read outside the
    # mounted document stays blocked by the file-editor surface and learns nothing.
    from packages.core.ai.runtime.approvals import RuntimeApprovalDecision, RuntimeApprovalMiddleware
    from packages.core.ai.runtime.tool_execution import runtime_prepare_tool_execution

    ref = EditorDocumentSnapshotRef(
        path=f"uploads/chat/.editor-snapshots/{'0' * 32}.txt", chars=1, lines=1, is_json=False,
    )
    envelope = _editor_envelope({"path": "docs/saved.md", "document_snapshot": ref.to_context()})
    async def allow(request):
        return RuntimeApprovalDecision.allow(RuntimeApprovalMiddleware().classify_request(request))

    with patch(
        "packages.core.ai.runtime.tool_execution.RuntimeApprovalMiddleware.guard_request", side_effect=allow,
    ):
        prepared = asyncio.run(runtime_prepare_tool_execution(
            tool_name="read_file", arguments={"path": path}, entity_id="entity", user_id="user",
            allowed_tool_names={"read_file", "grep_files"}, runtime_envelope=envelope,
        ))
    assert prepared.blocked is not reaches_snapshot
    assert ("_runtime_envelope_from_context" in prepared.arguments) is reaches_snapshot


def test_harness_hands_the_round_processor_to_the_loop():
    from packages.core.ai.runtime.harness import runtime_execute_agentic_loop

    seen = {}
    processor = object()

    async def fake_agentic_loop(**kwargs):
        seen["processor"] = kwargs.get("round_text_processor")
        return SimpleNamespace(content="ok", usage={}, rounds=1, tool_calls_made=[])

    with (
        patch("packages.core.ai.agentic_loop.agentic_loop", new=fake_agentic_loop),
        patch("packages.core.ai.llm_client.ensure_billing_context", new=AsyncMock()),
    ):
        asyncio.run(runtime_execute_agentic_loop(
            runtime_envelope=None, system_prompt="system", user_message="user", tools=[],
            entity_id="entity", agent_id=None, user_id="user", round_text_processor=processor,
        ))
    assert seen["processor"] is processor


def _run_loop(monkeypatch, responses, root, ref):
    sent: list[list[dict]] = []
    events: list[tuple[str, dict]] = []

    async def complete(messages, tools, **kwargs):
        sent.append([dict(message) for message in messages])
        return responses[len(sent) - 1]

    async def execute(name, args):
        return "x" * 5_000

    async def stream_handler(event, payload):
        events.append((event, payload))

    monkeypatch.setattr(loop, "runtime_execute_agentic_round_tool_completion", complete)
    result = asyncio.run(loop.agentic_loop(
        system_prompt="system", user_message="go",
        tools=[{"type": "function", "function": {"name": "read_file", "parameters": {"type": "object"}}}],
        tool_executor=execute, max_rounds=6, stream_handler=stream_handler,
        round_text_processor=EditorDocumentRoundProcessor(str(root), ref),
    ))
    return result, sent, events


def test_loop_sends_a_failed_patch_back_without_ending_the_turn(monkeypatch, tmp_path):
    ref = create_editor_document_snapshot(str(tmp_path), "one two")
    result, sent, events = _run_loop(monkeypatch, [
        (_patch({"op": "replace", "find": "missing", "replace": "x"}), [], {"total": 1}),
        ("Done." + _patch({"op": "replace", "find": "one", "replace": "1"}), [], {"total": 1}),
    ], tmp_path, ref)
    assert read_editor_snapshot(str(tmp_path), ref) == "1 two"
    assert result.content.startswith("Done.") and result.stop_reason == "completed"
    assert "did not apply" in sent[1][-1]["content"]
    assert "manor-live-patch" not in sent[1][-2]["content"]
    assert [payload["operations"] for event, payload in events if event == "text_commit"] == [
        [],
        [{"op": "replace", "find": "one", "replace": "1"}],
    ]


def test_loop_keeps_patch_rounds_out_of_later_requests(monkeypatch, tmp_path):
    document = "".join(f"line {i}\n" for i in range(2_000))
    ref = create_editor_document_snapshot(str(tmp_path), document)
    read = {"id": "c1", "name": "read_file", "arguments": {"path": "current_editor_file"}}
    read_next = {"id": "c2", "name": "read_file", "arguments": {"path": "current_editor_file", "offset": 5}}
    result, sent, events = _run_loop(monkeypatch, [
        ("", [read], {"total": 1}),
        (_patch({"op": "replace", "find": "line 1\n", "replace": "LINE ONE\n"}), [read_next], {"total": 1}),
        ("Finished.", [], {"total": 1}),
    ], tmp_path, ref)
    assert result.content == "Finished."
    last_request = sent[-1]
    assert "LINE ONE" not in json.dumps(last_request)
    first_read = next(m for m in last_request if m.get("tool_call_id") == "c1")
    second_read = next(m for m in last_request if m.get("tool_call_id") == "c2")
    assert len(first_read["content"]) < 200 and len(second_read["content"]) > 1_500
    assert ("text_commit", {"operations": [{"op": "replace", "find": "line 1\n", "replace": "LINE ONE\n"}]}) in events


def test_a_multi_section_pass_is_not_ended_by_a_response_that_forgets_to_read(monkeypatch, tmp_path):
    document = "".join(f"line {i}\n" for i in range(3_000))
    ref = create_editor_document_snapshot(str(tmp_path), document)
    read = {"id": "c1", "name": "read_file", "arguments": {"path": "current_editor_file"}}
    record_editor_snapshot_read(str(tmp_path), ref, 100)
    result, sent, _ = _run_loop(monkeypatch, [
        (_patch({"op": "replace", "find": "line 1\n", "replace": "ONE\n"}), [read], {"total": 1}),
        (_patch({"op": "replace", "find": "line 2\n", "replace": "TWO\n"}), [], {"total": 1}),
        ("That was the whole request.", [], {"total": 1}),
    ], tmp_path, ref)
    assert "Continue with read_file" in sent[2][-1]["content"]
    assert result.content == "That was the whole request."


def test_a_single_patch_ends_the_turn_without_a_question(monkeypatch, tmp_path):
    document = "".join(f"line {i}\n" for i in range(3_000))
    ref = create_editor_document_snapshot(str(tmp_path), document)
    record_editor_snapshot_read(str(tmp_path), ref, 100)
    _, sent, _ = _run_loop(monkeypatch, [
        ("Done." + _patch({"op": "replace", "find": "line 1\n", "replace": "ONE\n"}), [], {"total": 1}),
    ], tmp_path, ref)
    assert len(sent) == 1


def test_loop_withdraws_a_response_that_was_cut_off(monkeypatch, tmp_path):
    ref = create_editor_document_snapshot(str(tmp_path), "one")
    result, sent, events = _run_loop(monkeypatch, [
        ('Working <manor-live-patch>[{"op":"replace"', [], {"total": 1, "finish_reason": "length"}),
        ("Fixed." + _patch({"op": "replace", "find": "one", "replace": "1"}), [], {"total": 1}),
    ], tmp_path, ref)
    assert read_editor_snapshot(str(tmp_path), ref) == "1"
    assert ("text_reset", {"discard_candidate": True}) in events
    assert "cut off" in sent[1][-1]["content"]


def test_ai_edit_opens_every_workflow_manor_ships(entity_root):
    """Each built-in workflow, indented as the editor sends it, reaches the model."""
    workflows = [
        workflow
        for config_path in Path("packages/core/blueprints/configs").rglob("*.json")
        for workflow in json.loads(config_path.read_text()).get("recipe", {}).get("workflows", [])
    ]
    assert workflows
    # The snapshot path must run even where only small workflows ship (OSS
    # excludes the largest Cloud product configs), so add an oversized one.
    workflows.append({
        "slug": "oversized-snapshot-guard",
        "steps": [{"text": "x" * (EDITOR_CURRENT_DOCUMENT_MAX_CHARS + 1)}],
    })

    snapshots = 0
    for workflow in workflows:
        slug = workflow.get("slug")
        content = json.dumps(workflow, ensure_ascii=False, indent=2) + "\n"
        context = asyncio.run(_parse_editor_context_request(
            {"currentDocumentContent": content, "documentName": f"{slug}.json"}, entity_id="entity",
        ))
        if len(content) <= EDITOR_CURRENT_DOCUMENT_MAX_CHARS:
            assert context["current_document_content"] == content, slug
            continue
        snapshots += 1
        snapshot = context["document_snapshot"]
        assert (entity_root / snapshot["path"]).read_text(encoding="utf-8") == content, slug
        assert context["document_map"], slug
    # The largest ones are what this guards: they do not fit in the prompt.
    assert snapshots
