"""Bounded model-context projections for successful Chrome ``read_page`` calls."""

from __future__ import annotations

import json
import re
from typing import Any


_TREE_REF = re.compile(r"\[ref=([^\]]+)\]")
_IDENTITY_KEYS = (
    "ref", "role", "label", "href", "frame_id", "frame_selector", "frame_url", "scope_id",
    "selector", "shadow_host", "shadow_selector", "dialog_label", "container_label", "type", "name",
    "value", "description", "min", "max", "step", "min_length", "max_length", "pattern",
    "checked", "checked_state", "pressed", "pressed_state", "selected", "disabled", "read_only",
    "required", "expanded", "valid", "validation_message", "editable", "clickable", "in_viewport",
)
_ESSENTIAL_KEYS = (
    "ok", "status", "driver", "snapshot_contract", "snapshot_id", "tabId", "url", "title",
    "observation_quality", "observation_coverage", "frame_summary", "skipped_frames",
    "pending_verification", "action_verification", "read_page_required", "next_required_tool",
    "action_executed", "do_not_replay", "verified", "retryable", "event_receipt",
)
_OPTIONAL_KEYS = ("page_feedback", "page_blockers", "status_flags")
_METADATA_LIST_KEYS = ("form_candidates", "choice_candidates", "upload_candidates", "input_candidates", "refMap")
_ATOMIC_TEXT_KEYS = frozenset({"url", "href", "selector", "frame_selector", "frame_url", "shadow_host", "shadow_selector"})
_IDENTITY_RESERVE = 2048


def is_successful_read_page(tool_name: str, value: Any) -> bool:
    return (
        tool_name in {"mcp__chrome__read_page", "browser_read_page"}
        and isinstance(value, dict)
        and value.get("ok") is True
        and value.get("status") == "read_page"
        and isinstance(value.get("pageContent"), str)
    )


def _encoded(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _compact_value(value: Any, *, key: str = "", depth: int = 0) -> Any:
    if isinstance(value, str):
        if key in _ATOMIC_TEXT_KEYS:
            return value
        return value if len(value) <= 480 else value[:477] + "..."
    if isinstance(value, dict):
        if depth >= 3:
            return None
        return {
            child_key: compacted for child_key, item in value.items()
            if (compacted := _compact_value(item, key=child_key, depth=depth + 1)) is not None
        }
    if isinstance(value, list):
        if depth >= 3:
            return None
        return [compacted for item in value[:8]
                if (compacted := _compact_value(item, depth=depth + 1)) is not None]
    return value


def _tree_prefix_at_boundary(tree: str, limit: int) -> tuple[str, bool]:
    if len(tree) <= limit:
        return tree, False
    # ``boundary + 1`` must be no larger than limit. Keeping ``limit`` as an
    # exclusive search end also guarantees shrink loops remove a trailing line.
    boundary = tree.rfind("\n", 0, max(0, limit))
    return (tree[:boundary + 1] if boundary >= 0 else ""), True


def _identity(item: dict[str, Any]) -> dict[str, Any]:
    return {key: item[key] for key in _IDENTITY_KEYS if key in item}


def _matching_refs(tree: str, refs: Any) -> list[dict[str, Any]]:
    tree_refs = set(_TREE_REF.findall(tree))
    return [
        _identity(item) for item in refs if isinstance(item, dict)
        if str(item.get("ref") or "") in tree_refs
        or (isinstance(item.get("href"), str) and bool(item["href"]))
    ] if isinstance(refs, list) else []


def _content_links(refs: Any, existing: Any) -> list[dict[str, Any]]:
    candidates = [item for item in existing if isinstance(item, dict)] if isinstance(existing, list) else []
    if isinstance(refs, list):
        candidates.extend(item for item in refs if isinstance(item, dict))
    links: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for item in candidates:
        href = item.get("href")
        if not isinstance(href, str) or not href or item.get("hidden") is True or item.get("visible") is False:
            continue
        marker = (href, item.get("frame_id"), item.get("ref"))
        if marker in seen:
            continue
        seen.add(marker)
        link = _identity(item)
        for key in ("snippet", "source", "published_time"):
            if key in item:
                link[key] = _compact_value(item[key], key=key)
        links.append(link)
    return links


def _prior_omissions(value: dict[str, Any], key: str) -> int:
    prior = value.get("_tool_result_truncated")
    return int(prior.get(key) or 0) if isinstance(prior, dict) else 0


def compact_successful_read_page(tool_name: str, value: dict[str, Any], *, max_chars: int, digest: str) -> str | None:
    """Project successful reads with the hierarchy ahead of optional metadata."""
    if not is_successful_read_page(tool_name, value):
        return None
    from packages.core.ai.runtime.chrome_page_text import render_read_page

    readable = render_read_page(tool_name, value, max_chars)
    if readable is not None:
        return readable
    prior_tree_cut = bool(value.get("page_content_truncated")) or bool(_prior_omissions(value, "page_content_truncated"))
    output: dict[str, Any] = {
        "semantic_refs": [], "content_links": [], "pageContent": "", "page_content_truncated": prior_tree_cut,
        "_tool_result_truncated": {"tool": tool_name, "sha256": digest, "strategy": "chrome_read_page_context", "page_content_truncated": prior_tree_cut},
    }

    def fits(candidate: dict[str, Any]) -> bool:
        return len(_encoded(candidate)) <= max_chars

    omitted_metadata = _prior_omissions(value, "omitted_metadata_fields")
    # Immutable page facts first. Optional diagnostics wait until the tree is
    # known, which prevents repeated feedback from starving the hierarchy.
    for key in _ESSENTIAL_KEYS:
        if key not in value:
            continue
        candidate = {**output, key: _compact_value(value[key], key=key)}
        if fits(candidate):
            output[key] = candidate[key]
        else:
            omitted_metadata += 1

    source_refs = value.get("semantic_refs")
    links = _content_links(source_refs, value.get("content_links"))
    smallest_link = min((len(_encoded(link)) for link in links), default=0)
    identity_reserve = _IDENTITY_RESERVE if 0 < smallest_link <= _IDENTITY_RESERVE else 0
    tree_budget = max(0, max_chars - len(_encoded(output)) - identity_reserve)
    page_content, tree_cut = _tree_prefix_at_boundary(value["pageContent"], tree_budget)
    if tree_cut:
        # Only a tree that already needs cutting yields a bounded lane for
        # follow-up form/upload evidence. A complete reading tree never pays
        # this metadata reservation.
        tree_budget = max(0, tree_budget - min(1000, max_chars // 4))
        page_content, tree_cut = _tree_prefix_at_boundary(value["pageContent"], tree_budget)
    output["pageContent"] = page_content
    output["page_content_truncated"] = prior_tree_cut or tree_cut
    output["_tool_result_truncated"]["page_content_truncated"] = prior_tree_cut or tree_cut

    omitted_refs = _prior_omissions(value, "omitted_semantic_refs")
    omitted_links = _prior_omissions(value, "omitted_content_links")
    for key, records in (("semantic_refs", _matching_refs(page_content, source_refs)), ("content_links", links)):
        for record in records:
            candidate = {**output, key: [*output[key], record]}
            if fits(candidate):
                output[key].append(record)
            elif key == "semantic_refs":
                omitted_refs += 1
            else:
                omitted_links += 1

    # Only after the hierarchy has been retained do we spend spare capacity on
    # form and feedback evidence. Every record is complete or explicitly
    # counted as omitted; no nested locator is silently shortened.
    for key in (*_METADATA_LIST_KEYS, *_OPTIONAL_KEYS):
        records = value.get(key)
        if isinstance(records, list):
            admitted: list[Any] = []
            for record in records[:8]:
                compacted = _compact_value(record, key=key)
                candidate = {**output, key: [*admitted, compacted]}
                if compacted is not None and fits(candidate):
                    admitted.append(compacted)
                else:
                    omitted_metadata += 1
            omitted_metadata += max(0, len(records) - 8)
            if admitted:
                output[key] = admitted
        elif records is not None:
            compacted = _compact_value(records, key=key)
            candidate = {**output, key: compacted}
            if compacted is not None and fits(candidate):
                output[key] = compacted
            else:
                omitted_metadata += 1

    if omitted_metadata:
        output["_tool_result_truncated"]["omitted_metadata_fields"] = omitted_metadata
    if omitted_refs:
        output["_tool_result_truncated"]["omitted_semantic_refs"] = omitted_refs
    if omitted_links:
        output["_tool_result_truncated"]["omitted_content_links"] = omitted_links

    # Preserve a complete tree before duplicate identities. For an already
    # cut tree, trim at semantic line boundaries after whole records are gone.
    if not tree_cut:
        while len(_encoded(output)) > max_chars and output["semantic_refs"]:
            output["semantic_refs"].pop()
            omitted_refs += 1
        while len(_encoded(output)) > max_chars and output["content_links"]:
            output["content_links"].pop()
            omitted_links += 1
        if omitted_refs:
            output["_tool_result_truncated"]["omitted_semantic_refs"] = omitted_refs
        if omitted_links:
            output["_tool_result_truncated"]["omitted_content_links"] = omitted_links
    while len(_encoded(output)) > max_chars and output["pageContent"]:
        excess = len(_encoded(output)) - max_chars
        output["pageContent"], _ = _tree_prefix_at_boundary(output["pageContent"], len(output["pageContent"]) - excess - 1)
        output["page_content_truncated"] = True
        output["_tool_result_truncated"]["page_content_truncated"] = True

    if len(_encoded(output)) > max_chars:
        output = {
            "ok": True, "status": "read_page", "pageContent": "", "page_content_truncated": True,
            "_tool_result_truncated": {"tool": tool_name, "sha256": digest, "strategy": "chrome_read_page_context", "page_content_truncated": True, "coverage_insufficient": True},
        }
    return _encoded(output)
