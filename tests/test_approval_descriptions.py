"""Per-tool approval prompts must be human-readable, not JSON.

Reproducer (the bug this fixes): user sees the chat card display
``"Approve Manor to run external action with mcp__linkedin_browser__
send_invitation?"`` followed by a JSON dump of {tool, action,
risk_level, arguments}. We replace that with a tool-specific
description like ``Send LinkedIn connection invitation to
dannaredmond``.

These tests pin the wording per tool. Adding a new high-risk tool
should also add a case here so we don't regress to JSON dumps.
"""

from __future__ import annotations

import pytest

from packages.core.ai.runtime.approval_messages import (
    describe_runtime_approval_action as _describe_action,
    runtime_approval_prompt as _approval_prompt,
)
from packages.core.ai.runtime.approvals import RuntimeApprovalAction as ToolAction


def _action(
    action_key: str = "social_post.publish", risk: str = "high", title: str = "publish social post"
) -> ToolAction:
    return ToolAction(
        kind="action",
        action_key=action_key,
        risk_level=risk,
        title=title,
        resource_kind="external_account",
        operation="publish",
    )


# ── _describe_action: per-tool template wording ─────────────────────

@pytest.mark.parametrize(
    "tool,args,expected_substr",
    [
        # linkedin_browser
        (
            "mcp__linkedin_browser__send_invitation",
            {"profile": "dannaredmond"},
            "Send LinkedIn connection invitation to dannaredmond",
        ),
        (
            "mcp__linkedin_browser__send_invitation",
            {"profile": "alice", "note": "Met at AI conf, would love to chat."},
            'Send LinkedIn connection invitation to alice — note: "Met at AI conf, would love to chat."',
        ),
        (
            "mcp__linkedin_browser__send_message",
            {"conversation": "urn:li:msg:42", "text": "Hi Alice, just following up."},
            'Send LinkedIn message to urn:li:msg:42: "Hi Alice, just following up."',
        ),
        (
            "mcp__linkedin_browser__easy_apply",
            {"job": "https://linkedin.com/jobs/view/123"},
            "Submit LinkedIn Easy Apply for: https://linkedin.com/jobs/view/123",
        ),
        (
            "mcp__linkedin_browser__create_post",
            {"text": "Excited to share that..."},
            'Publish LinkedIn post: "Excited to share that..."',
        ),
        # email
        (
            "mcp__gmail__send_email",
            {"to": "alice@example.com", "subject": "Q4 Plan", "body": "..."},
            'Send email to alice@example.com: "Q4 Plan"',
        ),
        (
            "mcp__gmail__send_draft",
            {"draft_id": "draft-123"},
            "Send prepared Gmail draft (draft-123)",
        ),
        (
            "mcp__gmail__send_draft",
            {"draft_id": "draft-123", "to": "alice@example.com", "subject": "Q4 Plan"},
            'Send prepared Gmail draft to alice@example.com: "Q4 Plan"',
        ),
        # messaging
        (
            "mcp__telegram__send_message",
            {"chat": "123", "text": "Hi"},
            'Send Telegram message to 123: "Hi"',
        ),
    ],
)
def test_describe_action_renders_human_template(tool, args, expected_substr):
    out = _describe_action(tool, args)
    assert expected_substr in out, (
        f"\ntool: {tool}\nargs: {args}\nexpected substring: {expected_substr!r}\ngot: {out!r}"
    )


def test_describe_action_unknown_tool_returns_empty():
    """Unknown tools fall through to the generic action.title path
    in _approval_prompt, NOT to a JSON dump."""
    assert _describe_action("mcp__unknown__random_action", {"x": 1}) == ""


# ── _approval_prompt: end-to-end wording the chat card receives ──────


def test_approval_prompt_uses_template_when_available():
    out = _approval_prompt(
        _action(action_key="social_post.publish", title="publish social post"),
        "mcp__linkedin_browser__send_invitation",
        {"profile": "dannaredmond"},
    )
    assert "Send LinkedIn connection invitation to dannaredmond" in out
    # No JSON braces — the historical bug was a {…tool, action, args…} dump.
    assert "{" not in out
    assert "mcp__" not in out


def test_approval_prompt_falls_back_to_action_title_for_unknown_tool():
    """Unknown tool: still no JSON, just the human-readable
    action.title — capitalized."""
    out = _approval_prompt(
        _action(title="run external action"),
        "mcp__unknown__random_action",
        {"x": 1},
    )
    assert "Run external action" in out
    assert "{" not in out
    # The JSON dump of args isn't included.
    assert '"x"' not in out
    assert "'x'" not in out


def test_approval_prompt_includes_email_body_when_present():
    """For tools whose template doesn't exist but whose args carry a
    'text' / 'message' / 'body' field, the fallback path surfaces
    that public content for context — still no JSON."""
    out = _approval_prompt(
        _action(action_key="email.send", title="send email"),
        "mcp__some_email_provider__send",
        {"to": "alice@example.com", "body": "Quick note: meeting at 3pm."},
    )
    assert "Quick note: meeting at 3pm." in out
    assert "{" not in out


# ── Regression: the original screenshot's behavior ───────────────────


def test_screenshot_repro_no_json_dump():
    """User screenshot showed:

        Approve Manor to run external action with `mcp__linkedin_browser__send_invitation`?
        {
          "tool": "mcp__linkedin_browser__send_invitation",
          "action": "linkedin_browser.send_invitation",
          "risk_level": "high",
          "arguments": {
            "confirm": true,
            "profile": "dannaredmond"
          }
        }

    After this fix the prompt must be a single human sentence, no
    braces, no `mcp__` token, no `risk_level` key visible.
    """
    out = _approval_prompt(
        _action(action_key="social_post.publish", title="publish social post"),
        "mcp__linkedin_browser__send_invitation",
        {"confirm": True, "profile": "dannaredmond"},
    )
    forbidden_substrings = ["{", "}", "mcp__", "risk_level", '"tool":', '"arguments":']
    for forbidden in forbidden_substrings:
        assert forbidden not in out, f"prompt contains forbidden substring {forbidden!r}: {out!r}"
    # And it must be the actual description.
    assert "Send LinkedIn connection invitation to dannaredmond" in out
