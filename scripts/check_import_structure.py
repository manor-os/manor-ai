#!/usr/bin/env python3
"""Check import placement that is not covered by the repo's current linters."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SEARCH_ROOTS = ("apps", "packages", "scripts", "tests")
EXTENSIONS = {".cjs", ".js", ".jsx", ".mjs", ".ts", ".tsx"}
SKIP_PARTS = {".vite", ".wxt", "build", "coverage", "dist", "node_modules"}

STATIC_IMPORT_RE = re.compile(r"^import(?:\s|\{|\*)")
IMPORT_END_RE = re.compile(r"(?:^import\s+[\"'][^\"']+[\"']|from\s+[\"'][^\"']+[\"'])$")
DIRECTIVES = {
    '"use client";',
    '"use strict";',
    "'use client';",
    "'use strict';",
}


def _should_skip(path: Path) -> bool:
    return path.suffix not in EXTENSIONS or any(part in SKIP_PARTS for part in path.parts)


def _import_continues(line: str) -> bool:
    stripped = line.rstrip(";")
    return not (line.endswith(";") or IMPORT_END_RE.search(stripped))


def _violations(path: Path) -> list[tuple[int, str]]:
    lines = path.read_text(errors="ignore").splitlines()
    out: list[tuple[int, str]] = []
    code_seen = False
    in_block_comment = False
    in_import = False

    for lineno, raw in enumerate(lines, 1):
        line = raw.strip()

        if in_import:
            if not _import_continues(line):
                in_import = False
            continue
        if not line:
            continue
        if lineno == 1 and line.startswith("#!"):
            continue
        if in_block_comment:
            if "*/" in line:
                in_block_comment = False
            continue
        if line.startswith("/*"):
            if "*/" not in line:
                in_block_comment = True
            continue
        if line.startswith("//") or line in DIRECTIVES:
            continue

        if STATIC_IMPORT_RE.match(line):
            if code_seen:
                out.append((lineno, line))
            if _import_continues(line):
                in_import = True
            continue

        code_seen = True

    return out


def main() -> int:
    failures: list[str] = []
    for root_name in SEARCH_ROOTS:
        root = ROOT / root_name
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if not path.is_file() or _should_skip(path):
                continue
            for lineno, line in _violations(path):
                failures.append(f"{path.relative_to(ROOT)}:{lineno}: static import after code: {line}")

    if failures:
        print("Import structure check failed:")
        print("\n".join(failures))
        return 1

    print("Import structure check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
