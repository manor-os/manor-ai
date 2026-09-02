#!/usr/bin/env python3
"""Sync the canonical Stickman Skill body/version into its embedded Blueprint copy."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILL_PATH = ROOT / "packages/core/ai/marketplace_skills/stickman-video-creator/SKILL.md"
BLUEPRINT_PATH = ROOT / "packages/core/blueprints/configs/solo_company/solo-faceless-stickman-studio-v1.json"
CHANGELOG_ENTRY = (
    "v1.43 (2026-08-17): verify final Stickman publication from durable MP4, subtitle, "
    "and narration-timeline receipts before the YouTube continuation."
)


def _skill_parts() -> tuple[str, str, list[str]]:
    source = SKILL_PATH.read_text(encoding="utf-8")
    parts = source.split("---", 2)
    if len(parts) != 3:
        raise RuntimeError("Stickman SKILL.md has invalid frontmatter")
    match = re.search(r"^version:\s*([^\s]+)\s*$", parts[1], re.MULTILINE)
    if match is None:
        raise RuntimeError("Stickman SKILL.md has no version")
    tools_match = re.search(r"^tools:\s*\n((?:\s{2}-\s+[^\n]+\n)+)", parts[1], re.MULTILINE)
    if tools_match is None:
        raise RuntimeError("Stickman SKILL.md has no tools")
    tools = [
        line.split("-", 1)[1].strip()
        for line in tools_match.group(1).splitlines()
        if line.strip().startswith("-")
    ]
    return match.group(1), parts[2].lstrip("\n"), tools


def _json_string_span(source: str, key_start: int) -> tuple[int, int, str]:
    value_start = source.index(":", key_start) + 1
    while source[value_start].isspace():
        value_start += 1
    value, consumed = json.JSONDecoder().raw_decode(source[value_start:])
    if not isinstance(value, str):
        raise RuntimeError("Expected a JSON string")
    return value_start, value_start + consumed, value


def synced_source() -> tuple[str, bool]:
    version, prompt, tools = _skill_parts()
    original_source = BLUEPRINT_PATH.read_text(encoding="utf-8")
    source = original_source

    changelog_key = source.index('"changelog"')
    changelog_start, changelog_end, changelog = _json_string_span(source, changelog_key)
    if not changelog.startswith(CHANGELOG_ENTRY):
        source = (
            source[:changelog_start]
            + json.dumps(f"{CHANGELOG_ENTRY} {changelog}", ensure_ascii=False)
            + source[changelog_end:]
        )

    slug_at = source.index('"slug": "stickman-video-creator"')
    prompt_key = source.index('"system_prompt"', slug_at)
    prompt_start, prompt_end, _old_prompt = _json_string_span(source, prompt_key)

    tools_key = source.index('"tools"', prompt_end)
    tools_start = source.index(":", tools_key) + 1
    while source[tools_start].isspace():
        tools_start += 1
    old_tools, tools_consumed = json.JSONDecoder().raw_decode(source[tools_start:])
    if not isinstance(old_tools, list):
        raise RuntimeError("Embedded Stickman Skill tools are not an array")
    tools_end = tools_start + tools_consumed
    source = source[:tools_start] + json.dumps(tools, ensure_ascii=False) + source[tools_end:]

    version_match = re.search(r'"version":\s*"[^"]+"', source[prompt_end:])
    if version_match is None:
        raise RuntimeError("Embedded Stickman Skill has no version")
    version_start = prompt_end + version_match.start()
    version_end = prompt_end + version_match.end()

    updated = (
        source[:prompt_start]
        + json.dumps(prompt, ensure_ascii=False)
        + source[prompt_end:version_start]
        + f'"version": {json.dumps(version)}'
        + source[version_end:]
    )
    return updated, updated != original_source


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    updated, changed = synced_source()
    if args.check:
        if changed:
            print("Stickman Blueprint embedded Skill is out of sync")
            return 1
        print("Stickman Blueprint embedded Skill is synchronized")
        return 0
    if changed:
        BLUEPRINT_PATH.write_text(updated, encoding="utf-8")
        print(f"Updated {BLUEPRINT_PATH}")
    else:
        print("Already synchronized")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
