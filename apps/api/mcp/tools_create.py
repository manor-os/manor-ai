"""Create a Workspace: blank, or from a description through Manor's Workspace Architect."""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

from apps.api.mcp.manor_api import ManorApiError
from apps.api.mcp.registry import Tool, ToolContext, ToolError, obj, require, string
from packages.core.models.base import generate_ulid

logger = logging.getLogger(__name__)

_WS = "/api/v1/workspaces"
_DRAFTS = "/api/v1/workspace-drafts"

_CREATE_FIELDS = ("description", "category", "kind", "primary_work", "operating_context")
_WORKSPACE_KEYS = ("id", "name", "description", "kind", "category", "status", "can_manage", "created_at")
_DRAFT_KEYS = ("status", "ready", "working", "build_progress", "missing", "suggested_blueprint",
               "applied_blueprint_id", "finalized_workspace_id", "updated_at")
_DEFAULT_WAIT_SECONDS = 30
_MAX_WAIT_SECONDS = 90

# An Architect turn takes minutes. It runs as the REST route in a task of its
# own that outlives the tool call; these references keep it alive after the
# call returns "working".
_TURNS: set[asyncio.Task] = set()


async def create_workspace(ctx: ToolContext, args: dict) -> Any:
    body = {"name": require(args, "name"), **{key: args[key] for key in _CREATE_FIELDS if args.get(key)}}
    workspace = await ctx.api.post(_WS, body)
    return {**{key: workspace.get(key) for key in _WORKSPACE_KEYS},
            "note": "The Workspace is empty. Configure it with the stage_workspace_* tools, "
                    "upsert_workflow and upsert_skill, then apply_workspace_change."}


def _latest_reply(draft: dict) -> dict:
    """The Architect's answer to the latest message; none while that is the user's."""
    messages = draft.get("messages") or []
    return messages[-1] if messages and messages[-1].get("role") == "assistant" else {}


def _question(draft: dict) -> Any:
    """The Architect's open question rides on its latest reply as answer choices."""
    return _latest_reply(draft).get("question")


def _reply(draft: dict) -> Any:
    return _latest_reply(draft).get("content")


def _next_step(draft_id: str, draft: dict) -> str:
    if draft.get("working"):
        designing = [part for part, state in (draft.get("build_progress") or {}).items()
                     if state not in {"accepted", "skipped"}]
        return (f"The Architect is still working{' (designing ' + ', '.join(designing) + ')' if designing else ''}. "
                f"Call get_workspace_draft with draft_id={draft_id} until working is false to read its reply.")
    if draft.get("status") == "finalized":
        return f"Created Workspace {draft.get('finalized_workspace_id')}; read it with get_workspace."
    messages = draft.get("messages") or []
    if messages and messages[-1].get("role") == "user":
        return ("The Architect stopped before answering the latest message. To go on, send a message "
                f"with continue_workspace_draft (draft_id={draft_id}); it sees the conversation so far.")
    if draft.get("ready"):
        return (f"The draft is ready. Review it with get_workspace_draft, then call "
                f"finalize_workspace_draft with draft_id={draft_id} to create the Workspace.")
    return ("Answer the Architect (question.options are suggested answers) or ask for changes "
            "with continue_workspace_draft.")


def _turn(result: dict) -> dict:
    draft = result.get("draft") or {}
    return {"draft_id": draft.get("id"), "reply": result.get("reply"), "question": _question(draft),
            **{key: draft.get(key) for key in _DRAFT_KEYS}, "next_step": _next_step(draft.get("id"), draft)}


def _document_ids(args: dict) -> dict:
    return {"document_ids": args["document_ids"]} if args.get("document_ids") else {}


def _turn_done(task: asyncio.Task) -> None:
    _TURNS.discard(task)
    error = None if task.cancelled() else task.exception()
    # A busy draft or a refused message is the caller's to read; log what is not.
    if error is not None and not (isinstance(error, ManorApiError) and error.status_code < 500):
        logger.warning("Workspace draft turn from MCP failed: %r", error)


async def _run_turn(draft_id: str, call: Callable[[], Awaitable[Any]], args: dict) -> dict:
    """Run the turn's REST call in its own task; wait up to wait_seconds for it."""
    wait = min(max(float(args.get("wait_seconds", _DEFAULT_WAIT_SECONDS) or 0), 0), _MAX_WAIT_SECONDS)
    task = asyncio.get_running_loop().create_task(call())
    _TURNS.add(task)
    task.add_done_callback(_turn_done)
    done, _ = await asyncio.wait({task}, timeout=wait)
    if not done:
        return {"draft_id": draft_id, "status": "working", "working": True,
                "next_step": _next_step(draft_id, {"working": True})}
    try:
        return _turn(task.result())
    except ManorApiError as exc:
        if exc.status_code == 409 and isinstance(exc.detail, dict) and exc.detail.get("code") == "draft_busy":
            raise ToolError(
                f"The Architect is still working on draft {draft_id}, so this message was not sent. "
                f"Call get_workspace_draft with draft_id={draft_id} until working is false, then send "
                "it again."
            ) from exc
        raise


async def start_workspace_draft(ctx: ToolContext, args: dict) -> Any:
    # Choosing the id up front lets a turn that outlasts the wait be read back.
    draft_id = generate_ulid()
    body = {"initial_brief": require(args, "brief"), "draft_id": draft_id, **_document_ids(args)}
    return await _run_turn(draft_id, lambda: ctx.api.post(_DRAFTS, body), args)


async def continue_workspace_draft(ctx: ToolContext, args: dict) -> Any:
    draft_id = require(args, "draft_id")
    body = {"message": require(args, "message"), **_document_ids(args)}
    return await _run_turn(draft_id, lambda: ctx.api.post(f"{_DRAFTS}/{draft_id}/messages", body), args)


async def get_workspace_draft(ctx: ToolContext, args: dict) -> Any:
    draft_id = args.get("draft_id")
    if draft_id:
        draft = await ctx.api.get(f"{_DRAFTS}/{draft_id}") or {}
        return {**draft, "draft_id": draft_id, "reply": _reply(draft), "question": _question(draft),
                "next_step": _next_step(draft_id, draft)}
    drafts = await ctx.api.get(_DRAFTS, status=args.get("status"))
    return [{"draft_id": draft.get("id"), "name": (draft.get("fields") or {}).get("name"),
             **{key: draft.get(key) for key in _DRAFT_KEYS}} for draft in drafts or []]


async def list_workspace_recommendations(ctx: ToolContext, args: dict) -> Any:
    return await ctx.api.get(f"{_DRAFTS}/recommendations")


async def update_workspace_draft_fields(ctx: ToolContext, args: dict) -> Any:
    fields = require(args, "fields")
    if not isinstance(fields, dict):
        raise ToolError("'fields' must be an object of draft fields")
    draft = await ctx.api.patch(f"{_DRAFTS}/{require(args, 'draft_id')}/fields", fields)
    return {"draft_id": draft.get("id"), "fields": draft.get("fields"),
            **{key: draft.get(key) for key in _DRAFT_KEYS}}


async def apply_blueprint_to_workspace_draft(ctx: ToolContext, args: dict) -> Any:
    draft = await ctx.api.post(f"{_DRAFTS}/{require(args, 'draft_id')}/apply-blueprint",
                               {"blueprint_id": require(args, "blueprint_id")})
    return {"draft_id": draft.get("id"), "name": (draft.get("fields") or {}).get("name"),
            **{key: draft.get(key) for key in _DRAFT_KEYS}}


async def finalize_workspace_draft(ctx: ToolContext, args: dict) -> Any:
    result = await ctx.api.post(f"{_DRAFTS}/{require(args, 'draft_id')}/finalize")
    return {"workspace_id": result.get("workspace_id"),
            "note": "Read it with get_workspace; refine it with the stage_workspace_* tools."}


_DRAFT_ID = string("Workspace draft id (from start_workspace_draft or get_workspace_draft).")
_DOCUMENT_IDS = {"type": "array", "items": {"type": "string"}, "maxItems": 20,
                 "description": "Knowledge document ids the Architect should read (e.g. the customer's brief)."}
_WAIT_SECONDS = {"type": "number", "minimum": 0, "maximum": _MAX_WAIT_SECONDS, "default": _DEFAULT_WAIT_SECONDS,
                 "description": "How long to wait for the Architect's reply (0 = return at once)."}

TOOLS = [
    Tool("create_workspace", "Create Workspace",
         "Create an empty Workspace with a name and description; you then configure everything "
         "yourself. To have Manor's Workspace Architect design it from a description instead, use "
         "start_workspace_draft. Counts against the team's Workspace limit.",
         create_workspace,
         properties={"name": string("Workspace name."),
                     "description": string("What the Workspace is for."),
                     "category": string("Category, e.g. marketing, support."),
                     "kind": string("Kind of operating unit, e.g. business, team, project."),
                     "primary_work": string("The main work it does."),
                     "operating_context": string("Who it serves and where.")},
         required=("name",)),
    Tool("start_workspace_draft", "Start Workspace draft",
         "Describe the Workspace you want; Manor's Workspace Architect drafts its services, agents, "
         "workflows, goals and rules and replies, often with a question (question.options are the "
         "suggested answers). Answer with continue_workspace_draft until ready is true, then "
         "finalize_workspace_draft creates it. A turn often takes a few minutes, a larger Workspace "
         "longer while its parts are designed in parallel: this waits up to wait_seconds, then returns "
         "status working with the draft_id while the turn goes on; read the reply with "
         "get_workspace_draft once working is false (build_progress shows each part meanwhile). "
         "next_step says what to do next. Nothing is created until finalize.",
         start_workspace_draft,
         properties={"brief": string("What the Workspace should do, for whom, in the customer's words."),
                     "document_ids": _DOCUMENT_IDS, "wait_seconds": _WAIT_SECONDS},
         required=("brief",)),
    Tool("continue_workspace_draft", "Continue Workspace draft",
         "Send the next message to the Workspace Architect (an answer to its question or a change) and "
         "read its reply, readiness (ready, missing) and any new question. Waits up to wait_seconds; "
         "status working means the turn goes on: poll get_workspace_draft until working is false "
         "(build_progress shows each part being designed: pending, candidate, accepted, skipped). "
         "While a turn is running the draft is busy and a new message is refused, not queued.",
         continue_workspace_draft,
         properties={"draft_id": _DRAFT_ID, "message": string("Your reply to the Architect."),
                     "document_ids": _DOCUMENT_IDS, "wait_seconds": _WAIT_SECONDS},
         required=("draft_id", "message")),
    Tool("list_workspace_recommendations", "List recommended Workspaces",
         "Workspaces worth creating for the signed-in user, from their memories, Workspaces, "
         "connected integrations and public information about their company. Each item has a "
         "blueprint_id (null when no Blueprint fits), title, reason and a brief to pass to "
         "start_workspace_draft. status pending means the first result is still being prepared.",
         list_workspace_recommendations,
         read_only=True, idempotent=True),
    Tool("get_workspace_draft", "Get Workspace draft",
         "One draft with its full proposed configuration (fields), conversation, the Architect's "
         "reply to the latest message and its question (none when it stopped before answering; "
         "next_step says how to go on), readiness, working (a turn is still running), build_progress "
         "(each part of a larger build: pending, candidate, accepted, skipped, lead) and next_step; "
         "or, without draft_id, your drafts.",
         get_workspace_draft,
         properties={"draft_id": _DRAFT_ID,
                     "status": {"enum": ["active", "ready", "finalized", "abandoned"],
                                "description": "Without draft_id: only drafts in this status."}},
         read_only=True, idempotent=True),
    Tool("update_workspace_draft_fields", "Update Workspace draft fields",
         "Set top-level fields of a draft directly (e.g. name, heartbeat_enabled, "
         "knowledge_attachments approvals) as the setup screen does; only the keys you pass change. "
         "Returns the updated fields and readiness.",
         update_workspace_draft_fields,
         properties={"draft_id": _DRAFT_ID,
                     "fields": obj("Field values by name, as returned in get_workspace_draft fields.")},
         required=("draft_id", "fields"), idempotent=True),
    Tool("apply_blueprint_to_workspace_draft", "Start Workspace draft from Blueprint",
         "Fill a draft from a published Blueprint: its services, agents, workflows, goals, rules, App "
         "and knowledge replace what the draft has. Use the draft's suggested_blueprint once the user "
         "chose to install it or customize from it; then continue_workspace_draft for any changes and "
         "finalize_workspace_draft. A paid Blueprint needs a completed purchase.",
         apply_blueprint_to_workspace_draft,
         properties={"draft_id": _DRAFT_ID,
                     "blueprint_id": string("Published Blueprint id, e.g. suggested_blueprint.id.")},
         required=("draft_id", "blueprint_id"), destructive=True),
    Tool("finalize_workspace_draft", "Create Workspace from draft",
         "Create the Workspace a ready draft describes: its services, agents, workflows, Knowledge "
         "and schedules. If the draft turns on autonomous mode, the Workspace starts working right "
         "away. Counts against the team's Workspace limit.",
         finalize_workspace_draft,
         properties={"draft_id": _DRAFT_ID},
         required=("draft_id",), open_world=True),
]
