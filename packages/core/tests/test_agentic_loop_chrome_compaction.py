import json

from packages.core.ai.agentic_loop import _compact_tool_result_for_context


def test_chrome_snapshot_compaction_preserves_model_action_context():
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

    assert parsed["status"] == "snapshot"
    assert parsed["driver"] == "chrome-extension"
    assert parsed["page_kind"] == "search_results"
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
    assert parsed["next_actions"][0]["tool"] == "click_node"
    assert parsed["next_actions"][0]["candidate_kind"] == "navigation_url"
    assert parsed["next_actions"][0]["href"] == "https://news.google.com/search?q=Elon+Musk+SpaceX"
    assert parsed["next_actions"][0]["url"] == "https://news.google.com/search?q=Elon+Musk+SpaceX"
    assert "Snapshot again" in parsed["action_policy"]
    assert parsed["_tool_result_truncated"]["tool"] == "mcp__chrome__snapshot"


def test_chrome_snapshot_minimal_compaction_preserves_form_validation_context():
    payload = {
        "ok": True,
        "status": "snapshot",
        "driver": "chrome-extension",
        "tabId": 42,
        "url": "https://example.test/form",
        "title": "Form",
        "page_kind": "form_or_search",
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

    assert parsed["status"] == "snapshot"
    assert parsed["tabId"] == 42
    assert parsed["input_candidates"][0]["validation_message"] == "Please enter an email address."
    assert parsed["input_candidates"][0]["validity_flags"] == ["typeMismatch"]
    assert parsed["form_candidates"][0]["invalid_fields_count"] == 1
    assert parsed["form_candidates"][0]["invalid_fields"][0]["validation_message"] == "Please enter an email address."
    assert parsed["form_candidates"][0]["fields"][0]["valid"] is False
    assert parsed["_tool_result_truncated"]["strategy"] in {
        "chrome_browser_context",
        "chrome_browser_minimal_context",
    }


def test_chrome_snapshot_minimal_compaction_preserves_page_blocker_context():
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
    assert parsed["state_hint"]["next"] == "snapshot"
    assert parsed["_tool_result_truncated"]["tool"] == "mcp__chrome__click_node"


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
    assert parsed["key"]["enter_recovery"]["recommended_next_action"] == "click_node"
    assert parsed["key"]["enter_recovery"]["submit_candidate_ref"] == "e7"
    assert parsed["key"]["enter_recovery"]["submit_candidate_label"] == "Search news"
    assert parsed["key"]["enter_recovery"]["submit_candidate_selector"] == "button#go"
    assert parsed["key"]["enter_recovery"]["form_selector"] == "form#search-form"
    assert parsed["key"]["enter_recovery"]["submit_candidate_node_id"] == "e7"
    assert parsed["key"]["enter_recovery"]["form_label"] == "Site search"
    assert parsed["state_hint"]["default_prevented"] is True
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
    assert parsed["state_hint"]["reason"] == "ref_not_editable"
    assert parsed["_tool_result_truncated"]["tool"] == "mcp__chrome__fill_node"


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
    assert parsed["_tool_result_truncated"]["strategy"] in {
        "chrome_browser_context",
        "chrome_browser_minimal_context",
        "chrome_action_context",
        "chrome_action_ultra_minimal_context",
    }


def test_chrome_action_compaction_preserves_wait_then_snapshot_hints():
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
    assert parsed["state_hint"]["recommended_next_action"] == "wait_then_snapshot"
    assert parsed["state_hint"]["wait_tool"] == "browser_wait"
    assert parsed["state_hint"]["wait_state"] == "stable"
    assert parsed["state_hint"]["after_wait"] == "snapshot"
    assert "upload may trigger" in parsed["state_hint"]["wait_reason"]
