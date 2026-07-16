from __future__ import annotations

import json

import pytest

from packages.core.ai.runtime.dashboard_submission import (
    runtime_capture_dashboard_submission,
)
from packages.core.ai.tools.code_tool import _code_handler
from packages.core.ai.tools.dashboard_tools import _submit_dashboard_module
from packages.core.services.dashboard_agent import (
    dashboard_agent_tool_is_allowed,
    dashboard_tool_is_read_only,
)


def _code() -> dict:
    return {
        "version": 1,
        "runtime": "sandboxed_html",
        "html": '<div class="rows" data-rows></div>',
        "css": ".rows{color:var(--module-text);border:1px solid var(--module-border)}",
        "javascript": (
            "window.renderDashboardModule=function(data){"
            "document.querySelector('[data-rows]').textContent=String(data.items||'');};"
        ),
        "data_requests": [
            {"key": "items", "source": "tasks", "params": {"limit": 5}}
        ],
    }


@pytest.mark.asyncio
async def test_code_validation_records_exact_dashboard_bundle_for_submission() -> None:
    code = _code()
    with runtime_capture_dashboard_submission() as capture:
        validation = json.loads(
            await _code_handler(
                action="dashboard_module_validate",
                params={"code": code},
                _manual_skill_slugs_from_context=["dashboard-module-builder"],
            )
        )
        submission = json.loads(
            await _submit_dashboard_module(
                widgets=[],
                module_changes=[
                    {"action": "create", "title": "Work", "code": code}
                ],
            )
        )

    assert validation["platform_ready"] is True
    assert validation["recorded_for_dashboard_submission"] is True
    assert submission["status"] == "accepted"
    assert capture.submission is not None


@pytest.mark.asyncio
async def test_dashboard_submission_rejects_unvalidated_or_changed_code() -> None:
    code = _code()
    with runtime_capture_dashboard_submission() as capture:
        await _code_handler(
            action="dashboard_module_validate",
            params={"code": code},
            _manual_skill_slugs_from_context=["dashboard-module-builder"],
        )
        changed_code = {**code, "css": code["css"] + ".changed{display:block}"}
        result = json.loads(
            await _submit_dashboard_module(
                widgets=[],
                module_changes=[
                    {"action": "create", "title": "Work", "code": changed_code}
                ],
            )
        )

    assert result["status"] == "rejected"
    assert result["unvalidated_change_indexes"] == [0]
    assert capture.submission is None


@pytest.mark.asyncio
async def test_dashboard_skill_cannot_use_other_code_actions() -> None:
    result = json.loads(
        await _code_handler(
            action="git_status",
            params={},
            _manual_skill_slugs_from_context=["dashboard-module-builder"],
        )
    )

    assert "may only use" in result["error"]
    assert dashboard_agent_tool_is_allowed("code") is True
    assert dashboard_agent_tool_is_allowed(
        "code",
        {"action": "dashboard_module_validate"},
    ) is True
    assert dashboard_agent_tool_is_allowed("code", {"action": "git_status"}) is False
    assert dashboard_agent_tool_is_allowed("code", {"action": "search"}) is False
    assert dashboard_tool_is_read_only(
        "mcp__gmail__list_messages",
        {"query": "is:unread newer_than:7d", "max_results": 8, "include_details": True},
    ) is True
    assert dashboard_tool_is_read_only(
        "mcp__gmail__get_message",
        {"message_id": "msg_123", "format": "metadata"},
    ) is True
    assert dashboard_tool_is_read_only(
        "mcp__gmail__send_message",
        {"to": "person@example.com", "subject": "Hi", "body": "No"},
    ) is False
    assert dashboard_tool_is_read_only(
        "mcp__gmail__mark_read",
        {"message_id": "msg_123"},
    ) is False
    search_result = json.loads(
        await _code_handler(
            action="search",
            query="write file",
            _manual_skill_slugs_from_context=["dashboard-module-builder"],
        )
    )
    assert "may only use" in search_result["error"]
    assert dashboard_tool_is_read_only(
        "code",
        {"action": "dashboard_module_validate"},
    ) is False


@pytest.mark.asyncio
async def test_platform_warnings_block_dashboard_submission_recording() -> None:
    code = _code()
    code["css"] = ".rows{color:#111;border-radius:20px}"
    with runtime_capture_dashboard_submission() as capture:
        result = json.loads(
            await _code_handler(
                action="dashboard_module_validate",
                params={"code": code},
                _manual_skill_slugs_from_context=["dashboard-module-builder"],
            )
        )

    assert result["valid"] is True
    assert result["platform_ready"] is False
    assert result["recorded_for_dashboard_submission"] is False
    assert capture.validated_code_hashes == set()
