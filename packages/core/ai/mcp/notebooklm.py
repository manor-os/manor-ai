"""NotebookLM (notebooklm.google.com) — MCP wrapper.

NotebookLM has no public API. Manor uses the browser-runner sidecar
to drive a logged-in Chromium against the web app.

Auth: bearer_token = the user's exported cookie JSON, stored as an
entity Integration with provider="notebooklm" and credentials
``{"cookies_json": "<paste>"}``. Two formats are accepted (see
``_browser_runner.parse_storage_state``):

  1. Playwright storage_state JSON
  2. Cookie-Editor / EditThisCookie list-export JSON

The user typically uses a Chrome extension to export their session
cookies after signing into Google in their normal browser.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List

from . import _browser_runner

logger = logging.getLogger(__name__)


def list_tools() -> List[Dict[str, Any]]:
    return [
        {
            "name": "list_notebooks",
            "description": (
                "List the user's NotebookLM notebooks. Returns each "
                "notebook's id and title — the agent uses the id with "
                "ask() to query a specific notebook."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
        {
            "name": "create_notebook",
            "description": (
                "Create a new NotebookLM notebook, optionally seeding "
                "it with web sources. Returns the new notebook's id "
                "and URL."
            ),
            "parameters": {
                "type": "object",
                "required": ["name"],
                "properties": {
                    "name": {"type": "string", "description": "Notebook title."},
                    "sources": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional list of source URLs (web pages NotebookLM will ingest).",
                    },
                },
            },
        },
        {
            "name": "ask",
            "description": (
                "Ask a question against an existing notebook. Returns "
                "NotebookLM's answer text with citation chips mapped "
                "back to source titles."
            ),
            "parameters": {
                "type": "object",
                "required": ["notebook_id", "question"],
                "properties": {
                    "notebook_id": {"type": "string"},
                    "question": {"type": "string"},
                },
            },
        },
    ]


async def call_tool(
    name: str,
    arguments: Dict[str, Any],
    bearer_token: str,
) -> Dict[str, Any]:
    """``bearer_token`` here is the user's exported cookie JSON."""
    if not bearer_token:
        return _error(
            "NotebookLM cookies are missing. Export your Google session "
            "cookies (Cookie-Editor extension → Export → JSON) and paste "
            "into Integrations → NotebookLM."
        )
    storage_state = _browser_runner.parse_storage_state(bearer_token)
    if not storage_state:
        return _error(
            "Could not parse the NotebookLM cookies. Expected either "
            "Playwright storage_state JSON or a Cookie-Editor export. "
            "Re-export and paste again."
        )

    try:
        resp = await _browser_runner.perform(
            provider="notebooklm",
            action=name,
            params=arguments,
            storage_state=storage_state,
            # NotebookLM answers can take 30-60s to settle.
            timeout_ms=120_000,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("notebooklm tool %s crashed", name)
        return _error(f"NotebookLM call failed: {exc}")

    if not resp.get("ok"):
        return _error(resp.get("error") or "browser-runner returned non-ok")
    return _content(json.dumps(resp.get("result") or {}, ensure_ascii=False, indent=2))


def _content(text: str) -> Dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": False}


def _error(msg: str) -> Dict[str, Any]:
    return {"content": [{"type": "text", "text": msg}], "isError": True}
