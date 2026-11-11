from __future__ import annotations

import json
from typing import Any

from packages.core.ai.runtime.tool_context import (
    runtime_tool_call_context_from_kwargs,
    runtime_tool_call_context_is_external_customer,
)
from packages.core.ai.runtime.surfaces import ChatSurface
from packages.core.services.response_surfaces import (
    ResponseSurfaceValidationError,
    normalize_response_surface,
)


_LINKED_NAME = {
    "type": "object",
    "properties": {"name": {"type": "string", "maxLength": 160}, "url": {"type": "string", "maxLength": 2000}},
    "required": ["name"],
    "additionalProperties": False,
}
_CARD_TEMPLATE_PROPS = {
    # card.email
    "status": {"type": "string", "enum": ["received", "draft"]},
    "from_name": {"type": "string", "maxLength": 160},
    "from_address": {"type": "string", "maxLength": 254},
    "to": {"type": "array", "maxItems": 10, "items": {"type": "string", "maxLength": 160}},
    "subject": {"type": "string", "maxLength": 300},
    "sent_at": {"type": "string", "maxLength": 40, "description": "As the person should read it, such as 09:41."},
    "snippet": {"type": "string", "maxLength": 600},
    "body": {"type": "string", "maxLength": 6000},
    "attachments": {"type": "array", "maxItems": 10, "items": _LINKED_NAME},
    "url": {"type": "string", "maxLength": 2000},
    # card.delivery
    "items": {
        "type": "array",
        "minItems": 1,
        "maxItems": 20,
        "items": {
            "type": "object",
            "properties": {
                "label": {"type": "string", "maxLength": 120},
                "status": {"type": "string", "enum": ["sent", "published", "scheduled", "failed"]},
                "count": {"type": "integer", "minimum": 0},
                "detail": {"type": "string", "maxLength": 160},
                "url": {"type": "string", "maxLength": 2000},
            },
            "required": ["label", "status"],
            "additionalProperties": False,
        },
    },
    # card.search_results
    "results": {
        "type": "array",
        "minItems": 1,
        "maxItems": 10,
        "items": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "maxLength": 200},
                "source": {"type": "string", "maxLength": 120},
                "snippet": {"type": "string", "maxLength": 300},
                "when": {"type": "string", "maxLength": 40},
                "url": {"type": "string", "maxLength": 2000},
            },
            "required": ["title", "source"],
            "additionalProperties": False,
        },
    },
    "total": {"type": "integer", "minimum": 0},
    # card.post
    "posts": {
        "type": "array",
        "minItems": 1,
        "maxItems": 6,
        "items": {
            "type": "object",
            "properties": {
                "platform": {"type": "string", "maxLength": 40},
                "account": {"type": "string", "maxLength": 80},
                "format": {"type": "string", "enum": ["vertical_video", "horizontal_video", "square_image", "portrait_image", "text"]},
                "title": {"type": "string", "maxLength": 200},
                "text": {"type": "string", "maxLength": 2000},
                "media": _LINKED_NAME,
                "images": {
                    "type": "array",
                    "maxItems": 20,
                    "items": _LINKED_NAME,
                    "description": "Every image of an image post, in order. Use instead of media.",
                },
                "status": {"type": "string", "enum": ["draft", "scheduled", "published", "failed"]},
                "status_detail": {"type": "string", "maxLength": 160},
                "metrics": {
                    "type": "array",
                    "maxItems": 4,
                    "items": {
                        "type": "object",
                        "properties": {"label": {"type": "string", "maxLength": 24}, "value": {"type": "string", "maxLength": 24}},
                        "required": ["label", "value"],
                        "additionalProperties": False,
                    },
                },
                "url": {"type": "string", "maxLength": 2000},
            },
            "required": ["platform", "status"],
            "additionalProperties": False,
        },
    },
    # card.comment
    "platform": {"type": "string", "maxLength": 40},
    "account": {"type": "string", "maxLength": 80},
    "post_title": {"type": "string", "maxLength": 200},
    "post_url": {"type": "string", "maxLength": 2000},
    "comments": {
        "type": "array",
        "minItems": 1,
        "maxItems": 20,
        "items": {
            "type": "object",
            "properties": {
                "author": {"type": "string", "maxLength": 80},
                "text": {"type": "string", "maxLength": 1000},
                "when": {"type": "string", "maxLength": 40},
                "likes": {"type": "integer", "minimum": 0},
                "status": {
                    "type": "string",
                    "enum": ["draft", "scheduled", "sent", "failed"],
                    "description": "Only for a comment we wrote; leave it out for a comment we received.",
                },
                "url": {"type": "string", "maxLength": 2000},
                "reply": {
                    "type": "object",
                    "properties": {
                        "author": {"type": "string", "maxLength": 80},
                        "text": {"type": "string", "maxLength": 1000},
                        "status": {"type": "string", "enum": ["draft", "scheduled", "sent", "failed"]},
                    },
                    "required": ["text", "status"],
                    "additionalProperties": False,
                },
            },
            "required": ["author", "text"],
            "additionalProperties": False,
        },
    },
    # card.person
    "name": {"type": "string", "maxLength": 120, "description": "card.person or card.stock."},
    "title": {"type": "string", "maxLength": 120},
    "organization": {"type": "string", "maxLength": 120},
    "email": {"type": "string", "maxLength": 254},
    "phone": {"type": "string", "maxLength": 40},
    "note": {"type": "string", "maxLength": 300},
    # card.event
    "events": {
        "type": "array",
        "minItems": 1,
        "maxItems": 8,
        "items": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "maxLength": 200},
                "date": {"type": "string", "description": "YYYY-MM-DD."},
                "time": {"type": "string", "maxLength": 40, "description": "As the person should read it, such as 14:00-16:00."},
                "location": {"type": "string", "maxLength": 200},
                "attendees": {"type": "array", "maxItems": 20, "items": {"type": "string", "maxLength": 80}},
                "status": {"type": "string", "enum": ["proposed", "confirmed", "cancelled"]},
                "url": {"type": "string", "maxLength": 2000},
            },
            "required": ["title", "date"],
            "additionalProperties": False,
        },
    },
    # card.stock
    "symbol": {"type": "string", "maxLength": 20},
    "price": {"type": "number"},
    "currency": {"type": "string", "maxLength": 8, "description": "A sign such as $ or ¥."},
    "change": {"type": "number"},
    "change_percent": {"type": "number"},
    "as_of": {"type": "string", "maxLength": 60, "description": "How fresh the quote is, as the person should read it."},
    "history": {"type": "array", "maxItems": 240, "items": {"type": "number"}, "description": "Recent prices, oldest first."},
    # card.product
    "products": {
        "type": "array",
        "minItems": 1,
        "maxItems": 6,
        "items": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "maxLength": 200},
                "price": {"type": "string", "maxLength": 40, "description": "As displayed, such as ¥1,299."},
                "seller": {"type": "string", "maxLength": 120},
                "detail": {"type": "string", "maxLength": 160},
                "image": _LINKED_NAME,
                "url": {"type": "string", "maxLength": 2000},
            },
            "required": ["name"],
            "additionalProperties": False,
        },
    },
    # card.map
    "places": {
        "type": "array",
        "minItems": 1,
        "maxItems": 12,
        "items": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "maxLength": 160},
                "address": {"type": "string", "maxLength": 240},
                "detail": {"type": "string", "maxLength": 160},
                "lat": {"type": "number", "description": "A rough latitude estimated from what you know of the place is fine. A place without one is not on the map."},
                "lng": {"type": "number", "description": "A rough longitude estimated from what you know of the place is fine. A place without one is not on the map."},
                "url": {"type": "string", "maxLength": 2000},
            },
            "required": ["name"],
            "additionalProperties": False,
        },
    },
    # card.chart
    "chart_type": {"type": "string", "enum": ["line", "bar", "share", "ranking"]},
    "categories": {"type": "array", "minItems": 1, "maxItems": 24, "items": {"type": "string", "maxLength": 40}},
    "series": {
        "type": "array",
        "minItems": 1,
        "maxItems": 3,
        "description": "One value per category. Only line charts take more than one series.",
        "items": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "maxLength": 40},
                "values": {"type": "array", "maxItems": 24, "items": {"type": "number"}},
            },
            "required": ["name", "values"],
            "additionalProperties": False,
        },
    },
    "unit": {"type": "string", "maxLength": 12},
    "reference": {
        "type": "object",
        "description": "A target line on a line chart.",
        "properties": {"label": {"type": "string", "maxLength": 40}, "value": {"type": "number"}},
        "required": ["label", "value"],
        "additionalProperties": False,
    },
}

RENDER_RESPONSE_SURFACE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "render_response_surface",
        "description": (
            "Return an interactive visual surface embedded inside the current Manor Chat or Task "
            "Session response when text alone is materially less useful. This tool is only for "
            "response-embedded UI. If the user asks to create a standalone webpage, Site, or page "
            "artifact, do not use this tool and do not constrain that page to the Response Surface "
            "visual policy; use the appropriate page or artifact workflow and follow the user's "
            "requested design. Prefer a registered template for code labs or choices; use "
            "sandboxed_html only for a one-off interface with no suitable template. "
            "To show an email or draft, what was sent or published, search results, social posts, comments, "
            "a person, events, a stock quote, products, places or a chart, always use render_kind template with the matching card template; never "
            "hand-write sandboxed_html or SVG for these. Fill a card only with values "
            "from tool results or the user: never invent numbers, recipients, statuses or links. A "
            "card shows; it does not send or publish anything, so call the real tool first and ask "
            "decisions through the normal approval or question flow. Generated "
            "sandboxed_html must follow the Manor Response Surface UI policy: calm warm-neutral, "
            "low-saturation, content-first UI; use the host tokens --module-text, --module-strong, "
            "--module-muted, --module-faint, --module-surface, --module-row, --module-sunken, "
            "--module-border, --module-accent, --module-on-accent, and --module-ring instead of "
            "raw colors when those "
            "roles apply; reserve the accent for the primary action, focus, and direct form-control "
            "state; keep secondary chrome neutral; use semantic accessible controls; and make the "
            "layout responsive. Use local class or element selectors; the host scopes them below "
            "its root. Do not select #surface-root, #surface-activity, or #surface-error, do not use "
            ":scope, position:fixed, position:sticky, or z-index, and do not redefine :root, html, body, "
            "host --module-* tokens, or use !important. "
            "Do not use inline style or presentational color attributes such as fill, stroke, "
            "color, bgcolor, or bordercolor; put response-surface styling in css and use the host --module-* "
            "tokens so the host can validate and scope it. Do not embed resource attributes such "
            "as src, srcset, poster, or background, and do not use SVG animation or filter "
            "elements. Do not use the reserved host ids surface-root, surface-activity, or "
            "surface-error. Do not use input type=color. Do not "
            "use CSS attr() values; put "
            "presentation values directly in validated CSS through the host tokens. "
            "Generated HTML is declarative: put visible content directly in html, give fields "
            "names, and connect controls with data-manor-action; JavaScript is not allowed. Keep "
            "the title, description, instructions, prompts, action labels, and fallback user-facing, "
            "and always include a complete text fallback. Do not mention template IDs, renderers, "
            "schemas, tests, demos, or other implementation details unless the user explicitly asks "
            "for technical diagnostics. The host validates and isolates the result. Do not "
            "put raw HTML in the assistant message."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "maxLength": 120},
                "description": {"type": "string", "maxLength": 300},
                "render_kind": {
                    "type": "string",
                    "enum": ["template", "sandboxed_html"],
                },
                "template_id": {
                    "type": "string",
                    "enum": [
                        "learning.code_lab",
                        "response.choice",
                        "card.email",
                        "card.delivery",
                        "card.search_results",
                        "card.person",
                        "card.event",
                        "card.stock",
                        "card.product",
                        "card.map",
                        "card.post",
                        "card.comment",
                        "card.chart",
                    ],
                    "description": (
                        "Use learning.code_lab with language, optional languages, instructions, starter_code, and string tests. "
                        "Use response.choice with prompt and 2-12 id/label options. "
                        "card.email: subject, snippet or body, status, from_name, from_address, to, sent_at, attachments, url. "
                        "card.delivery: items, one per outcome, with label, status, count, detail, url. "
                        "card.search_results: results with an unnumbered title, source, snippet, when, url; total if more exist. "
                        "card.post: posts, one per platform, with platform, format, status, text; media for one video or image, images for an image post. "
                        "card.comment: platform, post_title, comments with author, text, when, likes, reply; total if more exist. "
                        "card.chart: chart_type, categories, series, unit, reference. "
                        "card.person: name, title, organization, email, phone, note, url. "
                        "card.event: events with title, date, time, location, attendees, status. "
                        "card.stock: name, symbol, price, currency, change, change_percent, as_of, history. "
                        "card.product: products with name, price, seller, detail, image, url. "
                        "card.map: places with name, address, detail, lat, lng, url. "
                        "Links must be http(s) or a Manor file address such as /viewer/<document id>."
                    ),
                },
                "template_props": {
                    "type": "object",
                    "description": (
                        "Template-specific props. learning.code_lab: language, optional languages (id, label, "
                        "filename, optional starter_code), instructions, starter_code, tests (strings). "
                        "response.choice: prompt, options (objects with id, label, optional "
                        "description). Never use question/choices or object-shaped tests."
                    ),
                    "properties": {
                        "language": {"type": "string", "maxLength": 32},
                        "languages": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": 8,
                            "items": {
                                "type": "object",
                                "properties": {
                                    "id": {
                                        "type": "string",
                                        "pattern": "^[a-z][a-z0-9_.-]{0,31}$",
                                    },
                                    "label": {"type": "string", "maxLength": 40},
                                    "filename": {"type": "string", "maxLength": 80},
                                    "starter_code": {"type": "string", "maxLength": 10000},
                                },
                                "required": ["id", "label", "filename"],
                                "additionalProperties": False,
                            },
                        },
                        "instructions": {"type": "string", "maxLength": 4000},
                        "starter_code": {"type": "string", "maxLength": 30000},
                        "tests": {
                            "type": "array",
                            "maxItems": 20,
                            "items": {"type": "string", "maxLength": 2000},
                        },
                        **_CARD_TEMPLATE_PROPS,
                        "prompt": {"type": "string", "maxLength": 1000},
                        "options": {
                            "type": "array",
                            "minItems": 2,
                            "maxItems": 12,
                            "items": {
                                "type": "object",
                                "properties": {
                                    "id": {
                                        "type": "string",
                                        "pattern": "^[a-z][a-z0-9_.-]{0,63}$",
                                    },
                                    "label": {"type": "string", "maxLength": 160},
                                    "description": {"type": "string", "maxLength": 400},
                                },
                                "required": ["id", "label"],
                                "additionalProperties": False,
                            },
                        },
                    },
                    "additionalProperties": False,
                },
                "html": {
                    "type": "string",
                    "maxLength": 20000,
                    "description": (
                        "Declarative markup for an embedded response surface, not a standalone page. "
                        "Use semantic controls with names and data-manor-action bindings. Inline "
                        "style, presentational color attributes, embedded resource attributes, reserved "
                        "host ids, SVG animation/filter elements, and input type=color are not allowed; put "
                        "styling in css and use the host --module-* tokens."
                    ),
                },
                "css": {
                    "type": "string",
                    "maxLength": 30000,
                    "description": (
                        "Local CSS that the host scopes below its root, using the host --module-* "
                        "tokens. Follow the Manor Response Surface UI policy; do not select the host "
                        "root, do not style host activity chrome or host error chrome, and do not use "
                        ":scope, fixed/sticky positioning, or z-index. Do not "
                        "redefine document/root styles, do not change color-scheme, and do not use "
                        "CSS attr() values."
                    ),
                },
                "javascript": {
                    "type": "string",
                    "maxLength": 0,
                    "description": "Must be empty. Use a registered template when JavaScript behavior is required.",
                },
                "data": {"type": "object"},
                "preferred_display": {
                    "type": "string",
                    "enum": ["inline", "focus"],
                },
                "inline_height": {
                    "type": "integer",
                    "minimum": 180,
                    "maximum": 720,
                },
                "actions": {
                    "type": "array",
                    "maxItems": 8,
                    "description": (
                        "Controls declared in sandboxed_html must use matching data-manor-action ids. "
                        "Omit this for registered templates; their reviewed action contract is supplied automatically."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string", "pattern": "^[a-z][a-z0-9_.-]{0,63}$"},
                            "label": {"type": "string", "maxLength": 80},
                        },
                        "required": ["id", "label"],
                        "additionalProperties": False,
                    },
                },
                "fallback_markdown": {"type": "string", "maxLength": 4000},
            },
            "required": ["title", "render_kind", "fallback_markdown"],
            "additionalProperties": False,
        },
    },
}

_INTERACTIVE_CHAT_SURFACES = frozenset({
    ChatSurface.GLOBAL_OWNER_CHAT,
    ChatSurface.AGENT_DM,
    ChatSurface.WORKSPACE_CHAT,
    ChatSurface.TASK_COMMENT_THREAD,
})
_INTERACTIVE_CHAT_SURFACE_VALUES = frozenset(
    surface.value for surface in _INTERACTIVE_CHAT_SURFACES
)


def _json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, allow_nan=False)


async def _render_response_surface(
    title: str,
    render_kind: str,
    fallback_markdown: str,
    description: str = "",
    template_id: str | None = None,
    template_props: dict[str, Any] | None = None,
    html: str = "",
    css: str = "",
    javascript: str = "",
    data: dict[str, Any] | None = None,
    preferred_display: str = "inline",
    inline_height: int = 360,
    actions: list[dict[str, Any]] | None = None,
    **kwargs: Any,
) -> str:
    context = runtime_tool_call_context_from_kwargs(kwargs)
    if runtime_tool_call_context_is_external_customer(kwargs):
        return _json({
            "ok": False,
            "error": {
                "code": "response_surface_not_available_on_external_surface",
                "message": "Interactive response surfaces are available only in internal Manor web chat.",
            },
        })
    runtime_surface = getattr(context.runtime_envelope, "surface", None)
    runtime_surface_value = getattr(runtime_surface, "value", runtime_surface)
    if runtime_surface_value not in _INTERACTIVE_CHAT_SURFACE_VALUES:
        return _json({
            "ok": False,
            "error": {
                "code": "response_surface_not_available_on_runtime_surface",
                "message": "Interactive response surfaces require an internal interactive Chat surface.",
            },
        })
    if not context.conversation_id:
        return _json({
            "ok": False,
            "error": {
                "code": "missing_conversation_context",
                "message": "A conversation context is required to render a response surface.",
            },
        })

    if render_kind not in {"template", "sandboxed_html"}:
        return _json({
            "ok": False,
            "error": {
                "code": "invalid_render_kind",
                "message": "render_kind must be template or sandboxed_html.",
            },
        })

    if render_kind == "template":
        render: dict[str, Any] = {
            "kind": "template",
            "template_id": template_id,
            "template_version": 1,
            "props": template_props or {},
        }
    else:
        render = {
            "kind": "sandboxed_html",
            "code": {
                "version": 1,
                "runtime": "sandboxed_html",
                "html": html,
                "css": css,
                "javascript": javascript,
            },
            "data": data or {},
        }
    candidate = {
        "version": 1,
        "title": title,
        "description": description,
        "render": render,
        "display": {
            "preferred": preferred_display,
            "inline_height": inline_height,
            "focusable": True,
        },
        "actions": actions or [],
        "fallback_markdown": fallback_markdown,
    }
    try:
        surface = normalize_response_surface(candidate)
    except ResponseSurfaceValidationError as exc:
        return _json({
            "ok": False,
            "error": {
                "code": "invalid_response_surface",
                "message": str(exc),
                "issues": exc.errors,
            },
        })
    return _json({"ok": True, "response_surface": surface})


def get_tools() -> list[tuple[dict[str, Any], Any]]:
    return [(RENDER_RESPONSE_SURFACE_SCHEMA, _render_response_surface)]


__all__ = ["RENDER_RESPONSE_SURFACE_SCHEMA", "get_tools"]
