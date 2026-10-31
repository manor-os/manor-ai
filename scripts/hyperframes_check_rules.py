#!/usr/bin/env python3
"""Extract the findings `hyperframes check --strict` can report from the pinned CLI.

`video_edit(operation="review")` runs the strict check, which fails on every
error *and* warning. This lists each finding code the pinned CLI can emit, with
its possible severities, the CLI's own fixHint text and message templates, into
packages/core/ai/tools/hyperframes_check_rules.json. The list is read from the
built CLI bundle, so it stays tied to HYPERFRAMES_VERSION.

    # from a local sandbox image (read-only: create, copy out, remove)
    python scripts/hyperframes_check_rules.py --image sandbox-skill:latest
    # from an unpacked `hyperframes` npm package
    python scripts/hyperframes_check_rules.py --package-dir /path/to/hyperframes
    # verify the committed list instead of writing it
    python scripts/hyperframes_check_rules.py --image sandbox-skill:latest --check
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import uuid

REPO_ROOT = Path(__file__).resolve().parents[1]
RULES_PATH = REPO_ROOT / "packages/core/ai/tools/hyperframes_check_rules.json"
TOOLS_PATH = REPO_ROOT / "packages/core/ai/tools/video_edit_tools.py"
IMAGE_PACKAGE = "/usr/local/lib/node_modules/hyperframes"

# Bundle modules (esbuild `// <path>` headers) whose findings `check` reports.
CHECK_MODULES = {
    "../lint/dist/index.js": "lint",
    "src/utils/checkBrowser.ts": "runtime",
    "src/utils/checkPipeline.ts": "check",
    "src/utils/layoutAudit.ts": "layout",
    "src/utils/motionAudit.ts": "motion",
}
# The browser-side layout audit ships as its own script injected into the page.
BROWSER_SCRIPTS = {"dist/commands/layout-audit.browser.js": "layout"}

# Severities that are not a literal at the emitting site, resolved for `check`.
# Each override names the bundle text it was read from; when that text changes
# the extraction fails instead of keeping a stale classification.
SEVERITY_OVERRIDES = {
    # lintProject never passes `distributed`, so check always sees "info".
    "system_font_will_alias": (
        ["info"], 'const severity = options.distributed ? "warning" : "info";',
    ),
    # auditClipDurations only produces level "warning" rows.
    "clip_media_fit": (
        ["warning"], 'drafts.push({ code: "clip_media_fit", severity: entry.level',
    ),
}
STRICT_FAILING = {"error", "warning"}

_HEADER = re.compile(r"^// (\S+)$", re.MULTILINE)
_CODE = re.compile(r"\bcode: ")
_STRING = re.compile(r'"((?:[^"\\]|\\.)*)"')


def _pinned_version() -> str:
    match = re.search(r'^HYPERFRAMES_VERSION = "([^"]+)"$', TOOLS_PATH.read_text(), re.MULTILINE)
    if not match:
        raise SystemExit("HYPERFRAMES_VERSION not found in video_edit_tools.py")
    return match.group(1)


def _skip_string(text: str, index: int) -> int:
    """Index just past the JS string/template literal starting at ``index``."""
    quote = text[index]
    index += 1
    while index < len(text):
        char = text[index]
        if char == "\\":
            index += 2
            continue
        if quote == "`" and text.startswith("${", index):
            index = _matching(text, index + 1) + 1
            continue
        if char == quote:
            return index + 1
        index += 1
    raise ValueError("unterminated string")


def _skip_comment(text: str, index: int) -> int:
    """Index past a ``//`` or ``/* */`` comment at ``index``, else ``index``."""
    if text.startswith("//", index):
        end = text.find("\n", index)
        return len(text) if end < 0 else end
    if text.startswith("/*", index):
        return text.index("*/", index) + 2
    return index


def _matching(text: str, index: int) -> int:
    """Index of the bracket closing the one at ``index``, skipping literals."""
    pairs = {"{": "}", "(": ")", "[": "]"}
    stack = [pairs[text[index]]]
    index += 1
    while stack:
        char = text[index]
        if char in "\"'`":
            index = _skip_string(text, index)
            continue
        if (skipped := _skip_comment(text, index)) != index:
            index = skipped
            continue
        if char in pairs:
            stack.append(pairs[char])
        elif char == stack[-1]:
            stack.pop()
        index += 1
    return index - 1


def _split_top_level(text: str) -> list[str]:
    parts, depth, start, index = [], 0, 0, 0
    while index < len(text):
        char = text[index]
        if char in "\"'`":
            index = _skip_string(text, index)
            continue
        if (skipped := _skip_comment(text, index)) != index:
            text = text[:index] + " " * (skipped - index) + text[skipped:]
            index = skipped
            continue
        if char in "{([":
            depth += 1
        elif char in "})]":
            depth -= 1
        elif char == "," and depth == 0:
            parts.append(text[start:index].strip())
            start = index + 1
        index += 1
    parts.append(text[start:].strip())
    return [part for part in parts if part]


def _enclosing_brace(text: str, at: int) -> int:
    """The unmatched ``{`` before ``at`` (the code just before it has no literals)."""
    depth, index = 0, at
    while index > 0:
        index -= 1
        if text[index] == "}":
            depth += 1
        elif text[index] == "{":
            if depth == 0:
                return index
            depth -= 1
    raise ValueError("no enclosing block")


def _object_props(text: str, code_at: int) -> dict[str, str]:
    """Top-level ``key: value`` pairs of the object literal around ``code_at``."""
    index = _enclosing_brace(text, code_at)
    end = _matching(text, index)
    props = {}
    for part in _split_top_level(text[index + 1:end]):
        key, sep, value = part.partition(":")
        key = key.strip()
        if re.fullmatch(r"\w+", key):
            props[key] = value.strip() if sep else key  # `{ severity }` shorthand
    return props


_ESCAPE = re.compile(r"\\(u[0-9a-fA-F]{4}|x[0-9a-fA-F]{2}|.)", re.DOTALL)
_SIMPLE_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f", "v": "\v", "0": "\0"}


def _unescape(body: str) -> str:
    def replace(match: re.Match) -> str:
        escape = match.group(1)
        if escape[0] in "ux" and len(escape) > 1:
            return chr(int(escape[1:], 16))
        return _SIMPLE_ESCAPES.get(escape, escape)

    return _ESCAPE.sub(replace, body)


def _literal(value: str) -> str | None:
    value = value.strip()
    if value and value[0] in "\"'" and _skip_string(value, 0) == len(value):
        return _unescape(value[1:-1])
    if value.startswith("`") and _skip_string(value, 0) == len(value):
        return _unescape(value[1:-1])  # template text, `${...}` placeholders kept
    return None


def _texts(value: str) -> list[str]:
    """Each text a literal or a ``cond ? a : b`` chain of literals can produce."""
    literal = _literal(value)
    if literal is not None:
        return [literal]
    depth, index, question, nested = 0, 0, None, 0
    while index < len(value):
        char = value[index]
        if char in "\"'`":
            index = _skip_string(value, index)
            continue
        if char in "{([":
            depth += 1
        elif char in "})]":
            depth -= 1
        elif depth == 0 and char == "?" and value[index + 1:index + 2] not in {".", "?"} and value[index - 1] != "?":
            if question is None:
                question = index
            else:
                nested += 1
        elif depth == 0 and char == ":" and question is not None:
            if nested:
                nested -= 1
            else:
                return _texts(value[question + 1:index]) + _texts(value[index + 1:])
        index += 1
    return []  # computed at runtime (a variable or a helper call)


def _literals(value: str) -> list[str]:
    return [_unescape(match) for match in _STRING.findall(value)]


def _ternary(value: str) -> tuple[str, list[str]] | None:
    match = re.fullmatch(r"(.+?) \? \"([^\"]*)\" : \"([^\"]*)\"", value.strip())
    return (match.group(1), [match.group(2), match.group(3)]) if match else None


class _Rules:
    def __init__(self) -> None:
        self.rules: dict[str, dict] = {}

    def add(self, code, source, severities, *, fix_hints=(), messages=()):
        rule = self.rules.setdefault(code, {
            "code": code, "sources": [], "severities": [], "fix_hints": [], "messages": [],
        })
        for key, values in (
            ("sources", [source]), ("severities", severities),
            ("fix_hints", fix_hints), ("messages", messages),
        ):
            for value in values:
                if value and value not in rule[key]:
                    rule[key].append(value)


def _severities(code: str, value: str, text: str) -> list[str]:
    if code in SEVERITY_OVERRIDES:
        severities, evidence = SEVERITY_OVERRIDES[code]
        if evidence not in text:
            raise SystemExit(f"{code}: severity override evidence changed; re-read the CLI and update it")
        return severities
    literal = _literal(value)
    if literal is not None:
        return [literal]
    found = _literals(value)
    if not found:
        raise SystemExit(f"{code}: unresolved severity expression {value!r}; add an override")
    return found


def _statement_end(text: str, index: int) -> int:
    depth = 0
    while index < len(text):
        char = text[index]
        if char in "\"'`":
            index = _skip_string(text, index)
            continue
        if char in "{([":
            depth += 1
        elif char in "})]":
            depth -= 1
        elif char == ";" and depth == 0:
            return index
        index += 1
    return index


def _resolve(text: str, at: int, value: str) -> str:
    """A ``const name = <expr>;`` initializer for a bare identifier used near ``at``."""
    if not re.fullmatch(r"\w+", value.strip()):
        return value
    start = text.rfind(f"const {value.strip()} = ", max(0, at - 3000), at)
    if start < 0:
        return value
    start = text.index("=", start) + 1
    return text[start:_statement_end(text, start)].strip()


def _scan(rules: _Rules, text: str, source: str, whole: str) -> None:
    for match in _CODE.finditer(text):
        props = _object_props(text, match.start())
        code_value = props.get("code", "")
        severity_value = props.get("severity")
        if severity_value is None:
            continue  # not a finding (e.g. render warnings with code/message only)
        texts = {
            "messages": _texts(_resolve(text, match.start(), props.get("message", ""))),
            "fix_hints": _texts(_resolve(text, match.start(), props.get("fixHint", ""))),
        }
        code = _literal(code_value)
        if code is not None:
            rules.add(code, source, _severities(code, severity_value, whole), **texts)
            continue
        paired_code, paired_severity = _ternary(code_value), _ternary(severity_value)
        if paired_code and paired_severity and paired_code[0] == paired_severity[0]:
            for code, severity in zip(paired_code[1], paired_severity[1]):
                rules.add(code, source, [severity], **texts)
            continue
        if paired_code:
            for code in paired_code[1]:
                rules.add(code, source, _severities(code, severity_value, whole), **texts)
            continue
        # Anything else (`draft.code`, `finding.code`) re-emits a finding found above.
    _scan_helper_calls(rules, text, source)


_HELPER = re.compile(r"const (\w+) = \(code, message, fixHint\) => \{\s*findings\.push\(\{\s*code,")


def _scan_helper_calls(rules: _Rules, text: str, source: str) -> None:
    """``const report = (code, message, fixHint) => findings.push({code, ...})`` callers."""
    for helper in _HELPER.finditer(text):
        severity = _literal(_object_props(text, helper.end())["severity"])
        scope_start = _enclosing_brace(text, helper.start())
        scope = text[scope_start:_matching(text, scope_start)]
        for call in re.finditer(rf"\b{helper.group(1)}\(\s*\"", scope):
            open_paren = scope.index("(", call.start())
            args = _split_top_level(scope[open_paren + 1:_matching(scope, open_paren)])
            rules.add(_literal(args[0]), source, [severity], messages=_texts(args[1]), fix_hints=_texts(args[2]))


def _scan_pipeline_extras(rules: _Rules, text: str) -> None:
    for code, severity in re.findall(r'findingAtRoot\(\s*"(\w+)",\s*"(\w+)"', text):
        rules.add(code, "check", [severity])
    for code in re.findall(r'runtimeFailure\(\w+(?:, "(\w+)")?\)', text):
        rules.add(code or "check_runtime_failure", "check", ["error"])
    default = re.search(r'function runtimeFailure\(error, code = "(\w+)"\)', text)
    if default:
        rules.add(default.group(1), "check", ["error"])


def _scan_persistence_tiers(rules: _Rules, text: str) -> None:
    """Layout codes whose severity check re-tiers by how many samples saw them."""
    tiered = re.search(r"PERSISTENCE_TIERED_CODES = /\* @__PURE__ \*/ new Set\(\[([^\]]*)\]", text)
    if not tiered:
        raise SystemExit("PERSISTENCE_TIERED_CODES not found; re-read applyPersistenceTier")
    for code in _literals(tiered.group(1)):
        rules.add(code, "layout", ["info"])  # seen in only one sample
        rules.rules[code]["persistence_tiered"] = True
    body = text[text.index("function applyPersistenceTier("):]
    body = body[:_matching(body, body.index("{"))]
    for code, severity in re.findall(
        r'issue\.code === "(\w+)"[^{]*\{\s*return \{ \.\.\.issue, severity: "(\w+)" \}', body,
    ):
        rules.add(code, "layout", [severity])


def extract(package_dir: Path) -> dict:
    package = json.loads((package_dir / "package.json").read_text())
    bundle = (package_dir / "dist/cli.js").read_text()
    rules = _Rules()
    headers = list(_HEADER.finditer(bundle))
    seen_modules = set()
    for index, header in enumerate(headers):
        source = CHECK_MODULES.get(header.group(1))
        if source is None:
            continue
        seen_modules.add(header.group(1))
        end = headers[index + 1].start() if index + 1 < len(headers) else len(bundle)
        _scan(rules, bundle[header.end():end], source, bundle)
    missing = set(CHECK_MODULES) - seen_modules
    if missing:
        raise SystemExit(f"check modules not found in the bundle: {sorted(missing)}")
    for rel_path, source in BROWSER_SCRIPTS.items():
        _scan(rules, (package_dir / rel_path).read_text(), source, bundle)
    _scan_pipeline_extras(rules, bundle)
    _scan_persistence_tiers(rules, bundle)
    ordered = []
    for code in sorted(rules.rules):
        rule = rules.rules[code]
        rule["severities"].sort(key=["error", "warning", "info"].index)
        rule["strict_fails"] = bool(STRICT_FAILING & set(rule["severities"]))
        ordered.append({key: rule[key] for key in (
            "code", "sources", "severities", "strict_fails", "persistence_tiered", "fix_hints", "messages",
        ) if key in rule})
    return {
        "hyperframes_version": package["version"],
        "generated_by": "scripts/hyperframes_check_rules.py",
        "strict_failing_severities": sorted(STRICT_FAILING),
        "rules": ordered,
    }


def _from_image(image: str, workdir: Path) -> Path:
    name = f"manor-hf-rules-{uuid.uuid4().hex[:12]}"
    subprocess.run(["docker", "create", "--name", name, image], check=True, capture_output=True)
    try:
        subprocess.run(["docker", "cp", f"{name}:{IMAGE_PACKAGE}", str(workdir)], check=True, capture_output=True)
    finally:
        subprocess.run(["docker", "rm", name], check=True, capture_output=True)
    return workdir / "hyperframes"


def render(rules: dict) -> str:
    return json.dumps(rules, indent=1, ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--image", help="sandbox image with the pinned CLI installed")
    source.add_argument("--package-dir", type=Path, help="unpacked hyperframes npm package")
    parser.add_argument("--check", action="store_true", help="fail when the committed list differs")
    args = parser.parse_args(argv)
    with tempfile.TemporaryDirectory() as workdir:
        package_dir = _from_image(args.image, Path(workdir)) if args.image else args.package_dir
        rules = extract(package_dir)
    pinned = _pinned_version()
    if rules["hyperframes_version"] != pinned:
        print(f"CLI is {rules['hyperframes_version']}, HYPERFRAMES_VERSION is {pinned}", file=sys.stderr)
        return 1
    text = render(rules)
    if args.check:
        if RULES_PATH.read_text() != text:
            print(f"{RULES_PATH.relative_to(REPO_ROOT)} is stale; regenerate it", file=sys.stderr)
            return 1
        return 0
    RULES_PATH.write_text(text)
    blocking = sum(rule["strict_fails"] for rule in rules["rules"])
    print(f"{len(rules['rules'])} codes ({blocking} fail --strict) -> {RULES_PATH.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
