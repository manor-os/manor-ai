from __future__ import annotations

import inspect
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from apps.api.routers import chat as chat_router
from apps.api.routers.chat import (
    _bind_response_surface_submission,
    _canonicalize_response_surface_submission,
    _find_response_surface_submission_replay,
    _parse_response_surface_submission,
    _response_surface_submission_agent,
    _response_surface_submission_replay_after_integrity_error,
    _response_surface_submission_replay_stream,
    _response_surface_submission_replay_source,
    _same_response_surface_submission,
    _to_chat_message_response,
)

from packages.core.ai.runtime.capabilities import CORE_CAPABILITIES
from packages.core.ai.runtime.approval_classifier import classify_runtime_tool
from packages.core.ai.runtime.output_policy import PUBLIC_REQUEST_FAILURE_MESSAGE
from packages.core.ai.runtime.profiles import RuntimeProfile
from packages.core.ai.runtime.surfaces import ChatSurface
from packages.core.ai.runtime.tool_effect_classification import RuntimeToolEffect
from packages.core.ai.tools.response_surface_tools import (
    _render_response_surface,
    get_tools,
)
from packages.core.models.runtime_run import RuntimeRunStatus
from packages.core.services.assistant_blocks import AssistantBlocksBuilder
from packages.core.services.response_surfaces import (
    RESPONSE_SURFACE_POLICY,
    ResponseSurfaceValidationError,
    normalize_response_surface,
)


def _generated_surface(**overrides):
    surface = {
        "version": 1,
        "title": "Pipeline explorer",
        "render": {
            "kind": "sandboxed_html",
            "code": {
                "version": 1,
                "runtime": "sandboxed_html",
                "html": (
                    '<form><strong>Qualified leads</strong>'
                    '<input name="segment" value="qualified">'
                    '<button data-manor-action="submit">Apply</button></form>'
                ),
                "css": "#result { color: var(--module-text); }",
                "javascript": "",
            },
            "data": {"label": "Qualified leads"},
        },
        "display": {"preferred": "inline", "inline_height": 320, "focusable": True},
        "actions": [{"id": "submit", "label": "Apply"}],
        "fallback_markdown": "Pipeline explorer for qualified leads.",
    }
    surface.update(overrides)
    return surface


def test_response_surface_normalizes_registered_code_lab_template() -> None:
    surface = normalize_response_surface({
        "version": 1,
        "title": "Implement debounce",
        "render": {
            "kind": "template",
            "template_id": "learning.code_lab",
            "template_version": 1,
            "props": {
                "language": "typescript",
                "languages": [
                    {
                        "id": "typescript",
                        "label": "TypeScript",
                        "filename": "main.ts",
                        "starter_code": "export function debounce() {}",
                    },
                    {
                        "id": "javascript",
                        "label": "JavaScript",
                        "filename": "main.js",
                        "starter_code": "export function debounce() {}",
                    },
                ],
                "instructions": "Implement debounce without a library.",
                "starter_code": "export function debounce() {}",
                "tests": ["waits before invoking"],
            },
        },
        "display": {"preferred": "focus", "inline_height": 520},
        "actions": [{"id": "run", "label": "Run code"}],
        "fallback_markdown": "Implement debounce in TypeScript.",
    })

    assert surface["render"]["template_id"] == "learning.code_lab"
    assert surface["render"]["props"]["language"] == "typescript"
    assert surface["render"]["props"]["languages"][1] == {
        "id": "javascript",
        "label": "JavaScript",
        "filename": "main.js",
        "starter_code": "export function debounce() {}",
    }
    assert surface["display"] == {
        "preferred": "focus",
        "inline_height": 520,
        "focusable": True,
    }
    assert surface["actions"] == [{"id": "run", "label": "Run code", "intent": "submit"}]


@pytest.mark.parametrize(("template_id", "props", "supplied_action", "expected_action"), [
    (
        "learning.code_lab",
        {
            "language": "python",
            "instructions": "Implement add(a, b).",
            "starter_code": "def add(a, b): pass",
            "tests": ["add(2, 3) == 5"],
        },
        {"id": "execute", "label": "Execute"},
        {"id": "run", "label": "Run code", "intent": "submit"},
    ),
    (
        "response.choice",
        {
            "prompt": "Continue?",
            "options": [
                {"id": "yes", "label": "Yes"},
                {"id": "no", "label": "No"},
            ],
        },
        {"id": "choose", "label": "Choose"},
        {"id": "answer", "label": "Submit answer", "intent": "submit"},
    ),
])
def test_response_surface_registered_templates_canonicalize_action_ids(
    template_id: str,
    props: dict[str, object],
    supplied_action: dict[str, str],
    expected_action: dict[str, str],
) -> None:
    surface = normalize_response_surface({
        "version": 1,
        "title": "Interactive template",
        "render": {
            "kind": "template",
            "template_id": template_id,
            "template_version": 1,
            "props": props,
        },
        "actions": [supplied_action],
        "fallback_markdown": "Interactive template",
    })

    assert surface["actions"] == [expected_action]


def test_response_surface_registered_template_keeps_canonical_action_label() -> None:
    surface = normalize_response_surface({
        "version": 1,
        "title": "Localized code lab",
        "render": {
            "kind": "template",
            "template_id": "learning.code_lab",
            "template_version": 1,
            "props": {
                "language": "python",
                "instructions": "Implement add(a, b).",
                "starter_code": "def add(a, b): pass",
                "tests": ["add(2, 3) == 5"],
            },
        },
        "actions": [{"id": "run", "label": "Execute checks"}],
        "fallback_markdown": "Implement add.",
    })

    assert surface["actions"] == [
        {"id": "run", "label": "Execute checks", "intent": "submit"}
    ]


def test_response_surface_generated_html_receives_validation_receipt() -> None:
    surface = normalize_response_surface(_generated_surface())

    validation = surface["render"]["validation"]
    assert validation["policy"] == RESPONSE_SURFACE_POLICY
    assert len(validation["code_hash"]) == 64
    assert surface["render"]["data"] == {"label": "Qualified leads"}


@pytest.mark.parametrize("html", [
    '<pre><code>&lt;div style="color:red"&gt;Hello&lt;/div&gt;</code></pre>',
    '<p>HTML examples may contain style="..." and javascript: as visible text.</p>',
])
def test_response_surface_generated_html_allows_blocked_words_in_visible_text(html: str) -> None:
    candidate = _generated_surface()
    candidate["render"]["code"]["html"] = html

    surface = normalize_response_surface(candidate)

    assert surface["render"]["code"]["html"] == html


def test_response_surface_generated_html_allows_color_text_in_ordinary_input_values() -> None:
    candidate = _generated_surface()
    candidate["render"]["code"]["html"] = '<input name="sku" value="#ff0000">'

    surface = normalize_response_surface(candidate)

    assert surface["render"]["code"]["html"] == '<input name="sku" value="#ff0000">'


def test_response_surface_generated_html_allows_static_svg_with_host_token_css() -> None:
    candidate = _generated_surface()
    candidate["render"]["code"]["html"] = (
        '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M2 8h12"></path></svg>'
    )
    candidate["render"]["code"]["css"] = "svg { stroke: var(--module-text); }"

    surface = normalize_response_surface(candidate)

    assert surface["render"]["code"]["html"].startswith("<svg")


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_response_surface_generated_data_rejects_non_finite_numbers(value: float) -> None:
    candidate = _generated_surface()
    candidate["render"]["data"] = {"score": value}

    with pytest.raises(ResponseSurfaceValidationError) as exc_info:
        normalize_response_surface(candidate)

    assert "json" in {item["code"] for item in exc_info.value.errors}


def test_response_surface_generated_data_rejects_recursive_objects() -> None:
    recursive: dict[str, object] = {}
    recursive["self"] = recursive
    candidate = _generated_surface()
    candidate["render"]["data"] = recursive

    with pytest.raises(ResponseSurfaceValidationError) as exc_info:
        normalize_response_surface(candidate)

    assert "json" in {item["code"] for item in exc_info.value.errors}


@pytest.mark.parametrize("css", [
    "#surface-activity { display: none !important; }",
    "#surface-root { display: grid; }",
    ":root { --module-accent: red; }",
    "body { background: red; }",
    "#surface-root { --module-text: transparent; }",
    ":scope { display: none; }",
    ":where(:scope) { position: fixed; inset: 0; }",
    ".cover { position: fixed; inset: 0; }",
    ".cover { position: sticky; top: 0; }",
    ".cover { z-index: 2147483647; }",
    r":scope { --module\-text: transparent; }",
    r".card { color: red !\69mportant; }",
    ".card { background: #ff0000; }",
    ".card { color: red; }",
    ".card { color: rgb(255 0 0); }",
    ".card { -webkit-text-stroke: 4px red; }",
    r".card { -webkit-text-stroke: 4px r\65 d; }",
    ".card { -webkit-text-stroke: 4px #ff0000; }",
    ".card { -webkit-text-stroke: 4px attr(data-accent type(<color>)); }",
    r".card { -webkit-text-stroke: 4px a\74 tr(data-accent type(<color>)); }",
    ".card { -webkit-text-stroke: 4px var(--accent); }",
    ".card { list-style-image: linear-gradient(red, blue); }",
    ".card { list-style-image: linear-gradient(CanvasText, AccentColor); }",
    ".card { list-style-image: linear-gradient(var(--accent), var(--module-surface)); }",
    ".card { color-scheme: dark; }",
    ".panel { color: red; }}",
])
def test_response_surface_generated_css_rejects_host_policy_overrides(css: str) -> None:
    candidate = _generated_surface()
    candidate["render"]["code"]["css"] = css

    with pytest.raises(ResponseSurfaceValidationError) as exc_info:
        normalize_response_surface(candidate)

    assert "css_policy" in {item["code"] for item in exc_info.value.errors}


@pytest.mark.parametrize("css", [
    ".card { background-image: url(data:image/svg+xml,%3Csvg%3E); }",
    r".card { background-image: u\72l(data:image/svg+xml,%3Csvg%3E); }",
])
def test_response_surface_generated_css_rejects_resource_loading(css: str) -> None:
    candidate = _generated_surface()
    candidate["render"]["code"]["css"] = css

    with pytest.raises(ResponseSurfaceValidationError) as exc_info:
        normalize_response_surface(candidate)

    assert "css_capability" in {item["code"] for item in exc_info.value.errors}


@pytest.mark.parametrize("css", [
    ".card { color: var(--module-text); background: var(--module-surface); }",
    ".card { border: 1px solid var(--module-border); box-shadow: 0 4px 12px var(--module-border); }",
    ".card { -webkit-text-stroke: 1px var(--module-accent); }",
    ".card { list-style-image: linear-gradient(var(--module-accent), var(--module-surface)); }",
    ".card { transition: background 160ms ease; }",
    ".card { animation-name: red; }",
    ".card { grid-area: red; }",
    '.card::before { content: "red"; }',
    '.card { font-family: "Arial Black", sans-serif; }',
    ".card { font-family: Arial Black, sans-serif; }",
    "@media (max-width: 640px) { .card { background: var(--module-row); } }",
])
def test_response_surface_generated_css_accepts_host_color_tokens(css: str) -> None:
    candidate = _generated_surface()
    candidate["render"]["code"]["css"] = css

    surface = normalize_response_surface(candidate)

    assert surface["render"]["code"]["css"] == css


def test_response_surface_tool_is_classified_as_read_only_rendering() -> None:
    classification = classify_runtime_tool("render_response_surface", {})

    assert classification.effect is RuntimeToolEffect.READ_ONLY
    assert classification.authorization is not None
    assert classification.authorization.action_key == "tool.render_response_surface.read"
    assert classification.authorization.capability_id == "response.render"


@pytest.mark.parametrize(
    ("field", "value", "issue"),
    [
        ("html", '<form action="https://example.com"><button>Send</button></form>', "html_capability"),
        ("html", '<a href="https://example.com">Leave Manor</a>', "html_capability"),
        ("html", '<button formaction="https://example.com">Send</button>', "html_capability"),
        (
            "html",
            '<section style="--module-accent:red;position:fixed;inset:0">Open</section>',
            "html_capability",
        ),
        (
            "html",
            '<svg/onload="window.top.location=\'https://example.com\'"></svg>',
            "html_capability",
        ),
        (
            "html",
            '<section/style="--module-accent:red;position:fixed;inset:0">Open</section>',
            "html_capability",
        ),
        (
            "html",
            '<div title=">" onload="window.top.location=\'https://example.com\'">Open</div>',
            "html_capability",
        ),
        ("html", '<svg><rect fill="#ff0000"></rect></svg>', "html_capability"),
        ("html", '<svg><path stroke="#ff0000"></path></svg>', "html_capability"),
        ("html", '<table bgcolor="#ff0000"><tr><td>Alert</td></tr></table>', "html_capability"),
        (
            "html",
            '<table border="5" bordercolor="red"><tr><td>Alert</td></tr></table>',
            "html_capability",
        ),
        ("html", '<font color="#ff0000">Alert</font>', "html_capability"),
        ("html", '<input type="color" value="#ff0000">', "html_capability"),
        ("html", '<div id="surface-root">Fake root</div>', "html_capability"),
        ("html", '<div id="surface-activity">Fake activity</div>', "html_capability"),
        ("html", '<div id="surface-error">Fake error</div>', "html_capability"),
        ("html", '</div><section>Outside root</section><div>', "html_capability"),
        (
            "html",
            '<svg><rect><set attributeName="fill" to="#ff0000"></set></rect></svg>',
            "html_capability",
        ),
        (
            "html",
            '<svg><rect><animate attributeName="fill" values="red;blue"></animate></rect></svg>',
            "html_capability",
        ),
        (
            "html",
            '<svg><rect><animateColor attributeName="fill" values="red;blue"></animateColor></rect></svg>',
            "html_capability",
        ),
        (
            "html",
            '<svg><filter id="f"><feColorMatrix values="0 0 0 0 1"></feColorMatrix></filter></svg>',
            "html_capability",
        ),
        (
            "html",
            '<img alt="Preview" src="data:image/svg+xml,%3Csvg%3E%3C/svg%3E">',
            "html_capability",
        ),
        ("javascript", "window.renderResponseSurface = () => {};", "generated_javascript_not_allowed"),
        ("javascript", "window['loc' + 'ation'] = 'https://example.com';", "generated_javascript_not_allowed"),
    ],
)
def test_response_surface_generated_html_rejects_host_and_network_capabilities(
    field: str,
    value: str,
    issue: str,
) -> None:
    candidate = _generated_surface()
    candidate["render"]["code"][field] = value

    with pytest.raises(ResponseSurfaceValidationError) as exc_info:
        normalize_response_surface(candidate)

    assert issue in {item["code"] for item in exc_info.value.errors}


def test_render_response_surface_schema_describes_each_template_shape() -> None:
    tool = get_tools()[0][0]["function"]
    schema = tool["parameters"]
    props = schema["properties"]["template_props"]

    assert "inline style or presentational color attributes" in tool["description"]
    assert "Do not use input type=color" in tool["description"]
    assert "bordercolor" in tool["description"]
    assert "reserved host ids" in schema["properties"]["html"]["description"]
    assert "Do not use CSS attr() values" in tool["description"]
    assert "input type=color are not allowed" in schema["properties"]["html"]["description"]
    assert "embedded resource attributes" in schema["properties"]["html"]["description"]
    assert "SVG animation/filter elements" in schema["properties"]["html"]["description"]
    assert "do not use CSS attr() values" in schema["properties"]["css"]["description"]
    assert "change color-scheme" in schema["properties"]["css"]["description"]

    assert props["additionalProperties"] is False
    assert set(props["properties"]) == {
        "language",
        "languages",
        "instructions",
        "starter_code",
        "tests",
        "prompt",
        "options",
    }
    assert props["properties"]["tests"]["items"] == {"type": "string", "maxLength": 2000}
    assert props["properties"]["languages"]["maxItems"] == 8
    assert props["properties"]["languages"]["items"]["required"] == ["id", "label", "filename"]
    assert props["properties"]["languages"]["items"]["additionalProperties"] is False
    assert props["properties"]["options"]["items"]["required"] == ["id", "label"]
    assert props["properties"]["options"]["items"]["additionalProperties"] is False
    assert schema["properties"]["javascript"]["maxLength"] == 0
    assert "registered template" in schema["properties"]["javascript"]["description"]
    assert "only for response-embedded UI" in tool["description"]
    assert "standalone webpage, Site, or page artifact" in tool["description"]
    assert "do not constrain that page" in tool["description"]
    assert "Manor Response Surface UI policy" in tool["description"]
    assert "--module-accent" in tool["description"]
    assert "the host scopes them below its root" in tool["description"]
    assert "do not use :scope" in tool["description"]
    assert "do not style host activity chrome" in schema["properties"]["css"]["description"]
    assert "Omit this for registered templates" in schema["properties"]["actions"]["description"]
    assert "choices" not in props["properties"]
    assert "question" not in props["properties"]


def test_response_surface_code_lab_rejects_invalid_language_switch_configuration() -> None:
    with pytest.raises(ResponseSurfaceValidationError) as exc_info:
        normalize_response_surface({
            "version": 1,
            "title": "Language lab",
            "render": {
                "kind": "template",
                "template_id": "learning.code_lab",
                "template_version": 1,
                "props": {
                    "language": "python",
                    "languages": [{
                        "id": "javascript",
                        "label": "JavaScript",
                        "filename": "main.js",
                    }],
                    "instructions": "Implement add(a, b).",
                    "starter_code": "def add(a, b): pass",
                    "tests": ["add(2, 3) == 5"],
                },
            },
            "actions": [{"id": "run", "label": "Run code"}],
            "fallback_markdown": "Implement add.",
        })

    assert "template_language_initial" in {item["code"] for item in exc_info.value.errors}


@pytest.mark.asyncio
async def test_render_response_surface_tool_returns_bounded_surface() -> None:
    envelope = SimpleNamespace(
        surface=ChatSurface.GLOBAL_OWNER_CHAT,
        profile=RuntimeProfile.OWNER_COPILOT,
        conversation_id="conv_1",
    )
    result = json.loads(await _render_response_surface(
        title="Choose a direction",
        render_kind="template",
        template_id="response.choice",
        template_props={
            "prompt": "Which direction should we take?",
            "options": [
                {"id": "quality", "label": "Improve quality"},
                {"id": "speed", "label": "Ship faster"},
            ],
        },
        fallback_markdown="Choose between quality and speed.",
        actions=[{"id": "answer", "label": "Submit answer"}],
        conversation_id="conv_1",
        _runtime_envelope_from_context=envelope,
    ))

    assert result["ok"] is True
    assert result["response_surface"]["render"]["template_id"] == "response.choice"
    assert get_tools()[0][0]["function"]["name"] == "render_response_surface"
    assert "render_response_surface" in CORE_CAPABILITIES["response.render"].tool_names


@pytest.mark.asyncio
async def test_render_response_surface_accepts_serialized_interactive_surface() -> None:
    envelope = SimpleNamespace(
        surface=ChatSurface.WORKSPACE_CHAT.value,
        profile=RuntimeProfile.WORKSPACE_OPERATOR,
        conversation_id="conv_string_surface",
    )
    result = json.loads(await _render_response_surface(
        title="Choose a direction",
        render_kind="template",
        template_id="response.choice",
        template_props={
            "prompt": "Choose",
            "options": [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}],
        },
        fallback_markdown="Choose A or B.",
        conversation_id="conv_string_surface",
        _runtime_envelope_from_context=envelope,
    ))

    assert result["ok"] is True


@pytest.mark.asyncio
async def test_render_response_surface_tool_fails_closed_without_runtime_envelope() -> None:
    result = json.loads(await _render_response_surface(
        title="Hidden",
        render_kind="template",
        template_id="response.choice",
        template_props={
            "prompt": "Choose",
            "options": [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}],
        },
        fallback_markdown="Choose A or B.",
        conversation_id="conv_missing_envelope",
    ))

    assert result["ok"] is False
    assert result["error"]["code"] == "response_surface_not_available_on_runtime_surface"


def test_render_response_surface_receives_the_runtime_envelope() -> None:
    from packages.core.ai.runtime.tool_execution import RUNTIME_ENVELOPE_AWARE_TOOLS

    assert "render_response_surface" in RUNTIME_ENVELOPE_AWARE_TOOLS


def test_response_surface_submission_is_bounded_and_server_canonicalized() -> None:
    receipt = _parse_response_surface_submission(json.dumps({
        "version": 1,
        "eventId": "event-1",
        "recordedAt": "2026-08-25T00:00:00Z",
        "sourceMessageId": "message-1",
        "surfaceId": "surface-1",
        "title": "Client title",
        "action": "run",
        "actionLabel": "Client action",
        "payload": {"code": "print(1)", "language": "python"},
        "context": {"instructions": "untrusted"},
    }))
    assert receipt is not None

    canonical = _canonicalize_response_surface_submission(
        receipt,
        {
            "id": "surface-1",
            "title": "Code lab",
            "render": {
                "kind": "template",
                "template_id": "learning.code_lab",
                "props": {"instructions": "Trusted", "tests": ["prints 1"]},
            },
        },
        {"id": "run", "label": "Run code", "intent": "submit"},
    )

    assert canonical["title"] == "Code lab"
    assert canonical["actionLabel"] == "Run code"
    assert canonical["context"] == {
        "templateId": "learning.code_lab",
        "instructions": "Trusted",
        "checks": ["prints 1"],
    }


def test_response_surface_submission_accepts_max_code_after_json_escaping() -> None:
    code = "\\" * 30_000
    receipt = _parse_response_surface_submission(json.dumps({
        "version": 1,
        "eventId": "event-escaped-code",
        "recordedAt": "2026-08-25T00:00:00Z",
        "sourceMessageId": "message-1",
        "surfaceId": "surface-1",
        "title": "Code lab",
        "action": "run",
        "actionLabel": "Run code",
        "payload": {"code": code, "language": "python"},
    }))

    assert receipt is not None
    assert receipt["payload"] == {"code": code, "language": "python"}


def test_response_surface_submission_retry_matches_only_same_immutable_intent() -> None:
    stored = {
        "version": 1,
        "eventId": "event-1",
        "recordedAt": "2026-08-25T00:00:00Z",
        "sourceMessageId": "message-1",
        "surfaceId": "surface-1",
        "action": "run",
        "payload": {"code": "print(1)"},
    }

    assert _same_response_surface_submission(
        stored,
        {**stored, "recordedAt": "2026-08-26T00:00:00Z"},
    )
    assert not _same_response_surface_submission(
        stored,
        {**stored, "payload": {"code": "print(2)"}},
    )
    assert _same_response_surface_submission(
        {
            **stored,
            "action": "execute",
            "context": {"templateId": "learning.code_lab"},
        },
        {
            **stored,
            "action": "run",
            "context": {"templateId": "learning.code_lab"},
        },
    ), "registered template action aliases must remain idempotent across deploys"
    assert not _same_response_surface_submission(
        {**stored, "action": "execute"},
        {**stored, "action": "run"},
    ), "unregistered generated actions must remain exact"


def test_response_surface_running_local_replay_is_not_terminal() -> None:
    assistant = SimpleNamespace(
        role="assistant",
        meta={"stream_status": "running"},
    )

    with pytest.raises(HTTPException) as exc_info:
        _response_surface_submission_replay_source(
            "conversation-1",
            assistant,
            None,
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == "response_surface_submission_in_progress"
    assert exc_info.value.headers == {"Retry-After": "1"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("meta", "private_detail"),
    [
        (
            {
                "stream_status": "error",
                "stream_error": True,
                "error_message": "sandbox failed",
            },
            "sandbox failed",
        ),
        (
            {
                "stream_status": "interrupted",
                "stream_interrupted": True,
            },
            "interrupted",
        ),
        (
            {
                "stop_reason": "credit_exhausted",
                "error": "No credits",
                "limit_detail": {"message": "No credits"},
            },
            "No credits",
        ),
        (
            {
                "stop_reason": "error",
                "error": "sandbox failed",
            },
            "sandbox failed",
        ),
    ],
)
async def test_response_surface_failed_local_replay_remains_failed(
    meta: dict,
    private_detail: str,
) -> None:
    assistant = SimpleNamespace(
        id="assistant-1",
        role="assistant",
        content="Persisted terminal content",
        meta=meta,
    )

    source = _response_surface_submission_replay_source(
        "conversation-1",
        assistant,
        None,
    )
    replay = "".join([frame async for frame in source])

    assert "event: error" in replay
    assert PUBLIC_REQUEST_FAILURE_MESSAGE in replay
    assert private_detail not in replay
    assert '"submission_reused": true' in replay


@pytest.mark.asyncio
async def test_response_surface_failure_replay_redacts_internal_database_error() -> None:
    assistant = SimpleNamespace(
        id="assistant-1",
        role="assistant",
        content="legacy raw failure",
        meta={
            "stream_status": "error",
            "stream_error": True,
            "error_message": (
                "Internal error: (sqlalchemy.dialects.postgresql.asyncpg.Error) "
                "SELECT messages.secret FROM messages"
            ),
        },
    )

    source = _response_surface_submission_replay_source(
        "conversation-1",
        assistant,
        None,
    )
    replay = "".join([frame async for frame in source])

    assert "Sorry, the request failed. Please try again." in replay
    assert "sqlalchemy" not in replay.lower()
    assert "SELECT messages.secret" not in replay


@pytest.mark.asyncio
async def test_response_surface_success_replay_projects_persisted_tool_failure() -> None:
    internal_failure = (
        "Tool key mcp__private_server__private_action does not match "
        "registered handler mcp__other__action"
    )
    assistant = SimpleNamespace(
        id="assistant-1",
        role="assistant",
        content=f"I could not continue because {internal_failure}.",
        meta={"stream_status": "completed"},
        token_usage={},
        tool_calls=[{
            "name": "mcp__private_server__private_action",
            "result": internal_failure,
            "raw_result": internal_failure,
            "status": "failed",
        }],
        attachments=None,
    )

    replay = "".join([
        frame
        async for frame in _response_surface_submission_replay_stream(
            "conversation-1",
            assistant,
        )
    ])

    assert "Sorry, the request failed. Please try again." in replay
    assert "registered handler" not in replay
    assert "raw_result" not in replay


def test_response_surface_failed_durable_replay_uses_runtime_events(
    monkeypatch,
) -> None:
    sentinel = object()
    runtime_run = SimpleNamespace(status="failed")
    monkeypatch.setattr(
        chat_router,
        "_durable_runtime_event_stream",
        lambda run: sentinel if run is runtime_run else None,
    )

    source = _response_surface_submission_replay_source(
        "conversation-1",
        SimpleNamespace(role="assistant", meta={"stream_status": "error"}),
        runtime_run,
    )

    assert source is sentinel


def test_response_surface_completed_durable_placeholder_replays_runtime_events(
    monkeypatch,
) -> None:
    sentinel = object()
    runtime_run = SimpleNamespace(status=RuntimeRunStatus.COMPLETED.value)
    assistant = SimpleNamespace(
        role="assistant",
        meta={"stream_status": "streaming"},
    )
    monkeypatch.setattr(
        chat_router,
        "_durable_runtime_event_stream",
        lambda run: sentinel if run is runtime_run else None,
    )

    source = _response_surface_submission_replay_source(
        "conversation-1",
        assistant,
        runtime_run,
    )

    assert source is sentinel


@pytest.mark.asyncio
async def test_response_surface_integrity_replay_conflict_releases_lease(
    monkeypatch,
) -> None:
    class _Lease:
        releases = 0

        async def release(self) -> None:
            self.releases += 1

    async def raise_conflict(*_args, **_kwargs):
        raise HTTPException(409, "response_surface_event_conflict")

    lease = _Lease()
    monkeypatch.setattr(
        chat_router,
        "_find_response_surface_submission_replay",
        raise_conflict,
    )

    with pytest.raises(HTTPException) as exc_info:
        await _response_surface_submission_replay_after_integrity_error(
            SimpleNamespace(),
            lease,
            conversation_id="conversation-1",
            user_id="user-1",
            submission={"eventId": "event-1"},
        )

    assert exc_info.value.detail == "response_surface_event_conflict"
    assert lease.releases == 1


def test_response_surface_event_has_database_unique_index() -> None:
    from packages.core.models.task import Message

    indexes = {index.name: index for index in Message.__table__.indexes}
    event_index = indexes["uq_messages_conversation_response_surface_event"]
    assert event_index.unique is True
    assert tuple(event_index.columns.keys()) == (
        "conversation_id",
        "response_surface_event_id",
    )


@pytest.mark.asyncio
async def test_response_surface_retry_finds_its_persisted_assistant(db_session) -> None:
    from packages.core.models.base import generate_ulid
    from packages.core.models.task import Message
    from packages.core.services.conversation_messages import (
        ORIGIN_USER_MESSAGE_ID_META_KEY,
    )

    conversation_id = generate_ulid()
    origin_id = generate_ulid()
    assistant_id = generate_ulid()
    submission = {
        "version": 1,
        "eventId": "event-replay-1",
        "recordedAt": "2026-08-26T00:00:00Z",
        "sourceMessageId": "message-source-1",
        "surfaceId": "surface-1",
        "action": "run",
        "payload": {"code": "print(1)"},
    }
    db_session.add_all([
        Message(
            id=origin_id,
            conversation_id=conversation_id,
            role="user",
            content="Run",
            response_surface_event_id=submission["eventId"],
            meta={"response_surface_submission": submission},
        ),
        Message(
            id=assistant_id,
            conversation_id=conversation_id,
            role="assistant",
            content="Passed",
            meta={ORIGIN_USER_MESSAGE_ID_META_KEY: origin_id},
        ),
    ])
    await db_session.flush()

    replay = await _find_response_surface_submission_replay(
        db_session,
        conversation_id=conversation_id,
        user_id=generate_ulid(),
        submission={**submission, "recordedAt": "2026-08-26T00:00:01Z"},
    )

    assert replay is not None
    assistant, runtime_run = replay
    assert assistant is not None and assistant.id == assistant_id
    assert runtime_run is None

    from sqlalchemy.exc import IntegrityError

    db_session.add(Message(
        id=generate_ulid(),
        conversation_id=conversation_id,
        role="user",
        content="Duplicate run",
        response_surface_event_id=submission["eventId"],
        meta={"response_surface_submission": submission},
    ))
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


@pytest.mark.parametrize("value", [
    "not-json",
    json.dumps({"version": 1}),
    (
        '{"version":1,"eventId":"event-nan","recordedAt":"now",'
        '"sourceMessageId":"message-1","surfaceId":"surface-1",'
        '"title":"Title","action":"run","actionLabel":"Run",'
        '"payload":{"score":NaN}}'
    ),
    json.dumps({
        "version": 1,
        "eventId": "bad event",
        "recordedAt": "now",
        "sourceMessageId": "message-1",
        "surfaceId": "surface-1",
        "title": "Title",
        "action": "run",
        "actionLabel": "Run",
        "payload": {},
    }),
])
def test_response_surface_submission_rejects_invalid_receipts(value: str) -> None:
    with pytest.raises(HTTPException) as exc_info:
        _parse_response_surface_submission(value)
    assert exc_info.value.status_code == 422


class _ScalarResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value

    def scalars(self):
        return self

    def all(self):
        return self.value


class _ResponseSurfaceDB:
    def __init__(self, source):
        self.source = source

    async def execute(self, _statement):
        return _ScalarResult(self.source)


class _SequenceResponseSurfaceDB:
    def __init__(self, *values):
        self.values = list(values)

    async def execute(self, _statement):
        return _ScalarResult(self.values.pop(0))


@pytest.mark.asyncio
async def test_response_surface_submission_uses_source_agent_subscription() -> None:
    conversation = SimpleNamespace(
        id="conversation-1",
        entity_id="entity-1",
        workspace_id="workspace-1",
        agent_id="conversation-agent",
    )
    submission = {"sourceMessageId": "assistant-1"}

    source_agent = await _response_surface_submission_agent(
        _SequenceResponseSurfaceDB(
            "subscription-1",
            SimpleNamespace(id="subscription-1", agent_id="surface-agent"),
        ),
        conversation=conversation,
        submission=submission,
    )

    assert source_agent.agent_id == "surface-agent"
    assert source_agent.agent_subscription_id == "subscription-1"


@pytest.mark.asyncio
async def test_response_surface_submission_falls_back_to_conversation_agent() -> None:
    conversation = SimpleNamespace(
        id="conversation-1",
        entity_id="entity-1",
        workspace_id=None,
        agent_id="conversation-agent",
    )

    source_agent = await _response_surface_submission_agent(
        _SequenceResponseSurfaceDB(None),
        conversation=conversation,
        submission={"sourceMessageId": "assistant-1"},
    )

    assert source_agent.agent_id == "conversation-agent"
    assert source_agent.agent_subscription_id is None


@pytest.mark.asyncio
async def test_response_surface_submission_keeps_workspace_manor_agent() -> None:
    conversation = SimpleNamespace(
        id="conversation-1",
        entity_id="entity-1",
        workspace_id="workspace-1",
        agent_id="manor-master",
    )

    source_agent = await _response_surface_submission_agent(
        _SequenceResponseSurfaceDB(None),
        conversation=conversation,
        submission={"sourceMessageId": "assistant-1"},
    )

    assert source_agent.agent_id == "manor-master"
    assert source_agent.agent_subscription_id is None


@pytest.mark.asyncio
async def test_response_surface_submission_resolves_unique_legacy_workspace_role() -> None:
    conversation = SimpleNamespace(
        id="conversation-1",
        entity_id="entity-1",
        workspace_id="workspace-1",
        agent_id="conversation-agent",
    )

    source_agent = await _response_surface_submission_agent(
        _SequenceResponseSurfaceDB(
            None,
            [SimpleNamespace(id="legacy-workspace-sub", agent_id="conversation-agent")],
        ),
        conversation=conversation,
        submission={"sourceMessageId": "assistant-1"},
    )

    assert source_agent.agent_id == "conversation-agent"
    assert source_agent.agent_subscription_id == "legacy-workspace-sub"


@pytest.mark.asyncio
async def test_response_surface_submission_rejects_ambiguous_legacy_workspace_role() -> None:
    conversation = SimpleNamespace(
        id="conversation-1",
        entity_id="entity-1",
        workspace_id="workspace-1",
        agent_id="conversation-agent",
    )

    with pytest.raises(HTTPException) as exc_info:
        await _response_surface_submission_agent(
            _SequenceResponseSurfaceDB(
                None,
                [
                    SimpleNamespace(id="legacy-sub-1", agent_id="conversation-agent"),
                    SimpleNamespace(id="legacy-sub-2", agent_id="conversation-agent"),
                ],
            ),
            conversation=conversation,
            submission={"sourceMessageId": "assistant-1"},
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == "Response surface Agent is ambiguous"


@pytest.mark.asyncio
async def test_response_surface_submission_validates_conversation_subscription() -> None:
    conversation = SimpleNamespace(
        id="conversation-1",
        entity_id="entity-1",
        workspace_id="workspace-1",
        agent_id="conversation-agent",
        agent_subscription_id="conversation-subscription",
    )

    source_agent = await _response_surface_submission_agent(
        _SequenceResponseSurfaceDB(
            None,
            SimpleNamespace(
                id="conversation-subscription",
                agent_id="conversation-agent",
            ),
        ),
        conversation=conversation,
        submission={"sourceMessageId": "assistant-1"},
    )

    assert source_agent.agent_id == "conversation-agent"
    assert source_agent.agent_subscription_id == "conversation-subscription"


@pytest.mark.asyncio
async def test_response_surface_submission_rejects_inactive_source_agent() -> None:
    conversation = SimpleNamespace(
        id="conversation-1",
        entity_id="entity-1",
        workspace_id="workspace-1",
        agent_id=None,
    )

    with pytest.raises(HTTPException) as exc_info:
        await _response_surface_submission_agent(
            _SequenceResponseSurfaceDB("subscription-1", None),
            conversation=conversation,
            submission={"sourceMessageId": "assistant-1"},
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == "Response surface Agent is unavailable"


def test_response_surface_submission_threads_source_subscription_into_runtime() -> None:
    source = inspect.getsource(chat_router.chat_stream)

    assert "response_surface_agent.agent_subscription_id" in source
    assert 'runtime_metadata_payload["agent_subscription_id"]' in source
    assert "author_subscription_id=response_surface_agent_subscription_id" in source
    assert "response_surface_event_id=response_surface_event_id" in source


@pytest.mark.asyncio
async def test_response_surface_runtime_uses_exact_source_subscription_context(
    db_session,
    monkeypatch,
) -> None:
    from packages.core.ai.runtime.prompt_adapter import ChatContext
    from packages.core.models.task import Conversation
    from packages.core.models.workspace import Agent, AgentSubscription
    from packages.core.services import runtime_chat_context, workspace_runtime

    db_session.add(Agent(
        id="surface-runtime-agent",
        entity_id="surface-runtime-entity",
        name="Surface Agent",
        status="active",
    ))
    db_session.add_all([
        AgentSubscription(
            id="surface-source-sub",
            entity_id="surface-runtime-entity",
            workspace_id="surface-runtime-workspace",
            agent_id="surface-runtime-agent",
            custom_prompt="Use the role that authored this response surface.",
            status="active",
        ),
        AgentSubscription(
            id="surface-conv-sub",
            entity_id="surface-runtime-entity",
            workspace_id="surface-runtime-workspace",
            agent_id="surface-runtime-agent",
            custom_prompt="This conversation fallback must not win.",
            status="active",
        ),
    ])
    db_session.add(Conversation(
        id="surface-runtime-conv",
        entity_id="surface-runtime-entity",
        workspace_id="surface-runtime-workspace",
        agent_id="surface-runtime-agent",
        agent_subscription_id="surface-conv-sub",
        scope="workspace_thread",
    ))
    await db_session.flush()

    captured: dict[str, object] = {}

    async def fake_resolve_workspace_runtime(_db, **kwargs):
        captured["agent_subscription_id"] = kwargs.get("agent_subscription_id")
        return workspace_runtime.WorkspaceRuntimeEnvelope(
            workspace_id="surface-runtime-workspace",
            runtime_profile="workspace_operator",
            tool_profile="workspace_agent",
            extra_context="Base workspace context.",
            bound_tool_names=set(),
            mcp_allowed_names=set(),
        )

    async def fake_assemble(_db, *, request, **kwargs):
        captured["request_metadata"] = request.metadata
        captured["extra_context"] = kwargs.get("legacy_extra_context")
        return SimpleNamespace(
            context=ChatContext(
                db=_db,
                agent_id=request.agent_id,
                workspace_id=request.workspace_id,
                conversation_id=request.conversation_id,
            ),
            tool_schemas=[],
            prompt="surface prompt",
        )

    async def empty_history(*_args, **_kwargs):
        return []

    monkeypatch.setattr(
        workspace_runtime,
        "resolve_workspace_runtime",
        fake_resolve_workspace_runtime,
    )
    monkeypatch.setattr(
        runtime_chat_context,
        "runtime_assemble_prompt_for_turn",
        fake_assemble,
    )
    monkeypatch.setattr(
        runtime_chat_context,
        "load_conversation_history",
        empty_history,
    )

    await runtime_chat_context.resolve_runtime_chat_context(
        db_session,
        "Run the selected response-surface action",
        entity_id="surface-runtime-entity",
        agent_id="surface-runtime-agent",
        conversation_id="surface-runtime-conv",
        workspace_id="surface-runtime-workspace",
        disable_tools=True,
        runtime_metadata={
            "agent_subscription_id": "surface-source-sub",
        },
    )

    assert captured["agent_subscription_id"] == "surface-source-sub"
    assert captured["request_metadata"] == {
        "agent_subscription_id": "surface-source-sub",
        "disable_tools": True,
        "legacy_path": "runtime_chat_context.resolve_runtime_chat_context",
    }
    assert "Use the role that authored this response surface." in str(
        captured["extra_context"]
    )
    assert "This conversation fallback must not win." not in str(
        captured["extra_context"]
    )


@pytest.mark.asyncio
async def test_response_surface_placeholder_preserves_source_subscription(
    db_session,
) -> None:
    from packages.core.models.task import Conversation, Message
    from packages.core.models.workspace import Agent, AgentSubscription
    from packages.core.services.conversation_messages import (
        create_assistant_stream_placeholder,
        save_or_update_assistant_stream_message,
    )

    db_session.add(Agent(
        id="surface-author-agent",
        entity_id="surface-author-entity",
        name="Surface Author",
        status="active",
    ))
    db_session.add_all([
        AgentSubscription(
            id="surface-author-source-sub",
            entity_id="surface-author-entity",
            workspace_id="surface-author-workspace",
            agent_id="surface-author-agent",
            status="active",
        ),
        AgentSubscription(
            id="surface-author-conv-sub",
            entity_id="surface-author-entity",
            workspace_id="surface-author-workspace",
            agent_id="surface-author-agent",
            status="active",
        ),
    ])
    db_session.add(Conversation(
        id="surface-author-conv",
        entity_id="surface-author-entity",
        workspace_id="surface-author-workspace",
        agent_id="surface-author-agent",
        agent_subscription_id="surface-author-conv-sub",
        scope="workspace_thread",
    ))
    await db_session.commit()

    placeholder = await create_assistant_stream_placeholder(
        db_session,
        "surface-author-conv",
        entity_id="surface-author-entity",
        workspace_id="surface-author-workspace",
        agent_id="surface-author-agent",
        author_subscription_id="surface-author-source-sub",
        meta={"origin_user_message_id": "surface-receipt-message"},
    )
    await db_session.commit()
    placeholder_id = placeholder.id

    saved_id = await save_or_update_assistant_stream_message(
        conversation_id="surface-author-conv",
        entity_id="surface-author-entity",
        workspace_id="surface-author-workspace",
        agent_id="surface-author-agent",
        message_id=placeholder_id,
        content="Handled by the source role.",
        meta={"stream_status": "completed"},
    )

    assert saved_id == placeholder_id
    db_session.expire_all()
    persisted = await db_session.get(Message, placeholder_id)
    assert persisted is not None
    assert persisted.author_subscription_id == "surface-author-source-sub"
    assert persisted.meta["origin_user_message_id"] == "surface-receipt-message"


def test_chat_message_response_exposes_surface_receipt_origin() -> None:
    message = SimpleNamespace(
        id="assistant-1",
        conversation_id="conversation-1",
        role="assistant",
        content="No credits",
        tool_calls=None,
        token_usage=None,
        attachments=None,
        message_kind="text",
        refs=None,
        meta={
            "origin_user_message_id": "receipt-message-1",
            "stop_reason": "credit_exhausted",
        },
        pending_action=None,
        resolved_at=None,
        resolution=None,
        created_at=None,
    )

    response = _to_chat_message_response(message)

    assert response.meta["origin_user_message_id"] == "receipt-message-1"
    assert response.stop_reason == "credit_exhausted"


def test_chat_message_response_hides_failed_surface_and_redacts_internal_error() -> None:
    message = SimpleNamespace(
        id="assistant-error-1",
        conversation_id="conversation-1",
        role="assistant",
        content=(
            "Internal error: (sqlalchemy.dialects.postgresql.asyncpg.Error) "
            "SELECT messages.secret FROM messages"
        ),
        tool_calls=None,
        token_usage=None,
        attachments=None,
        message_kind="text",
        refs=None,
        meta={
            "stream_status": "error",
            "stream_error": True,
            "stop_reason": "error",
            "error": (
                "Internal error: (sqlalchemy.dialects.postgresql.asyncpg.Error) "
                "SELECT messages.secret FROM messages"
            ),
            "assistant_blocks": [{
                "id": "surface-1",
                "type": "surface",
                "surface": _generated_surface(),
            }],
        },
        pending_action=None,
        resolved_at=None,
        resolution=None,
        created_at=None,
    )

    response = _to_chat_message_response(message)

    assert response.assistant_blocks is None
    assert response.content == "Sorry, the request failed. Please try again."
    assert response.error == "Sorry, the request failed. Please try again."
    assert "sqlalchemy" not in response.content.lower()
    assert "SELECT messages.secret" not in response.content
    assert "sqlalchemy" not in response.error.lower()
    assert "SELECT messages.secret" not in response.error


def test_chat_message_response_hides_internal_tool_contract_failures() -> None:
    internal_error = (
        "Error: unknown tool 'mcp__private_server__private_action'"
    )
    message = SimpleNamespace(
        id="assistant-tool-error-1",
        conversation_id="conversation-1",
        role="assistant",
        content="I could not complete that operation.",
        tool_calls=[{
            "name": "mcp__private_server__private_action",
            "result": internal_error,
            "raw_result": internal_error,
            "status": "error",
        }],
        token_usage=None,
        attachments=None,
        message_kind="text",
        refs=None,
        meta={
            "assistant_blocks": [{
                "id": "process-1",
                "type": "process",
                "steps": [{
                    "id": "step-1",
                    "name": "mcp__private_server__private_action",
                    "status": "error",
                    "result_preview": internal_error,
                }],
            }],
        },
        pending_action=None,
        resolved_at=None,
        resolution=None,
        created_at=None,
    )

    response = _to_chat_message_response(message)

    assert response.tool_calls[0]["result"] == (
        "This operation is temporarily unavailable. Please try again."
    )
    assert "raw_result" not in response.tool_calls[0]
    step = response.assistant_blocks[0]["steps"][0]
    assert step["result_preview"] == (
        "This operation is temporarily unavailable. Please try again."
    )
    assert "private_action" not in step["result_preview"]


def test_workspace_message_response_hides_internal_tool_contract_failures() -> None:
    from apps.api.routers.workspace_chat import _to_message

    internal_error = "Tool key private.execute is not registered"
    now = datetime.now(timezone.utc)
    message = SimpleNamespace(
        id="workspace-assistant-tool-error-1",
        conversation_id="workspace-conversation-1",
        content="Operation failed",
        tool_calls=[{
            "name": "private.execute",
            "result": internal_error,
            "raw_result": internal_error,
            "status": "error",
        }],
        created_at=now,
        message_kind="text",
        author_kind="agent",
        author_subscription_id="subscription-1",
        refs=None,
        attachments=None,
        meta={
            "assistant_blocks": [{
                "id": "process-1",
                "type": "process",
                "steps": [{
                    "id": "step-1",
                    "name": "private.execute",
                    "status": "error",
                    "result_preview": internal_error,
                }],
            }],
        },
        pending_action=None,
        resolved_at=None,
        resolution=None,
        resolved_by_user_id=None,
    )

    response = _to_message(message)

    assert response.tool_calls[0]["result"] == (
        "This operation is temporarily unavailable. Please try again."
    )
    assert "raw_result" not in response.tool_calls[0]
    step = response.assistant_blocks[0]["steps"][0]
    assert step["result_preview"] == (
        "This operation is temporarily unavailable. Please try again."
    )
    assert response.meta["assistant_blocks"][0]["steps"][0] == step


@pytest.mark.asyncio
async def test_response_surface_submission_binds_to_persisted_source_and_action() -> None:
    source = SimpleNamespace(meta={
        "assistant_blocks": [{
            "id": "surface-1",
            "type": "surface",
            "version": 1,
            "title": "Choose",
            "render": {
                "kind": "template",
                "template_id": "response.choice",
                "template_version": 1,
                "props": {
                    "prompt": "Choose",
                    "options": [
                        {"id": "a", "label": "A"},
                        {"id": "b", "label": "B"},
                    ],
                },
            },
            "actions": [{"id": "answer", "label": "Submit", "intent": "submit"}],
        }],
    })
    receipt = {
        "version": 1,
        "eventId": "event-1",
        "recordedAt": "2026-08-25T00:00:00Z",
        "sourceMessageId": "message-1",
        "surfaceId": "surface-1",
        "title": "Forged title",
        "action": "answer",
        "actionLabel": "Forged label",
        "payload": {"choice": "a"},
    }

    bound = await _bind_response_surface_submission(
        _ResponseSurfaceDB(source),
        conversation_id="conversation-1",
        receipt=receipt,
    )

    assert bound is not None
    assert bound["title"] == "Choose"
    assert bound["actionLabel"] == "Submit"
    assert bound["payload"] == {"choice": "a"}

    with pytest.raises(HTTPException) as exc_info:
        await _bind_response_surface_submission(
            _ResponseSurfaceDB(source),
            conversation_id="conversation-1",
            receipt={**receipt, "action": "forged"},
        )
    assert exc_info.value.status_code == 409


@pytest.mark.parametrize((
    "template_id",
    "props",
    "historical_action",
    "canonical_action",
    "canonical_label",
    "payload",
), [
    (
        "learning.code_lab",
        {"language": "python"},
        {"id": "execute", "label": "Execute", "intent": "submit"},
        "run",
        "Run code",
        {"language": "python", "code": "print('ok')"},
    ),
    (
        "response.choice",
        {"options": [{"id": "continue", "label": "Continue"}]},
        {"id": "choose", "label": "Choose", "intent": "submit"},
        "answer",
        "Submit answer",
        {"choice": "continue"},
    ),
])
@pytest.mark.asyncio
async def test_response_surface_submission_binds_historical_template_actions(
    template_id: str,
    props: dict[str, object],
    historical_action: dict[str, str],
    canonical_action: str,
    canonical_label: str,
    payload: dict[str, str],
) -> None:
    source = SimpleNamespace(meta={
        "assistant_blocks": [{
            "id": "surface-historical",
            "type": "surface",
            "title": "Historical template",
            "render": {
                "kind": "template",
                "template_id": template_id,
                "props": props,
            },
            "actions": [historical_action],
        }],
    })
    for submitted_action in (historical_action["id"], canonical_action):
        receipt = {
            "version": 1,
            "eventId": f"event-{submitted_action}",
            "recordedAt": "2026-08-25T00:00:00Z",
            "sourceMessageId": "message-historical",
            "surfaceId": "surface-historical",
            "title": "Historical template",
            "action": submitted_action,
            "actionLabel": historical_action["label"],
            "payload": payload,
        }

        bound = await _bind_response_surface_submission(
            _ResponseSurfaceDB(source),
            conversation_id="conversation-1",
            receipt=receipt,
        )

        assert bound is not None
        assert bound["action"] == canonical_action
        assert bound["actionLabel"] == canonical_label
        assert bound["payload"] == payload


@pytest.mark.asyncio
async def test_response_surface_submission_keeps_generated_actions_exact() -> None:
    source = SimpleNamespace(meta={
        "assistant_blocks": [{
            "id": "surface-generated",
            "type": "surface",
            "title": "Generated surface",
            "render": {"kind": "sandboxed_html"},
            "actions": [{"id": "submit", "label": "Apply", "intent": "submit"}],
        }],
    })
    receipt = {
        "version": 1,
        "eventId": "event-generated",
        "recordedAt": "2026-08-25T00:00:00Z",
        "sourceMessageId": "message-generated",
        "surfaceId": "surface-generated",
        "title": "Generated surface",
        "action": "run",
        "actionLabel": "Run code",
        "payload": {},
    }

    with pytest.raises(HTTPException) as exc_info:
        await _bind_response_surface_submission(
            _ResponseSurfaceDB(source),
            conversation_id="conversation-1",
            receipt=receipt,
        )

    assert exc_info.value.status_code == 409


@pytest.mark.asyncio
async def test_response_surface_submission_rejects_unregistered_choice() -> None:
    source = SimpleNamespace(meta={
        "assistant_blocks": [{
            "id": "surface-1",
            "type": "surface",
            "title": "Choose",
            "render": {
                "kind": "template",
                "template_id": "response.choice",
                "props": {
                    "options": [
                        {"id": "a", "label": "A"},
                        {"id": "b", "label": "B"},
                    ],
                },
            },
            "actions": [{"id": "answer", "label": "Submit", "intent": "submit"}],
        }],
    })
    receipt = {
        "version": 1,
        "eventId": "event-invalid-choice",
        "recordedAt": "2026-08-25T00:00:00Z",
        "sourceMessageId": "message-1",
        "surfaceId": "surface-1",
        "title": "Choose",
        "action": "answer",
        "actionLabel": "Submit",
        "payload": {"choice": "forged"},
    }

    with pytest.raises(HTTPException) as exc_info:
        await _bind_response_surface_submission(
            _ResponseSurfaceDB(source),
            conversation_id="conversation-1",
            receipt=receipt,
        )

    assert exc_info.value.status_code == 422


@pytest.mark.asyncio
async def test_response_surface_submission_validates_and_canonicalizes_code_lab() -> None:
    source = SimpleNamespace(meta={
        "assistant_blocks": [{
            "id": "surface-code",
            "type": "surface",
            "title": "Code",
            "render": {
                "kind": "template",
                "template_id": "learning.code_lab",
                "props": {
                    "language": "python",
                    "languages": [
                        {"id": "python", "label": "Python"},
                        {"id": "javascript", "label": "JavaScript"},
                    ],
                },
            },
            "actions": [{"id": "run", "label": "Run code", "intent": "submit"}],
        }],
    })
    receipt = {
        "version": 1,
        "eventId": "event-code",
        "recordedAt": "2026-08-25T00:00:00Z",
        "sourceMessageId": "message-code",
        "surfaceId": "surface-code",
        "title": "Code",
        "action": "run",
        "actionLabel": "Run code",
        "payload": {"language": " JavaScript ", "code": "console.log(1)", "extra": True},
    }

    bound = await _bind_response_surface_submission(
        _ResponseSurfaceDB(source),
        conversation_id="conversation-1",
        receipt=receipt,
    )

    assert bound is not None
    assert bound["payload"] == {"language": "javascript", "code": "console.log(1)"}

    escaped_code = "\\" * 30_000
    escaped_bound = await _bind_response_surface_submission(
        _ResponseSurfaceDB(source),
        conversation_id="conversation-1",
        receipt={
            **receipt,
            "eventId": "event-code-escaped",
            "payload": {"language": "python", "code": escaped_code},
        },
    )
    assert escaped_bound is not None
    assert escaped_bound["payload"] == {
        "language": "python",
        "code": escaped_code,
    }

    for invalid_payload in (
        {"language": "ruby", "code": "puts 1"},
        {"language": "python", "code": ""},
        {"language": "python", "code": "x" * 30_001},
    ):
        with pytest.raises(HTTPException) as exc_info:
            await _bind_response_surface_submission(
                _ResponseSurfaceDB(source),
                conversation_id="conversation-1",
                receipt={**receipt, "payload": invalid_payload},
            )
        assert exc_info.value.status_code == 422


@pytest.mark.asyncio
async def test_response_surface_submission_rejects_cross_conversation_source() -> None:
    with pytest.raises(HTTPException) as exc_info:
        await _bind_response_surface_submission(
            _ResponseSurfaceDB(None),
            conversation_id="conversation-2",
            receipt={
                "version": 1,
                "eventId": "event-1",
                "recordedAt": "2026-08-25T00:00:00Z",
                "sourceMessageId": "message-from-another-conversation",
                "surfaceId": "surface-1",
                "title": "Choose",
                "action": "answer",
                "actionLabel": "Submit",
                "payload": {"choice": "a"},
            },
        )
    assert exc_info.value.status_code == 409


@pytest.mark.asyncio
async def test_render_response_surface_tool_fails_closed_on_external_chat() -> None:
    envelope = SimpleNamespace(
        surface=ChatSurface.PUBLIC_CUSTOMER_CHAT,
        profile=RuntimeProfile.EXTERNAL_CUSTOMER_SAFE,
        conversation_id="conv_external",
    )
    result = json.loads(await _render_response_surface(
        title="Hidden",
        render_kind="template",
        template_id="response.choice",
        template_props={
            "prompt": "Choose",
            "options": [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}],
        },
        fallback_markdown="Choose A or B.",
        conversation_id="conv_external",
        _runtime_envelope_from_context=envelope,
    ))

    assert result["ok"] is False
    assert result["error"]["code"] == "response_surface_not_available_on_external_surface"


@pytest.mark.asyncio
async def test_render_response_surface_tool_rejects_background_runtime() -> None:
    envelope = SimpleNamespace(
        surface=ChatSurface.SCHEDULED_AGENT_RUN,
        profile=RuntimeProfile.BACKGROUND_WORKER,
        conversation_id="conv_background",
    )
    result = json.loads(await _render_response_surface(
        title="Hidden",
        render_kind="template",
        template_id="response.choice",
        template_props={
            "prompt": "Choose",
            "options": [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}],
        },
        fallback_markdown="Choose A or B.",
        conversation_id="conv_background",
        _runtime_envelope_from_context=envelope,
    ))

    assert result["ok"] is False
    assert result["error"]["code"] == "response_surface_not_available_on_runtime_surface"


def test_assistant_blocks_persist_validated_response_surface() -> None:
    surface = normalize_response_surface(_generated_surface())
    tool_result = json.dumps({"ok": True, "response_surface": surface})
    builder = AssistantBlocksBuilder()

    builder.start_tool("render_response_surface", {})
    builder.end_tool(
        "render_response_surface",
        result="Surface rendered",
        structured_result=tool_result,
    )

    blocks = builder.meta()["assistant_blocks"]
    surface_block = next(block for block in blocks if block["type"] == "surface")
    assert surface_block["id"] == "blk_surface_1"
    assert surface_block["render"]["validation"]["policy"] == RESPONSE_SURFACE_POLICY


def test_assistant_blocks_ignore_unvalidated_response_surface() -> None:
    builder = AssistantBlocksBuilder()
    builder.start_tool("render_response_surface", {})
    builder.end_tool(
        "render_response_surface",
        result="Surface rejected",
        structured_result=json.dumps({
            "ok": True,
            "response_surface": _generated_surface(
                render={
                    "kind": "sandboxed_html",
                    "code": {
                        "version": 1,
                        "runtime": "sandboxed_html",
                        "html": "<main>Unsafe</main>",
                        "css": "",
                        "javascript": "window.renderResponseSurface = () => fetch('/private')",
                    },
                },
            ),
        }),
    )

    assert all(block["type"] != "surface" for block in builder.blocks())
