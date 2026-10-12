import json

import pytest


async def _initialize_provider_approval_owner(db, *, entity_id, user_id):
    """Provider consumers use the same persisted account/personal policy as Chat."""
    from sqlalchemy import select
    from packages.core.governance.approval_policy_service import (
        account_defaults, initialize_personal_approval,
    )
    from packages.core.models.user import Entity, User, UserMembership

    if await db.get(Entity, entity_id) is None:
        db.add(Entity(id=entity_id, name="Provider approval test account",
            settings={"approval_defaults": account_defaults()}))
    if await db.get(User, user_id) is None:
        db.add(User(id=user_id, entity_id=entity_id,
            email=f"{user_id}@example.com", password_hash="test-only", role="owner"))
    membership = (await db.execute(select(UserMembership).where(
        UserMembership.user_id == user_id, UserMembership.entity_id == entity_id,
    ))).scalar_one_or_none()
    if membership is None:
        db.add(UserMembership(user_id=user_id, entity_id=entity_id,
            role="owner", status="active"))
    await db.flush()
    await initialize_personal_approval(db, user_id=user_id, entity_id=entity_id)


def _chrome_approval_request(*, approval_id: str = "chrome-approval-1") -> dict:
    from packages.core.ai.runtime.provider_approvals import (
        normalize_provider_approval,
    )

    arguments = {
        "tabId": 123,
        "ref": "e7",
        "snapshot_id": "snap-1",
        "label": "Publish",
        "role": "button",
        "url": "https://www.linkedin.com/feed/",
        "input": "x" * 300,
        "files": [f"asset-{index}.png" for index in range(12)],
    }
    result = json.dumps(
        {
            "ok": False,
            "status": "approval_required",
            "approval_required": True,
            "provider": "chrome",
            "action_key": "click_element",
            "approvalId": approval_id,
            "expires_at": "2099-07-19T12:00:00Z",
            "reason": "side_effect_action_requires_confirmation",
            "confirmation_mode": "always_action_time",
            "policy_category": "representational_communication",
            "preapproved": False,
            "target_label": "Publish",
            "target_role": "button",
            "url": "https://www.linkedin.com/feed/",
            "data_summary": "Publish the prepared post",
            "retry_action": {
                "name": "mcp__chrome__click_element",
                "arguments": arguments,
            },
        }
    )
    request = normalize_provider_approval(
        "mcp__chrome__click_element",
        arguments,
        result,
    )
    assert request is not None
    return request


def _chrome_upload_approval_request(
    *, approval_id: str = "chrome-upload-approval-1",
) -> dict:
    from packages.core.ai.runtime.provider_approvals import (
        normalize_provider_approval,
    )

    arguments = {
        "tabId": 321,
        "selector": 'input[name="Filedata"]',
        "snapshot_id": "snap-upload-1",
        "files": ["/tmp/final.mp4"],
    }
    result = json.dumps(
        {
            "ok": False,
            "status": "approval_required",
            "approval_required": True,
            "provider": "chrome",
            "action_key": "upload",
            "approvalId": approval_id,
            "expires_at": "2099-07-19T12:00:00Z",
            "reason": "file_upload_requires_confirmation",
            "confirmation_mode": "preapproval_allowed",
            "policy_category": "file_upload",
            "preapproved": False,
            "target_label": "Select files",
            "target_role": "file input",
            "url": "https://studio.youtube.com/channel/example",
            "data_summary": "Upload final.mp4",
            "retry_action": {
                "name": "mcp__chrome__upload",
                "arguments": arguments,
            },
        }
    )
    request = normalize_provider_approval(
        "mcp__chrome__upload",
        arguments,
        result,
    )
    assert request is not None
    return request


def test_chrome_provider_approval_adapter_preserves_exact_retry_arguments() -> None:
    request = _chrome_approval_request()

    assert request["provider"] == "chrome"
    assert request["provider_approval_id"] == "chrome-approval-1"
    assert request["confirmation_tool"] == "mcp__chrome__confirm_action"
    assert request["confirmation_arguments"] == {
        "approvalId": "chrome-approval-1",
        "confirmation_mode": "always_action_time",
        "policy_category": "representational_communication",
        "preapproved": False,
    }
    assert request["confirmation_mode"] == "always_action_time"
    assert request["policy_category"] == "representational_communication"
    assert request["preapproved"] is False
    assert request["retry_tool"] == "mcp__chrome__click_element"
    assert request["retry_arguments"]["input"] == "x" * 300
    assert request["retry_arguments"]["files"] == [
        f"asset-{index}.png" for index in range(12)
    ]


def test_chrome_provider_approval_ignores_provider_retry_target_rewrite() -> None:
    from packages.core.ai.runtime.provider_approvals import normalize_provider_approval

    original_arguments = {
        "groupId": "youtube-upload",
        "tabId": 321,
        "role": "file",
        "files": ["/tmp/final.mp4"],
    }
    request = normalize_provider_approval(
        "mcp__chrome__upload",
        original_arguments,
        {
            "status": "approval_required",
            "approval_required": True,
            "provider": "chrome",
            "approvalId": "chrome-upload-target-rewrite",
            "snapshot_id": "provider-snapshot",
            "ref": "provider-file-input",
            "retry_action": {
                "name": "mcp__chrome__upload",
                "arguments": {
                    **original_arguments,
                    "role": "file input",
                    "snapshot_id": "provider-snapshot",
                    "ref": "provider-file-input",
                },
            },
        },
    )

    assert request is not None
    assert request["retry_arguments"] == original_arguments


def test_sequence_provider_approval_requires_runtime_owned_child_identity() -> None:
    from packages.core.ai.runtime.control import runtime_owned_sequence_child_result
    from packages.core.ai.runtime.human_gate import runtime_detect_blocking_human_gate
    from packages.core.ai.runtime.provider_approvals import normalize_provider_approval

    child_arguments = {"tabId": 123, "ref": "e7", "snapshot_id": "snap-1"}
    result = json.dumps({
        "ok": False,
        "status": "approval_required",
        "approval_required": True,
        "provider": "chrome",
        "approvalId": "chrome-sequence-approval",
        "__manor_runtime_child_owner": {
            "version": 1,
            "tool_name": "mcp__chrome__click_element",
            "arguments": child_arguments,
        },
        "retry_action": {
            "name": "mcp__chrome__click_element",
            "arguments": child_arguments,
        },
    })

    assert normalize_provider_approval(
        "mcp__chrome__run_sequence",
        {"steps": []},
        result,
    ) is None
    assert runtime_detect_blocking_human_gate(
        "mcp__chrome__run_sequence",
        {"steps": []},
        result,
    ) is None

    owned_result = runtime_owned_sequence_child_result(
        tool_name="mcp__chrome__click_element",
        arguments=child_arguments,
        result=result,
        public_result=json.dumps({
            "ok": False,
            "status": "approval_required",
            "approval_required": True,
            "provider": "chrome",
            "approvalId": "chrome-sequence-approval",
        }),
    )
    request = normalize_provider_approval(
        "mcp__chrome__run_sequence",
        {"steps": []},
        owned_result,
    )

    assert request is not None
    assert request["retry_tool"] == "mcp__chrome__click_element"
    assert request["retry_arguments"] == child_arguments


@pytest.mark.asyncio
async def test_sequence_provider_gate_streams_only_bounded_public_json() -> None:
    from packages.core.ai.runtime.chrome_sequence import (
        CHROME_RUN_SEQUENCE_MAX_RESULT_CHARS,
        runtime_execute_chrome_sequence,
    )
    from packages.core.ai.runtime.streams import (
        RuntimeToolStreamSink,
        runtime_tool_result_for_chat,
    )

    class Queue:
        def __init__(self) -> None:
            self.items: list[dict] = []

        def put_nowait(self, item: dict) -> None:
            self.items.append(item)

    private_input = "private-stream-input-" + ("x" * 7_000)
    child_arguments = {
        "tabId": 123,
        "target": {"role": "textbox", "name": "Draft", "exact": True},
        "value": private_input,
    }
    raw_result = json.dumps({
        "ok": False,
        "status": "approval_required",
        "approval_required": True,
        "provider": "chrome",
        "approvalId": "chrome-sequence-stream",
        "data_summary": "Approve editing the draft",
        "provider_debug": "d" * 20_000,
        "retry_action": {
            "name": "mcp__chrome__fill_or_select",
            "arguments": {
                **child_arguments,
                "_runtime_tool_call_id_from_context": "private-runtime-id",
            },
        },
    })

    async def execute_child(_name: str, _arguments: dict):
        return raw_result

    owned_result = await runtime_execute_chrome_sequence(
        {
            "steps": [{
                "id": "fill-draft",
                "tool": "fill_or_select",
                "arguments": child_arguments,
            }],
        },
        execute_child=execute_child,
    )
    queue = Queue()
    recorded: list[dict] = []
    sink = RuntimeToolStreamSink(
        event_queue=queue,
        record_tool_event=lambda _event_type, payload: recorded.append(payload),
        format_event=lambda event_type, payload: {
            "event": event_type,
            "payload": payload,
        },
        format_tool_result=runtime_tool_result_for_chat,
    )

    sink.emit_tool_end(
        "mcp__chrome__run_sequence",
        owned_result,
        args={"steps": []},
    )

    streamed = queue.items[0]["payload"]["tool_call"]["result"]
    persisted = recorded[0]["tool_call"]["raw_result"]
    request = recorded[0]["tool_call"]["provider_approval"]
    assert request["retry_arguments"] == child_arguments
    for public_result in (str(owned_result), streamed, persisted):
        assert len(public_result) <= CHROME_RUN_SEQUENCE_MAX_RESULT_CHARS
        json.loads(public_result)
        assert "__manor_runtime_child_owner" not in public_result
        assert "retry_action" not in public_result
        assert private_input not in public_result
        assert "private-runtime-id" not in public_result


def test_provider_approval_adapter_rejects_oversized_continuation() -> None:
    from packages.core.ai.runtime.provider_approvals import normalize_provider_approval

    request = normalize_provider_approval(
        "mcp__chrome__click_element",
        {"tabId": 123, "ref": "e7"},
        {
            "status": "approval_required",
            "approval_required": True,
            "provider": "chrome",
            "approvalId": "chrome-oversized",
            "data_summary": "界" * 30_000,
            "retry_action": {
                "name": "mcp__chrome__click_element",
                "arguments": {"tabId": 123, "ref": "e7"},
            },
        },
    )

    assert request is None


def test_provider_approval_adapter_rejects_missing_retry_arguments() -> None:
    from packages.core.ai.runtime.provider_approvals import normalize_provider_approval

    request = normalize_provider_approval(
        "mcp__chrome__click_element",
        None,
        {
            "status": "approval_required",
            "approval_required": True,
            "provider": "chrome",
            "approvalId": "chrome-missing-retry",
        },
    )

    assert request is None


def test_provider_confirmation_token_stays_out_of_public_tool_history() -> None:
    from packages.core.ai.runtime.provider_approvals import (
        normalize_provider_approval_resolution,
        provider_approval_confirmation_receipt,
    )

    result = {
        "ok": True,
        "status": "approved",
        "approvalId": "chrome-private-receipt",
        "approvalToken": "single-use-secret",
    }
    receipt = provider_approval_confirmation_receipt(
        "mcp__chrome__confirm_action",
        {"approvalId": "chrome-private-receipt"},
        result,
    )
    public = normalize_provider_approval_resolution(
        "mcp__chrome__confirm_action",
        {"approvalId": "chrome-private-receipt"},
        result,
    )

    assert receipt == {
        "provider": "chrome",
        "provider_approval_id": "chrome-private-receipt",
        "approval_token": "single-use-secret",
    }
    assert public == {
        "provider": "chrome",
        "provider_approval_id": "chrome-private-receipt",
    }
    assert "single-use-secret" not in json.dumps(public)


def test_provider_continuation_retries_chrome_with_standard_token_only() -> None:
    """Retry the exact approved Chrome action with only its provider token."""
    from packages.core.ai.agentic_loop import _retry_call_after_tool_continuation
    from packages.core.ai.runtime.provider_approvals import provider_approval_runtime_metadata

    request = _chrome_approval_request()
    metadata = provider_approval_runtime_metadata(
        {
            "kind": "provider",
            "provider": "chrome",
            "provider_approval_id": request["provider_approval_id"],
            "tool": request["retry_tool"],
            "continuation": request,
        }
    )
    continuation = metadata["forced_tool_calls"][0]["arguments"][
        "__manor_tool_continuation"
    ]

    retry = _retry_call_after_tool_continuation(
        {
            "ok": True,
            "status": "approved",
            "approvalToken": "fresh-token",
        },
        continuation,
        {"mcp__chrome__click_element"},
    )

    assert retry["arguments"]["approvalToken"] == "fresh-token"
    assert "approval_token" not in retry["arguments"]
    assert retry["arguments"]["ref"] == request["retry_arguments"]["ref"]


def _provider_runtime_metadata(request: dict) -> dict:
    from packages.core.ai.runtime.provider_approvals import (
        provider_approval_runtime_metadata,
    )

    metadata = provider_approval_runtime_metadata(
        {
            "kind": "provider",
            "provider": request["provider"],
            "provider_approval_id": request["provider_approval_id"],
            "tool": request["retry_tool"],
            "continuation": request,
        }
    )
    assert metadata is not None
    return metadata


_WIZARD_FOLLOWUP_TOOL_NAMES = [
    "mcp__chrome__wait",
    "mcp__chrome__read_page",
    "mcp__chrome__fill_or_select",
    "mcp__chrome__target_state",
    "mcp__chrome__run_sequence",
    "mcp__chrome__click_element",
    "mcp__chrome__resolve_target",
    "mcp__chrome__screenshot",
]


def test_intermediate_upload_approval_allowlists_followup_tools() -> None:
    request = _chrome_upload_approval_request()
    metadata = _provider_runtime_metadata(request)

    assert metadata["forced_tool_calls"][0]["disable_followup_tools"] is False
    assert metadata["followup_tool_names"] == _WIZARD_FOLLOWUP_TOOL_NAMES
    assert metadata["forced_tool_calls"][0]["followup_tool_names"] == (
        _WIZARD_FOLLOWUP_TOOL_NAMES
    )
    assert "mcp__chrome__upload" not in metadata["followup_tool_names"]
    assert "mcp__chrome__computer" not in metadata["followup_tool_names"]
    assert "mcp__chrome__inject_script" not in metadata["followup_tool_names"]
    extra_tool_names = metadata["extra_tool_names"]
    assert "mcp__chrome__confirm_action" in extra_tool_names
    assert "mcp__chrome__upload" in extra_tool_names
    assert "mcp__chrome__read_page" in extra_tool_names
    assert "mcp__chrome__wait" in extra_tool_names
    assert "mcp__chrome__fill_or_select" in extra_tool_names
    assert "mcp__chrome__click_element" in extra_tool_names
    assert "mcp__chrome__computer" not in extra_tool_names
    guidance = metadata["approval_resume_guidance"]
    assert "files_selected result is not publication" in guidance
    assert "same Chrome session" in guidance
    assert "remaining authorized page actions the current task" in guidance
    assert "Do not click Public, Unlisted, or Schedule in this resume turn" in guidance

    youtube_start = {
        **request,
        "policy_category": "youtube_upload_start",
        "confirmation_arguments": {
            **request["confirmation_arguments"],
            "policy_category": "youtube_upload_start",
        },
    }
    youtube_metadata = _provider_runtime_metadata(youtube_start)
    assert youtube_metadata["forced_tool_calls"][0]["disable_followup_tools"] is False
    assert youtube_metadata["followup_tool_names"] == _WIZARD_FOLLOWUP_TOOL_NAMES
    assert youtube_metadata["approval_resume_guidance"] == guidance

    stepped = {
        **request,
        "retry_arguments": {
            **request["retry_arguments"],
            "_workflow_step_id_from_context": "upload_youtube_video",
        },
    }
    stepped_metadata = _provider_runtime_metadata(stepped)
    assert stepped_metadata["forced_tool_calls"][0]["disable_followup_tools"] is False
    assert stepped_metadata["followup_tool_names"] == _WIZARD_FOLLOWUP_TOOL_NAMES
    assert stepped_metadata["approval_resume_guidance"] == guidance


@pytest.mark.asyncio
@pytest.mark.parametrize("confirmation_approved,retry_executed", [
    (False, False), (True, False), (True, "unknown"), (True, True),
])
async def test_legacy_upload_continuation_opens_wizard_only_after_retry_succeeds(monkeypatch, confirmation_approved, retry_executed):
    from packages.core.ai import agentic_loop as loop_module

    # Guidance admission is tested separately; this case owns the provider
    # confirmation, retry, and wizard follow-up sequence.
    monkeypatch.setattr(
        "packages.core.ai.runtime.tool_guidance.runtime_tool_guidance",
        lambda _name: None,
    )
    metadata = _provider_runtime_metadata(_chrome_upload_approval_request())
    available = sorted(set(metadata["extra_tool_names"]) | {"mcp__chrome__run_sequence"})
    schemas = [{"type": "function", "function": {
        "name": name, "parameters": {"type": "object", "additionalProperties": True},
    }} for name in available]
    called = []
    llm_tool_sets = []
    llm_transcripts = []

    async def execute(name, _arguments):
        called.append(name)
        if name == "mcp__chrome__confirm_action":
            return json.dumps({
                "ok": confirmation_approved,
                "status": "approved" if confirmation_approved else "connection_unavailable",
                "approvalToken": "private-token" if confirmation_approved else None,
                "action_executed": False,
            })
        assert name == "mcp__chrome__upload"
        return json.dumps({
            "ok": retry_executed is True,
            "status": "uploaded" if retry_executed is True else "uncertain" if retry_executed == "unknown" else "selector_not_found",
            "action_executed": retry_executed,
            "do_not_replay": retry_executed == "unknown",
        })

    async def model_with_tools(messages, tools, **_kwargs):
        llm_tool_sets.append({item["function"]["name"] for item in tools})
        llm_transcripts.append(json.dumps(messages))
        return "<manor-final-response>Upload did not complete.</manor-final-response>", None, {}

    async def model_without_tools(*_args, **_kwargs):
        raise AssertionError("a failed continuation must not force a tool-free summary")

    monkeypatch.setattr(loop_module, "runtime_execute_agentic_round_tool_completion", model_with_tools)
    monkeypatch.setattr(loop_module, "runtime_execute_agentic_round_text_completion", model_without_tools)
    result = await loop_module.agentic_loop(
        system_prompt="Continue the upload safely.",
        user_message="Proceed.",
        tools=schemas,
        tool_executor=execute,
        forced_tool_calls=metadata["forced_tool_calls"],
        max_rounds=4,
    )

    assert called == (["mcp__chrome__confirm_action", "mcp__chrome__upload"]
                      if confirmation_approved else ["mcp__chrome__confirm_action"]), (
        result.stop_reason,
        result.control.get("error_reason") if isinstance(result.control, dict) else None,
    )
    assert result.stop_reason == "completed" and result.error is None
    assert len(llm_tool_sets) == 1
    assert "private-token" not in llm_transcripts[0]
    if confirmation_approved and retry_executed is True:
        # Only a successful retry opens the wizard, bounded to its allowlist.
        assert llm_tool_sets[0] == set(_WIZARD_FOLLOWUP_TOOL_NAMES)
    else:
        # A failed or uncertain continuation is an observation for the model:
        # it keeps its ordinary tools (not the wizard allowlist), the spent
        # approval is never replayed, and any new upload needs fresh approval.
        assert llm_tool_sets[0] == set(available)
        expected_status = (
            "connection_unavailable" if not confirmation_approved
            else "uncertain" if retry_executed == "unknown" else "selector_not_found"
        )
        assert expected_status in llm_transcripts[0]


def test_restrict_provider_followup_tools_filters_to_allowlist() -> None:
    from packages.core.ai.runtime.provider_approvals import (
        restrict_provider_followup_tools,
    )

    tools = [
        {"function": {"name": "mcp__chrome__wait"}},
        {"function": {"name": "mcp__chrome__upload"}},
        {"function": {"name": "mcp__chrome__computer"}},
        {"function": {"name": "mcp__chrome__fill_or_select"}},
        {"name": "mcp__chrome__read_page"},
    ]
    loaded = {
        "mcp__chrome__wait",
        "mcp__chrome__upload",
        "mcp__chrome__computer",
        "mcp__chrome__fill_or_select",
        "mcp__chrome__read_page",
    }
    filtered, filtered_names = restrict_provider_followup_tools(
        tools,
        loaded,
        followup_tool_names=_WIZARD_FOLLOWUP_TOOL_NAMES,
    )
    assert [item.get("function", item).get("name") for item in filtered] == [
        "mcp__chrome__wait",
        "mcp__chrome__fill_or_select",
        "mcp__chrome__read_page",
    ]
    assert filtered_names == {
        "mcp__chrome__wait",
        "mcp__chrome__fill_or_select",
        "mcp__chrome__read_page",
    }
    empty_tools, empty_names = restrict_provider_followup_tools(
        tools,
        loaded,
        disable_followup_tools=True,
        followup_tool_names=_WIZARD_FOLLOWUP_TOOL_NAMES,
    )
    assert empty_tools == []
    assert empty_names == set()


def test_missing_policy_category_still_disables_followup_tools() -> None:
    request = _chrome_upload_approval_request()
    confirmation_arguments = {
        key: value
        for key, value in request["confirmation_arguments"].items()
        if key != "policy_category"
    }
    missing = {
        **request,
        "confirmation_arguments": confirmation_arguments,
    }
    missing.pop("policy_category", None)
    metadata = _provider_runtime_metadata(missing)

    assert metadata["forced_tool_calls"][0]["disable_followup_tools"] is True
    assert "followup_tool_names" not in metadata
    assert metadata["approval_resume_guidance"] == (
        "Resume the approved provider action using the supplied forced "
        "tool continuation. Do not rediscover or alter the approved action."
    )


def test_terminal_publish_approval_still_disables_followup_tools() -> None:
    request = _chrome_approval_request()
    metadata = _provider_runtime_metadata(request)

    assert metadata["forced_tool_calls"][0]["disable_followup_tools"] is True
    assert "followup_tool_names" not in metadata
    assert "followup_tool_names" not in metadata["forced_tool_calls"][0]
    assert metadata["approval_resume_guidance"] == (
        "Resume the approved provider action using the supplied forced "
        "tool continuation. Do not rediscover or alter the approved action."
    )




async def _completed_text_response():
    return "<manor-final-response>Upload completed.", {}


def test_provider_continuation_restores_chrome_confirmation_receipt() -> None:
    """A resumed Chat turn must restore the receipt required by confirm_action."""
    from packages.core.ai.runtime.chrome_routing import (
        runtime_blocked_chrome_workflow_contract,
    )
    from packages.core.ai.runtime.provider_approvals import (
        provider_approval_runtime_metadata,
    )

    request = _chrome_approval_request()
    metadata = provider_approval_runtime_metadata(
        {
            "kind": "provider",
            "provider": "chrome",
            "provider_approval_id": request["provider_approval_id"],
            "tool": request["retry_tool"],
            "continuation": request,
        }
    )

    assert metadata is not None
    receipt = metadata["chrome_runtime_contract_v1"]["pending_chrome_confirmation"]
    assert receipt["approval_id"] == request["provider_approval_id"]
    assert receipt["tool_name"] == request["retry_tool"]
    assert receipt["action_signature"]
    blocked = runtime_blocked_chrome_workflow_contract(
        tool_name="mcp__chrome__confirm_action",
        arguments={"approvalId": request["provider_approval_id"]},
        runtime_metadata=metadata,
        active_user_message="Use Chrome to publish content on LinkedIn",
    )

    assert blocked is None


def test_provider_continuation_without_confirmation_policy_does_not_restore_receipt() -> None:
    from packages.core.ai.runtime.provider_approvals import provider_approval_runtime_metadata

    request = _chrome_approval_request()
    request["confirmation_arguments"] = {"approvalId": request["provider_approval_id"]}
    metadata = provider_approval_runtime_metadata({
        "kind": "provider", "provider": request["provider"],
        "provider_approval_id": request["provider_approval_id"],
        "tool": request["retry_tool"], "continuation": request,
    })
    assert metadata is not None
    assert "chrome_runtime_contract_v1" not in metadata


def test_provider_continuation_must_match_its_exact_stored_subject() -> None:
    from packages.core.ai.runtime.provider_approvals import (
        provider_approval_runtime_metadata,
    )

    request = _chrome_approval_request()
    item = {
        "kind": "provider",
        "provider": request["provider"],
        "provider_approval_id": request["provider_approval_id"],
        "tool": request["retry_tool"],
        "continuation": request,
    }

    assert provider_approval_runtime_metadata(item) is not None
    assert provider_approval_runtime_metadata({
        **item,
        "provider_approval_id": "different-approval",
    }) is None
    assert provider_approval_runtime_metadata({
        **item,
        "tool": "mcp__chrome__different_action",
    }) is None
    assert provider_approval_runtime_metadata({
        **item,
        "continuation": {
            **request,
            "confirmation_arguments": {"approvalId": "different-approval"},
        },
    }) is None


def test_preapproved_chrome_confirmation_clears_collected_provider_gate() -> None:
    from packages.core.ai.runtime.provider_approvals import ProviderApprovalCollector

    collector = ProviderApprovalCollector()
    request = _chrome_approval_request()
    collector.capture(
        request["retry_tool"],
        request["retry_arguments"],
        {
            "status": "approval_required",
            "approval_required": True,
            "provider": "chrome",
            "approvalId": request["provider_approval_id"],
            "confirmation_mode": "preapproval_allowed",
            "policy_category": "file_upload",
            "retry_action": {
                "name": request["retry_tool"],
                "arguments": request["retry_arguments"],
            },
        },
    )
    assert collector.pending_request() is not None

    collector.capture(
        "mcp__chrome__confirm_action",
        {
            "approvalId": request["provider_approval_id"],
            "confirmation_mode": "preapproval_allowed",
            "policy_category": "file_upload",
            "preapproved": True,
        },
        {
            "ok": True,
            "status": "approved",
            "approvalId": request["provider_approval_id"],
            "approvalToken": "single-use-token",
        },
    )

    assert collector.pending_request() is None


def test_nested_tool_stream_records_provider_approval_without_exposing_it_to_sse() -> None:
    from packages.core.ai.runtime.streams import RuntimeToolStreamSink

    class Queue:
        def __init__(self) -> None:
            self.items: list[dict] = []

        def put_nowait(self, item: dict) -> None:
            self.items.append(item)

    queue = Queue()
    recorded: list[tuple[str, dict]] = []
    arguments = _chrome_approval_request()["retry_arguments"]
    result = json.dumps(
        {
            "status": "approval_required",
            "approval_required": True,
            "provider": "chrome",
            "approvalId": "chrome-approval-1",
            "expires_at": "2099-07-19T12:00:00Z",
            "target_label": "Publish",
            "retry_action": {
                "name": "mcp__chrome__click_element",
                "arguments": arguments,
            },
        }
    )
    sink = RuntimeToolStreamSink(
        event_queue=queue,
        record_tool_event=lambda event_type, payload: recorded.append(
            (event_type, payload)
        ),
        format_event=lambda event_type, payload: {
            "event": event_type,
            "payload": payload,
        },
        format_tool_arguments=lambda _name, args: {"ref": args.get("ref")},
        format_tool_result=lambda _name, value: value[:80],
        resolve_tool_status=lambda _value: "success",
    )

    sink.emit_tool_end(
        "mcp__chrome__click_element",
        result,
        args=arguments,
    )

    recorded_call = recorded[0][1]["tool_call"]
    streamed_call = queue.items[0]["payload"]["tool_call"]
    assert recorded_call["raw_result"] == result
    assert recorded_call["provider_approval"]["retry_arguments"] == arguments
    assert "raw_result" not in streamed_call
    assert "provider_approval" not in streamed_call
    assert "raw_result" not in json.dumps(queue.items[0])
    assert "provider_approval" not in json.dumps(queue.items[0])


def test_nested_preapproved_confirmation_clears_collected_provider_gate() -> None:
    from packages.core.ai.runtime.provider_approvals import ProviderApprovalCollector
    from packages.core.ai.runtime.streams import RuntimeToolStreamSink

    collector = ProviderApprovalCollector()
    sink = RuntimeToolStreamSink(
        record_tool_event=lambda _event_type, payload: collector.capture_recorded_tool_call(
            payload["tool_call"]
        ),
        format_tool_arguments=lambda _name, args: dict(args or {}),
        format_tool_result=lambda _name, value: value[:80],
        resolve_tool_status=lambda _value: "success",
    )
    request = _chrome_approval_request()
    sink.emit_tool_end(
        request["retry_tool"],
        json.dumps({
            "status": "approval_required",
            "approval_required": True,
            "provider": "chrome",
            "approvalId": request["provider_approval_id"],
            "confirmation_mode": "preapproval_allowed",
            "policy_category": "file_upload",
            "retry_action": {
                "name": request["retry_tool"],
                "arguments": request["retry_arguments"],
            },
        }),
        args=request["retry_arguments"],
    )
    assert collector.pending_request() is not None

    sink.emit_tool_end(
        "mcp__chrome__confirm_action",
        json.dumps({
            "ok": True,
            "status": "approved",
            "approvalId": request["provider_approval_id"],
            "approvalToken": "single-use-token",
        }),
        args={
            "approvalId": request["provider_approval_id"],
            "confirmation_mode": "preapproval_allowed",
            "policy_category": "file_upload",
            "preapproved": True,
        },
    )

    assert collector.pending_request() is None


def test_nested_provider_confirmation_keeps_token_only_in_private_receipt() -> None:
    from packages.core.ai.runtime.streams import RuntimeToolStreamSink

    recorded: list[dict] = []
    result = json.dumps({
        "ok": True,
        "status": "approved",
        "approvalId": "chrome-private-receipt",
        "approvalToken": "single-use-secret",
    })
    sink = RuntimeToolStreamSink(
        record_tool_event=lambda _event_type, payload: recorded.append(payload),
        format_tool_result=lambda _name, value: value,
    )

    sink.emit_tool_end(
        "mcp__chrome__confirm_action",
        result,
        args={"approvalId": "chrome-private-receipt"},
    )

    tool_call = recorded[0]["tool_call"]
    assert tool_call["provider_approval_resolution"] == {
        "provider": "chrome",
        "provider_approval_id": "chrome-private-receipt",
    }
    assert "single-use-secret" not in tool_call["raw_result"]
    assert "approvalToken" not in json.loads(tool_call["raw_result"])


def test_top_level_provider_confirmation_keeps_token_out_of_chat_history() -> None:
    from packages.core.services.chat_service import _attach_raw_tool_result

    recorded = [{
        "name": "mcp__chrome__confirm_action",
        "status": "success",
        "result": "approved",
    }]
    _attach_raw_tool_result(
        recorded,
        "mcp__chrome__confirm_action",
        json.dumps({
            "ok": True,
            "status": "approved",
            "approvalId": "chrome-private-receipt",
            "approvalToken": "single-use-secret",
        }),
    )

    assert "single-use-secret" not in recorded[0]["raw_result"]
    assert "approvalToken" not in json.loads(recorded[0]["raw_result"])


def test_chat_tool_log_arguments_remove_nested_provider_credentials() -> None:
    from packages.core.ai.chat_logger import safe_tool_args

    safe = safe_tool_args({
        "approvalToken": "top-level-secret",
        "api_key": "provider-api-secret",
        "payload": [{
            "approval_token": "nested-secret",
            "password": "mailbox-secret",
            "ref": "e7",
        }],
        "note": "approvalToken=embedded-secret",
        "authorization": "Bearer oauth-secret-value",
    })

    assert safe is not None
    assert safe["payload"][0]["ref"] == "e7"
    serialized = json.dumps(safe)
    for secret in (
        "top-level-secret",
        "provider-api-secret",
        "nested-secret",
        "mailbox-secret",
        "embedded-secret",
        "oauth-secret-value",
    ):
        assert secret not in serialized


@pytest.mark.asyncio
async def test_stream_nested_manor_hitl_is_promoted_to_approval_event() -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, patch

    from packages.core.ai.agentic_loop import AgenticResult
    from packages.core.ai.runtime import ChatSurface
    from packages.core.ai.runtime.streams import runtime_skill_nested_tool_callbacks
    from packages.core.services.chat_service import stream_chat_response

    hitl_payload = json.dumps({
        "__hitl__": True,
        "error": "approval_required",
        "approval_token": "hitl-calendar-delete",
        "hitl": {
            "id": "hitl-calendar-delete",
            "type": "approval",
            "prompt": "Delete the temporary calendar event?",
            "action": "calendar.event.delete",
            "tool": "mcp__google_calendar__delete_event",
            "options": ["approve", "reject"],
        },
        "operation": {
            "tool": "mcp__google_calendar__delete_event",
            "action_key": "calendar.event.delete",
        },
    })

    async def fake_context(*_args, **_kwargs):
        return (
            "system",
            [{"type": "function", "function": {"name": "invoke_skill"}}],
            [],
            SimpleNamespace(
                workspace_id=None,
                task_id=None,
                runtime_envelope=None,
                tool_profile=None,
                allowed_tool_names=None,
                user=None,
                entity=None,
            ),
        )

    async def fake_loop(**_kwargs):
        on_start, on_end = runtime_skill_nested_tool_callbacks(
            skill_name="calendar",
            invoke_skill_args={"skill": "calendar"},
        )
        assert on_start is not None and on_end is not None
        on_start("mcp__google_calendar__delete_event", {"event_id": "event-1"})
        on_end(
            "mcp__google_calendar__delete_event",
            hitl_payload,
            12,
            {"event_id": "event-1"},
        )
        return AgenticResult(
            content="Waiting for approval.",
            messages=[],
            usage={},
            rounds=1,
            tool_calls_made=["invoke_skill"],
        )

    events: list[tuple[str, dict]] = []
    with (
        patch(
            "packages.core.services.chat_service.resolve_runtime_chat_context",
            new=fake_context,
        ),
        patch(
            "packages.core.services.chat_service.runtime_execute_chat_agent_loop",
            new=fake_loop,
        ),
        patch(
            "packages.core.services.chat_service.runtime_persist_chat_stream_runtime_events",
            new=AsyncMock(),
        ),
        patch(
            "packages.core.services.chat_service.record_chat_llm_usage",
            new=AsyncMock(),
        ),
    ):
        async for raw in stream_chat_response(
            "Delete the temporary calendar event",
            "conv-nested-hitl",
            entity_id="entity-1",
            user_id="user-1",
            persist_messages=False,
            runtime_surface=ChatSurface.GLOBAL_OWNER_CHAT,
        ):
            event_name = raw.split("\n", 1)[0].removeprefix("event: ")
            if "\ndata: " not in raw:
                continue
            payload = json.loads(raw.split("\ndata: ", 1)[1].strip())
            events.append((event_name, payload))

    approvals = [payload for event, payload in events if event == "hitl_required"]
    assert len(approvals) == 1
    assert approvals[0]["hitl"]["id"] == "hitl-calendar-delete"
    assert approvals[0]["operation"]["tool"] == "mcp__google_calendar__delete_event"


@pytest.mark.asyncio
async def test_register_provider_approval_creates_standard_hitl_and_deduplicates(
    db_session,
) -> None:
    from packages.core.ai.runtime.approval_service import (
        register_provider_runtime_approval,
    )
    from packages.core.models.hitl_request import HitlRequest
    from packages.core.models.base import generate_ulid
    from packages.core.models.task import Conversation

    entity_id = generate_ulid()
    user_id = generate_ulid()
    conversation_id = generate_ulid()
    conversation = Conversation(
        id=conversation_id,
        entity_id=entity_id,
        user_id=user_id,
        title="Provider approval",
    )
    db_session.add(conversation)
    await db_session.flush()
    await _initialize_provider_approval_owner(db_session, entity_id=entity_id, user_id=user_id)
    request = _chrome_approval_request()

    first = await register_provider_runtime_approval(
        db_session,
        conversation_id=conversation_id,
        entity_id=entity_id,
        user_id=user_id,
        request=request,
    )
    second = await register_provider_runtime_approval(
        db_session,
        conversation_id=conversation_id,
        entity_id=entity_id,
        user_id=user_id,
        request=request,
    )

    assert first is not None
    assert second is not None
    assert first["__hitl__"] is True
    assert first["hitl"]["id"] != request["provider_approval_id"]
    assert first["hitl"]["id"] == second["hitl"]["id"]
    assert first["hitl"]["type"] == "approval"
    # This card decides one exact continuation; saved rules use settings CAS.
    assert first["hitl"]["options"] == [
        "approve", "revise", "reject",
    ]
    stored = await db_session.get(HitlRequest, first["hitl"]["id"])
    assert stored is not None
    assert stored.status == "pending"
    assert (stored.context or {}).get("kind") == "provider"
    assert (stored.context or {}).get("provider") == "chrome"
    assert (stored.context or {}).get("continuation", {}).get(
        "retry_arguments"
    ) == request["retry_arguments"]


@pytest.mark.asyncio
async def test_provider_saved_rule_auto_confirms_next_gate_end_to_end(
    db_session,
    monkeypatch,
) -> None:
    from packages.core import database
    from packages.core.ai.runtime.approval_service import (
        register_provider_runtime_approval,
        resolve_runtime_approval_turn,
        runtime_auto_confirm_provider_approval,
    )
    from packages.core.models.base import generate_ulid
    from packages.core.models.task import Conversation
    from packages.core.models.user import User

    class _SessionContext:
        async def __aenter__(self):
            return db_session

        async def __aexit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(database, "async_session", lambda: _SessionContext())

    entity_id = generate_ulid()
    user_id = generate_ulid()
    conversation_id = generate_ulid()
    db_session.add(User(
        id=user_id,
        entity_id=entity_id,
        email=f"{user_id}@example.com",
        password_hash="x",
        role="owner",
    ))
    db_session.add(Conversation(
        id=conversation_id,
        entity_id=entity_id,
        user_id=user_id,
        title="Provider saved rule E2E",
    ))
    await db_session.flush()
    await _initialize_provider_approval_owner(db_session, entity_id=entity_id, user_id=user_id)

    first_request = _chrome_approval_request(approval_id="chrome-first")
    first_card = await register_provider_runtime_approval(
        db_session,
        conversation_id=conversation_id,
        entity_id=entity_id,
        user_id=user_id,
        request=first_request,
    )
    assert first_card is not None
    resolution = await resolve_runtime_approval_turn(
        db_session,
        conversation_id=conversation_id,
        entity_id=entity_id,
        user_id=user_id,
        hitl_id=first_card["hitl"]["id"],
        action="approve",
    )
    assert resolution.runtime_metadata is not None
    await db_session.flush()

    from approval_settings_test_utils import action_rule, set_personal_test_approval
    saved = await set_personal_test_approval(db_session, entity_id=entity_id,
        actor_id=user_id, user_rules=[action_rule(first_request["action_key"])])
    assert saved["saved_rules"][0]["selector"]["action_key"] == first_request["action_key"]

    second_request = _chrome_approval_request(approval_id="chrome-second")
    second_card = await register_provider_runtime_approval(
        db_session,
        conversation_id=conversation_id,
        entity_id=entity_id,
        user_id=user_id,
        request=second_request,
    )
    assert second_card is None, "the saved action rule must suppress the next approval card"

    provider_result = json.dumps({
        "ok": False,
        "status": "approval_required",
        "approval_required": True,
        "provider": "chrome",
        "action_key": "click_element",
        "approvalId": "chrome-second",
        "expires_at": "2099-07-19T12:00:00Z",
        "confirmation_mode": "always_action_time",
        "policy_category": "representational_communication",
        "target_label": "Publish",
        "target_role": "button",
        "url": "https://www.linkedin.com/feed/",
        "retry_action": {
            "name": second_request["retry_tool"],
            "arguments": second_request["retry_arguments"],
        },
    })
    calls: list[tuple[str, dict]] = []

    async def execute(name: str, arguments: dict) -> str:
        calls.append((name, arguments))
        if name == "mcp__chrome__confirm_action":
            return json.dumps({
                "ok": True,
                "status": "approved",
                "approvalToken": "single-use-next-token",
            })
        return json.dumps({"ok": True, "status": "published"})

    result = await runtime_auto_confirm_provider_approval(
        tool_name=second_request["retry_tool"],
        arguments=second_request["retry_arguments"],
        result=provider_result,
        execute=execute,
        entity_id=entity_id,
        user_id=user_id,
        workspace_id=None,
    )

    assert json.loads(result)["status"] == "published"
    assert [name for name, _ in calls] == [
        "mcp__chrome__confirm_action",
        "mcp__chrome__click_element",
    ]
    assert calls[0][1]["approvalId"] == "chrome-second"
    assert calls[1][1]["approvalToken"] == "single-use-next-token"


@pytest.mark.asyncio
async def test_provider_granted_retry_rejects_unversioned_promotion(
    db_session,
) -> None:
    from packages.core.ai.runtime.approval_service import (
        register_provider_runtime_approval,
        resolve_runtime_approval_turn,
    )
    from packages.core.models.base import generate_ulid
    from packages.core.models.hitl_request import HitlRequest
    from packages.core.models.task import Conversation
    from packages.core.models.user import User

    entity_id = generate_ulid()
    user_id = generate_ulid()
    conversation_id = generate_ulid()
    db_session.add(User(
        id=user_id,
        entity_id=entity_id,
        email=f"{user_id}@example.com",
        password_hash="x",
        role="owner",
    ))
    db_session.add(Conversation(
        id=conversation_id,
        entity_id=entity_id,
        user_id=user_id,
        title="Reject unversioned provider promotion",
    ))
    await db_session.flush()
    await _initialize_provider_approval_owner(db_session, entity_id=entity_id, user_id=user_id)

    first_card = await register_provider_runtime_approval(
        db_session,
        conversation_id=conversation_id,
        entity_id=entity_id,
        user_id=user_id,
        request=_chrome_approval_request(approval_id="promote-first"),
    )
    first = await resolve_runtime_approval_turn(
        db_session,
        conversation_id=conversation_id,
        entity_id=entity_id,
        user_id=user_id,
        hitl_id=first_card["hitl"]["id"],
        action="approve",
    )
    with pytest.raises(ValueError, match="versioned approval-settings command"):
        await resolve_runtime_approval_turn(
            db_session,
            conversation_id=conversation_id,
            entity_id=entity_id,
            user_id=user_id,
            hitl_id=first_card["hitl"]["id"],
            action="always_approve",
        )

    stored = await db_session.get(HitlRequest, first_card["hitl"]["id"])
    assert first.runtime_metadata is not None
    assert stored.status == "granted"
    assert stored.decided_via == "chat_card"
    second_card = await register_provider_runtime_approval(
        db_session,
        conversation_id=conversation_id,
        entity_id=entity_id,
        user_id=user_id,
        request=_chrome_approval_request(approval_id="promote-second"),
    )
    assert second_card is not None, "approve once cannot authorize the next action"
    from approval_settings_test_utils import action_rule, set_personal_test_approval
    await set_personal_test_approval(db_session, entity_id=entity_id,
        actor_id=user_id, user_rules=[action_rule(_chrome_approval_request()["action_key"])])
    pending = await register_provider_runtime_approval(db_session,
        conversation_id=conversation_id, entity_id=entity_id, user_id=user_id,
        request=_chrome_approval_request(approval_id="promote-second"))
    assert pending["hitl"]["id"] == second_card["hitl"]["id"]
    assert (await db_session.get(HitlRequest, second_card["hitl"]["id"])).status == "pending"
    assert await register_provider_runtime_approval(db_session,
        conversation_id=conversation_id, entity_id=entity_id, user_id=user_id,
        request=_chrome_approval_request(approval_id="promote-third")) is None


@pytest.mark.asyncio
async def test_register_provider_approval_rejects_incomplete_continuation(
    db_session,
) -> None:
    from packages.core.ai.runtime.approval_service import (
        register_provider_runtime_approval,
    )
    from packages.core.models.base import generate_ulid
    from packages.core.models.task import Conversation
    from packages.core.models.user import User

    entity_id = generate_ulid()
    user_id = generate_ulid()
    conversation_id = generate_ulid()
    db_session.add(User(
        id=user_id,
        entity_id=entity_id,
        email=f"{user_id}@example.com",
        password_hash="x",
        role="owner",
    ))
    db_session.add(
        Conversation(
            id=conversation_id,
            entity_id=entity_id,
            user_id=user_id,
            title="Invalid provider approval",
        )
    )
    await db_session.flush()
    await _initialize_provider_approval_owner(db_session, entity_id=entity_id, user_id=user_id)
    request = _chrome_approval_request()
    request.pop("confirmation_tool")

    result = await register_provider_runtime_approval(
        db_session,
        conversation_id=conversation_id,
        entity_id=entity_id,
        user_id=user_id,
        request=request,
    )

    assert result is None


@pytest.mark.asyncio
async def test_provider_approval_resolution_accepts_runtime_injected_retry_context(
    db_session,
) -> None:
    from packages.core.ai.runtime.approval_service import (
        register_provider_runtime_approval,
        resolve_runtime_approval_turn,
    )
    from packages.core.models.base import generate_ulid
    from packages.core.models.task import Conversation
    from packages.core.models.user import User

    entity_id = generate_ulid()
    user_id = generate_ulid()
    conversation_id = generate_ulid()
    db_session.add(User(
        id=user_id,
        entity_id=entity_id,
        email=f"{user_id}@example.com",
        password_hash="x",
        role="owner",
    ))
    db_session.add(
        Conversation(
            id=conversation_id,
            entity_id=entity_id,
            user_id=user_id,
            title="Provider approval",
        )
    )
    await db_session.flush()
    await _initialize_provider_approval_owner(db_session, entity_id=entity_id, user_id=user_id)
    request = _chrome_approval_request()
    hitl_data = await register_provider_runtime_approval(
        db_session,
        conversation_id=conversation_id,
        entity_id=entity_id,
        user_id=user_id,
        request=request,
    )
    assert hitl_data is not None
    hitl_id = hitl_data["hitl"]["id"]

    resolution = await resolve_runtime_approval_turn(
        db_session,
        conversation_id=conversation_id,
        entity_id=entity_id,
        user_id=user_id,
        hitl_id=hitl_id,
        action="approve",
    )

    assert resolution.message.startswith("[Runtime approval approved]")
    from packages.core.ai.runtime.chrome_routing import _chrome_action_signature

    assert resolution.runtime_metadata == {
        "chrome_runtime_contract_v1": {
            "pending_chrome_confirmation": {
                "approval_id": request["provider_approval_id"],
                "tool_name": request["retry_tool"],
                "confirmation_mode": request["confirmation_mode"],
                "policy_category": request["policy_category"],
                "preapproved": request["preapproved"],
                "destination": request["url"],
                "data_summary": request["data_summary"],
                "action_signature": _chrome_action_signature(request["retry_tool"], request["retry_arguments"]),
            }
        },
        "provider_approval_execution": {
            "hitl_id": hitl_id,
            "confirmation_tool": "mcp__chrome__confirm_action",
            "retry_tool": "mcp__chrome__click_element",
        },
        "extra_tool_names": [
            "mcp__chrome__click_element",
            "mcp__chrome__confirm_action",
            "mcp__chrome__read_page",
        ],
        "forced_tool_calls": [
            {
                "name": "mcp__chrome__confirm_action",
                "arguments": {
                    "approvalId": "chrome-approval-1",
                    "confirmation_mode": "always_action_time",
                    "policy_category": "representational_communication",
                    "preapproved": False,
                    "__manor_tool_continuation": {
                        "kind": "retry_with_result_token",
                        "tool": "mcp__chrome__click_element",
                        "arguments": request["retry_arguments"],
                        "required_status": "approved",
                        "result_token_keys": [
                            "approvalToken",
                            "approval_token",
                        ],
                        "argument_token_key": "approvalToken",
                    },
                },
                "disable_followup_tools": True,
            }
        ],
        "approval_resume_guidance": (
            "Resume the approved provider action using the supplied forced "
            "tool continuation. Do not rediscover or alter the approved action."
        ),
    }

    from packages.core.ai.runtime.approval_service import (
        prepare_provider_runtime_approval_execution,
        settle_provider_runtime_approval_execution,
    )
    from packages.core.models.hitl_request import HitlRequest

    stored = await db_session.get(HitlRequest, hitl_id)
    assert stored.status == "granted"
    confirmation_settled = await settle_provider_runtime_approval_execution(
        db_session,
        runtime_metadata=resolution.runtime_metadata,
        entity_id=entity_id,
        conversation_id=conversation_id,
        tool_name="mcp__chrome__confirm_action",
        arguments={"approvalId": "chrome-approval-1"},
        result={
            "ok": True,
            "status": "approved",
            "approvalToken": "single-use-token",
        },
    )
    assert confirmation_settled is True
    assert stored.status == "granted"
    assert stored.resolved_reason == "provider_confirmation_completed"
    assert stored.context["provider_confirmation_receipt"]["approval_token"] == (
        "single-use-token"
    )

    replay = await prepare_provider_runtime_approval_execution(
        db_session,
        runtime_metadata=resolution.runtime_metadata,
        entity_id=entity_id,
        conversation_id=conversation_id,
        tool_name="mcp__chrome__confirm_action",
        arguments={"approvalId": "chrome-approval-1"},
    )
    assert replay.changed is False
    assert json.loads(replay.result or "{}")["approvalToken"] == "single-use-token"

    retry_arguments = {
        **request["retry_arguments"],
        "approvalToken": "single-use-token",
        "workspace_id": generate_ulid(),
        "conversation_id": conversation_id,
        "_agent_id_from_context": generate_ulid(),
        "_user_id_from_context": user_id,
        "_active_user_message_from_context": "Publish the prepared video",
        "_manual_skill_selected_from_context": True,
        "_manual_skill_ids_from_context": [generate_ulid()],
        "_manual_skill_slugs_from_context": ["chrome"],
        "_tool_profile_from_context": "full",
        "_runtime_chrome_interaction_mode_from_context": "fast",
    }
    claimed = await prepare_provider_runtime_approval_execution(
        db_session,
        runtime_metadata=resolution.runtime_metadata,
        entity_id=entity_id,
        conversation_id=conversation_id,
        tool_name="mcp__chrome__click_element",
        arguments=retry_arguments,
    )
    assert claimed.result is None
    assert claimed.changed is True
    assert stored.resolved_reason == "provider_action_claimed"

    duplicate = await prepare_provider_runtime_approval_execution(
        db_session,
        runtime_metadata=resolution.runtime_metadata,
        entity_id=entity_id,
        conversation_id=conversation_id,
        tool_name="mcp__chrome__click_element",
        arguments=retry_arguments,
    )
    duplicate_payload = json.loads(duplicate.result or "{}")
    assert duplicate_payload["status"] == "ambiguous"

    action_settled = await settle_provider_runtime_approval_execution(
        db_session,
        runtime_metadata=resolution.runtime_metadata,
        entity_id=entity_id,
        conversation_id=conversation_id,
        tool_name="mcp__chrome__click_element",
        arguments=retry_arguments,
        result={"ok": True, "status": "clicked"},
    )
    assert action_settled is True
    assert stored.status == "consumed"
    assert stored.resolved_reason == "provider_action_completed"
    assert stored.consumed_at is not None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("approved_retry_overrides", "retried_retry_overrides"),
    [
        (
            {"source": "approved-source"},
            {"source": "changed-source"},
        ),
        (
            {"integration_account_id": "account-a"},
            {"integration_account_id": "account-b"},
        ),
        (
            {},
            {"ref": "e8"},
        ),
    ],
    ids=["source", "explicit-account", "ref"],
)
async def test_provider_approval_resolution_rejects_changed_public_retry_arguments(
    db_session,
    caplog,
    approved_retry_overrides,
    retried_retry_overrides,
) -> None:
    from packages.core.ai.runtime.approval_service import (
        prepare_provider_runtime_approval_execution,
        register_provider_runtime_approval,
        resolve_runtime_approval_turn,
        settle_provider_runtime_approval_execution,
    )
    from packages.core.models.base import generate_ulid
    from packages.core.models.hitl_request import HitlRequest
    from packages.core.models.task import Conversation
    from packages.core.models.user import User

    entity_id = generate_ulid()
    user_id = generate_ulid()
    conversation_id = generate_ulid()
    db_session.add(User(
        id=user_id,
        entity_id=entity_id,
        email=f"{user_id}@example.com",
        password_hash="x",
        role="owner",
    ))
    db_session.add(Conversation(
        id=conversation_id,
        entity_id=entity_id,
        user_id=user_id,
        title="Changed provider retry arguments",
    ))
    await db_session.flush()
    await _initialize_provider_approval_owner(db_session, entity_id=entity_id, user_id=user_id)
    request = _chrome_approval_request(approval_id="changed-provider-payload")
    request["retry_arguments"].update(approved_retry_overrides)
    caplog.set_level(
        "WARNING",
        logger="packages.core.ai.runtime.approval_service",
    )
    card = await register_provider_runtime_approval(
        db_session,
        conversation_id=conversation_id,
        entity_id=entity_id,
        user_id=user_id,
        request=request,
    )
    resolution = await resolve_runtime_approval_turn(
        db_session,
        conversation_id=conversation_id,
        entity_id=entity_id,
        user_id=user_id,
        hitl_id=card["hitl"]["id"],
        action="approve",
    )
    assert await settle_provider_runtime_approval_execution(
        db_session,
        runtime_metadata=resolution.runtime_metadata,
        entity_id=entity_id,
        conversation_id=conversation_id,
        tool_name=request["confirmation_tool"],
        arguments=request["confirmation_arguments"],
        result={
            "ok": True,
            "status": "approved",
            "approvalToken": "changed-payload-token",
        },
    )

    preflight = await prepare_provider_runtime_approval_execution(
        db_session,
        runtime_metadata=resolution.runtime_metadata,
        entity_id=entity_id,
        conversation_id=conversation_id,
        tool_name=request["retry_tool"],
        arguments={
            **request["retry_arguments"],
            **retried_retry_overrides,
            "approvalToken": "changed-payload-token",
            "_runtime_chrome_interaction_mode_from_context": "fast",
        },
    )

    stored = await db_session.get(HitlRequest, card["hitl"]["id"])
    assert json.loads(preflight.result or "{}") == {
        "ok": False,
        "error": "provider_approval_execution_blocked",
        "status": "expired",
        "approval_id": card["hitl"]["id"],
        "reason": "The provider retry no longer matches the approved action.",
    }
    assert preflight.changed is True
    assert stored.status == "expired"
    assert stored.resolved_reason == "provider_retry_payload_changed"
    assert "changed-payload-token" not in caplog.text
    assert "supplied_args_hash=" in caplog.text
    assert "expected_args_hash=" in caplog.text


@pytest.mark.asyncio
async def test_provider_approval_resolution_rejects_changed_private_token_separately(
    db_session,
    caplog,
) -> None:
    from packages.core.ai.runtime.approval_service import (
        prepare_provider_runtime_approval_execution,
        register_provider_runtime_approval,
        resolve_runtime_approval_turn,
        settle_provider_runtime_approval_execution,
    )
    from packages.core.models.base import generate_ulid
    from packages.core.models.hitl_request import HitlRequest
    from packages.core.models.task import Conversation
    from packages.core.models.user import User

    entity_id = generate_ulid()
    user_id = generate_ulid()
    conversation_id = generate_ulid()
    db_session.add(
        User(
            id=user_id,
            entity_id=entity_id,
            email=f"{user_id}@example.com",
            password_hash="x",
            role="owner",
        )
    )
    db_session.add(
        Conversation(
            id=conversation_id,
            entity_id=entity_id,
            user_id=user_id,
            title="Changed provider retry token",
        )
    )
    await db_session.flush()
    await _initialize_provider_approval_owner(db_session, entity_id=entity_id, user_id=user_id)
    request = _chrome_upload_approval_request(
        approval_id="changed-provider-token"
    )
    card = await register_provider_runtime_approval(
        db_session,
        conversation_id=conversation_id,
        entity_id=entity_id,
        user_id=user_id,
        request=request,
    )
    resolution = await resolve_runtime_approval_turn(
        db_session,
        conversation_id=conversation_id,
        entity_id=entity_id,
        user_id=user_id,
        hitl_id=card["hitl"]["id"],
        action="approve",
    )
    expected_token = "expected-private-token"
    supplied_token = "different-private-token"
    assert await settle_provider_runtime_approval_execution(
        db_session,
        runtime_metadata=resolution.runtime_metadata,
        entity_id=entity_id,
        conversation_id=conversation_id,
        tool_name=request["confirmation_tool"],
        arguments=request["confirmation_arguments"],
        result={
            "ok": True,
            "status": "approved",
            "approvalToken": expected_token,
        },
    )
    caplog.set_level(
        "WARNING",
        logger="packages.core.ai.runtime.approval_service",
    )

    preflight = await prepare_provider_runtime_approval_execution(
        db_session,
        runtime_metadata=resolution.runtime_metadata,
        entity_id=entity_id,
        conversation_id=conversation_id,
        tool_name=request["retry_tool"],
        arguments={
            **request["retry_arguments"],
            "approvalToken": supplied_token,
        },
    )

    stored = await db_session.get(HitlRequest, card["hitl"]["id"])
    assert json.loads(preflight.result or "{}")["status"] == "expired"
    assert preflight.changed is True
    assert stored.status == "expired"
    assert stored.resolved_reason == "provider_retry_token_mismatch"
    assert expected_token not in caplog.text
    assert supplied_token not in caplog.text
    assert "supplied_token_hash=" in caplog.text
    assert "expected_token_hash=" in caplog.text


@pytest.mark.asyncio
async def test_provider_grant_card_stays_actionable_until_terminal_settlement(
    db_session,
) -> None:
    from datetime import timedelta

    from packages.core.ai.runtime.approval_service import (
        prepare_provider_runtime_approval_execution,
        register_provider_runtime_approval,
        resolve_runtime_approval_turn,
        settle_provider_runtime_approval_execution,
    )
    from packages.core.models.base import generate_ulid
    from packages.core.models.hitl_request import HitlRequest
    from packages.core.models.task import Conversation, Message
    from packages.core.models.user import User

    entity_id = generate_ulid()
    user_id = generate_ulid()
    conversation_id = generate_ulid()
    db_session.add(User(
        id=user_id,
        entity_id=entity_id,
        email=f"{user_id}@example.com",
        password_hash="x",
        role="owner",
    ))
    db_session.add(Conversation(
        id=conversation_id,
        entity_id=entity_id,
        user_id=user_id,
        title="Provider approval recovery",
    ))
    await db_session.flush()
    await _initialize_provider_approval_owner(db_session, entity_id=entity_id, user_id=user_id)
    request = _chrome_approval_request(approval_id="recoverable-provider")
    card = await register_provider_runtime_approval(
        db_session,
        conversation_id=conversation_id,
        entity_id=entity_id,
        user_id=user_id,
        request=request,
    )
    message = Message(
        id=generate_ulid(),
        conversation_id=conversation_id,
        role="assistant",
        content="",
        message_kind="hitl_request",
        meta={"hitl_requests": [{
            "id": card["hitl"]["id"],
            "type": "approval",
        }]},
    )
    db_session.add(message)
    await db_session.flush()
    assert message.created_at is not None
    db_session.add_all([
        Message(
            id=generate_ulid(),
            conversation_id=conversation_id,
            role="user",
            content=f"Later message {index}",
            created_at=message.created_at + timedelta(seconds=index + 1),
        )
        for index in range(101)
    ])
    await db_session.flush()

    first = await resolve_runtime_approval_turn(
        db_session,
        conversation_id=conversation_id,
        entity_id=entity_id,
        user_id=user_id,
        hitl_id=card["hitl"]["id"],
        action="approve",
    )
    await db_session.flush()
    await db_session.refresh(message)
    stored = await db_session.get(HitlRequest, card["hitl"]["id"])
    assert stored.status == "granted"
    assert message.meta["hitl_requests"][0].get("resolved") is not True

    retried = await resolve_runtime_approval_turn(
        db_session,
        conversation_id=conversation_id,
        entity_id=entity_id,
        user_id=user_id,
        hitl_id=card["hitl"]["id"],
        action="approve",
    )
    assert retried.runtime_metadata == first.runtime_metadata

    assert await settle_provider_runtime_approval_execution(
        db_session,
        runtime_metadata=retried.runtime_metadata,
        entity_id=entity_id,
        conversation_id=conversation_id,
        tool_name=request["confirmation_tool"],
        arguments=request["confirmation_arguments"],
        result={
            "ok": True,
            "status": "approved",
            "approvalId": "recoverable-provider",
            "approvalToken": "recoverable-token",
        },
    )
    retry_arguments = {
        **request["retry_arguments"],
        "approvalToken": "recoverable-token",
    }
    claimed = await prepare_provider_runtime_approval_execution(
        db_session,
        runtime_metadata=retried.runtime_metadata,
        entity_id=entity_id,
        conversation_id=conversation_id,
        tool_name=request["retry_tool"],
        arguments=retry_arguments,
    )
    assert claimed.changed is True
    assert claimed.result is None
    assert await settle_provider_runtime_approval_execution(
        db_session,
        runtime_metadata=retried.runtime_metadata,
        entity_id=entity_id,
        conversation_id=conversation_id,
        tool_name=request["retry_tool"],
        arguments=retry_arguments,
        result={"ok": True, "status": "clicked"},
    )
    await db_session.flush()
    await db_session.refresh(message)

    assert stored.status == "consumed"
    assert message.meta["hitl_requests"][0]["resolved"] is True
    assert message.meta["hitl_requests"][0]["resolution"] == "approve"


@pytest.mark.asyncio
@pytest.mark.parametrize("envelope_kind", ["manor", "provider"])
@pytest.mark.parametrize("same_request", [False, True])
async def test_provider_retry_human_gate_is_not_consumed_and_can_recover(
    db_session, envelope_kind, same_request,
) -> None:
    from packages.core.ai.runtime.approval_service import (
        prepare_provider_runtime_approval_execution,
        register_provider_runtime_approval,
        resolve_runtime_approval_turn,
        settle_provider_runtime_approval_execution,
    )
    from packages.core.models.base import generate_ulid
    from packages.core.models.hitl_request import HitlRequest
    from packages.core.models.task import Conversation, Message
    from packages.core.models.user import User

    entity_id, user_id, conversation_id = [generate_ulid() for _ in range(3)]
    db_session.add(User(
        id=user_id, entity_id=entity_id, email=f"{user_id}@example.com",
        password_hash="x", role="owner",
    ))
    db_session.add(Conversation(
        id=conversation_id, entity_id=entity_id, user_id=user_id,
        title="Synthetic provider gate recovery",
    ))
    await db_session.flush()
    await _initialize_provider_approval_owner(db_session, entity_id=entity_id, user_id=user_id)
    request = _chrome_approval_request(approval_id="synthetic-original")
    card = await register_provider_runtime_approval(
        db_session, conversation_id=conversation_id, entity_id=entity_id,
        user_id=user_id, request=request,
    )
    message = Message(
        id=generate_ulid(), conversation_id=conversation_id, role="assistant",
        content="", message_kind="hitl_request",
        meta={"hitl_requests": [card["hitl"]]},
    )
    db_session.add(message)
    await db_session.flush()
    resolution = await resolve_runtime_approval_turn(
        db_session, conversation_id=conversation_id, entity_id=entity_id,
        user_id=user_id, hitl_id=card["hitl"]["id"], action="approve",
    )
    metadata = resolution.runtime_metadata
    assert await settle_provider_runtime_approval_execution(
        db_session, runtime_metadata=metadata, entity_id=entity_id,
        conversation_id=conversation_id, tool_name=request["confirmation_tool"],
        arguments=request["confirmation_arguments"],
        result={"ok": True, "status": "approved", "approvalToken": "first-token"},
    )
    retry_args = {**request["retry_arguments"], "approvalToken": "first-token"}
    claimed = await prepare_provider_runtime_approval_execution(
        db_session, runtime_metadata=metadata, entity_id=entity_id,
        conversation_id=conversation_id, tool_name=request["retry_tool"], arguments=retry_args,
    )
    assert claimed.changed and claimed.result is None

    next_request = request if same_request else _chrome_approval_request(approval_id="synthetic-next")
    next_card = card
    if envelope_kind == "manor":
        if not same_request:
            next_card = await register_provider_runtime_approval(
                db_session, conversation_id=conversation_id, entity_id=entity_id,
                user_id=user_id, request=next_request,
            )
        result = next_card
    else:
        result = {
            "status": "approval_required", "approval_required": True,
            "provider": "chrome", "approvalId": next_request["provider_approval_id"],
            "expires_at": next_request["expires_at"],
            "retry_action": {"name": request["retry_tool"], "arguments": request["retry_arguments"]},
        }
    assert await settle_provider_runtime_approval_execution(
        db_session, runtime_metadata=metadata, entity_id=entity_id,
        conversation_id=conversation_id, tool_name=request["retry_tool"],
        arguments=retry_args, result=json.dumps(result),
    )
    stored = await db_session.get(HitlRequest, card["hitl"]["id"])
    await db_session.flush()
    await db_session.refresh(message)
    assert stored.consumed_at is None
    assert stored.resolved_reason != "provider_action_completed"
    if same_request:
        assert stored.status == "granted"
        assert stored.resolved_reason == "provider_action_waiting_approval"
        assert "provider_action_claim" not in stored.context
        assert "provider_confirmation_receipt" not in stored.context
        assert message.meta["hitl_requests"][0].get("resolved") is not True
    else:
        assert stored.status == "expired"
        assert message.meta["hitl_requests"][0]["resolution"] == "expired"
        if envelope_kind == "provider":
            next_card = await register_provider_runtime_approval(
                db_session, conversation_id=conversation_id, entity_id=entity_id,
                user_id=user_id, request=next_request,
            )
        next_row = await db_session.get(HitlRequest, next_card["hitl"]["id"])
        assert next_row.status == "pending"
        stale = await prepare_provider_runtime_approval_execution(
            db_session, runtime_metadata=metadata, entity_id=entity_id,
            conversation_id=conversation_id, tool_name=request["retry_tool"], arguments=retry_args,
        )
        assert json.loads(stale.result)["status"] == "expired"

    # Another explicit card decision resumes the exact pending action, without
    # a stale claim turning a proven approval wait into an ambiguous execution.
    resumed = await resolve_runtime_approval_turn(
        db_session, conversation_id=conversation_id, entity_id=entity_id,
        user_id=user_id, hitl_id=next_card["hitl"]["id"], action="approve",
    )
    confirmation = await prepare_provider_runtime_approval_execution(
        db_session, runtime_metadata=resumed.runtime_metadata, entity_id=entity_id,
        conversation_id=conversation_id, tool_name=next_request["confirmation_tool"],
        arguments=next_request["confirmation_arguments"],
    )
    assert confirmation.result is None
    assert await settle_provider_runtime_approval_execution(
        db_session, runtime_metadata=resumed.runtime_metadata, entity_id=entity_id,
        conversation_id=conversation_id, tool_name=next_request["confirmation_tool"],
        arguments=next_request["confirmation_arguments"],
        result={"ok": True, "status": "approved", "approvalToken": "second-token"},
    )
    reclaimed = await prepare_provider_runtime_approval_execution(
        db_session, runtime_metadata=resumed.runtime_metadata, entity_id=entity_id,
        conversation_id=conversation_id, tool_name=next_request["retry_tool"],
        arguments={**next_request["retry_arguments"], "approvalToken": "second-token"},
    )
    assert reclaimed.changed and reclaimed.result is None


@pytest.mark.asyncio
async def test_provider_approval_execution_failure_expires_grant(db_session) -> None:
    from packages.core.ai.runtime.approval_service import (
        prepare_provider_runtime_approval_execution,
        register_provider_runtime_approval,
        resolve_runtime_approval_turn,
        settle_provider_runtime_approval_execution,
    )
    from packages.core.models.base import generate_ulid
    from packages.core.models.hitl_request import HitlRequest
    from packages.core.models.task import Conversation, Message
    from packages.core.models.user import User

    entity_id = generate_ulid()
    user_id = generate_ulid()
    conversation_id = generate_ulid()
    db_session.add(User(
        id=user_id,
        entity_id=entity_id,
        email=f"{user_id}@example.com",
        password_hash="x",
        role="owner",
    ))
    db_session.add(Conversation(
        id=conversation_id,
        entity_id=entity_id,
        user_id=user_id,
        title="Provider action failure",
    ))
    await db_session.flush()
    await _initialize_provider_approval_owner(db_session, entity_id=entity_id, user_id=user_id)
    card = await register_provider_runtime_approval(
        db_session,
        conversation_id=conversation_id,
        entity_id=entity_id,
        user_id=user_id,
        request=_chrome_approval_request(approval_id="provider-failure"),
    )
    message = Message(
        id=generate_ulid(),
        conversation_id=conversation_id,
        role="assistant",
        content="",
        message_kind="hitl_request",
        meta={"hitl_requests": [{
            "id": card["hitl"]["id"],
            "type": "approval",
        }]},
    )
    db_session.add(message)
    await db_session.flush()
    resolution = await resolve_runtime_approval_turn(
        db_session,
        conversation_id=conversation_id,
        entity_id=entity_id,
        user_id=user_id,
        hitl_id=card["hitl"]["id"],
        action="approve",
    )

    confirmation_result = {
        "ok": True,
        "status": "approved",
        "approvalToken": "failure-token",
    }
    confirmation_changed = await settle_provider_runtime_approval_execution(
        db_session,
        runtime_metadata=resolution.runtime_metadata,
        entity_id=entity_id,
        conversation_id=conversation_id,
        tool_name="mcp__chrome__confirm_action",
        arguments={"approvalId": "provider-failure"},
        result=confirmation_result,
    )
    assert confirmation_changed is True
    retry_arguments = {
        **_chrome_approval_request(approval_id="provider-failure")["retry_arguments"],
        "approvalToken": "failure-token",
    }
    claimed = await prepare_provider_runtime_approval_execution(
        db_session,
        runtime_metadata=resolution.runtime_metadata,
        entity_id=entity_id,
        conversation_id=conversation_id,
        tool_name="mcp__chrome__click_element",
        arguments=retry_arguments,
    )
    assert claimed.changed is True
    assert claimed.result is None

    changed = await settle_provider_runtime_approval_execution(
        db_session,
        runtime_metadata=resolution.runtime_metadata,
        entity_id=entity_id,
        conversation_id=conversation_id,
        tool_name="mcp__chrome__click_element",
        arguments=retry_arguments,
        result={"ok": False, "status": "failed"},
    )

    stored = await db_session.get(HitlRequest, card["hitl"]["id"])
    assert changed is True
    assert stored.status == "expired"
    assert stored.resolved_reason == "provider_action_failed"
    assert stored.consumed_at is None
    await db_session.refresh(message)
    assert message.meta["hitl_requests"][0]["resolved"] is True
    assert message.meta["hitl_requests"][0]["resolution"] == "expired"


@pytest.mark.asyncio
async def test_provider_retry_blocked_before_claim_expires_grant(db_session) -> None:
    from packages.core.ai.runtime.approval_service import (
        register_provider_runtime_approval,
        resolve_runtime_approval_turn,
        settle_provider_runtime_approval_execution,
    )
    from packages.core.models.base import generate_ulid
    from packages.core.models.hitl_request import HitlRequest
    from packages.core.models.task import Conversation
    from packages.core.models.user import User

    entity_id = generate_ulid()
    user_id = generate_ulid()
    conversation_id = generate_ulid()
    db_session.add(User(
        id=user_id,
        entity_id=entity_id,
        email=f"{user_id}@example.com",
        password_hash="x",
        role="owner",
    ))
    db_session.add(Conversation(
        id=conversation_id,
        entity_id=entity_id,
        user_id=user_id,
        title="Provider preflight denial",
    ))
    await db_session.flush()
    await _initialize_provider_approval_owner(db_session, entity_id=entity_id, user_id=user_id)
    request = _chrome_approval_request(approval_id="provider-preflight-denial")
    card = await register_provider_runtime_approval(
        db_session,
        conversation_id=conversation_id,
        entity_id=entity_id,
        user_id=user_id,
        request=request,
    )
    resolution = await resolve_runtime_approval_turn(
        db_session,
        conversation_id=conversation_id,
        entity_id=entity_id,
        user_id=user_id,
        hitl_id=card["hitl"]["id"],
        action="approve",
    )

    changed = await settle_provider_runtime_approval_execution(
        db_session,
        runtime_metadata=resolution.runtime_metadata,
        entity_id=entity_id,
        conversation_id=conversation_id,
        tool_name=request["retry_tool"],
        arguments=request["retry_arguments"],
        result={"ok": False, "status": "blocked", "error": "binding revoked"},
    )

    stored = await db_session.get(HitlRequest, card["hitl"]["id"])
    assert changed is True
    assert stored.status == "expired"
    assert stored.resolved_reason == "provider_action_blocked_before_execution"
    assert stored.consumed_at is None


@pytest.mark.asyncio
async def test_provider_approval_with_malformed_stored_continuation_expires_before_grant(
    db_session,
) -> None:
    from packages.core.ai.runtime.approval_service import (
        register_provider_runtime_approval,
        resolve_runtime_approval_turn,
    )
    from packages.core.models.base import generate_ulid
    from packages.core.models.hitl_request import HitlRequest
    from packages.core.models.task import Conversation
    from packages.core.models.user import User

    entity_id = generate_ulid()
    user_id = generate_ulid()
    conversation_id = generate_ulid()
    db_session.add(User(
        id=user_id,
        entity_id=entity_id,
        email=f"{user_id}@example.com",
        password_hash="x",
        role="owner",
    ))
    db_session.add(Conversation(
        id=conversation_id,
        entity_id=entity_id,
        user_id=user_id,
        title="Malformed provider approval",
    ))
    await db_session.flush()
    await _initialize_provider_approval_owner(db_session, entity_id=entity_id, user_id=user_id)
    card = await register_provider_runtime_approval(
        db_session,
        conversation_id=conversation_id,
        entity_id=entity_id,
        user_id=user_id,
        request=_chrome_approval_request(),
    )
    assert card is not None
    stored = await db_session.get(HitlRequest, card["hitl"]["id"])
    stored.context = {
        **dict(stored.context or {}),
        "continuation": "not-an-object",
    }
    await db_session.flush()

    resolution = await resolve_runtime_approval_turn(
        db_session,
        conversation_id=conversation_id,
        entity_id=entity_id,
        user_id=user_id,
        hitl_id=card["hitl"]["id"],
        action="approve",
    )

    assert resolution.runtime_metadata is None
    assert "could not be resumed" in resolution.message
    assert stored.status == "expired"
    assert stored.resolved_reason == "approval_continuation_unavailable"
    assert stored.decided_via is None
    assert stored.consumed_at is None


@pytest.mark.asyncio
async def test_provider_approval_rejection_never_returns_continuation(db_session) -> None:
    from packages.core.ai.runtime.approval_service import (
        register_provider_runtime_approval,
        resolve_runtime_approval_turn,
    )
    from packages.core.models.base import generate_ulid
    from packages.core.models.task import Conversation
    from packages.core.models.user import User

    entity_id = generate_ulid()
    user_id = generate_ulid()
    conversation_id = generate_ulid()
    db_session.add(User(
        id=user_id,
        entity_id=entity_id,
        email=f"{user_id}@example.com",
        password_hash="x",
        role="owner",
    ))
    db_session.add(
        Conversation(
            id=conversation_id,
            entity_id=entity_id,
            user_id=user_id,
            title="Provider approval",
        )
    )
    await db_session.flush()
    await _initialize_provider_approval_owner(db_session, entity_id=entity_id, user_id=user_id)
    hitl_data = await register_provider_runtime_approval(
        db_session,
        conversation_id=conversation_id,
        entity_id=entity_id,
        user_id=user_id,
        request=_chrome_approval_request(),
    )
    assert hitl_data is not None

    resolution = await resolve_runtime_approval_turn(
        db_session,
        conversation_id=conversation_id,
        entity_id=entity_id,
        user_id=user_id,
        hitl_id=hitl_data["hitl"]["id"],
        action="reject",
    )

    assert resolution.message.startswith("[Runtime approval rejected]")
    assert resolution.runtime_metadata == {
        "approval_kind": "runtime_terminal",
        "approval_outcome": "rejected",
    }


@pytest.mark.asyncio
async def test_provider_publish_approval_revision_closes_old_gate_without_continuation(
    db_session,
) -> None:
    from packages.core.ai.runtime.approval_service import (
        register_provider_runtime_approval,
        resolve_runtime_approval_turn,
    )
    from packages.core.models.base import generate_ulid
    from packages.core.models.hitl_request import HitlRequest
    from packages.core.models.task import Conversation
    from packages.core.models.user import User

    entity_id = generate_ulid()
    user_id = generate_ulid()
    conversation_id = generate_ulid()
    db_session.add(User(
        id=user_id,
        entity_id=entity_id,
        email=f"{user_id}@example.com",
        password_hash="x",
        role="owner",
    ))
    db_session.add(
        Conversation(
            id=conversation_id,
            entity_id=entity_id,
            user_id=user_id,
            title="Revise provider approval",
        )
    )
    await db_session.flush()
    await _initialize_provider_approval_owner(db_session, entity_id=entity_id, user_id=user_id)
    hitl_data = await register_provider_runtime_approval(
        db_session,
        conversation_id=conversation_id,
        entity_id=entity_id,
        user_id=user_id,
        request=_chrome_approval_request(),
    )
    assert hitl_data is not None
    hitl_id = hitl_data["hitl"]["id"]

    resolution = await resolve_runtime_approval_turn(
        db_session,
        conversation_id=conversation_id,
        entity_id=entity_id,
        user_id=user_id,
        hitl_id=hitl_id,
        action="revise",
        revision_request="Make the opening shorter and remove the hashtag.",
    )

    assert "Do not confirm or retry" in resolution.message
    assert "Make the opening shorter" in resolution.message
    assert resolution.runtime_metadata == {
        "approval_kind": "provider_revision",
        "approval_resume_guidance": (
            "Revise the proposed content according to the user's instructions. "
            "Do not confirm or reuse the superseded provider approval; request "
            "approval again only for the new final external action."
        ),
    }
    stored = await db_session.get(HitlRequest, hitl_id)
    assert stored.status == "expired"
    assert stored.resolved_reason == "revision_requested"
    assert stored.decided_via == "chat_card_revise"


@pytest.mark.asyncio
async def test_provider_approval_expiry_blocks_but_granted_retry_resumes_exact_call(
    db_session,
) -> None:
    from packages.core.ai.runtime.approval_service import (
        register_provider_runtime_approval,
        resolve_runtime_approval_turn,
    )
    from packages.core.models.hitl_request import HitlRequest
    from packages.core.models.base import generate_ulid
    from packages.core.models.task import Conversation
    from packages.core.models.user import User

    entity_id = generate_ulid()
    user_id = generate_ulid()
    conversation_id = generate_ulid()
    db_session.add(User(
        id=user_id,
        entity_id=entity_id,
        email=f"{user_id}@example.com",
        password_hash="x",
        role="owner",
    ))
    conversation = Conversation(
        id=conversation_id,
        entity_id=entity_id,
        user_id=user_id,
        title="Provider approval expiry",
    )
    db_session.add(conversation)
    await db_session.flush()
    await _initialize_provider_approval_owner(db_session, entity_id=entity_id, user_id=user_id)
    expired_request = _chrome_approval_request(approval_id="expired-provider")
    expired_request["expires_at"] = "2000-01-01T00:00:00Z"
    expired_hitl = await register_provider_runtime_approval(
        db_session,
        conversation_id=conversation_id,
        entity_id=entity_id,
        user_id=user_id,
        request=expired_request,
    )
    assert expired_hitl is not None

    expired = await resolve_runtime_approval_turn(
        db_session,
        conversation_id=conversation_id,
        entity_id=entity_id,
        user_id=user_id,
        hitl_id=expired_hitl["hitl"]["id"],
        action="approve",
    )
    assert expired.runtime_metadata is None
    expired_row = await db_session.get(HitlRequest, expired_hitl["hitl"]["id"])
    assert expired_row.status == "expired"

    active_hitl = await register_provider_runtime_approval(
        db_session,
        conversation_id=conversation_id,
        entity_id=entity_id,
        user_id=user_id,
        request=_chrome_approval_request(approval_id="active-provider"),
    )
    assert active_hitl is not None
    first = await resolve_runtime_approval_turn(
        db_session,
        conversation_id=conversation_id,
        entity_id=entity_id,
        user_id=user_id,
        hitl_id=active_hitl["hitl"]["id"],
        action="approve",
    )
    second = await resolve_runtime_approval_turn(
        db_session,
        conversation_id=conversation_id,
        entity_id=entity_id,
        user_id=user_id,
        hitl_id=active_hitl["hitl"]["id"],
        action="approve",
    )
    assert first.runtime_metadata is not None
    assert second.runtime_metadata == first.runtime_metadata


@pytest.mark.asyncio
async def test_chat_approval_turn_returns_provider_continuation_for_exact_hitl_id(
    db_session,
) -> None:
    from packages.core.ai.runtime.approval_service import (
        register_provider_runtime_approval,
    )
    from packages.core.models.base import generate_ulid
    from packages.core.models.task import Conversation
    from packages.core.models.user import User
    from packages.core.services.chat_approvals import resolve_chat_approval_turn

    entity_id = generate_ulid()
    user_id = generate_ulid()
    conversation_id = generate_ulid()
    db_session.add(User(
        id=user_id,
        entity_id=entity_id,
        email=f"{user_id}@example.com",
        password_hash="x",
        role="owner",
    ))
    db_session.add(
        Conversation(
            id=conversation_id,
            entity_id=entity_id,
            user_id=user_id,
            title="Structured provider approval",
        )
    )
    await db_session.flush()
    await _initialize_provider_approval_owner(db_session, entity_id=entity_id, user_id=user_id)
    first = await register_provider_runtime_approval(
        db_session,
        conversation_id=conversation_id,
        entity_id=entity_id,
        user_id=user_id,
        request=_chrome_approval_request(approval_id="first-provider"),
    )
    second = await register_provider_runtime_approval(
        db_session,
        conversation_id=conversation_id,
        entity_id=entity_id,
        user_id=user_id,
        request=_chrome_approval_request(approval_id="second-provider"),
    )
    assert first is not None and second is not None

    replacement, saved_text, save_user, runtime_metadata = (
        await resolve_chat_approval_turn(
            db_session,
            conversation_id=conversation_id,
            entity_id=entity_id,
            user_id=user_id,
            message=json.dumps(
                {"hitl_id": second["hitl"]["id"], "action": "approve"}
            ),
        )
    )

    assert replacement.startswith("[Runtime approval approved]")
    assert saved_text == "Approved the requested action."
    assert save_user is True
    assert runtime_metadata["forced_tool_calls"][0]["arguments"][
        "approvalId"
    ] == "second-provider"


@pytest.mark.asyncio
async def test_plain_reply_does_not_guess_between_provider_approvals(
    db_session,
) -> None:
    from packages.core.ai.runtime.approval_service import (
        register_provider_runtime_approval,
        resolve_pending_runtime_approval_turn_from_reply,
    )
    from packages.core.models.base import generate_ulid
    from packages.core.models.task import Conversation

    entity_id = generate_ulid()
    user_id = generate_ulid()
    conversation_id = generate_ulid()
    db_session.add(
        Conversation(
            id=conversation_id,
            entity_id=entity_id,
            user_id=user_id,
            title="Ambiguous provider approvals",
        )
    )
    await db_session.flush()
    await _initialize_provider_approval_owner(db_session, entity_id=entity_id, user_id=user_id)
    for approval_id in ("first-provider", "second-provider"):
        assert await register_provider_runtime_approval(
            db_session,
            conversation_id=conversation_id,
            entity_id=entity_id,
            user_id=user_id,
            request=_chrome_approval_request(approval_id=approval_id),
        )

    resolution = await resolve_pending_runtime_approval_turn_from_reply(
        db_session,
        conversation_id=conversation_id,
        entity_id=entity_id,
        user_id=user_id,
        message="yes",
    )

    assert resolution is None


@pytest.mark.asyncio
async def test_cancelling_provider_approval_resolves_existing_hitl_card(
    db_session,
) -> None:
    from packages.core.ai.runtime.approval_service import (
        cancel_pending_runtime_approvals,
        register_provider_runtime_approval,
    )
    from packages.core.models.hitl_request import HitlRequest
    from packages.core.models.base import generate_ulid
    from packages.core.models.task import Conversation, Message

    entity_id = generate_ulid()
    user_id = generate_ulid()
    conversation_id = generate_ulid()
    conversation = Conversation(
        id=conversation_id,
        entity_id=entity_id,
        user_id=user_id,
        title="Cancelled provider approval",
    )
    db_session.add(conversation)
    await db_session.flush()
    await _initialize_provider_approval_owner(db_session, entity_id=entity_id, user_id=user_id)
    hitl_data = await register_provider_runtime_approval(
        db_session,
        conversation_id=conversation_id,
        entity_id=entity_id,
        user_id=user_id,
        request=_chrome_approval_request(),
    )
    assert hitl_data is not None
    hitl_id = hitl_data["hitl"]["id"]
    message = Message(
        id=generate_ulid(),
        conversation_id=conversation_id,
        role="assistant",
        content="",
        message_kind="hitl_request",
        meta={"hitl_requests": [{"id": hitl_id, "type": "approval"}]},
    )
    db_session.add(message)
    await db_session.flush()

    cancelled = await cancel_pending_runtime_approvals(
        db_session,
        conversation_id=conversation_id,
        entity_id=entity_id,
        user_id=user_id,
        hitl_ids=[hitl_id],
    )

    assert cancelled == 1
    cancelled_row = await db_session.get(HitlRequest, hitl_id)
    assert cancelled_row.status == "expired"
    assert cancelled_row.resolved_reason == "request_stopped"
    await db_session.refresh(message)
    assert message.meta["hitl_requests"][0]["resolved"] is True
    assert message.meta["hitl_requests"][0]["resolution"] == "cancelled"


@pytest.mark.asyncio
async def test_cancelling_unclaimed_granted_provider_approval_closes_card(
    db_session,
) -> None:
    from packages.core.ai.runtime.approval_service import (
        cancel_pending_runtime_approvals,
        register_provider_runtime_approval,
        resolve_runtime_approval_turn,
    )
    from packages.core.models.base import generate_ulid
    from packages.core.models.hitl_request import HitlRequest
    from packages.core.models.task import Conversation, Message
    from packages.core.models.user import User

    entity_id = generate_ulid()
    user_id = generate_ulid()
    conversation_id = generate_ulid()
    db_session.add(User(
        id=user_id,
        entity_id=entity_id,
        email=f"{user_id}@example.com",
        password_hash="x",
        role="owner",
    ))
    db_session.add(Conversation(
        id=conversation_id,
        entity_id=entity_id,
        user_id=user_id,
        title="Cancel recoverable provider approval",
    ))
    await db_session.flush()
    await _initialize_provider_approval_owner(db_session, entity_id=entity_id, user_id=user_id)
    hitl_data = await register_provider_runtime_approval(
        db_session,
        conversation_id=conversation_id,
        entity_id=entity_id,
        user_id=user_id,
        request=_chrome_approval_request(approval_id="cancel-granted"),
    )
    hitl_id = hitl_data["hitl"]["id"]
    message = Message(
        id=generate_ulid(),
        conversation_id=conversation_id,
        role="assistant",
        content="",
        message_kind="hitl_request",
        meta={"hitl_requests": [{"id": hitl_id, "type": "approval"}]},
    )
    db_session.add(message)
    await db_session.flush()
    resolution = await resolve_runtime_approval_turn(
        db_session,
        conversation_id=conversation_id,
        entity_id=entity_id,
        user_id=user_id,
        hitl_id=hitl_id,
        action="approve",
    )
    assert resolution.runtime_metadata is not None

    cancelled = await cancel_pending_runtime_approvals(
        db_session,
        conversation_id=conversation_id,
        entity_id=entity_id,
        user_id=user_id,
        hitl_ids=[hitl_id],
    )

    stored = await db_session.get(HitlRequest, hitl_id)
    assert cancelled == 1
    assert stored.status == "expired"
    assert stored.resolved_reason == "request_stopped"
    await db_session.refresh(message)
    assert message.meta["hitl_requests"][0]["resolved"] is True
    assert message.meta["hitl_requests"][0]["resolution"] == "cancelled"


@pytest.mark.asyncio
async def test_rejecting_unclaimed_granted_provider_approval_revokes_grant(
    db_session,
) -> None:
    from packages.core.ai.runtime.approval_service import (
        register_provider_runtime_approval,
        resolve_runtime_approval_turn,
    )
    from packages.core.models.base import generate_ulid
    from packages.core.models.hitl_request import HitlRequest
    from packages.core.models.task import Conversation, Message
    from packages.core.models.user import User

    entity_id = generate_ulid()
    user_id = generate_ulid()
    conversation_id = generate_ulid()
    db_session.add(User(
        id=user_id,
        entity_id=entity_id,
        email=f"{user_id}@example.com",
        password_hash="x",
        role="owner",
    ))
    db_session.add(Conversation(
        id=conversation_id,
        entity_id=entity_id,
        user_id=user_id,
        title="Reject recoverable provider approval",
    ))
    await db_session.flush()
    await _initialize_provider_approval_owner(db_session, entity_id=entity_id, user_id=user_id)
    hitl_data = await register_provider_runtime_approval(
        db_session,
        conversation_id=conversation_id,
        entity_id=entity_id,
        user_id=user_id,
        request=_chrome_approval_request(approval_id="reject-granted"),
    )
    hitl_id = hitl_data["hitl"]["id"]
    message = Message(
        id=generate_ulid(),
        conversation_id=conversation_id,
        role="assistant",
        content="",
        message_kind="hitl_request",
        meta={"hitl_requests": [{"id": hitl_id, "type": "approval"}]},
    )
    db_session.add(message)
    await db_session.flush()
    approved = await resolve_runtime_approval_turn(
        db_session,
        conversation_id=conversation_id,
        entity_id=entity_id,
        user_id=user_id,
        hitl_id=hitl_id,
        action="approve",
    )
    assert approved.runtime_metadata is not None

    rejected = await resolve_runtime_approval_turn(
        db_session,
        conversation_id=conversation_id,
        entity_id=entity_id,
        user_id=user_id,
        hitl_id=hitl_id,
        action="reject",
    )

    stored = await db_session.get(HitlRequest, hitl_id)
    assert rejected.runtime_metadata == {
        "approval_kind": "runtime_terminal",
        "approval_outcome": "rejected",
    }
    assert stored.status == "denied"
    assert stored.resolved_reason == "provider_approval_revoked"
    await db_session.refresh(message)
    assert message.meta["hitl_requests"][0]["resolved"] is True
    assert message.meta["hitl_requests"][0]["resolution"] == "reject"


@pytest.mark.asyncio
async def test_cancelling_claimed_provider_approval_keeps_ambiguous_grant(
    db_session,
) -> None:
    from packages.core.ai.runtime.approval_service import (
        cancel_pending_runtime_approvals,
        prepare_provider_runtime_approval_execution,
        register_provider_runtime_approval,
        resolve_runtime_approval_turn,
        settle_provider_runtime_approval_execution,
    )
    from packages.core.models.base import generate_ulid
    from packages.core.models.hitl_request import HitlRequest
    from packages.core.models.task import Conversation
    from packages.core.models.user import User

    entity_id = generate_ulid()
    user_id = generate_ulid()
    conversation_id = generate_ulid()
    db_session.add(User(
        id=user_id,
        entity_id=entity_id,
        email=f"{user_id}@example.com",
        password_hash="x",
        role="owner",
    ))
    db_session.add(Conversation(
        id=conversation_id,
        entity_id=entity_id,
        user_id=user_id,
        title="Do not cancel claimed provider approval",
    ))
    await db_session.flush()
    await _initialize_provider_approval_owner(db_session, entity_id=entity_id, user_id=user_id)
    request = _chrome_approval_request(approval_id="claimed-provider")
    hitl_data = await register_provider_runtime_approval(
        db_session,
        conversation_id=conversation_id,
        entity_id=entity_id,
        user_id=user_id,
        request=request,
    )
    hitl_id = hitl_data["hitl"]["id"]
    resolution = await resolve_runtime_approval_turn(
        db_session,
        conversation_id=conversation_id,
        entity_id=entity_id,
        user_id=user_id,
        hitl_id=hitl_id,
        action="approve",
    )
    assert await settle_provider_runtime_approval_execution(
        db_session,
        runtime_metadata=resolution.runtime_metadata,
        entity_id=entity_id,
        conversation_id=conversation_id,
        tool_name=request["confirmation_tool"],
        arguments=request["confirmation_arguments"],
        result={
            "ok": True,
            "status": "approved",
            "approvalToken": "claimed-token",
        },
    )
    retry_arguments = {
        **request["retry_arguments"],
        "approvalToken": "claimed-token",
    }
    claim = await prepare_provider_runtime_approval_execution(
        db_session,
        runtime_metadata=resolution.runtime_metadata,
        entity_id=entity_id,
        conversation_id=conversation_id,
        tool_name=request["retry_tool"],
        arguments=retry_arguments,
    )
    assert claim.changed is True

    cancelled = await cancel_pending_runtime_approvals(
        db_session,
        conversation_id=conversation_id,
        entity_id=entity_id,
        user_id=user_id,
        hitl_ids=[hitl_id],
    )

    stored = await db_session.get(HitlRequest, hitl_id)
    assert cancelled == 0
    assert stored.status == "granted"
    assert stored.resolved_reason == "provider_action_claimed"
    rejected = await resolve_runtime_approval_turn(
        db_session,
        conversation_id=conversation_id,
        entity_id=entity_id,
        user_id=user_id,
        hitl_id=hitl_id,
        action="reject",
    )
    assert rejected.runtime_metadata is None
    assert "already approved" in rejected.message
    assert stored.status == "granted"


def test_runtime_chat_context_contains_no_provider_specific_approval_resume() -> None:
    from pathlib import Path

    source = Path("packages/core/services/runtime_chat_context.py").read_text()
    assert "_is_chrome_pending_action_confirmation" not in source
    assert "_chrome_approval_resume_for_confirmation" not in source
    assert "__manor_chrome_retry" not in source
    assert "approval_resume_guidance" in source


def test_generic_runtime_modules_do_not_embed_chrome_approval_projection() -> None:
    from pathlib import Path

    streams_source = Path("packages/core/ai/runtime/streams.py").read_text()
    approvals_source = Path("packages/core/ai/runtime/approvals.py").read_text()
    assert "_CHROME_APPROVAL_PROJECTION_KEYS" not in streams_source
    assert "_chrome_retry_arguments_for_persistence" not in streams_source
    assert "is_chrome_approval" not in approvals_source


@pytest.mark.asyncio
async def test_non_stream_nested_provider_approval_is_persisted_as_hitl(
    db_session,
) -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, patch

    from sqlalchemy import select

    from packages.core.ai.agentic_loop import AgenticResult
    from packages.core.ai.runtime.streams import runtime_skill_nested_tool_callbacks
    from packages.core.models.base import generate_ulid
    from packages.core.models.task import Conversation, Message
    from packages.core.services.chat_service import run_chat_message

    entity_id = generate_ulid()
    user_id = generate_ulid()
    conversation_id = generate_ulid()
    db_session.add(
        Conversation(
            id=conversation_id,
            entity_id=entity_id,
            user_id=user_id,
            title="Non-stream provider approval",
        )
    )
    await db_session.flush()
    await _initialize_provider_approval_owner(db_session, entity_id=entity_id, user_id=user_id)
    request = _chrome_approval_request()
    result_payload = json.dumps(
        {
            "status": "approval_required",
            "approval_required": True,
            "provider": "chrome",
            "approvalId": request["provider_approval_id"],
            "expires_at": request["expires_at"],
            "target_label": request["target_label"],
            "retry_action": {
                "name": request["retry_tool"],
                "arguments": request["retry_arguments"],
            },
        }
    )

    async def fake_context(*_args, **_kwargs):
        return (
            "system",
            [{"type": "function", "function": {"name": "invoke_skill"}}],
            [],
            SimpleNamespace(
                workspace_id=None,
                task_id=None,
                runtime_envelope=None,
                tool_profile=None,
                allowed_tool_names=None,
                user=None,
                entity=None,
            ),
        )

    async def fake_loop(**_kwargs):
        on_start, on_end = runtime_skill_nested_tool_callbacks(
            skill_name="chrome",
            invoke_skill_args={"skill": "chrome"},
        )
        assert on_start is not None and on_end is not None
        on_start(request["retry_tool"], request["retry_arguments"])
        on_end(
            request["retry_tool"],
            result_payload,
            42,
            request["retry_arguments"],
        )
        return AgenticResult(
            content="Waiting for approval.",
            messages=[],
            usage={},
            rounds=1,
            tool_calls_made=["invoke_skill"],
        )

    with (
        patch(
            "packages.core.services.chat_service.resolve_runtime_chat_context",
            new=fake_context,
        ),
        patch(
            "packages.core.services.chat_service.runtime_execute_chat_agent_loop",
            new=fake_loop,
        ),
        patch(
            "packages.core.services.model_resolver.resolve_model_for_user",
            new=AsyncMock(return_value="openai/gpt-5.5"),
        ),
        patch(
            "packages.core.services.chat_service.record_chat_llm_usage",
            new=AsyncMock(),
        ),
        patch(
            "packages.core.services.chat_service.resolve_author_subscription_id",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "packages.core.services.chat_service.record_chat_runtime_learning",
            new=AsyncMock(return_value=[]),
        ),
        patch(
            "packages.core.services.chat_service.schedule_learning_candidate_applies",
            new=AsyncMock(),
        ),
        patch(
            "packages.core.services.chat_service.runtime_persist_chat_runtime_events",
            new=AsyncMock(),
        ),
    ):
        result = await run_chat_message(
            "Publish the prepared post",
            conversation_id,
            entity_id=entity_id,
            user_id=user_id,
            db=db_session,
        )

    assert result["hitl_requests"]
    hitl_id = result["hitl_requests"][0]["id"]
    message = (
        await db_session.execute(
            select(Message).where(
                Message.conversation_id == conversation_id,
                Message.role == "assistant",
            )
        )
    ).scalar_one()
    assert message.message_kind == "hitl_request"
    assert message.meta["hitl_requests"][0]["id"] == hitl_id
