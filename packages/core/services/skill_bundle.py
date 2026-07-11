"""Packaging logic for LLM-generated skills.

Pure, dependency-free helpers (no DB, no runtime, no network) so they can be
unit-tested in isolation. Given a generated skill spec, decide whether it is a
plain *prompt* skill or a *sandbox bundle* (SKILL.md + standalone scripts +
on-demand references) and produce the ``(tools, config)`` to persist.
"""
from __future__ import annotations

import json
import re
from typing import Any

# Toolset for sandbox (bundle) skills — mirrors builtin skills so generated
# bundles run through the same SKILL.md/sandbox executor (progressive
# disclosure via sandbox_read_file, standalone scripts via sandbox_exec).
SANDBOX_SKILL_TOOLS = [
    "invoke_skill",
    "search_tools",
    "generate_file",
    "sandbox_exec",
    "sandbox_read_file",
    "sandbox_write_file",
    "sandbox_save_result",
    "sandbox_destroy",
]


def extract_json_object(raw: str) -> dict:
    """Robustly pull a JSON object out of an LLM completion.

    LLMs don't always return clean JSON — they wrap it in ```json fences, add a
    "Here's the skill:" preamble, or trail commentary after the object. The
    naive "strip fences then json.loads" approach fails on any of those with a
    cryptic "Expecting value: line 1 column 1 (char 0)". This tries, in order:
    the whole text, each fenced block, and a balanced object scanned from every
    ``{`` via the JSON decoder — returning the first that parses to a dict.

    Raises ``ValueError`` with a readable snippet when nothing parses, instead
    of leaking the raw ``JSONDecodeError``.
    """
    text = (raw or "").strip()
    if not text:
        raise ValueError("LLM returned an empty response (expected a JSON object)")

    candidates: list[str] = [text]
    # Fenced ```json ... ``` blocks (anywhere in the text).
    candidates.extend(
        m.group(1).strip()
        for m in re.finditer(r"```(?:json)?\s*([\s\S]*?)```", text, re.IGNORECASE)
    )
    # Any balanced object starting at a `{`, tolerating surrounding prose.
    decoder = json.JSONDecoder()
    for idx, ch in enumerate(text):
        if ch != "{":
            continue
        try:
            value, end = decoder.raw_decode(text[idx:])
        except Exception:
            continue
        if isinstance(value, dict):
            candidates.append(text[idx : idx + end])

    for candidate in candidates:
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
        except Exception:
            continue
        if isinstance(parsed, dict):
            return parsed

    snippet = text if len(text) <= 280 else f"{text[:280]}…"
    raise ValueError(f"LLM did not return a JSON object. Got: {snippet}")


def parse_clarifying_questions(text: str) -> list[str]:
    """Parse the skill clarifier's output into a list of questions.

    Returns an empty list when the request was deemed READY (no questions
    needed) or the model produced nothing usable. Strips numbering/bullets,
    de-duplicates while preserving order, and caps at 3.
    """
    body = str(text or "").strip()
    if not body or body.upper().startswith("READY"):
        return []
    questions: list[str] = []
    seen: set[str] = set()
    for line in body.splitlines():
        cleaned = line.strip().lstrip("-*0123456789.) ").strip()
        if not cleaned:
            continue
        key = cleaned.lower()
        if key in seen:
            continue
        seen.add(key)
        questions.append(cleaned)
    return questions[:3]


def _clean_scripts(raw: Any) -> dict[str, str]:
    """Normalize a ``scripts`` map: bare filename → non-empty content."""
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for key, value in raw.items():
        name = str(key).strip().lstrip("/").split("/")[-1]
        content = str(value)
        if name and content.strip():
            out[name] = content
    return out


def _clean_references(raw: Any) -> dict[str, str]:
    """Normalize a ``references`` map: stored under ``references/<name>``."""
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for key, value in raw.items():
        name = str(key).strip().lstrip("/").split("/")[-1]
        content = str(value)
        if name and content.strip():
            out[f"references/{name}"] = content
    return out


def assemble_skill_bundle(
    spec: dict[str, Any], base_config: dict[str, Any]
) -> tuple[list[str], dict[str, Any]]:
    """Derive ``(tools, config)`` for a generated skill.

    When the spec carries standalone ``scripts`` (filename → content) or
    ``references`` (name → content), the skill is packaged as a **sandbox
    bundle**: ``system_prompt`` becomes the SKILL.md, the files are written to
    skill storage, and ``_determine_skill_type`` routes it to the sandbox
    executor (``config.type == "sandbox"`` / ``config.scripts``). Otherwise it
    stays a plain prompt skill with the model-declared tools.
    """
    scripts = _clean_scripts(spec.get("scripts"))
    extra_files = _clean_references(spec.get("references"))

    config = dict(base_config)
    if scripts or extra_files:
        config["type"] = "sandbox"
        if scripts:
            config["scripts"] = scripts
        if extra_files:
            config["extra_files"] = extra_files
        return list(SANDBOX_SKILL_TOOLS), config

    tools = spec.get("tools")
    tools = tools if isinstance(tools, list) else []
    return [str(t) for t in tools if str(t).strip()], config
