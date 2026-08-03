import base64
import importlib
import json
import logging

import pytest

from packages.core.ai.agentic_loop import _compact_tool_result_for_context
from packages.core.ai.agentic_loop import _build_minimal_chrome_blocker_result
from packages.core.ai.runtime.harness import runtime_execute_agentic_loop


@pytest.mark.asyncio
async def test_chrome_screenshot_is_ephemeral_multimodal_input(monkeypatch):
    screenshot_data_url = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAAB"
    llm_calls = 0
    second_call_messages = []
    callback_results = []

    async def fake_completion(messages, tools, **kwargs):
        nonlocal llm_calls, second_call_messages
        llm_calls += 1
        if llm_calls == 1:
            return (
                "",
                [{"id": "shot-1", "name": "mcp__chrome__screenshot", "arguments": {"tabId": 42}}],
                {"prompt": 1, "completion": 1, "total": 2},
            )
        second_call_messages = messages
        return "I can see the page.", None, {"prompt": 1, "completion": 1, "total": 2}

    async def fake_executor(name, args):
        return json.dumps(
            {
                "ok": True,
                "tabId": 42,
                "screenshot": {"dataUrl": screenshot_data_url},
            }
        )

    def on_tool_end(name, result, *_args):
        callback_results.append(str(result))

    loop_module = importlib.import_module("packages.core.ai.agentic_loop")
    monkeypatch.setattr(loop_module, "runtime_execute_agentic_round_tool_completion", fake_completion)
    completions_module = importlib.import_module("packages.core.ai.runtime.completions")
    billing_module = importlib.import_module("packages.core.ai.runtime.billing")

    async def fake_completion_route(**_kwargs):
        return None, {}, None

    monkeypatch.setattr(completions_module, "runtime_resolve_text_completion_route", fake_completion_route)
    monkeypatch.setattr(billing_module, "runtime_ensure_billing_context", lambda *_args, **_kwargs: None)

    result = await runtime_execute_agentic_loop(
        runtime_envelope=None,
        system_prompt="Use browser observations.",
        user_message="Inspect the current page.",
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "mcp__chrome__screenshot",
                    "parameters": {"type": "object"},
                },
            }
        ],
        entity_id="entity-1",
        agent_id=None,
        tool_executor=fake_executor,
        on_tool_end=on_tool_end,
        max_rounds=3,
    )

    image_blocks = [
        block
        for message in second_call_messages
        if message.get("role") == "user" and isinstance(message.get("content"), list)
        for block in message["content"]
        if isinstance(block, dict) and block.get("type") == "image_url"
    ]
    assert image_blocks[0]["image_url"]["url"] == screenshot_data_url
    assert "untrusted browser screenshot" in json.dumps(second_call_messages, ensure_ascii=False).lower()
    assert screenshot_data_url not in json.dumps(result.messages, ensure_ascii=False)
    assert all(screenshot_data_url not in callback for callback in callback_results)


@pytest.mark.asyncio
async def test_read_file_image_is_ephemeral_multimodal_input(monkeypatch):
    image_data_url = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAAB"
    llm_calls = 0
    second_call_messages = []
    callback_results = []

    async def fake_completion(messages, tools, **kwargs):
        nonlocal llm_calls, second_call_messages
        llm_calls += 1
        if llm_calls == 1:
            return (
                "",
                [{"id": "read-1", "name": "read_file", "arguments": {"path": "frame.png"}}],
                {"prompt": 1, "completion": 1, "total": 2},
            )
        second_call_messages = messages
        return "I can inspect the frame.", None, {"prompt": 1, "completion": 1, "total": 2}

    async def fake_executor(name, args):
        return json.dumps(
            {
                "path": args["path"],
                "image": {
                    "data_url": image_data_url,
                    "mime_type": "image/png",
                    "bytes": 24,
                    "delivery": "ephemeral_multimodal",
                },
            }
        )

    def on_tool_end(name, result, *_args):
        callback_results.append(str(result))

    loop_module = importlib.import_module("packages.core.ai.agentic_loop")
    monkeypatch.setattr(loop_module, "runtime_execute_agentic_round_tool_completion", fake_completion)
    completions_module = importlib.import_module("packages.core.ai.runtime.completions")
    billing_module = importlib.import_module("packages.core.ai.runtime.billing")

    async def fake_completion_route(**_kwargs):
        return None, {}, None

    monkeypatch.setattr(completions_module, "runtime_resolve_text_completion_route", fake_completion_route)
    monkeypatch.setattr(billing_module, "runtime_ensure_billing_context", lambda *_args, **_kwargs: None)

    result = await runtime_execute_agentic_loop(
        runtime_envelope=None,
        system_prompt="Inspect durable QA frames.",
        user_message="Read the frame and verify visible content.",
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "read_file",
                    "parameters": {"type": "object"},
                },
            }
        ],
        entity_id="entity-1",
        agent_id=None,
        tool_executor=fake_executor,
        on_tool_end=on_tool_end,
        max_rounds=3,
    )

    image_blocks = [
        block
        for message in second_call_messages
        if message.get("role") == "user" and isinstance(message.get("content"), list)
        for block in message["content"]
        if isinstance(block, dict) and block.get("type") == "image_url"
    ]
    assert image_blocks[0]["image_url"]["url"] == image_data_url
    assert "untrusted local image" in json.dumps(second_call_messages, ensure_ascii=False).lower()
    assert image_data_url not in json.dumps(result.messages, ensure_ascii=False)
    assert all(image_data_url not in callback for callback in callback_results)


def test_chrome_screenshot_rejects_invalid_webp_without_leaking_base64():
    loop_module = importlib.import_module("packages.core.ai.agentic_loop")
    invalid_data_url = "data:image/webp;base64," + base64.b64encode(
        b"RIFF\x10\x00\x00\x00NOPEinvalid-image"
    ).decode("ascii")
    raw_result = json.dumps(
        {"ok": True, "screenshot": {"dataUrl": invalid_data_url}}
    )

    observation, safe_result = loop_module._browser_screenshot_observation(
        "mcp__chrome__screenshot",
        raw_result,
    )

    assert observation is None
    assert invalid_data_url not in safe_result
    assert json.loads(safe_result)["screenshot"] == {
        "available": False,
        "delivery": "rejected",
        "reason": "invalid_image_signature",
    }


@pytest.mark.asyncio
async def test_chrome_task_ledger_survives_structural_compaction():
    loop_module = importlib.import_module("packages.core.ai.agentic_loop")
    messages = [
        {"role": "system", "content": "Operate Chrome purposefully."},
        {"role": "user", "content": "Use Chrome to draft an article and ask before publishing."},
    ]
    tool_results = [
        (
            "mcp__chrome__open_or_reuse",
            {
                "ok": True,
                "status": "opened",
                "tabId": 42,
                "groupId": "conversation-123",
                "url": "https://example.com/editor",
            },
        ),
        (
            "mcp__chrome__read_page",
            {
                "ok": True,
                "status": "read_page",
                "tabId": 42,
                "groupId": "conversation-123",
                "url": "https://example.com/editor",
                "editable_refs_count": 2,
            },
        ),
        (
            "mcp__chrome__fill_or_select",
            {
                "ok": True,
                "status": "filled",
                "tabId": 42,
                "groupId": "conversation-123",
                "url": "https://example.com/editor",
                "ref": "f7:e1",
                "state_hint": {"label": "Title", "next": "use_post_action_state"},
            },
        ),
        (
            "mcp__chrome__fill_or_select",
            {
                "ok": True,
                "status": "filled",
                "tabId": 42,
                "groupId": "conversation-123",
                "url": "https://example.com/editor",
                "ref": "f7:e2",
                "state_hint": {"label": "Article content", "next": "use_post_action_state"},
            },
        ),
    ]
    tool_results.extend(
        (
            "mcp__chrome__read_page" if index % 2 else "mcp__chrome__wait",
            {
                "ok": True,
                "status": "read_page" if index % 2 else "matched",
                "tabId": 42,
                "groupId": "conversation-123",
                "url": "https://example.com/editor",
            },
        )
        for index in range(5, 20)
    )
    tool_results.append(
        (
            "mcp__chrome__click_element",
            {
                "ok": False,
                "status": "approval_required",
                "reason": "side_effect_action_requires_confirmation",
                "approvalId": "approval-1",
                "tabId": 42,
                "groupId": "conversation-123",
                "url": "https://example.com/editor",
                "ref": "f7:e9",
                "target": {"label": "Publish", "role": "button"},
            },
        )
    )

    for index, (name, payload) in enumerate(tool_results):
        call_id = f"call-{index}"
        messages.append(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {"name": name, "arguments": "{}"},
                    }
                ],
            }
        )
        messages.append(
            {
                "role": "tool",
                "tool_call_id": call_id,
                "content": json.dumps(payload),
            }
        )

    compacted = await loop_module._compact_messages(messages, None, 0.2)
    ledger_message = next(
        message["content"]
        for message in compacted
        if message.get("role") == "user" and "[Chrome task ledger]" in str(message.get("content"))
    )
    ledger_json = ledger_message.split("[Chrome task ledger]\n", 1)[1].splitlines()[0]
    ledger = json.loads(ledger_json)

    assert ledger["goal"] == "Use Chrome to draft an article and ask before publishing."
    assert ledger["stage"] == "awaiting_confirmation"
    assert ledger["tab_id"] == 42
    assert ledger["group_id"] == "conversation-123"
    assert ledger["url"] == "https://example.com/editor"
    assert ledger["pending_action"] == "mcp__chrome__click_element"
    assert ledger["approval_id"] == "approval-1"
    assert ledger["target_label"] == "Publish"
    assert ledger["next"] == "mcp__chrome__confirm_action"


@pytest.mark.asyncio
async def test_chrome_task_ledger_preserves_durable_capture_receipts():
    loop_module = importlib.import_module("packages.core.ai.agentic_loop")
    messages = [
        {"role": "system", "content": "Capture one approved product scene."},
        {"role": "user", "content": "Capture one PNG and one WebM for scene-3."},
    ]
    tool_results = [
        (
            "mcp__chrome__screenshot",
            {
                "ok": True,
                "status": "captured",
                "artifact_ready": True,
                "tabId": 42,
                "document_id": "png-document",
                "screenshot_path": "Screenshots/scene-3.png",
                "knowledge_path": "Knowledge/Screenshots/scene-3.png",
                "mime_type": "image/png",
                "file_size": 322848,
                "sha256": "png-sha256",
            },
        ),
        (
            "mcp__chrome__stop_tab_recording",
            {
                "ok": True,
                "status": "completed",
                "tabId": 42,
                "recording_id": "recording-3",
                "document_id": "webm-document",
                "knowledge_path": "Knowledge/Recordings/scene-3.webm",
                "mime_type": "video/webm",
                "size": 2203981,
                "sha256": "webm-sha256",
                "duration_ms": 20087,
            },
        ),
    ]
    tool_results.extend(
        (
            "mcp__chrome__read_page",
            {
                "ok": True,
                "status": "read_page",
                "tabId": 42,
                "snapshot_id": f"snap-{index}",
            },
        )
        for index in range(20)
    )

    for index, (name, payload) in enumerate(tool_results):
        call_id = f"capture-{index}"
        messages.extend([
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "id": call_id,
                    "type": "function",
                    "function": {"name": name, "arguments": "{}"},
                }],
            },
            {"role": "tool", "tool_call_id": call_id, "content": json.dumps(payload)},
        ])

    compacted = await loop_module._compact_messages(messages, None, 0.2)
    ledger_message = next(
        message["content"]
        for message in compacted
        if message.get("role") == "user" and "[Chrome task ledger]" in str(message.get("content"))
    )
    ledger_json = ledger_message.split("[Chrome task ledger]\n", 1)[1].splitlines()[0]
    receipts = json.loads(ledger_json)["capture_receipts"]

    assert receipts == [
        {
            "tool": "mcp__chrome__screenshot",
            "document_id": "png-document",
            "path": "Knowledge/Screenshots/scene-3.png",
            "mime_type": "image/png",
            "file_size": 322848,
            "sha256": "png-sha256",
        },
        {
            "tool": "mcp__chrome__stop_tab_recording",
            "document_id": "webm-document",
            "path": "Knowledge/Recordings/scene-3.webm",
            "mime_type": "video/webm",
            "file_size": 2203981,
            "sha256": "webm-sha256",
            "duration_ms": 20087,
            "recording_id": "recording-3",
        },
    ]


@pytest.mark.asyncio
async def test_structural_compaction_keeps_latest_chrome_result_as_valid_rich_json():
    loop_module = importlib.import_module("packages.core.ai.agentic_loop")
    messages = [
        {"role": "system", "content": "Use current Chrome refs."},
        {"role": "user", "content": "Open Gmail and draft a reply."},
    ]
    for index in range(20):
        call_id = f"read-{index}"
        payload = {
            "ok": True,
            "action_key": "read_page",
            "snapshot_id": f"snap-{index}",
            "pageContent": "\n".join(f'- button "Action {item}" [ref=e{item}]' for item in range(90)),
            "semantic_refs": [
                {
                    "ref": f"e{item}",
                    "role": "button",
                    "label": "Reply" if item == 88 else f"Action {item}",
                    "selector": f"button:nth-of-type({item + 1})",
                    "aria_label": "Reply" if item == 88 else f"Action {item}",
                }
                for item in range(90)
            ],
            "next_actions": [{"ref": "e88", "role": "button", "label": "Reply"}],
        }
        messages.extend([
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "id": call_id,
                    "type": "function",
                    "function": {"name": "mcp__chrome__read_page", "arguments": "{}"},
                }],
            },
            {"role": "tool", "tool_call_id": call_id, "content": json.dumps(payload)},
        ])

    compacted = await loop_module._compact_messages(messages, None, 0.2)
    latest = next(message for message in reversed(compacted) if message.get("role") == "tool")
    parsed = json.loads(latest["content"])

    assert parsed["snapshot_id"] == "snap-19"
    assert isinstance(parsed.get("semantic_refs"), list)
    assert any(ref.get("label") == "Reply" and ref.get("selector") for ref in parsed["semantic_refs"])


def test_chrome_read_page_compaction_preserves_model_action_context():
    payload = {
        "ok": True,
        "status": "snapshot",
        "driver": "chrome-extension",
        "snapshot_contract": "codex_style_dom_snapshot_v1",
        "dom_snapshot": "\n".join(
            f'- link "Result {index} with a long label for context" node_id=e{index}' for index in range(100)
        ),
        "snapshot": "legacy snapshot duplicate that should not crowd out structured fields",
        "tab": {"id": 42, "url": "https://news.google.com/search?q=Elon%20Musk"},
        "tabId": 42,
        "url": "https://news.google.com/search?q=Elon%20Musk",
        "title": "Elon Musk - Google News",
        "page_kind": "search_results",
        "observation_quality": "medium",
        "visual_fallback_recommended": True,
        "visual_fallback_reasons": ["ambiguous_controls"],
        "page_text": " ".join(f"Visible page sentence {index} about Musk news." for index in range(180)),
        "visible_text": "Elon Musk latest news\nReuters\nTesla\nSpaceX",
        "page_text_sample": "Elon Musk latest news Reuters Tesla SpaceX",
        "content_summary": {
            "title": "Elon Musk - Google News",
            "description": "Latest articles and search result text",
            "main_content_text": " ".join(f"Main content block {index}" for index in range(150)),
            "main_content_blocks": [
                {"text": f"Main content block {index}", "selector": f"article:nth-of-type({index})"}
                for index in range(30)
            ],
            "structured_data": [
                {
                    "type": "NewsArticle",
                    "headline": "Latest Musk news item 1",
                    "author": "Jane Reporter",
                    "publisher": "Example News",
                    "published_time": "2026-06-17T04:00:00Z",
                }
            ],
        },
        "visible_text_blocks": [
            {"text": f"Visible block {index}", "selector": f"div:nth-of-type({index})"} for index in range(50)
        ],
        "viewport": {"width": 1280, "height": 720},
        "refs_count": 240,
        "editable_refs_count": 1,
        "result_candidates": [
            {
                "rank": index,
                "node_id": f"e{index}",
                "candidate_kind": "content_result",
                "title": f"Latest Musk news item {index}",
                "source": "Reuters",
                "published_time": "2026-06-17T04:00:00Z",
                "relative_time": "2 hours ago",
                "snippet": "Tesla and SpaceX details from the visible result card",
                "evidence_text": "Latest Musk news item Reuters 2 hours ago Tesla and SpaceX details from the visible result card",
                "href": f"https://example.com/musk-news-{index}",
                "context": "Reuters source and article summary",
            }
            for index in range(1, 16)
        ],
        "search_refinement_candidates": [
            {
                "rank": 1,
                "node_id": "e201",
                "candidate_kind": "search_refinement",
                "title": "Elon Musk Tesla news",
                "href": "https://news.google.com/search?q=Elon+Musk+Tesla",
                "reason": "search refinement URL candidate",
            }
        ],
        "search_discovery_candidates": [
            {
                "rank": 1,
                "node_id": "e202",
                "candidate_kind": "search_discovery",
                "title": "Technology",
                "href": "https://news.google.com/topics/technology",
                "reason": "search/filter/navigation URL candidate",
            }
        ],
        "input_candidates": [
            {
                "rank": 1,
                "node_id": "e101",
                "role": "searchbox",
                "label": "Search",
                "name": "q",
                "value": "Elon Musk",
                "required": True,
                "max_length": 120,
                "pattern": "[A-Za-z ]+",
                "description": "Search terms",
                "valid": False,
                "validation_message": "Please use letters and spaces only.",
                "validity_flags": ["patternMismatch"],
            }
        ],
        "choice_candidates": [{"rank": 1, "node_id": "e102", "role": "combobox", "label": "Sort"}],
        "upload_candidates": [{"rank": 1, "selector": "input[type=file]", "supported": True}],
        "form_candidates": [
            {
                "rank": 1,
                "selector": "form#post",
                "fields": [
                    {
                        "node_id": "e101",
                        "label": "Title",
                        "name": "title",
                        "value": "Draft",
                        "required": True,
                        "min_length": 10,
                        "max_length": 80,
                        "description": "Use a concise title.",
                        "valid": False,
                        "validation_message": "Title must be at least 10 characters.",
                        "validity_flags": ["tooShort"],
                    }
                ],
                "invalid_fields_count": 1,
                "invalid_fields": [
                    {
                        "kind": "field",
                        "label": "Title",
                        "node_id": "e101",
                        "name": "title",
                        "validation_message": "Title must be at least 10 characters.",
                        "validity_flags": ["tooShort"],
                    }
                ],
                "required_fields_count": 2,
                "completed_required_fields_count": 1,
                "missing_required_fields_count": 1,
                "missing_required_fields": [{"kind": "upload", "label": "Business license", "selector": "#license"}],
                "submit_ready": False,
                "form_progress": {
                    "required": 2,
                    "completed": 1,
                    "missing": 1,
                    "submit_ready": False,
                },
            }
        ],
        "submit_candidates": [{"rank": 1, "node_id": "e103", "label": "Submit"}],
        "actionable_refs": [
            {
                "rank": 1,
                "node_id": "e104",
                "role": "tab",
                "label": "News",
                "href": "https://example.com/search?type=news",
            }
        ],
        "node_candidates": [
            {"kind": "result", "node_id": "e1", "label": "Latest Musk news item 1"},
            {"kind": "upload", "node_id": "e-file", "label": "Upload"},
        ],
        "next_actions": [
            {
                "rank": 1,
                "tool": "click_node",
                "node_id": "e1",
                "candidate_kind": "navigation_url",
                "href": "https://news.google.com/search?q=Elon+Musk+SpaceX",
                "url": "https://news.google.com/search?q=Elon+Musk+SpaceX",
                "reason": "open URL candidate #1 when it matches the user goal",
            },
            {"rank": 2, "tool": "upload", "selector": "input[type=file]", "reason": "upload when requested"},
            {
                "rank": 3,
                "tool": "click_node",
                "node_id": "e103",
                "submit_ready": False,
                "missing_required_fields": ["Business license"],
                "missing_required_fields_count": 1,
                "reason": "submit candidate blocked until missing required fields are completed: Business license",
            },
        ],
        "action_policy": "Snapshot again after every state-changing action.",
    }

    compacted = _compact_tool_result_for_context(
        "mcp__chrome__snapshot",
        json.dumps(payload, ensure_ascii=False),
        max_chars=4000,
    )
    parsed = json.loads(compacted)

    assert parsed["status"] == "read_page"
    assert parsed["driver"] == "chrome-extension"
    assert parsed["page_kind"] == "search_results"
    assert parsed["observation_quality"] == "medium"
    assert parsed["visual_fallback_recommended"] is True
    assert parsed["visual_fallback_reasons"] == ["ambiguous_controls"]
    assert parsed["tabId"] == 42
    assert parsed["content_summary"]["structured_data"][0]["type"] == "NewsArticle"
    assert parsed["content_summary"]["structured_data"][0]["author"] == "Jane Reporter"
    assert parsed["result_candidates"][0]["node_id"] == "e1"
    assert parsed["result_candidates"][0]["href"] == "https://example.com/musk-news-1"
    assert parsed["result_candidates"][0]["source"] == "Reuters"
    assert parsed["result_candidates"][0]["published_time"] == "2026-06-17T04:00:00Z"
    assert parsed["result_candidates"][0]["relative_time"] == "2 hours ago"
    assert parsed["result_candidates"][0]["snippet"] == "Tesla and SpaceX details from the visible result card"
    assert "Latest Musk news item" in parsed["result_candidates"][0]["evidence_text"]
    assert parsed["search_refinement_candidates"][0]["node_id"] == "e201"
    assert parsed["search_refinement_candidates"][0]["candidate_kind"] == "search_refinement"
    assert parsed["search_discovery_candidates"][0]["node_id"] == "e202"
    assert parsed["search_discovery_candidates"][0]["candidate_kind"] == "search_discovery"
    assert parsed["input_candidates"][0]["node_id"] == "e101"
    assert parsed["input_candidates"][0]["name"] == "q"
    assert parsed["input_candidates"][0]["required"] is True
    assert parsed["input_candidates"][0]["max_length"] == 120
    assert parsed["input_candidates"][0]["pattern"] == "[A-Za-z ]+"
    assert parsed["input_candidates"][0]["description"] == "Search terms"
    assert parsed["input_candidates"][0]["valid"] is False
    assert parsed["input_candidates"][0]["validation_message"] == "Please use letters and spaces only."
    assert parsed["input_candidates"][0]["validity_flags"] == ["patternMismatch"]
    assert parsed["upload_candidates"][0]["selector"] == "input[type=file]"
    assert parsed["form_candidates"][0]["selector"] == "form#post"
    assert parsed["form_candidates"][0]["fields"][0]["name"] == "title"
    assert parsed["form_candidates"][0]["fields"][0]["min_length"] == 10
    assert parsed["form_candidates"][0]["fields"][0]["max_length"] == 80
    assert parsed["form_candidates"][0]["fields"][0]["description"] == "Use a concise title."
    assert parsed["form_candidates"][0]["fields"][0]["valid"] is False
    assert parsed["form_candidates"][0]["fields"][0]["validation_message"] == "Title must be at least 10 characters."
    assert parsed["form_candidates"][0]["fields"][0]["validity_flags"] == ["tooShort"]
    assert parsed["form_candidates"][0]["invalid_fields_count"] == 1
    assert (
        parsed["form_candidates"][0]["invalid_fields"][0]["validation_message"]
        == "Title must be at least 10 characters."
    )
    assert parsed["form_candidates"][0]["invalid_fields"][0]["validity_flags"] == ["tooShort"]
    assert parsed["form_candidates"][0]["required_fields_count"] == 2
    assert parsed["form_candidates"][0]["completed_required_fields_count"] == 1
    assert parsed["form_candidates"][0]["missing_required_fields_count"] == 1
    assert parsed["form_candidates"][0]["missing_required_fields"][0]["label"] == "Business license"
    assert parsed["form_candidates"][0]["submit_ready"] is False
    assert parsed["form_candidates"][0]["form_progress"]["missing"] == 1
    assert parsed["submit_candidates"][0]["node_id"] == "e103"
    assert parsed["node_candidates"][0]["kind"] == "result"
    assert parsed["next_actions"][0]["tool"] == "click_element"
    assert parsed["next_actions"][0]["candidate_kind"] == "navigation_url"
    assert parsed["next_actions"][0]["href"] == "https://news.google.com/search?q=Elon+Musk+SpaceX"
    assert parsed["next_actions"][0]["url"] == "https://news.google.com/search?q=Elon+Musk+SpaceX"
    assert "read_page again" in parsed["action_policy"]
    assert parsed["_tool_result_truncated"]["tool"] == "mcp__chrome__read_page"


def test_chrome_read_page_compaction_retains_semantic_page_action_with_larger_budget():
    loop_module = importlib.import_module("packages.core.ai.agentic_loop")
    payload = {
        "ok": True,
        "status": "read_page",
        "driver": "chrome-extension",
        "snapshot_id": "snap-feed",
        "tabId": 42,
        "url": "https://example.test/feed",
        "title": "Feed",
        "dom_snapshot": "\n".join(
            ['- link "Create post" [ref=e1] href="/compose"']
            + [
                f'- link "Feed item {index} with verbose background content" [ref=e{index + 1}]'
                for index in range(1, 260)
            ]
        ),
        "semantic_refs": [
            {
                "ref": "e1",
                "role": "link",
                "label": "Create post",
                "href": "/compose",
                "clickable": True,
                "in_viewport": True,
            },
            *[
                {
                    "ref": f"e{index + 1}",
                    "role": "link",
                    "label": f"Feed item {index}",
                    "href": f"/item/{index}",
                    "clickable": True,
                    "in_viewport": True,
                }
                for index in range(1, 96)
            ],
        ],
        "next_actions": [
            {
                "tool": "click_element",
                "ref": f"e{index + 1}",
                "label": f"Feed item {index}",
            }
            for index in range(1, 20)
        ],
    }

    compacted = _compact_tool_result_for_context(
        "mcp__chrome__read_page",
        json.dumps(payload),
    )
    parsed = json.loads(compacted)

    assert loop_module.CHROME_TOOL_RESULT_MAX_CHARS == 12000
    assert len(compacted) <= loop_module.CHROME_TOOL_RESULT_MAX_CHARS
    assert any(item.get("label") == "Create post" for item in parsed["semantic_refs"])
    assert "Create post" in parsed["dom_snapshot"]


def test_chrome_read_page_compaction_retains_late_page_content_action():
    loop_module = importlib.import_module("packages.core.ai.agentic_loop")
    background_refs = [
        {
            "ref": f"e{index}",
            "role": "link",
            "label": f"Background navigation {index}",
            "href": f"/navigation/{index}",
            "clickable": True,
            "in_viewport": True,
        }
        for index in range(1, 27)
    ]
    compose_ref = {
        "ref": "e27",
        "role": "link",
        "label": "Create post",
        "href": "/preload/sharebox/",
        "clickable": True,
        "in_viewport": True,
    }
    feed_refs = [
        {
            "ref": f"e{index}",
            "role": "link",
            "label": f"Feed item {index}",
            "href": f"/feed/{index}",
            "clickable": True,
            "in_viewport": True,
        }
        for index in range(28, 97)
    ]
    page_lines = [
        *(f'- link "Background navigation {index}" [ref=e{index}]' for index in range(1, 27)),
        '- link "Create post" [ref=e27] href="/preload/sharebox/"',
        *(f'- link "Feed item {index}" [ref=e{index}]' for index in range(28, 260)),
    ]
    payload = {
        "ok": True,
        "status": "read_page",
        "driver": "chrome-extension",
        "snapshot_id": "snap-real-feed",
        "tabId": 42,
        "url": "https://example.test/feed",
        "title": "Feed",
        "pageContent": "\n".join(page_lines),
        "semantic_refs": [*background_refs, compose_ref, *feed_refs],
        "candidate_summary": {
            "counts": {"semantic_refs": 96, "next_actions": 20},
            "semantic_refs": [*background_refs, compose_ref, *feed_refs],
            "next_actions": [
                {"tool": "click_element", "ref": f"e{index}", "label": f"Feed item {index}"}
                for index in range(28, 48)
            ],
        },
        "content_summary": {
            "main_content_text": "Background feed content. " * 600,
            "main_content_blocks": [
                {"text": f"Feed block {index}", "selector": f"article:nth-child({index})"}
                for index in range(40)
            ],
        },
        "result_candidates": [
            {
                "node_id": f"e{index}",
                "title": f"Feed item {index}",
                "snippet": "Background feed result with verbose supporting content",
                "href": f"/feed/{index}",
            }
            for index in range(28, 48)
        ],
        "next_actions": [
            {"tool": "click_element", "ref": f"e{index}", "label": f"Feed item {index}"}
            for index in range(28, 48)
        ],
    }

    compacted = _compact_tool_result_for_context(
        "mcp__chrome__read_page",
        json.dumps(payload),
    )
    parsed = json.loads(compacted)

    assert len(compacted) <= loop_module.CHROME_TOOL_RESULT_MAX_CHARS
    assert any(item.get("ref") == "e27" for item in parsed["semantic_refs"])
    assert "Create post" in parsed["pageContent"]


def test_chrome_minimal_compaction_retains_dialog_semantic_refs():
    payload = {
        "ok": True,
        "status": "read_page",
        "driver": "chrome-extension",
        "snapshot_id": "snap-compose",
        "tabId": 42,
        "url": "https://example.test/feed",
        "title": "Feed",
        "semantic_refs": [
            *[
                {"ref": f"e{index}", "role": "link", "label": f"Background {index}", "clickable": True}
                for index in range(1, 30)
            ],
            {"ref": "e-editor", "role": "textbox", "label": "Draft body", "editable": True},
            {"ref": "e-submit", "role": "button", "label": "Publish", "clickable": True},
        ],
        "dialog_candidates": [
            {"label": "Compose", "field_refs": ["e-editor"], "submit_refs": ["e-submit"]}
        ],
        "page_text": "Background feed content. " * 300,
    }

    compacted = _compact_tool_result_for_context(
        "mcp__chrome__read_page",
        json.dumps(payload),
        max_chars=1300,
    )
    parsed = json.loads(compacted)

    assert len(compacted) <= 1300
    assert [item["ref"] for item in parsed["semantic_refs"][:2]] == ["e-editor", "e-submit"]
    assert parsed["dialog_candidates"][0]["field_refs"] == ["e-editor"]
    assert parsed["dialog_candidates"][0]["submit_refs"] == ["e-submit"]


def test_chrome_compaction_preserves_gmail_semantic_ref_locator_context():
    payload = {
        "ok": True,
        "status": "read_page",
        "driver": "chrome-extension",
        "snapshot_id": "snap-gmail",
        "tabId": 42,
        "url": "https://mail.google.com/mail/u/0/#inbox",
        "title": "Inbox - Gmail",
        "pageContent": "\n".join(
            [
                '- searchbox "Search mail" [ref=e-search] placeholder="Search mail"',
                '- link "Alice, Project update, Jul 21" [ref=e-thread]',
                '- textbox "Message Body" [ref=e-editor]',
                '- button "Send (Ctrl-Enter)" [ref=e-send]',
                *[f'- link "Background mail {index}" [ref=e-bg-{index}]' for index in range(260)],
            ]
        ),
        "frame_summary": {
            "total": 2,
            "observed": 1,
            "skipped": 1,
            "hidden": 1,
            "unresolved": 0,
        },
        "skipped_frames": [
            {
                "frame_id": 687,
                "parent_frame_id": 0,
                "url": "https://ogs.google.com/widget/account",
                "status": "hidden",
                "reason": "display_none",
                "host_selector": "iframe#account",
                "host_visible": False,
                "secret_child_text": "must not survive compaction",
            }
        ],
        "semantic_refs": [
            {
                "ref": "e-search",
                "role": "searchbox",
                "label": "Search mail",
                "tag": "input",
                "type": "text",
                "name": "q",
                "placeholder": "Search mail",
                "autocomplete": "off",
                "input_mode": "search",
                "min": "2",
                "max": "200",
                "step": "2",
                "pattern": "[A-Za-z ]+",
                "min_length": 2,
                "max_length": 200,
                "aria_label": "Search mail",
                "title": "Search mail",
                "data_testid": "gmail-search",
                "selector": 'input[name="q"][aria-label="Search mail"]',
                "frame_selector": "iframe#mail-frame",
                "frame_url": "https://mail.google.com/mail/u/0/",
                "frame_id": 7,
                "coordinate_space": "frame",
                "container_label": "Mail search",
                "checked": False,
                "selected": True,
                "expanded": False,
                "editable": True,
                "in_viewport": True,
            },
            {
                "ref": "e-editor",
                "role": "textbox",
                "label": "Message Body",
                "tag": "div",
                "placeholder": "Message Body",
                "description": "Reply message editor",
                "aria_label": "Message Body",
                "selector": 'div[aria-label="Message Body"]',
                "editable": True,
                "in_viewport": True,
            },
            {
                "ref": "e-send",
                "role": "button",
                "label": "Send (Ctrl-Enter)",
                "tag": "div",
                "title": "Send (Ctrl-Enter)",
                "aria_label": "Send (Ctrl-Enter)",
                "selector": 'div[aria-label="Send (Ctrl-Enter)"]',
                "clickable": True,
                "in_viewport": True,
            },
        ],
    }

    compacted = _compact_tool_result_for_context(
        "mcp__chrome__read_page",
        json.dumps(payload),
    )
    parsed = json.loads(compacted)

    search = next(item for item in parsed["semantic_refs"] if item["ref"] == "e-search")
    assert search["selector"] == 'input[name="q"][aria-label="Search mail"]'
    assert search["name"] == "q"
    assert search["placeholder"] == "Search mail"
    assert search["aria_label"] == "Search mail"
    assert search["data_testid"] == "gmail-search"
    assert search["frame_url"] == "https://mail.google.com/mail/u/0/"
    assert search["frame_id"] == 7
    assert search["coordinate_space"] == "frame"
    assert search["min"] == "2"
    assert search["max"] == "200"
    assert search["step"] == "2"
    assert search["pattern"] == "[A-Za-z ]+"
    assert search["min_length"] == 2
    assert search["max_length"] == 200
    assert search["checked"] is False
    assert search["selected"] is True
    assert search["expanded"] is False
    assert parsed["frame_summary"] == payload["frame_summary"]
    assert parsed["skipped_frames"] == [
        {
            "frame_id": 687,
            "parent_frame_id": 0,
            "url": "https://ogs.google.com/widget/account",
            "status": "hidden",
            "reason": "display_none",
            "host_selector": "iframe#account",
            "host_visible": False,
        }
    ]

    minimal = json.loads(
        _compact_tool_result_for_context(
            "mcp__chrome__read_page",
            json.dumps(payload),
            max_chars=1700,
        )
    )
    minimal_search = next(item for item in minimal["semantic_refs"] if item["ref"] == "e-search")
    assert minimal_search["selector"] == 'input[name="q"][aria-label="Search mail"]'
    assert minimal_search["placeholder"] == "Search mail"
    assert minimal_search["aria_label"] == "Search mail"
    assert minimal_search["frame_url"] == "https://mail.google.com/mail/u/0/"
    assert minimal_search["frame_id"] == 7
    assert minimal_search["coordinate_space"] == "frame"
    assert minimal_search["pattern"] == "[A-Za-z ]+"
    assert minimal_search["min_length"] == 2
    assert minimal_search["max_length"] == 200
    assert minimal_search["selected"] is True
    assert minimal_search["expanded"] is False
    assert minimal["frame_summary"] == payload["frame_summary"]
    assert minimal["skipped_frames"][0]["reason"] == "display_none"
    assert "secret_child_text" not in minimal["skipped_frames"][0]


def test_chrome_inspect_selector_compaction_keeps_count_and_bounded_matches():
    payload = {
        "ok": True,
        "status": "inspected",
        "tabId": 42,
        "snapshot_id": "snap-selector",
        "selector": ".mail-row",
        "count": 2,
        "matched_refs": 2,
        "visible_count": 2,
        "unique": False,
        "semantic_ref_count": 2,
        "page_text": "x" * 5000,
        "matches": [
            {"ref": "e1", "role": "link", "label": "First", "selector": ".mail-row"},
            {"ref": "e2", "role": "link", "label": "Second", "selector": ".mail-row"},
        ],
        "target_resolution": {
            "selector": ".mail-row",
            "matched_refs": 2,
            "refs": ["e1", "e2"],
            "snapshot_id": "snap-selector",
        },
    }

    parsed = json.loads(_compact_tool_result_for_context(
        "mcp__chrome__inspect_selector",
        json.dumps(payload),
        max_chars=900,
    ))

    assert parsed["count"] == 2
    assert parsed["visible_count"] == 2
    assert parsed["unique"] is False
    assert [item["ref"] for item in parsed["matches"]] == ["e1", "e2"]
    assert parsed["target_resolution"]["matched_refs"] == 2


def test_chrome_read_page_minimal_compaction_preserves_form_validation_context():
    payload = {
        "ok": True,
        "status": "snapshot",
        "driver": "chrome-extension",
        "tabId": 42,
        "url": "https://example.test/form",
        "title": "Form",
        "page_kind": "form_or_search",
        "observation_quality": "low",
        "visual_fallback_recommended": True,
        "visual_fallback_reasons": [
            "structured_content_sparse",
            "active_dialog_unresolved",
        ],
        "dom_snapshot": "\n".join(f'- textbox "Field {index}" node_id=e{index}' for index in range(120)),
        "page_text": " ".join(f"Long page text {index}" for index in range(220)),
        "result_candidates": [
            {
                "node_id": "e1",
                "candidate_kind": "content_result",
                "title": "A long visible result title",
                "href": "https://example.test/result",
                "snippet": "Visible summary",
                "evidence_text": "Visible result title and summary",
            }
        ],
        "input_candidates": [
            {
                "node_id": "e101",
                "label": "Email",
                "role": "textbox",
                "name": "email",
                "value": "not-an-email",
                "required": True,
                "valid": False,
                "validation_message": "Please enter an email address.",
                "validity_flags": ["typeMismatch"],
            }
        ],
        "form_candidates": [
            {
                "selector": "form#contact",
                "invalid_fields_count": 1,
                "invalid_fields": [
                    {
                        "kind": "field",
                        "label": "Email",
                        "node_id": "e101",
                        "name": "email",
                        "validation_message": "Please enter an email address.",
                        "validity_flags": ["typeMismatch"],
                    }
                ],
                "required_fields_count": 1,
                "completed_required_fields_count": 1,
                "missing_required_fields_count": 0,
                "missing_required_fields": [],
                "submit_ready": True,
                "form_progress": {
                    "required": 1,
                    "completed": 1,
                    "missing": 0,
                    "submit_ready": True,
                },
                "fields": [
                    {
                        "node_id": "e101",
                        "label": "Email",
                        "name": "email",
                        "value": "not-an-email",
                        "required": True,
                        "valid": False,
                        "validation_message": "Please enter an email address.",
                        "validity_flags": ["typeMismatch"],
                    }
                ],
            }
        ],
    }

    compacted = _compact_tool_result_for_context(
        "mcp__chrome__snapshot",
        json.dumps(payload, ensure_ascii=False),
        max_chars=2700,
    )
    parsed = json.loads(compacted)

    assert parsed["status"] == "read_page"
    assert parsed["tabId"] == 42
    assert parsed["observation_quality"] == "low"
    assert parsed["visual_fallback_recommended"] is True
    assert parsed["visual_fallback_reasons"] == [
        "structured_content_sparse",
        "active_dialog_unresolved",
    ]
    assert parsed["input_candidates"][0]["validation_message"] == "Please enter an email address."
    assert parsed["input_candidates"][0]["validity_flags"] == ["typeMismatch"]
    assert parsed["form_candidates"][0]["invalid_fields_count"] == 1
    assert parsed["form_candidates"][0]["invalid_fields"][0]["validation_message"] == "Please enter an email address."
    assert parsed["form_candidates"][0]["fields"][0]["valid"] is False
    assert parsed["_tool_result_truncated"]["strategy"] in {
        "chrome_browser_context",
        "chrome_browser_minimal_context",
    }


def test_chrome_compaction_reserves_active_dialog_editor_and_submit_candidates():
    background_inputs = [
        {"rank": index, "ref": f"e{index}", "label": f"Background search {index}", "role": "textbox"}
        for index in range(1, 10)
    ]
    background_submits = [
        {"rank": index, "ref": f"s{index}", "label": f"Background action {index}", "role": "button"}
        for index in range(1, 10)
    ]
    payload = {
        "ok": True,
        "status": "snapshot",
        "driver": "chrome-extension",
        "tabId": 42,
        "snapshot_id": "snap-compose",
        "url": "https://example.test/feed",
        "title": "Feed",
        "input_candidates": [
            *background_inputs,
            {"rank": 10, "ref": "e-editor", "label": "Draft body", "role": "textbox", "value": ""},
        ],
        "submit_candidates": [
            *background_submits,
            {"rank": 10, "ref": "e-submit", "label": "Publish", "role": "button"},
        ],
        "dialog_candidates": [
            {
                "rank": 1,
                "selector": "[role=dialog]",
                "label": "Compose",
                "field_refs": ["e-editor"],
                "submit_refs": ["e-submit"],
                "next_actions": [
                    {"rank": 1, "tool": "fill_or_select", "ref": "e-editor", "label": "Draft body"},
                    {"rank": 2, "tool": "click_element", "ref": "e-submit", "label": "Publish"},
                ],
            }
        ],
        "next_actions": [
            *[
                {"rank": index, "tool": "fill_or_select", "ref": f"e{index}", "label": f"Background {index}"}
                for index in range(1, 10)
            ],
            {"rank": 10, "tool": "fill_or_select", "ref": "e-editor", "label": "Draft body"},
            {"rank": 11, "tool": "click_element", "ref": "e-submit", "label": "Publish"},
        ],
        "page_text": "Background feed content. " * 100,
    }

    compacted = _compact_tool_result_for_context(
        "mcp__chrome__read_page",
        json.dumps(payload),
        max_chars=2800,
    )
    parsed = json.loads(compacted)

    assert parsed["input_candidates"][0]["ref"] == "e-editor"
    assert parsed["submit_candidates"][0]["ref"] == "e-submit"
    assert [candidate["ref"] for candidate in parsed["next_actions"][:2]] == ["e-editor", "e-submit"]
    assert parsed["dialog_candidates"][0]["field_refs"] == ["e-editor"]
    assert parsed["dialog_candidates"][0]["submit_refs"] == ["e-submit"]


def test_chrome_compaction_preserves_and_logs_bounded_candidate_summary(caplog):
    payload = {
        "ok": True,
        "status": "read_page",
        "driver": "chrome-extension",
        "tabId": 42,
        "snapshot_id": "snap-compose",
        "url": "https://example.test/feed",
        "title": "Feed",
        "page_text": "Background feed content. " * 800,
        "filter_summary": {
            "mode": "terms",
            "terms": ["editor", "publish"],
            "matched_ref_count": 2,
            "matched_refs": ["e9", "e10"],
        },
        "candidate_summary": {
            "counts": {
                "semantic_refs": 1,
                "input_candidates": 1,
                "dialog_candidates": 1,
                "upload_candidates": 0,
                "submit_candidates": 1,
                "next_actions": 2,
            },
            "semantic_refs": [
                {
                    "ref": "e9",
                    "label": "Draft body",
                    "role": "textbox",
                    "selector": "[aria-label='Message Body']",
                    "value": "secret draft value",
                }
            ],
            "input_candidates": [
                {
                    "ref": "e9",
                    "node_id": "e9",
                    "label": "Draft body",
                    "role": "textbox",
                    "value": "secret draft value",
                }
            ],
            "dialog_candidates": [
                {"label": "Compose", "field_refs": ["e9"], "submit_refs": ["e10"]}
            ],
            "submit_candidates": [{"ref": "e10", "label": "Publish"}],
            "next_actions": [
                {"tool": "fill_or_select", "ref": "e9", "label": "Draft body"},
                {"tool": "click_element", "ref": "e10", "label": "Publish"},
            ],
        },
        "input_candidates": [
            {"rank": 1, "ref": "e9", "label": "Draft body", "role": "textbox", "value": "secret draft value"}
        ],
        "dialog_candidates": [
            {"rank": 1, "label": "Compose", "field_refs": ["e9"], "submit_refs": ["e10"]}
        ],
        "submit_candidates": [{"rank": 1, "ref": "e10", "label": "Publish"}],
    }

    caplog.set_level(logging.INFO, logger="packages.core.ai.agentic_loop")
    compacted = json.loads(
        _compact_tool_result_for_context(
            "mcp__chrome__read_page",
            json.dumps(payload),
        )
    )

    assert compacted["filter_summary"]["matched_refs"] == ["e9", "e10"]
    assert compacted["candidate_summary"]["semantic_refs"][0]["ref"] == "e9"
    assert "value" not in compacted["candidate_summary"]["semantic_refs"][0]
    assert compacted["candidate_summary"]["input_candidates"][0]["ref"] == "e9"
    assert compacted["candidate_summary"]["dialog_candidates"][0]["submit_refs"] == ["e10"]
    assert "secret draft value" not in json.dumps(compacted["candidate_summary"])
    assert "chrome_context_compaction" in caplog.text
    assert "candidate_refs=['e9', 'e10']" in caplog.text
    assert "secret draft value" not in caplog.text


def test_chrome_read_page_compaction_prioritizes_late_filtered_gmail_ref():
    background_refs = [
        {
            "ref": f"e{index}",
            "role": "button",
            "label": f"Background Gmail control {index}",
            "aria_label": f"Background Gmail control {index}",
            "title": f"Background Gmail control {index}",
            "data_testid": f"gmail-control-{index}",
            "selector": f"div[role='button']:nth-of-type({index})",
            "description": "Verbose Gmail toolbar control used to reproduce production ref density",
            "container_label": "Message toolbar",
            "clickable": True,
            "in_viewport": True,
            "bounds": {"x": index * 4, "y": 20, "width": 32, "height": 32},
        }
        for index in range(1, 60)
    ]
    reply_ref = {
        "ref": "e60",
        "role": "button",
        "label": "Reply",
        "aria_label": "Reply",
        "selector": "div[role='button'][aria-label='Reply']",
        "clickable": True,
        "in_viewport": True,
    }
    payload = {
        "ok": True,
        "status": "read_page",
        "driver": "chrome-extension",
        "snapshot_id": "snap-gmail-thread",
        "tabId": 42,
        "url": "https://mail.google.com/mail/u/0/#inbox/thread-1",
        "title": "Message - Gmail",
        "page_status": "ready",
        "page_blockers": [],
        "reason": None,
        "pageContent": "\n".join(
            [
                *(f'- button "Background Gmail control {index}" [ref=e{index}]' for index in range(1, 60)),
                '- button "Reply" [ref=e60] aria-label="Reply"',
            ]
        ),
        "filter_summary": {
            "mode": "terms",
            "terms": ["reply"],
            "matched_ref_count": 1,
            "matched_refs": ["e60"],
        },
        "semantic_refs": [*background_refs, reply_ref],
        "candidate_summary": {
            "counts": {"semantic_refs": 60, "next_actions": 1},
            "semantic_refs": [*background_refs, reply_ref],
            "next_actions": [{"tool": "click_element", "ref": "e60", "label": "Reply"}],
        },
    }

    compacted = json.loads(_compact_tool_result_for_context(
        "mcp__chrome__read_page",
        json.dumps(payload),
    ))

    assert compacted["snapshot_id"] == "snap-gmail-thread"
    assert compacted["filter_summary"]["matched_refs"] == ["e60"]
    assert compacted["semantic_refs"][0]["ref"] == "e60"
    assert compacted["semantic_refs"][0]["label"] == "Reply"
    assert "Reply" in compacted["pageContent"]


def test_chrome_minimal_compaction_preserves_filtered_gmail_page_content():
    semantic_refs = [
        {
            "ref": f"e{index}",
            "node_id": f"e{index}",
            "role": "button",
            "label": "Reply" if index == 60 else f"Background Gmail control {index}",
            "aria_label": "Reply" if index == 60 else f"Background Gmail control {index}",
            "title": f"Verbose Gmail control title {index}",
            "data_testid": f"gmail-control-{index}",
            "selector": f"div[role='button']:nth-of-type({index})",
            "description": "Verbose Gmail toolbar control used to force the production minimal compaction path",
            "container_label": "Message toolbar",
            "clickable": True,
            "in_viewport": True,
            "bounds": {"x": index * 4, "y": 20, "width": 32, "height": 32},
        }
        for index in range(1, 97)
    ]
    dense_candidates = [dict(item) for item in semantic_refs[:12]]
    payload = {
        "ok": True,
        "status": "read_page",
        "driver": "chrome-extension",
        "snapshot_id": "snap-gmail-dense",
        "tabId": 42,
        "url": "https://mail.google.com/mail/u/0/#inbox/thread-1",
        "title": "Message - Gmail",
        "page_status": "ready",
        "page_blockers": [],
        "pageContent": '- button "Reply" [ref=e60] aria-label="Reply"',
        "filter_summary": {
            "mode": "terms",
            "terms": ["reply", "message", "body", "send"],
            "matched_ref_count": 1,
            "matched_refs": ["e60"],
        },
        "semantic_refs": semantic_refs,
        "candidate_summary": {
            "counts": {"semantic_refs": 96, "next_actions": 12},
            "semantic_refs": semantic_refs,
            "next_actions": dense_candidates,
        },
        "content_summary": {
            "main_content_text": "Dense Gmail message body. " * 1000,
            "main_content_blocks": dense_candidates,
        },
        **{
            key: dense_candidates
            for key in (
                "result_candidates",
                "input_candidates",
                "choice_candidates",
                "upload_candidates",
                "form_candidates",
                "dialog_candidates",
                "submit_candidates",
                "actionable_refs",
                "node_candidates",
                "next_actions",
                "content_links",
                "search_refinement_candidates",
                "search_discovery_candidates",
            )
        },
    }

    compacted_text = _compact_tool_result_for_context(
        "mcp__chrome__read_page",
        json.dumps(payload),
    )
    compacted = json.loads(compacted_text)

    assert len(compacted_text) <= 12000
    assert compacted["_tool_result_truncated"]["strategy"] == "chrome_browser_minimal_context"
    assert compacted["filter_summary"]["matched_refs"] == ["e60"]
    assert "Reply" in compacted["pageContent"]
    assert any(item.get("ref") == "e60" for item in compacted["semantic_refs"])


def test_chrome_ready_page_with_empty_blockers_does_not_use_blocker_fallback():
    assert _build_minimal_chrome_blocker_result(
        "mcp__chrome__read_page",
        {
            "ok": True,
            "status": "read_page",
            "snapshot_id": "snap-ready",
            "page_status": "ready",
            "page_blockers": [],
        },
        digest="digest",
    ) is None


def test_chrome_read_page_minimal_compaction_preserves_page_blocker_context():
    payload = {
        "ok": True,
        "status": "snapshot",
        "driver": "chrome-extension",
        "tabId": 42,
        "url": "https://example.test/private",
        "title": "Checking your browser",
        "page_kind": "content_page",
        "page_status": "blocked",
        "status_flags": ["captcha_or_human_verification"],
        "page_blockers": [
            {
                "kind": "captcha_or_human_verification",
                "severity": "blocker",
                "message": "Page requires CAPTCHA or human verification in Chrome before automation can continue.",
                "evidence_text": "Checking your browser Please complete the CAPTCHA to continue",
                "recommended_next_action": "ask_user_to_resolve",
            }
        ],
        "dom_snapshot": "\n".join(f'- text "Checking browser line {index}"' for index in range(140)),
        "page_text": " ".join(f"Long blocker text {index}" for index in range(300)),
        "next_actions": [
            {
                "rank": 1,
                "action": "wait_for_user",
                "tool": "none",
                "candidate_kind": "page_blocker",
                "blocker_kind": "captcha_or_human_verification",
                "label": "Page requires CAPTCHA or human verification in Chrome before automation can continue.",
                "reason": "real page blocker detected; ask the user to resolve it in Chrome before continuing automation",
                "recommended_next_action": "ask_user_to_resolve",
            }
        ],
        "action_policy": "Use dom_snapshot plus page_blockers as the page-understanding source. This page has a real blocker; stop Chrome actions and ask the user to resolve it in Chrome before continuing.",
    }

    compacted = _compact_tool_result_for_context(
        "mcp__chrome__snapshot",
        json.dumps(payload, ensure_ascii=False),
        max_chars=900,
    )
    parsed = json.loads(compacted)

    assert parsed["page_status"] == "blocked"
    assert parsed["status_flags"] == ["captcha_or_human_verification"]
    assert parsed["page_blockers"][0]["kind"] == "captcha_or_human_verification"
    assert parsed["page_blockers"][0]["recommended_next_action"] == "ask_user_to_resolve"
    assert parsed["next_actions"][0]["action"] == "wait_for_user"
    assert parsed["next_actions"][0]["tool"] == "none"
    assert parsed["next_actions"][0]["blocker_kind"] == "captcha_or_human_verification"
    assert parsed["next_actions"][0]["recommended_next_action"] == "ask_user_to_resolve"
    assert parsed["_tool_result_truncated"]["strategy"] in {
        "chrome_browser_context",
        "chrome_browser_minimal_context",
        "chrome_browser_blocker_context",
    }


def test_chrome_click_compaction_preserves_submit_and_navigation_semantics():
    payload = {
        "ok": True,
        "status": "clicked",
        "driver": "chrome-extension",
        "tabId": 42,
        "acted_tab_id": 42,
        "target_tab_id": 43,
        "ref": "e7",
        "node_id": "e7",
        "action": {
            "ok": True,
            "ref": "e7",
            "label": "Submit",
            "role": "button",
            "interaction": "submit",
            "submitted_form": True,
            "default_prevented": True,
            "form_selector": "form#signup",
            "form_label": "Signup",
            "href": "https://news.google.com/search?q=Elon%20Musk",
            "target": "_blank",
        },
        "navigation": {
            "opened_new_tab": True,
            "target_tab_id": 43,
            "after_url": "https://news.google.com/search?q=Elon%20Musk",
        },
        "state_hint": {
            "action": "click_node",
            "ok": True,
            "target": "e7",
            "label": "Submit",
            "role": "button",
            "interaction": "submit",
            "submitted_form": True,
            "default_prevented": True,
            "form_selector": "form#signup",
            "form_label": "Signup",
            "href": "https://news.google.com/search?q=Elon%20Musk",
            "target_attribute": "_blank",
            "next": "snapshot",
        },
        "snapshot_required": True,
        "next_required_tool": "browser_dom_snapshot",
        "debug_payload": "x" * 600,
    }

    compacted = _compact_tool_result_for_context(
        "mcp__chrome__click_node",
        json.dumps(payload, ensure_ascii=False),
        max_chars=950,
    )
    parsed = json.loads(compacted)

    assert parsed["status"] == "clicked"
    assert parsed["target_tab_id"] == 43
    assert parsed["action"]["interaction"] == "submit"
    assert parsed["action"]["submitted_form"] is True
    assert parsed["action"]["default_prevented"] is True
    assert parsed["action"]["form_selector"] == "form#signup"
    assert parsed["action"]["form_label"] == "Signup"
    assert parsed["action"]["href"] == "https://news.google.com/search?q=Elon%20Musk"
    assert parsed["action"]["target"] == "_blank"
    assert parsed["state_hint"]["interaction"] == "submit"
    assert parsed["state_hint"]["target_attribute"] == "_blank"
    assert parsed["next_required_tool"] == "mcp__chrome__read_page"
    assert parsed["state_hint"]["action"] == "click_element"
    assert parsed["state_hint"]["next"] == "read_page"
    assert parsed["_tool_result_truncated"]["tool"] == "mcp__chrome__click_element"


def test_chrome_action_compaction_preserves_verified_post_action_state():
    payload = {
        "ok": True,
        "status": "clicked",
        "tabId": 42,
        "target_tab_id": 42,
        "action": {
            "ok": True,
            "ref": "e7",
            "label": "Start a post",
            "role": "button",
            "interaction": "click",
        },
        "post_action_page_state": {
            "ok": True,
            "state_verified": True,
            "snapshot_id": "snap-after",
            "tabId": 42,
            "page_status": "ready",
            "page_blockers": [],
            "dialog_candidates": [{"rank": 1, "label": "Create a post"}],
            "input_candidates": [{"rank": 1, "ref": "e12", "label": "Post text"}],
            "submit_candidates": [{"rank": 1, "ref": "e20", "label": "Post"}],
            "next_actions": [
                {"action": "fill_or_select", "tool": "fill_or_select", "ref": f"e{index}"}
                for index in range(12, 17)
            ],
            "page_text": "This must not be copied into compact action state. " * 40,
        },
        "snapshot_required": False,
        "state_hint": {
            "action": "click_element",
            "ok": True,
            "next": "use_post_action_state",
        },
        "debug_payload": "x" * 1800,
    }

    compacted = _compact_tool_result_for_context(
        "mcp__chrome__click_element",
        json.dumps(payload, ensure_ascii=False),
        max_chars=1000,
    )
    parsed = json.loads(compacted)
    state = parsed["post_action_page_state"]

    assert parsed["snapshot_required"] is False
    assert state["ok"] is True
    assert state["state_verified"] is True
    assert state["snapshot_id"] == "snap-after"
    assert state["tabId"] == 42
    assert len(state["next_actions"]) == 4
    assert state["next_actions"][0]["ref"] == "e12"
    assert "page_text" not in state


def test_chrome_action_compaction_drops_unverified_post_action_targets():
    payload = {
        "ok": True,
        "status": "clicked",
        "tabId": 42,
        "post_action_page_state": {
            "ok": True,
            "state_verified": False,
            "snapshot_id": "snap-observed-but-unstable",
            "tabId": 42,
            "semantic_refs": [{"ref": "e3", "label": "Message Body", "role": "textbox"}],
            "input_candidates": [{"ref": "e3", "label": "Message Body"}],
            "submit_candidates": [{"ref": "e4", "label": "Send"}],
            "next_actions": [{"tool": "fill_or_select", "ref": "e3"}],
        },
        "snapshot_required": True,
        "next_required_tool": "mcp__chrome__read_page",
    }

    compacted = _compact_tool_result_for_context(
        "mcp__chrome__click_element",
        json.dumps(payload, ensure_ascii=False),
        max_chars=1000,
    )
    state = json.loads(compacted)["post_action_page_state"]

    assert state["state_verified"] is False
    assert "semantic_refs" not in state
    assert "input_candidates" not in state
    assert "submit_candidates" not in state
    assert "next_actions" not in state


def test_chrome_press_key_compaction_preserves_prevented_enter_recovery():
    recovery = {
        "recommended_next_action": "click_node",
        "submit_candidate_ref": "e7",
        "submit_candidate_node_id": "e7",
        "submit_candidate_label": "Search news",
        "submit_candidate_selector": "button#go",
        "form_selector": "form#search-form",
        "form_label": "Site search",
        "reason": "Enter was prevented by the page; click this visible submit/search control next if it matches the user goal",
    }
    payload = {
        "ok": True,
        "status": "pressed",
        "driver": "chrome-extension",
        "tabId": 42,
        "key": {
            "ok": True,
            "key": "Enter",
            "label": "Search",
            "role": "searchbox",
            "default_prevented": True,
            "submitted_form": False,
            "enter_recovery": recovery,
        },
        "state_hint": {
            "action": "press_key",
            "ok": True,
            "default_prevented": True,
            "enter_recovery": recovery,
            "next": "snapshot",
        },
        "snapshot_required": True,
        "next_required_tool": "browser_dom_snapshot",
        "debug_payload": "x" * 600,
    }

    compacted = _compact_tool_result_for_context(
        "mcp__chrome__press_key",
        json.dumps(payload, ensure_ascii=False),
        max_chars=900,
    )
    parsed = json.loads(compacted)

    assert parsed["key"]["default_prevented"] is True
    assert parsed["key"]["enter_recovery"]["recommended_next_action"] == "click_element"
    assert parsed["key"]["enter_recovery"]["submit_candidate_ref"] == "e7"
    assert parsed["key"]["enter_recovery"]["submit_candidate_label"] == "Search news"
    assert parsed["key"]["enter_recovery"]["submit_candidate_selector"] == "button#go"
    assert parsed["key"]["enter_recovery"]["form_selector"] == "form#search-form"
    assert parsed["key"]["enter_recovery"]["submit_candidate_node_id"] == "e7"
    assert parsed["key"]["enter_recovery"]["form_label"] == "Site search"
    assert parsed["state_hint"]["default_prevented"] is True
    assert parsed["state_hint"]["next"] == "read_page"
    assert parsed["next_required_tool"] == "mcp__chrome__read_page"
    assert "enter_recovery" not in parsed["state_hint"]
    assert parsed["_tool_result_truncated"]["tool"] == "mcp__chrome__press_key"


def test_chrome_action_compaction_preserves_failure_reason():
    payload = {
        "ok": False,
        "status": "filled",
        "driver": "chrome-extension",
        "tabId": 42,
        "action": {
            "ok": False,
            "ref": "e7",
            "node_id": "e7",
            "label": "Settings",
            "role": "button",
            "reason": "ref_not_editable",
        },
        "state_hint": {
            "action": "fill_node",
            "ok": False,
            "target": "e7",
            "label": "Settings",
            "role": "button",
            "reason": "ref_not_editable",
            "next": "snapshot",
        },
        "snapshot_required": True,
        "next_required_tool": "browser_dom_snapshot",
        "debug_payload": "x" * 600,
    }

    compacted = _compact_tool_result_for_context(
        "mcp__chrome__fill_node",
        json.dumps(payload, ensure_ascii=False),
        max_chars=620,
    )
    parsed = json.loads(compacted)

    assert parsed["action"]["reason"] == "ref_not_editable"
    assert parsed["state_hint"]["action"] == "fill_or_select"
    assert parsed["state_hint"]["reason"] == "ref_not_editable"
    assert parsed["state_hint"]["next"] == "read_page"
    assert parsed["next_required_tool"] == "mcp__chrome__read_page"
    assert parsed["_tool_result_truncated"]["tool"] == "mcp__chrome__fill_or_select"


def test_chrome_action_compaction_preserves_structured_recovery_hints():
    payload = {
        "ok": False,
        "tool": "browser_fill_ref",
        "error": 'Missing required string parameter "value"',
        "reason": "missing_required_parameter",
        "missing_parameter": "value",
        "recommended_next_action": "retry_with_required_parameter",
        "candidate_sources": ["input_candidates", "form_candidates", "choice_candidates"],
        "recovery": "Retry fill_node with a valid editable ref/node_id and include value.",
        "state_hint": {
            "action": "browser_fill_ref",
            "ok": False,
            "reason": "missing_required_parameter",
            "missing_parameter": "value",
            "recommended_next_action": "retry_with_required_parameter",
            "candidate_sources": ["input_candidates", "form_candidates", "choice_candidates"],
            "next": "snapshot",
        },
        "snapshot_required": True,
        "next_required_tool": "browser_dom_snapshot",
        "debug_payload": "x" * 600,
    }

    compacted = _compact_tool_result_for_context(
        "mcp__chrome__fill_node",
        json.dumps(payload, ensure_ascii=False),
        max_chars=620,
    )
    parsed = json.loads(compacted)

    assert parsed["reason"] == "missing_required_parameter"
    assert parsed["missing_parameter"] == "value"
    assert parsed["recommended_next_action"] == "retry_with_required_parameter"
    assert parsed["candidate_sources"] == ["input_candidates", "form_candidates", "choice_candidates"]
    assert parsed["tool"] == "fill_or_select"
    assert parsed["next_required_tool"] == "mcp__chrome__read_page"
    assert parsed["_tool_result_truncated"]["strategy"] in {
        "chrome_browser_context",
        "chrome_browser_minimal_context",
        "chrome_action_context",
        "chrome_action_ultra_minimal_context",
    }


def test_chrome_action_compaction_preserves_wait_then_read_page_hints():
    payload = {
        "ok": True,
        "status": "uploaded",
        "driver": "chrome-extension",
        "tabId": 42,
        "selector": "#business-license",
        "files_count": 1,
        "state_hint": {
            "action": "upload",
            "ok": True,
            "target": "#business-license",
            "files_count": 1,
            "next": "wait",
            "recommended_next_action": "wait_then_snapshot",
            "wait_tool": "browser_wait",
            "wait_state": "stable",
            "after_wait": "snapshot",
            "wait_reason": "upload may trigger file processing, validation, or delayed UI updates",
        },
        "snapshot_required": True,
        "next_required_tool": "browser_dom_snapshot",
        "debug_payload": "x" * 600,
    }

    compacted = _compact_tool_result_for_context(
        "mcp__chrome__upload",
        json.dumps(payload, ensure_ascii=False),
        max_chars=620,
    )
    parsed = json.loads(compacted)

    assert parsed["state_hint"]["next"] == "wait"
    assert parsed["state_hint"]["recommended_next_action"] == "wait_then_read_page"
    assert parsed["state_hint"]["wait_tool"] == "mcp__chrome__wait"
    assert parsed["state_hint"]["wait_state"] == "stable"
    assert parsed["state_hint"]["after_wait"] == "read_page"
    assert parsed["next_required_tool"] == "mcp__chrome__read_page"
    assert "upload may trigger" in parsed["state_hint"]["wait_reason"]
