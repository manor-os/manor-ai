"""Scoped Instagram collection tools backed by durable Apify operations.

The dispatcher supplies actor and credential scope through ContextVar, never
through model-authored arguments. Collection service owns admission, provider
execution and settlement; progress reads do not incur a fixed MCP call fee.
"""
from __future__ import annotations

import contextvars
import copy
import json
import logging
from typing import Any

from jsonschema import Draft202012Validator

logger = logging.getLogger(__name__)
_context: contextvars.ContextVar[dict[str, Any]] = contextvars.ContextVar(
    "apify_call_context", default={},
)


def set_call_context(context: dict[str, Any]) -> None:
    _context.set(dict(context))


def clear_call_context() -> None:
    _context.set({})


def _schema(properties: dict, required: list[str]) -> dict:
    return {
        "type": "object", "properties": properties,
        "required": required, "additionalProperties": False,
    }


_COLLECTION_ID = {
    "type": "string", "minLength": 1, "maxLength": 64,
    "description": "Manor collection ID returned by quote/start, not an Apify run ID.",
}
_TOOLS = [
    {
        "name": "quote_instagram_collection",
        "description": (
            "Prepare a bounded Instagram collection and return its maximum Credits "
            "charge without starting Apify. Search creators, collect one profile's "
            "last 7/14/28 days, or process one selected Reel. Present the budget "
            "before starting; AI adaptation and asset generation are separate."
            " Select a connected integration_account_id to use customer-funded "
            "Apify; without a selection, configured Manor platform funding takes precedence."
        ),
        "inputSchema": _schema({
            "mode": {"type": "string", "enum": ["search", "profile", "reel"]},
            "query": {"type": "string", "minLength": 1, "maxLength": 100,
                      "description": "Creator search text; search mode only."},
            "profile_url": {"type": "string", "minLength": 1, "maxLength": 250,
                            "description": "One Instagram profile URL or handle; profile mode only."},
            "reel_url": {"type": "string", "minLength": 1, "maxLength": 250,
                         "description": "One public Instagram Reel URL; reel mode only."},
            "lookback_days": {"type": "integer", "enum": [7, 14, 28],
                              "description": "Profile mode only; default 14."},
            "results_limit": {"type": "integer", "minimum": 1, "maximum": 200,
                              "description": "Profile: default 50, max 200. Search: default 10, max 20. Reel: 1."},
            "include_transcript": {"type": "boolean", "default": False,
                                   "description": "Paid audio transcript add-on; selected Reel only."},
            "include_downloaded_video": {"type": "boolean", "default": False,
                                         "description": "Paid video download add-on; selected Reel only. Returned URLs are temporary."},
            "max_cost_usd": {"type": "string", "default": "1.00",
                             "description": "Supplier cost ceiling as a decimal USD string, e.g. 0.50. Deployment limit applies; not an exact estimate."},
        }, ["mode"]),
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "start_instagram_collection",
        "description": (
            "Start the exact previously quoted Instagram collection. Reserves its "
            "maximum Manor Credits before provider work when platform funded. "
            "Reuse the same quote ID on retry; never create a fresh quote to retry "
            "an unknown or pending start. Returns a durable collection ID."
        ),
        "inputSchema": _schema({"quote_id": _COLLECTION_ID}, ["quote_id"]),
        "annotations": {"readOnlyHint": False, "idempotentHint": True},
    },
    {
        "name": "get_instagram_collection",
        "description": (
            "Check an existing collection, settle completed provider usage once, "
            "and read available results. Does not start another run or charge a "
            "per-poll fee. Preserve coverage/partial indicators; temporary media "
            "links and transcripts are reference data, not finished Knowledge artifacts."
        ),
        "inputSchema": _schema({"collection_id": _COLLECTION_ID}, ["collection_id"]),
        "annotations": {"readOnlyHint": True, "idempotentHint": True},
    },
    {
        "name": "cancel_instagram_collection",
        "description": (
            "Request cancellation of this collection and reconcile its existing "
            "provider run. Already incurred costs still apply up to the quoted "
            "ceiling; an unconfirmed cancellation does not release its reservation."
        ),
        "inputSchema": _schema({"collection_id": _COLLECTION_ID}, ["collection_id"]),
        "annotations": {"readOnlyHint": False, "idempotentHint": True},
    },
]


def list_tools() -> list[dict[str, Any]]:
    return copy.deepcopy(_TOOLS)


def _result(payload: dict[str, Any], *, error: bool = False) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
        "isError": error,
    }


async def call_tool(name: str, arguments: dict[str, Any], bearer_token: str) -> dict[str, Any]:
    tool = next((tool for tool in _TOOLS if tool["name"] == name), None)
    if tool is None:
        return _result({"code": "unknown_tool", "message": "Unknown Apify operation."}, error=True)
    if not isinstance(arguments, dict) or not Draft202012Validator(tool["inputSchema"]).is_valid(arguments):
        return _result({"code": "invalid_arguments", "message": "Arguments do not match this Apify operation."}, error=True)
    context = _context.get()
    if (
        not context.get("entity_id") or not context.get("user_id")
        or context.get("user_id") == "ai-agent"
        or not context.get("credential_scope")
    ):
        return _result({"code": "actor_context_required", "message": "An authenticated Manor actor and selected connection are required."}, error=True)
    if not isinstance(bearer_token, str) or not bearer_token.strip():
        return _result({"code": "credentials_unavailable", "message": "Connect an Apify API key or configure Manor's platform credential."}, error=True)

    from packages.core.services import apify_collections
    from packages.core.services.apify_client import ApifyError

    scope = {
        "entity_id": context["entity_id"],
        "user_id": context["user_id"],
        "workspace_id": context.get("workspace_id"),
        "credential_scope": context["credential_scope"],
        "account_id": context.get("integration_account_id"),
    }
    try:
        if name == "quote_instagram_collection":
            args = dict(arguments)
            max_cost_usd = args.pop("max_cost_usd", "1.00")
            result = await apify_collections.quote_collection(
                **scope, arguments=args,
                max_cost_usd=max_cost_usd,
                agent_id=context.get("agent_id"),
                conversation_id=context.get("conversation_id"),
            )
        elif name == "start_instagram_collection":
            result = await apify_collections.start_collection(
                **scope, quote_id=arguments["quote_id"], token=bearer_token,
            )
        elif name == "get_instagram_collection":
            result = await apify_collections.get_collection(
                **scope, collection_id=arguments["collection_id"],
                token=bearer_token, include_items=True,
            )
        else:
            result = await apify_collections.cancel_collection(
                **scope, collection_id=arguments["collection_id"], token=bearer_token,
            )
        return _result(result)
    except apify_collections.ApifyCollectionError as exc:
        return _result({"code": exc.code, "message": str(exc)}, error=True)
    except ApifyError as exc:
        return _result({"code": exc.code, "message": exc.message}, error=True)
    except (ValueError, PermissionError) as exc:
        # Service validation messages are authored locally; provider exceptions
        # and raw response bodies must not escape the generic error boundary.
        return _result({"code": "collection_rejected", "message": str(exc)}, error=True)
    except Exception:  # noqa: BLE001
        logger.error("Apify collection operation could not complete: %s", name)
        return _result({
            "code": "collection_unavailable",
            "message": "Collection state could not be confirmed. Query the same collection again; do not start a new one.",
        }, error=True)
