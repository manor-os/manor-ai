"""Recover a public web-fetch denial from the same message's runtime evidence."""
from __future__ import annotations

import json
from collections import Counter

from packages.core.ai.runtime.output_policy import (
    PUBLIC_REQUEST_FAILURE_MESSAGE,
    PUBLIC_TOOL_FAILURE_MESSAGE,
    PUBLIC_WEBSITE_ACCESS_DENIED_MESSAGE,
)


def _fetch_url(call, arguments_key):
    if not isinstance(call, dict) or call.get("name") != "web_fetch":
        return None
    arguments = call.get(arguments_key)
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except (TypeError, ValueError):
            return None
    url = arguments.get("url") if isinstance(arguments, dict) else None
    return url if isinstance(url, str) and url.startswith(("https://", "http://")) else None


def _recoverable_failure(row, result_key):
    result = row.get(result_key)
    return row.get("status") in {"failed", "error"} and isinstance(result, str) and result.strip() in {
        PUBLIC_TOOL_FAILURE_MESSAGE, PUBLIC_REQUEST_FAILURE_MESSAGE,
        PUBLIC_WEBSITE_ACCESS_DENIED_MESSAGE,
    }


def project_web_access_denials(tool_calls, blocks, metadata):
    """Return copies of matched UI rows; leave execution/audit state untouched.

    Legacy rows have no tool call id. Recover only an exact URL with one call,
    one process step (when present), and one matching runtime end event in this
    message. Only generic legacy errors are recovered; specific results and
    conflicting outcomes remain unchanged.
    """
    if not isinstance(tool_calls, list) or not isinstance(metadata, dict):
        return tool_calls, blocks
    runtime = metadata.get("runtime")
    runtime_meta = runtime.get("metadata") if isinstance(runtime, dict) else None
    events = runtime_meta.get("runtime_events") if isinstance(runtime_meta, dict) else None
    if not isinstance(events, list):
        return tool_calls, blocks

    call_urls = Counter(url for call in tool_calls if (url := _fetch_url(call, "arguments")))
    steps = [
        step for block in blocks if isinstance(block, dict) and block.get("type") == "process"
        and isinstance(block.get("steps"), list)
        for step in block["steps"] if isinstance(step, dict)
        and step.get("kind", "tool") == "tool"
    ] if isinstance(blocks, list) else []
    step_urls = Counter(url for step in steps if (url := _fetch_url(step, "arguments_preview")))
    matching = {url: [] for url in call_urls}
    for event in events:
        if not isinstance(event, dict) or event.get("type") != "tool_end" or event.get("tool_name") != "web_fetch":
            continue
        error = event.get("error")
        if not isinstance(error, str):
            continue
        for url in matching:
            if error.startswith(f"Failed to fetch {url}: "):
                matching[url].append(event)
    denied = {
        url for url, evidence in matching.items()
        if call_urls[url] == 1 and step_urls[url] <= 1 and len(evidence) == 1
        and all(_recoverable_failure(call, "result") for call in tool_calls
                if _fetch_url(call, "arguments") == url)
        and all(_recoverable_failure(step, "result_preview") for step in steps
                if _fetch_url(step, "arguments_preview") == url)
        and evidence[0].get("status") in {"failed", "error"}
        and evidence[0].get("error") == f"Failed to fetch {url}: HTTP 403"
    }
    if not denied:
        return tool_calls, blocks

    def project(row, arguments_key, result_key):
        if _fetch_url(row, arguments_key) not in denied or row.get("status") not in {"error", "failed"}:
            return row
        return {**row, result_key: PUBLIC_WEBSITE_ACCESS_DENIED_MESSAGE,
                "reason": "website_access_denied", "status_code": 403}

    calls = [project(call, "arguments", "result") for call in tool_calls]
    projected_blocks = [
        {**block, "steps": [project(step, "arguments_preview", "result_preview")
                           if isinstance(step, dict) and step.get("kind", "tool") == "tool" else step
                           for step in block["steps"]]}
        if isinstance(block, dict) and block.get("type") == "process" and isinstance(block.get("steps"), list)
        else block for block in blocks
    ] if isinstance(blocks, list) else blocks
    return calls, projected_blocks
