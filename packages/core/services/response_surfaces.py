from __future__ import annotations

import hashlib
import json
import re
from html.parser import HTMLParser
from typing import Any


RESPONSE_SURFACE_POLICY = "response_surface.v2"
RESPONSE_SURFACE_TEMPLATE_IDS = frozenset({
    "learning.code_lab",
    "response.choice",
})

_REGISTERED_TEMPLATE_ACTIONS = {
    "learning.code_lab": {"id": "run", "label": "Run code", "intent": "submit"},
    "response.choice": {"id": "answer", "label": "Submit answer", "intent": "submit"},
}

_SAFE_ID = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_BLOCKED_HTML_ELEMENTS = frozenset({
    "a",
    "area",
    "base",
    "embed",
    "iframe",
    "link",
    "meta",
    "object",
    "script",
    "style",
})
_BLOCKED_PRESENTATION_ELEMENTS = frozenset({
    "animate",
    "animatecolor",
    "animatemotion",
    "animatetransform",
    "discard",
    "filter",
    "set",
})
_BLOCKED_HTML_ATTRIBUTES = frozenset({
    "action",
    "download",
    "formaction",
    "href",
    "srcdoc",
    "target",
})
_BLOCKED_EMBEDDED_RESOURCE_ATTRIBUTES = frozenset({
    "background",
    "poster",
    "src",
    "srcset",
})
_BLOCKED_PRESENTATION_ATTRIBUTES = frozenset({
    "bgcolor",
    "bordercolor",
    "color",
    "fill",
    "filter",
    "flood-color",
    "lighting-color",
    "stop-color",
    "stroke",
    "style",
})
_BLOCKED_HOST_ELEMENT_IDS = frozenset({
    "surface-activity",
    "surface-error",
    "surface-root",
})
_VOID_HTML_ELEMENTS = frozenset({
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
})
_JAVASCRIPT_PROTOCOL = re.compile(r"javascript\s*:", flags=re.IGNORECASE)
_BLOCKED_CSS = re.compile(r"@import\b|url\s*\(|</style", flags=re.IGNORECASE)
_BLOCKED_GENERATED_CSS = re.compile(
    r"!\s*important\b|"
    r"#surface-(?:activity|error|root)\b|"
    r":scope\b|"
    r"\bposition\s*:\s*(?:fixed|sticky)\b|"
    r"\bz-index\s*:|"
    r"--[a-z0-9_-]+\s*:|"
    r"@(?!media\b)|"
    r"(?:^|[,{])\s*(?::root\b|html\b|body\b)",
    flags=re.IGNORECASE | re.MULTILINE,
)
_CSS_DECLARATION = re.compile(
    r"(?:^|[;{])\s*(--[-a-z0-9_]+|[-a-z][a-z0-9-]*)\s*:\s*([^;{}]*)",
    flags=re.IGNORECASE | re.MULTILINE,
)
_RAW_CSS_COLOR = re.compile(
    r"#[0-9a-f]{3,8}\b|"
    r"\b(?:rgb|rgba|hsl|hsla|hwb|lab|lch|oklab|oklch|color|color-mix|"
    r"light-dark|device-cmyk)\s*\(",
    flags=re.IGNORECASE,
)
_CSS_ATTRIBUTE_VALUE = re.compile(r"\battr\s*\(", flags=re.IGNORECASE)
_CSS_NAMED_COLORS = frozenset("""
aliceblue antiquewhite aqua aquamarine azure beige bisque black blanchedalmond blue blueviolet brown
burlywood cadetblue chartreuse chocolate coral cornflowerblue cornsilk crimson cyan darkblue darkcyan
darkgoldenrod darkgray darkgreen darkgrey darkkhaki darkmagenta darkolivegreen darkorange darkorchid darkred
darksalmon darkseagreen darkslateblue darkslategray darkslategrey darkturquoise darkviolet deeppink deepskyblue
dimgray dimgrey dodgerblue firebrick floralwhite forestgreen fuchsia gainsboro ghostwhite gold goldenrod gray
green greenyellow grey honeydew hotpink indianred indigo ivory khaki lavender lavenderblush lawngreen
lemonchiffon lightblue lightcoral lightcyan lightgoldenrodyellow lightgray lightgreen lightgrey lightpink
lightsalmon lightseagreen lightskyblue lightslategray lightslategrey lightsteelblue lightyellow lime limegreen
linen magenta maroon mediumaquamarine mediumblue mediumorchid mediumpurple mediumseagreen mediumslateblue
mediumspringgreen mediumturquoise mediumvioletred midnightblue mintcream mistyrose moccasin navajowhite navy
oldlace olive olivedrab orange orangered orchid palegoldenrod palegreen paleturquoise palevioletred papayawhip
peachpuff peru pink plum powderblue purple rebeccapurple red rosybrown royalblue saddlebrown salmon sandybrown
seagreen seashell sienna silver skyblue slateblue slategray slategrey snow springgreen steelblue tan teal thistle
tomato turquoise violet wheat white whitesmoke yellow yellowgreen
""".split())
_CSS_SYSTEM_COLORS = frozenset({
    "accentcolor",
    "accentcolortext",
    "activetext",
    "buttonborder",
    "buttonface",
    "buttontext",
    "canvas",
    "canvastext",
    "field",
    "fieldtext",
    "graytext",
    "highlight",
    "highlighttext",
    "linktext",
    "mark",
    "marktext",
    "selecteditem",
    "selecteditemtext",
    "visitedtext",
    "-webkit-activelink",
    "-webkit-focus-ring-color",
    "-webkit-link",
})
_CSS_LITERAL_COLOR_WORDS = _CSS_NAMED_COLORS | _CSS_SYSTEM_COLORS
_MODULE_COLOR_TOKEN = re.compile(
    r"var\(\s*--module-[a-z0-9_-]+\s*\)",
    flags=re.IGNORECASE,
)
_CSS_NUMBER = re.compile(
    r"(?:^|(?<=[\s,(]))[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:%|[a-z]+)?",
    flags=re.IGNORECASE,
)
_CSS_WORD = re.compile(r"[a-z_][a-z0-9_-]*", flags=re.IGNORECASE)
_COLOR_AFFECTING_PROPERTIES = frozenset({
    "accent-color",
    "backdrop-filter",
    "background",
    "background-color",
    "background-image",
    "border",
    "box-reflect",
    "box-shadow",
    "caret-color",
    "column-rule",
    "content",
    "fill",
    "filter",
    "flood-color",
    "list-style",
    "list-style-image",
    "lighting-color",
    "mask",
    "mask-border",
    "mask-image",
    "outline",
    "shape-outside",
    "stop-color",
    "stroke",
    "text-decoration",
    "text-emphasis",
    "text-shadow",
    "text-stroke",
})
_ALLOWED_COLOR_VALUE_WORDS = frozenset({
    "auto",
    "bottom",
    "calc",
    "center",
    "circle",
    "clamp",
    "closest-corner",
    "closest-side",
    "conic-gradient",
    "contain",
    "cover",
    "currentcolor",
    "dashed",
    "double",
    "dotted",
    "ellipse",
    "farthest-corner",
    "farthest-side",
    "fixed",
    "from",
    "groove",
    "inherit",
    "initial",
    "inset",
    "left",
    "linear-gradient",
    "max",
    "min",
    "no-repeat",
    "none",
    "outset",
    "padding-box",
    "radial-gradient",
    "repeat",
    "repeating-conic-gradient",
    "repeating-linear-gradient",
    "repeating-radial-gradient",
    "revert",
    "revert-layer",
    "ridge",
    "right",
    "round",
    "scroll",
    "solid",
    "space",
    "to",
    "top",
    "transparent",
    "unset",
})


class ResponseSurfaceValidationError(ValueError):
    def __init__(self, errors: list[dict[str, str]]) -> None:
        super().__init__(errors[0]["message"] if errors else "Invalid response surface")
        self.errors = errors


class _GeneratedHtmlPolicyParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocked = False
        self.open_elements: list[str] = []

    def _inspect_tag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in _BLOCKED_HTML_ELEMENTS | _BLOCKED_PRESENTATION_ELEMENTS:
            self.blocked = True
            return
        for raw_name, value in attrs:
            name = raw_name.lower().rsplit(":", 1)[-1]
            if (
                tag.lower() == "input"
                and name == "type"
                and (value or "").strip().lower() == "color"
            ):
                self.blocked = True
                return
            if (
                name == "id"
                and (value or "").strip().lower() in _BLOCKED_HOST_ELEMENT_IDS
            ):
                self.blocked = True
                return
            if (
                name.startswith("on")
                or name in _BLOCKED_HTML_ATTRIBUTES
                or name in _BLOCKED_EMBEDDED_RESOURCE_ATTRIBUTES
                or name in _BLOCKED_PRESENTATION_ATTRIBUTES
                or (value is not None and _JAVASCRIPT_PROTOCOL.search(value))
            ):
                self.blocked = True
                return

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._inspect_tag(tag, attrs)
        tag_name = tag.lower()
        if not self.blocked and tag_name not in _VOID_HTML_ELEMENTS:
            self.open_elements.append(tag_name)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._inspect_tag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        tag_name = tag.lower()
        for index in range(len(self.open_elements) - 1, -1, -1):
            if self.open_elements[index] == tag_name:
                del self.open_elements[index:]
                return
        self.blocked = True


def _generated_html_policy_error(value: str) -> bool:
    parser = _GeneratedHtmlPolicyParser()
    parser.feed(value)
    parser.close()
    return parser.blocked


def _issue(code: str, message: str, path: str) -> dict[str, str]:
    return {"code": code, "message": message, "path": path}


def _bounded_string(
    value: Any,
    *,
    path: str,
    maximum: int,
    errors: list[dict[str, str]],
    required: bool = False,
) -> str:
    if not isinstance(value, str):
        if required or value is not None:
            errors.append(_issue("string", f"{path} must be a string.", path))
        return ""
    text = value.strip() if path != "render.code.javascript" else value
    if required and not text:
        errors.append(_issue("required", f"{path} is required.", path))
    if len(value) > maximum:
        errors.append(_issue("length", f"{path} exceeds {maximum} characters.", path))
    return value[:maximum]


def _normalized_json_object(
    value: Any,
    *,
    path: str,
    maximum: int,
    errors: list[dict[str, str]],
) -> dict[str, Any]:
    if not isinstance(value, dict):
        errors.append(_issue("json_object", f"{path} must be a JSON object.", path))
        return {}
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
        normalized = json.loads(encoded)
    except (TypeError, ValueError, RecursionError):
        errors.append(_issue("json", f"{path} must contain JSON values.", path))
        return {}
    if len(encoded) > maximum:
        errors.append(_issue("json_length", f"{path} exceeds {maximum} characters.", path))
        return {}
    return normalized


def _css_blocks_balanced(value: str) -> bool:
    depth = 0
    quote = ""
    escaped = False
    in_comment = False
    index = 0
    while index < len(value):
        current = value[index]
        following = value[index + 1] if index + 1 < len(value) else ""
        if in_comment:
            if current == "*" and following == "/":
                in_comment = False
                index += 2
                continue
            index += 1
            continue
        if quote:
            if escaped:
                escaped = False
            elif current == "\\":
                escaped = True
            elif current == quote:
                quote = ""
            index += 1
            continue
        if current == "/" and following == "*":
            in_comment = True
            index += 2
            continue
        if current in {'"', "'"}:
            quote = current
        elif current == "{":
            depth += 1
        elif current == "}":
            depth -= 1
            if depth < 0:
                return False
        index += 1
    return depth == 0 and not quote and not in_comment


def _decode_css_escapes(value: str) -> str:
    decoded: list[str] = []
    index = 0
    while index < len(value):
        current = value[index]
        if current != "\\" or index + 1 >= len(value):
            decoded.append(current)
            index += 1
            continue
        index += 1
        if value[index] in "\r\n\f":
            if value[index] == "\r" and index + 1 < len(value) and value[index + 1] == "\n":
                index += 1
            index += 1
            continue
        match = re.match(r"[0-9a-fA-F]{1,6}", value[index:])
        if match:
            code_point = int(match.group(0), 16)
            decoded.append(
                chr(code_point)
                if code_point and code_point <= 0x10FFFF and not 0xD800 <= code_point <= 0xDFFF
                else "\ufffd"
            )
            index += len(match.group(0))
            if index < len(value) and value[index] in " \t\r\n\f":
                if value[index] == "\r" and index + 1 < len(value) and value[index + 1] == "\n":
                    index += 1
                index += 1
            continue
        decoded.append(value[index])
        index += 1
    return "".join(decoded)


def _mask_css_strings(value: str) -> str:
    masked: list[str] = []
    quote = ""
    escaped = False
    for current in value:
        if quote:
            masked.append(current if current in "\r\n\f" else " ")
            if escaped:
                escaped = False
            elif current == "\\":
                escaped = True
            elif current == quote:
                quote = ""
            continue
        if current in {'"', "'"}:
            quote = current
            masked.append(" ")
        else:
            masked.append(current)
    return "".join(masked)


def _color_property(property_name: str) -> bool:
    property_name = re.sub(r"^-(?:moz|ms|o|webkit)-", "", property_name)
    return (
        property_name in _COLOR_AFFECTING_PROPERTIES
        or property_name.endswith("color")
        or property_name.startswith("background-")
        or property_name.startswith("border-")
        or property_name.startswith("mask-")
        or property_name.startswith("outline-")
    )


def _generated_color_policy_error(value: str) -> bool:
    for match in _CSS_DECLARATION.finditer(value):
        property_name = match.group(1).lower()
        if property_name.startswith("--") or property_name == "color-scheme":
            return True
        declaration_value = match.group(2)
        if _RAW_CSS_COLOR.search(declaration_value):
            return True
        if _CSS_ATTRIBUTE_VALUE.search(declaration_value):
            return True
        if not _color_property(property_name):
            continue
        declaration_words = {word.lower() for word in _CSS_WORD.findall(declaration_value)}
        if declaration_words & _CSS_LITERAL_COLOR_WORDS:
            return True
        remainder = _MODULE_COLOR_TOKEN.sub(" ", declaration_value)
        if re.search(r"\bvar\s*\(", remainder, flags=re.IGNORECASE):
            return True
        remainder = _CSS_NUMBER.sub(" ", remainder)
        words = _CSS_WORD.findall(remainder)
        if any(word.lower() not in _ALLOWED_COLOR_VALUE_WORDS for word in words):
            return True
    return False


def _normalized_generated_css(value: str) -> str:
    without_strings = _mask_css_strings(value)
    without_comments = re.sub(r"/\*.*?\*/", "", without_strings, flags=re.DOTALL)
    return _decode_css_escapes(without_comments)


def _generated_css_capability_error(value: str) -> bool:
    return bool(_BLOCKED_CSS.search(_normalized_generated_css(value)))


def _generated_css_policy_error(value: str) -> bool:
    normalized = _normalized_generated_css(value)
    return (
        bool(_BLOCKED_GENERATED_CSS.search(normalized))
        or _generated_color_policy_error(normalized)
        or not _css_blocks_balanced(value)
    )


def _normalize_actions(value: Any, errors: list[dict[str, str]]) -> list[dict[str, str]]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 8:
        errors.append(_issue("actions", "actions must contain at most 8 items.", "actions"))
        return []
    actions: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, raw in enumerate(value):
        path = f"actions[{index}]"
        if not isinstance(raw, dict):
            errors.append(_issue("action", "Each action must be an object.", path))
            continue
        action_id = str(raw.get("id") or "").strip()
        label = str(raw.get("label") or "").strip()
        if not _SAFE_ID.fullmatch(action_id) or action_id in seen:
            errors.append(_issue("action_id", "Action ids must be unique safe identifiers.", f"{path}.id"))
            continue
        if not label or len(label) > 80:
            errors.append(_issue("action_label", "Action labels must be 1-80 characters.", f"{path}.label"))
            continue
        seen.add(action_id)
        actions.append({"id": action_id, "label": label[:80], "intent": "submit"})
    return actions


def registered_response_surface_template_action(
    template_id: str,
    actions: Any = None,
) -> dict[str, str] | None:
    """Return the canonical submit action for a registered response template."""

    default_action = _REGISTERED_TEMPLATE_ACTIONS.get(template_id)
    if default_action is None:
        return None
    if isinstance(actions, list):
        for action in actions:
            if (
                isinstance(action, dict)
                and action.get("id") == default_action["id"]
                and action.get("intent") == "submit"
            ):
                label = action.get("label")
                if isinstance(label, str) and 0 < len(label.strip()) <= 80:
                    return {
                        "id": default_action["id"],
                        "label": label.strip(),
                        "intent": "submit",
                    }
    return default_action.copy()


def _normalize_display(value: Any, errors: list[dict[str, str]]) -> dict[str, Any]:
    raw = value if isinstance(value, dict) else {}
    preferred = str(raw.get("preferred") or "inline").strip()
    if preferred not in {"inline", "focus"}:
        errors.append(_issue("display_preferred", "display.preferred must be inline or focus.", "display.preferred"))
        preferred = "inline"
    try:
        height = int(raw.get("inline_height") or 360)
    except (TypeError, ValueError):
        height = 360
        errors.append(_issue("display_height", "display.inline_height must be an integer.", "display.inline_height"))
    if not 180 <= height <= 720:
        errors.append(_issue("display_height", "display.inline_height must be between 180 and 720.", "display.inline_height"))
        height = min(720, max(180, height))
    return {
        "preferred": preferred,
        "inline_height": height,
        "focusable": raw.get("focusable") is not False,
    }


def _normalize_code_lab(props: Any, errors: list[dict[str, str]]) -> dict[str, Any]:
    if not isinstance(props, dict):
        errors.append(_issue("template_props", "Code lab props must be an object.", "render.props"))
        return {}
    language = _bounded_string(
        props.get("language") or "text",
        path="render.props.language",
        maximum=32,
        errors=errors,
        required=True,
    ).strip().lower()
    if not _SAFE_ID.fullmatch(language):
        errors.append(_issue(
            "template_language_id",
            "The code lab language must be a safe identifier.",
            "render.props.language",
        ))
    instructions = _bounded_string(
        props.get("instructions"),
        path="render.props.instructions",
        maximum=4_000,
        errors=errors,
        required=True,
    )
    starter_code = _bounded_string(
        props.get("starter_code") or "",
        path="render.props.starter_code",
        maximum=30_000,
        errors=errors,
    )
    raw_languages = props.get("languages")
    languages: list[dict[str, str]] = []
    if raw_languages is not None:
        if not isinstance(raw_languages, list) or not 1 <= len(raw_languages) <= 8:
            errors.append(_issue(
                "template_languages",
                "Code lab languages must contain 1-8 language configurations.",
                "render.props.languages",
            ))
        else:
            seen_languages: set[str] = set()
            for index, raw_language in enumerate(raw_languages):
                path = f"render.props.languages[{index}]"
                if not isinstance(raw_language, dict):
                    errors.append(_issue(
                        "template_language",
                        "Each code lab language must be an object.",
                        path,
                    ))
                    continue
                language_id = str(raw_language.get("id") or "").strip().lower()
                if not _SAFE_ID.fullmatch(language_id) or language_id in seen_languages:
                    errors.append(_issue(
                        "template_language_id",
                        "Language ids must be unique safe identifiers.",
                        f"{path}.id",
                    ))
                    continue
                label = _bounded_string(
                    raw_language.get("label"),
                    path=f"{path}.label",
                    maximum=40,
                    errors=errors,
                    required=True,
                )
                filename = _bounded_string(
                    raw_language.get("filename") or language_id,
                    path=f"{path}.filename",
                    maximum=80,
                    errors=errors,
                    required=True,
                )
                language_starter = _bounded_string(
                    raw_language.get("starter_code") or "",
                    path=f"{path}.starter_code",
                    maximum=10_000,
                    errors=errors,
                )
                seen_languages.add(language_id)
                languages.append({
                    "id": language_id,
                    "label": label,
                    "filename": filename,
                    "starter_code": language_starter,
                })
            if language not in seen_languages:
                errors.append(_issue(
                    "template_language_initial",
                    "The initial code lab language must appear in languages.",
                    "render.props.language",
                ))
    raw_tests = props.get("tests", [])
    tests: list[str] = []
    if not isinstance(raw_tests, list) or len(raw_tests) > 20:
        errors.append(_issue("template_tests", "Code lab tests must contain at most 20 strings.", "render.props.tests"))
    else:
        for index, test in enumerate(raw_tests):
            tests.append(_bounded_string(
                test,
                path=f"render.props.tests[{index}]",
                maximum=2_000,
                errors=errors,
                required=True,
            ))
    return {
        "language": language,
        "instructions": instructions,
        "starter_code": starter_code,
        "languages": languages,
        "tests": tests,
    }


def _normalize_choice(props: Any, errors: list[dict[str, str]]) -> dict[str, Any]:
    if not isinstance(props, dict):
        errors.append(_issue("template_props", "Choice props must be an object.", "render.props"))
        return {}
    prompt = _bounded_string(
        props.get("prompt"),
        path="render.props.prompt",
        maximum=1_000,
        errors=errors,
        required=True,
    )
    raw_options = props.get("options")
    options: list[dict[str, str]] = []
    if not isinstance(raw_options, list) or not 2 <= len(raw_options) <= 12:
        errors.append(_issue("template_options", "Choice surfaces require 2-12 options.", "render.props.options"))
    else:
        seen: set[str] = set()
        for index, raw in enumerate(raw_options):
            path = f"render.props.options[{index}]"
            if not isinstance(raw, dict):
                errors.append(_issue("template_option", "Each choice option must be an object.", path))
                continue
            option_id = str(raw.get("id") or "").strip()
            label = str(raw.get("label") or "").strip()
            description = str(raw.get("description") or "").strip()
            if not _SAFE_ID.fullmatch(option_id) or option_id in seen:
                errors.append(_issue("template_option_id", "Option ids must be unique safe identifiers.", f"{path}.id"))
                continue
            if not label or len(label) > 160 or len(description) > 400:
                errors.append(_issue("template_option_text", "Option text exceeds its allowed length.", path))
                continue
            seen.add(option_id)
            option = {"id": option_id, "label": label}
            if description:
                option["description"] = description
            options.append(option)
    return {"prompt": prompt, "options": options}


def _normalize_template(render: dict[str, Any], errors: list[dict[str, str]]) -> dict[str, Any]:
    template_id = str(render.get("template_id") or "").strip()
    if template_id not in RESPONSE_SURFACE_TEMPLATE_IDS:
        errors.append(_issue("template_id", "Unknown response surface template.", "render.template_id"))
        return {"kind": "template", "template_id": template_id, "template_version": 1, "props": {}}
    if render.get("template_version") not in {None, 1}:
        errors.append(_issue("template_version", "Template version must be 1.", "render.template_version"))
    props = (
        _normalize_code_lab(render.get("props"), errors)
        if template_id == "learning.code_lab"
        else _normalize_choice(render.get("props"), errors)
    )
    return {
        "kind": "template",
        "template_id": template_id,
        "template_version": 1,
        "props": props,
    }


def _normalize_generated(render: dict[str, Any], errors: list[dict[str, str]]) -> dict[str, Any]:
    code = render.get("code")
    if not isinstance(code, dict):
        errors.append(_issue("code", "Generated surfaces require a code object.", "render.code"))
        code = {}
    html = _bounded_string(code.get("html"), path="render.code.html", maximum=20_000, errors=errors, required=True)
    css = _bounded_string(code.get("css") or "", path="render.code.css", maximum=30_000, errors=errors)
    javascript = _bounded_string(
        code.get("javascript") or "",
        path="render.code.javascript",
        maximum=50_000,
        errors=errors,
    )
    if code.get("version") != 1 or code.get("runtime") != "sandboxed_html":
        errors.append(_issue("runtime", "Generated surfaces require version 1 sandboxed_html code.", "render.code"))
    if _generated_html_policy_error(html):
        errors.append(_issue("html_capability", "HTML contains a blocked element or attribute.", "render.code.html"))
    if _generated_css_capability_error(css):
        errors.append(_issue("css_capability", "CSS cannot load external resources.", "render.code.css"))
    elif _generated_css_policy_error(css):
        errors.append(_issue(
            "css_policy",
            "CSS must use host color tokens, stay inside the generated surface, and cannot override host UI.",
            "render.code.css",
        ))
    if javascript.strip():
        errors.append(_issue(
            "generated_javascript_not_allowed",
            "Generated response surfaces are declarative; use a registered template for JavaScript behavior.",
            "render.code.javascript",
        ))
    data = _normalized_json_object(
        render.get("data", {}),
        path="render.data",
        maximum=40_000,
        errors=errors,
    )
    normalized_code = {
        "version": 1,
        "runtime": "sandboxed_html",
        "html": html,
        "css": css,
        "javascript": "",
    }
    code_hash = hashlib.sha256(
        json.dumps(normalized_code, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()
    return {
        "kind": "sandboxed_html",
        "code": normalized_code,
        "data": data,
        "validation": {"policy": RESPONSE_SURFACE_POLICY, "code_hash": code_hash},
    }


def normalize_response_surface(value: Any) -> dict[str, Any]:
    errors: list[dict[str, str]] = []
    if not isinstance(value, dict):
        raise ResponseSurfaceValidationError([_issue("surface", "Response surface must be an object.", "surface")])
    if value.get("version") != 1:
        errors.append(_issue("version", "Response surface version must be 1.", "version"))
    title = _bounded_string(value.get("title"), path="title", maximum=120, errors=errors, required=True)
    description = _bounded_string(value.get("description") or "", path="description", maximum=300, errors=errors)
    fallback = _bounded_string(
        value.get("fallback_markdown"),
        path="fallback_markdown",
        maximum=4_000,
        errors=errors,
        required=True,
    )
    render = value.get("render")
    if not isinstance(render, dict):
        errors.append(_issue("render", "render must be an object.", "render"))
        render = {}
    render_kind = str(render.get("kind") or "")
    if render_kind == "template":
        normalized_render = _normalize_template(render, errors)
    elif render_kind == "sandboxed_html":
        normalized_render = _normalize_generated(render, errors)
    else:
        errors.append(_issue("render_kind", "render.kind must be template or sandboxed_html.", "render.kind"))
        normalized_render = {"kind": render_kind}
    actions = _normalize_actions(value.get("actions"), errors)
    if render_kind == "template":
        registered_action = registered_response_surface_template_action(
            str(normalized_render.get("template_id") or ""),
            actions,
        )
        if registered_action:
            actions = [registered_action]
    display = _normalize_display(value.get("display"), errors)
    if errors:
        raise ResponseSurfaceValidationError(errors)
    result: dict[str, Any] = {
        "version": 1,
        "title": title,
        "render": normalized_render,
        "display": display,
        "actions": actions,
        "fallback_markdown": fallback,
    }
    if description:
        result["description"] = description
    return result


def response_surface_from_tool_result(value: Any) -> dict[str, Any] | None:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return None
    if not isinstance(value, dict) or value.get("ok") is not True:
        return None
    surface = value.get("response_surface")
    try:
        return normalize_response_surface(surface)
    except ResponseSurfaceValidationError:
        return None


__all__ = [
    "RESPONSE_SURFACE_POLICY",
    "RESPONSE_SURFACE_TEMPLATE_IDS",
    "ResponseSurfaceValidationError",
    "normalize_response_surface",
    "registered_response_surface_template_action",
    "response_surface_from_tool_result",
]
