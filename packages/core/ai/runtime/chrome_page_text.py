"""Readable model-only browser snapshots; raw observations remain runtime-owned."""

from __future__ import annotations

import json
import re
from typing import Any

PREFIX = "Browser page snapshot\n"
TREE_SEPARATOR = "\n\nPage tree:\n"
READ_TOOLS = {"mcp__chrome__read_page", "browser_read_page", "read_page"}
FACTS = (
    "action_executed", "do_not_replay", "verified", "retryable", "read_page_required", "next_required_tool",
    "pending_verification", "action_verification", "verification", "action_receipt", "event_receipt",
    "observation_quality", "visual_fallback_recommended", "visual_fallback_reasons",
    "page_blockers", "page_feedback", "status_flags",
    "skipped_frames", "operation_intent_candidates", "cached_path_hints",
)
INTERNAL = {"semantic_refs", "refMap", "candidate_summary", "pageContent", "observation", "post_action_page_state"}


def _scalar(value: Any) -> str:
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False) if "\n" in value or "\r" in value else value
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _leaves(key: str, value: Any, depth: int = 0):
    if depth > 4:
        yield f"{key}: [details omitted]"
    elif isinstance(value, dict):
        for child, item in value.items():
            if child not in INTERNAL:
                yield from _leaves(f"{key}.{child}", item, depth + 1)
    elif isinstance(value, list):
        for index, item in enumerate(value[:8]):
            yield from _leaves(f"{key}[{index}]", item, depth + 1)
        if len(value) > 8:
            yield f"{key}: {len(value) - 8} more entries omitted"
    elif value is not None and value != "":
        yield f"{key}: {_scalar(value)}"


def _cut_lines(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    boundary = text.rfind("\n", 0, max(0, limit))
    return text[:boundary + 1] if boundary >= 0 else ""


def compact_read_page_text(text: str, max_chars: int) -> str | None:
    """Recompact rendered history without breaking identifiers or tree lines."""
    if not text.startswith(PREFIX):
        return None
    if len(text) <= max_chars:
        return text
    header, _, tree = text.partition(TREE_SEPARATOR)
    lines = [line for line in header.splitlines() if not line.startswith(("coverage:", "output_truncated:"))]
    lines[1:1] = ["output_truncated: true"]
    header = "\n".join(lines)
    if len(header + TREE_SEPARATOR) > max_chars:
        # Whole facts only; a tiny budget must not manufacture partial URLs or
        # silently present a snapshot as actionable after losing its metadata.
        warning = "\nmetadata: incomplete; read_page required\n"
        mandatory = [line for line in lines if line.startswith((
            "Browser page", "output_truncated:", "action_executed:", "do_not_replay:", "verified:",
        ))]
        kept = "\n".join(mandatory)
        if len(kept + warning) > max_chars:
            return _cut_lines(kept + warning, max_chars)
        for line in lines:
            if line not in mandatory and len(kept + "\n" + line + warning) <= max_chars:
                kept += "\n" + line
        return kept + warning
    return header + TREE_SEPARATOR + _cut_lines(tree, max_chars - len(header + TREE_SEPARATOR))


def _combobox_semantics(tree: str, semantic_refs: Any) -> str:
    """Keep the facts that distinguish native selects from popup triggers."""
    if not isinstance(semantic_refs, list):
        return tree
    refs = {
        item["ref"]: item for item in semantic_refs
        if isinstance(item, dict) and isinstance(item.get("ref"), str)
        and item.get("role") == "combobox"
    }

    def annotate(match: re.Match) -> str:
        item = refs.get(match[2], {})
        facts = ""
        tag = item.get("tag")
        if isinstance(tag, str) and re.fullmatch(r"[a-z][a-z0-9-]{0,63}", tag):
            facts += f" [tag={tag}]"
        if isinstance(item.get("editable"), bool):
            facts += f" [editable={_scalar(item['editable'])}]"
        return match[1] + facts

    # Extension labels encode quotes as entities and keep backslashes literal.
    return re.sub(
        r'(?m)^([ \t]*- combobox(?: "[^"\n]*")? \[ref=([^\]\n]+)\])',
        annotate, tree,
    )


def render_read_page(tool_name: str, value: Any, max_chars: int = 1016384) -> str | None:
    if (tool_name not in READ_TOOLS or not isinstance(value, dict) or value.get("ok") is not True
            or value.get("status") != "read_page" or not isinstance(value.get("pageContent"), str)):
        return None
    tree = value["pageContent"]
    if (value.get("_tool_result_truncated") or {}).get("strategy") == "chrome_read_page_context":
        return None  # Recompact legacy JSON without losing its link evidence.
    if tree.strip() and not re.match(r"\s*- [\w/]", tree):
        return None  # Older extensions can still return a non-tree reading.
    tree = _combobox_semantics(tree, value.get("semantic_refs"))
    coverage = value.get("observation_coverage") or {}
    frame_coverage = value.get("frame_coverage") or {}
    frames = value.get("frame_summary") or {}
    lines = ["Browser page snapshot"]
    for key in ("tabId", "snapshot_id", "url", "title", "page_status"):
        if value.get(key) is not None and value[key] != "":
            lines.append(f"{key}: {_scalar(value[key])}")
    if value.get("page_content_truncated"):
        lines.append("page_content_truncated: true")
    if frame_coverage.get("complete") is False:
        lines.append("frame_coverage.complete: false")
    if frames.get("unresolved"):
        lines.append(f"unresolved_frames: {frames['unresolved']}")
    if frames:
        lines.append(f"frames: {frames.get('observed', '?')}/{frames.get('total', '?')} observed; {frames.get('skipped', 0)} skipped")
    for key, item in coverage.items():
        if item is False:
            lines.append(f"observation_coverage.{key}: false")
    # Critical state is listed first. Never spend the reading budget on empty
    # candidate arrays, every element's selectors, bounds, or duplicate names.
    facts = [line for key in FACTS if key in value for line in _leaves(key, value[key])]
    metadata_limit = min(8192, max(256, max_chars // 2))
    omitted = 0
    for line in facts:
        if len("\n".join([*lines, line])) <= metadata_limit:
            lines.append(line)
        else:
            omitted += 1
    if omitted:
        lines.append(f"metadata: {omitted} details omitted; use focused read or diagnostics")
    # Native trees already contain /url children. Retain bounded exact links
    # supplied only as metadata without reproducing the entire ref inventory.
    refs = set(re.findall(r"\[ref=([^\]]+)\]", tree))
    tree_urls = set()
    for literal in re.findall(r"^\s*- /url: (.+)$", tree, re.MULTILINE):
        try:
            tree_urls.add(json.loads(literal) if literal.startswith('"') else literal)
        except (ValueError, TypeError):
            pass
    seen = set()
    link_chars = 0
    links_omitted = 0
    for item in [*(value.get("content_links") or []), *(value.get("semantic_refs") or [])]:
        if not isinstance(item, dict) or item.get("hidden") is True or item.get("visible") is False:
            continue
        href = item.get("href")
        if not isinstance(href, str) or not href or href in tree_urls or href in seen:
            continue
        if item.get("ref") and item["ref"] not in refs:
            continue
        seen.add(href)
        line = f"link [{item.get('ref', '')}]: {_scalar(href)}"
        if link_chars + len(line) + 1 <= min(2048, max_chars // 4):
            lines.append(line)
            link_chars += len(line) + 1
        else:
            links_omitted += 1
    if links_omitted:
        lines.append(f"links_omitted: {links_omitted}; use focused read")
    text = "\n".join(lines) + TREE_SEPARATOR + tree
    return compact_read_page_text(text, max_chars)
