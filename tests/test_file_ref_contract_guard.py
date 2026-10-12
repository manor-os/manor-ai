"""Guard: one file-reference contract, repo-wide.

A file's address is built by ``entity_fs_open_url`` / ``file_ref`` and read by
``parse_file_reference`` (``packages/core/services/file_refs.py``). Code that
hand-builds ``/api/v1/fs/{...}`` strings or slices them apart drifts from the
contract: the producer forgets to percent-encode, the consumer forgets to
decode (or decodes twice), and a file with a space or a CJK name stops
resolving. This scans every Python file under ``packages/`` and ``apps/api``
(the class, not the instances fixed so far).
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCAN_ROOTS = ("packages", "apps/api")

# The contract itself.
_CONTRACT_FILES = frozenset({
    "packages/core/services/file_refs.py",
    "packages/core/services/generated_file_refs.py",
})

# (a) An f-string or concatenation that produces ``/api/v1/fs/{...}``.
# Signed ``/api/v1/fs/public/{token}`` builders do not match: the literal
# ``public/`` sits between the prefix and the placeholder.
_BUILD_PATTERNS = (
    re.compile(r"""(?<![A-Za-z0-9_])[rR]?[fF][rR]?["'][^"'\n]*/api/v1/fs/\{"""),
    re.compile(r"""["'][^"'\n]*/api/v1/fs/["']\s*\+"""),
    re.compile(r"""\+\s*["']/api/v1/fs/"""),
    re.compile(r"""["'][^"'\n]*/api/v1/fs/\{[^"'\n]*["']\s*\.format\("""),
)

# (b) Code that takes an fs URL apart (or classifies one) by string surgery.
_PARSE_PATTERNS = (
    re.compile(
        r"""\.(?:split|rsplit|partition|rpartition|removeprefix|startswith|find|index)\([^\n]*?api/v1/fs/"""
    ),
    re.compile(r"""\bre\.\w+\(\s*[rRfFbB]*["'][^"'\n]*api/v1/fs/"""),
    re.compile(r"""(?<![A-Za-z0-9_])[rR][fF]?["'][^"'\n]*api/v1/fs/"""),  # raw literal = a regex
    re.compile(r"""\blen\(\s*[rRfF]*["'][^"'\n]*api/v1/fs/"""),
    re.compile(r"""\b\w*(?:prefix|marker)\w*\s*=\s*\(?\s*[rRfF]*["'][^"'\n]*api/v1/fs/""", re.I),
    re.compile(r"""["'][^"'\n]*api/v1/fs/[^"'\n]*["']\s+(?:not\s+)?in\b"""),
)

# Permanent exceptions: exact file -> why it may touch the fs URL by hand.
# Every entry must still match (a stale entry fails the test).
_BUILD_ALLOWLIST: dict[str, str] = {
    "apps/api/routers/filesystem.py": (
        "the fs router's own avatar cleanup reconstructs the stored avatar URL to match it"
    ),
}
_PARSE_ALLOWLIST: dict[str, str] = {
    "packages/core/ai/tools/extended_tools.py": "regex finds [Image: name → url] reference lines in free text",
    "apps/api/routers/chat.py": "redaction regexes find file addresses in shared-chat free text",
    "packages/core/ai/mcp/_knowledge_url.py": (
        "decodes signed /api/v1/fs/public/<token> URLs (parse_file_reference rejects them by design)"
    ),
    "packages/core/ai/mcp/tiktok.py": "redacts the capability token of a signed /api/v1/fs/public/ URL",
    "packages/core/tasks/media_tasks.py": (
        "re-mints expired signed /api/v1/fs/public/ URLs and explains malformed fs URLs to the model"
    ),
    "packages/core/ai/runtime/artifacts.py": (
        "finds fs URLs inside free text (prose extraction regex); identity goes through parse_file_reference"
    ),
    "packages/core/ai/skills/pptx/scripts/import_system_image.py": (
        "standalone sandbox script that cannot import Manor packages; mirrors the decode-once rule"
    ),
}

# TEMPORARY: files owned by concurrent migrations (producer/consumer rewrites
# in flight). The lead removes each entry as its migration lands; the list
# should end up empty. Entries here are exempt from both checks and from the
# stale-entry check.
_PENDING_MIGRATION: dict[str, str] = {}


def _python_files() -> list[Path]:
    files: list[Path] = []
    for root in SCAN_ROOTS:
        for path in sorted((ROOT / root).rglob("*.py")):
            parts = path.relative_to(ROOT).parts
            if "tests" in parts or "node_modules" in parts or "__pycache__" in parts:
                continue
            files.append(path)
    return files


def scan_source(text: str, patterns: tuple[re.Pattern[str], ...]) -> list[tuple[int, str]]:
    hits: list[tuple[int, str]] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        if line.lstrip().startswith("#"):
            continue
        if any(pattern.search(line) for pattern in patterns):
            hits.append((lineno, line.strip()))
    return hits


def _violations(patterns, allowlist: dict[str, str]) -> tuple[list[str], set[str]]:
    violations: list[str] = []
    matched: set[str] = set()
    for path in _python_files():
        rel = path.relative_to(ROOT).as_posix()
        if rel in _CONTRACT_FILES:
            continue
        hits = scan_source(path.read_text(encoding="utf-8", errors="ignore"), patterns)
        if not hits:
            continue
        matched.add(rel)
        if rel in allowlist or rel in _PENDING_MIGRATION:
            continue
        violations.extend(f"{rel}:{lineno}: {line[:160]}" for lineno, line in hits)
    return violations, matched


def test_no_hand_built_fs_urls():
    violations, matched = _violations(_BUILD_PATTERNS, _BUILD_ALLOWLIST)
    assert not violations, (
        "Build file addresses with entity_fs_open_url(entity_id, fs_path) or "
        "file_ref(...) from packages/core/services/file_refs.py, never by hand "
        "(they must be percent-encoded exactly once):\n" + "\n".join(violations)
    )
    stale = sorted(set(_BUILD_ALLOWLIST) - matched)
    assert not stale, f"Remove stale _BUILD_ALLOWLIST entries: {stale}"


def test_no_independent_fs_url_parsers():
    violations, matched = _violations(_PARSE_PATTERNS, _PARSE_ALLOWLIST)
    assert not violations, (
        "Parse file addresses with parse_file_reference(...) (or is_open_address) "
        "from packages/core/services/file_refs.py instead of slicing or matching "
        "'/api/v1/fs/' strings (decoding must happen exactly once):\n"
        + "\n".join(violations)
    )
    stale = sorted(set(_PARSE_ALLOWLIST) - matched)
    assert not stale, f"Remove stale _PARSE_ALLOWLIST entries: {stale}"


def test_allowlists_name_existing_files():
    for rel in (*_BUILD_ALLOWLIST, *_PARSE_ALLOWLIST, *_PENDING_MIGRATION, *_CONTRACT_FILES):
        assert (ROOT / rel).is_file(), f"guard list names a missing file: {rel}"


@pytest.mark.parametrize(
    "source",
    [
        'url = f"/api/v1/fs/{entity_id}/{rel_path}"',
        "url = '/api/v1/fs/' + entity_id + '/' + path",
        'url = "/api/v1/fs/{}/{}".format(entity_id, path)',
    ],
)
def test_build_patterns_catch_hand_built_urls(source):
    assert scan_source(source, _BUILD_PATTERNS)


@pytest.mark.parametrize(
    "source",
    [
        'owner, _, rest = value[len("/api/v1/fs/"):].partition("/")',
        'parts = value.split("/api/v1/fs/", 1)',
        'if value.startswith(("/viewer/", "/api/v1/fs/")):',
        'rel = value.removeprefix(f"/api/v1/fs/{entity_id}/")',
        'marker = "/api/v1/fs/"',
        '_RE = re.compile(r"^/api/v1/fs/(?P<entity>[^/]+)/(?P<path>.+)$")',
        'if "/api/v1/fs/" in url:',
    ],
)
def test_parse_patterns_catch_string_surgery(source):
    assert scan_source(source, _PARSE_PATTERNS)


@pytest.mark.parametrize(
    "source",
    [
        'signed = f"{public_base}/api/v1/fs/public/{token}/{name}"',
        '"description": "Knowledge path or a /api/v1/fs/... link."',
    ],
)
def test_build_patterns_ignore_signed_urls_and_prose(source):
    assert not scan_source(source, _BUILD_PATTERNS)
