"""Load one Workspace App section's data on demand.

The section is looked up in the saved page, never taken from the request, so
the client cannot widen what a section reads. The App page itself stays cheap
to open: each block fetches its own data after the page renders.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from packages.core.services.viewer_language import localize_run_names, localized, run_names_for

_QUEUE_LIMIT = 20
_FILES_PER_RUN = 6
_MAX_STAGES = 24
# A run in one of these has stopped; nothing more will change unless it is started again.
_ENDED_RUN_STATUSES = frozenset({"completed", "failed", "cancelled"})
# On a chat request sent from an App entry: the entry's key, so the App lists
# the request and its reply under that entry as it lists a Flow's runs.
WORKSPACE_APP_ACTION_META_KEY = "workspace_app_action"


class AppSectionNotFound(LookupError):
    """No data-backed section with this id on the Workspace's App page."""


async def load_workspace_app_section(
    db: Any, workspace: Any, *, user: Any, section_id: str, locale: str | None = None,
) -> dict[str, Any]:
    """One App section's rows; Flow and entry names read in the viewer's ``locale``."""
    from packages.core.services.workspace_app_view import APP_SECTION_BLOCKS, app_view_sections, normalize_app_view_config

    model = workspace.operating_model if isinstance(workspace.operating_model, dict) else {}
    try:
        config = normalize_app_view_config(model.get("app_view"))
    except ValueError:
        config = normalize_app_view_config(None)
    section = next((item for item in app_view_sections(config) if item["id"] == section_id), None)
    if section is None or section["block"] == "actions":
        raise AppSectionNotFound(section_id)
    if section["block"] == "connections":
        items = await _connection_items(db, workspace, user=user)
    elif section["block"] == "queue":
        items = await _queue_items(db, workspace, user=user)
    elif section["block"] == "metrics":
        # The block shows ``limit`` at once and can expand to the rest.
        items = await _metric_items(db, workspace, user=user, limit=APP_SECTION_BLOCKS["metrics"]["limit"][1],
                                    refs=section.get("items"))
        # A section that names its numbers waits for them; an open one offers some.
        suggestions = [] if items or section.get("items") else await _metric_suggestions(db, workspace, user=user)
        return {"id": section["id"], "block": section["block"], "items": items, "suggestions": suggestions}
    else:
        items = await _output_items(db, workspace, user=user, section=section, locale=locale)
    return {"id": section["id"], "block": section["block"], "items": items}


async def _metric_suggestions(db: Any, workspace: Any, *, user: Any) -> list[dict[str, Any]]:
    """Audience metrics worth offering when a tracking block has nothing yet.

    A metrics block is on the page only when its config asks for one.
    """
    from sqlalchemy import select

    from packages.core.models.workspace_stat import WorkspaceStat
    from packages.core.stats.assessment_templates import list_assessment_templates

    taken = set((await db.execute(select(WorkspaceStat.key).where(
        WorkspaceStat.entity_id == user.entity_id, WorkspaceStat.workspace_id == workspace.id,
    ))).scalars())
    return [template for template in list_assessment_templates(getattr(user, "locale", None)) if template["key"] not in taken]


async def _connection_items(db: Any, workspace: Any, *, user: Any) -> list[dict[str, Any]]:
    """What the Workspace's Blueprint needs connected and is not yet, required first."""
    from packages.core.services.workspace_connection_status import WorkspaceConnectionStatusFactory

    status = await WorkspaceConnectionStatusFactory.create(db, workspace=workspace, user_id=user.id)
    missing = [item for item in status["requirements"] if not item["ready"]]
    return sorted(missing, key=lambda item: not item["required"])


async def _queue_items(db: Any, workspace: Any, *, user: Any) -> list[dict[str, Any]]:
    """Pending decisions: with a chat card, deciding stays on it; a run with none opens at its page."""
    from packages.core.workspace_chat.attention import list_workspace_attention

    # Every open decision: a read receipt never hides one from the App.
    reminders = await list_workspace_attention(db, user=user, workspace_id=workspace.id, unread_only=False)
    # A connect request is the connections block's row already; list it once.
    items = [item for item in reminders["items"] if not item.get("connection_provider")][:_QUEUE_LIMIT]
    await _attach_review_glimpses(db, items)
    return [*items, *await _runs_waiting_without_a_card(db, workspace)][:_QUEUE_LIMIT]


def _waiting_run_summary(run: Any, action: dict[str, Any]) -> str:
    """What the person is asked, named with its song when the run has one: "Made to Spark · Review the storyboard".

    Without a song the step's own question ("Approve the plan?") says more than its name ("Review").
    """
    what = " ".join(str(action.get("review_title") or action.get("title") or "").split())
    song = str((run.variables or {}).get("song_title") or "").strip() if isinstance(run.variables, dict) else ""
    if song and what:
        return f"{song} · {what}"[:240]
    return " ".join(str(action.get("prompt") or what or song).split())[:240]


async def _runs_waiting_without_a_card(db: Any, workspace: Any) -> list[dict[str, Any]]:
    """Runs stopped on a person's decision that were started outside any chat (the API, MCP)."""
    from sqlalchemy import select

    from packages.core.models.workflow import WorkflowRun
    from packages.core.services.workflow_chat_projection import workflow_chat_context
    from packages.core.services.workflow_run_control import run_resume_action

    # A run whose chat holds its card is already listed; only the others load
    # in full (their frozen step graph names the decision).
    paused = (await db.execute(select(
        WorkflowRun.id, WorkflowRun.trigger_data, WorkflowRun.trigger_source,
    ).where(
        WorkflowRun.entity_id == workspace.entity_id, WorkflowRun.workspace_id == workspace.id,
        WorkflowRun.status == "paused",
    ).order_by(WorkflowRun.started_at.desc()).limit(_QUEUE_LIMIT))).all()
    run_ids = [
        run_id for run_id, trigger_data, trigger_source in paused
        if workflow_chat_context(SimpleNamespace(trigger_data=trigger_data, trigger_source=trigger_source)) is None
    ]
    if not run_ids:
        return []
    loaded = {run.id: run for run in (await db.execute(
        select(WorkflowRun).where(WorkflowRun.id.in_(run_ids))
    )).scalars().all()}
    items = []
    for run in (loaded[run_id] for run_id in run_ids if run_id in loaded):
        action = run_resume_action(run)
        if (action or {}).get("kind") not in {"workflow_approval", "workflow_input"}:
            continue
        items.append({
            "key": f"workflow_run:{run.id}:{run.current_step_id}", "message_id": "", "source_id": run.id,
            "kind": action["kind"], "summary": _waiting_run_summary(run, action),
            "created_at": run.started_at, "connection_provider": None,
            "workflow_run": {"workflow_id": run.workflow_id, "run_id": run.id},
        })
    return items


_REVISION_CHOICES = {"revise", "request_changes", "changes_requested", "cancel", "reject"}
_PREVIEW_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".mp4", ".mov", ".webm")


async def _attach_review_glimpses(db: Any, items: list[dict[str, Any]]) -> None:
    """Give a workflow review its primary choice and a few deliverables to show.

    The App can then accept in place or show what is waiting; any change
    request still opens the full review.
    """
    from sqlalchemy import select

    from packages.core.models.task import Message
    from packages.core.models.workflow import WorkflowRun
    from packages.core.services.workflow_review_files import REVIEW_FILE_KEYS

    await _attach_question_glimpses(db, items)
    ids = [item["message_id"] for item in items if item.get("kind") == "workflow_approval"]
    if not ids:
        return
    actions = dict((await db.execute(select(Message.id, Message.pending_action).where(Message.id.in_(ids)))).all())
    run_ids = {action.get("workflow_run_id") for action in actions.values()
               if isinstance(action, dict) and action.get("review") is None and action.get("workflow_run_id")}
    runs = dict((await db.execute(select(WorkflowRun.id, WorkflowRun.step_results)
                                  .where(WorkflowRun.id.in_(run_ids)))).all()) if run_ids else {}
    for item in items:
        action = actions.get(item["message_id"])
        if item.get("kind") != "workflow_approval" or not isinstance(action, dict):
            continue
        review = action.get("review")
        if review is None:
            result = (runs.get(action.get("workflow_run_id")) or {}).get(str(action.get("step_id") or "")) or {}
            review = result.get("review") if isinstance(result, dict) else None
        if not isinstance(review, dict):
            continue
        options = [str(option) for option in action.get("options") or []]
        primary = next((option for option in options if option.lower() not in _REVISION_CHOICES), None)
        if primary:
            item["primary_option"] = primary
            item["has_changes_option"] = any(option.lower() in _REVISION_CHOICES - {"cancel", "reject"}
                                             for option in options)
        previews = []
        for entry in [item for key in REVIEW_FILE_KEYS for item in (review.get(key) if isinstance(review.get(key), list) else [])]:
            if not isinstance(entry, dict) or not entry.get("document_id"):
                continue
            name = str(entry.get("name") or entry.get("fs_path") or "").lower()
            if name.endswith(_PREVIEW_SUFFIXES) and entry["document_id"] not in {p["document_id"] for p in previews}:
                previews.append({"document_id": entry["document_id"], "name": entry.get("name") or name})
        item["previews"] = previews[:3]


async def _attach_question_glimpses(db: Any, items: list[dict[str, Any]]) -> None:
    """An Agent asking for a file review shows a few of the files it asks about.

    Answering stays on its chat card, which carries the options and the note.
    """
    from sqlalchemy import select

    from packages.core.models.hitl_request import HitlRequest

    asked = {item["source_id"]: item for item in items if item.get("kind") == "runtime_input" and item.get("source_id")}
    if not asked:
        return
    for request_id, context in (await db.execute(select(HitlRequest.id, HitlRequest.context).where(
        HitlRequest.id.in_(list(asked)),
    ))).all():
        question = (context if isinstance(context, dict) else {}).get("question") or {}
        previews = [
            {"document_id": entry["document_id"], "name": entry.get("name") or ""}
            for entry in question.get("review_files") or []
            if isinstance(entry, dict) and entry.get("document_id")
            and str(entry.get("name") or entry.get("fs_path") or "").lower().endswith(_PREVIEW_SUFFIXES)
        ]
        if previews:
            asked[request_id]["previews"] = previews[:3]


def _number(value: Any) -> float | None:
    return float(value) if value is not None else None


async def _metric_items(
    db: Any, workspace: Any, *, user: Any, limit: int, refs: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Tracked stats with their recent values, plus goals that have no stat.

    A section that names its numbers (``stat:<key>``, ``goal:<key>``) shows
    exactly those, in its order. Otherwise stats follow the viewer's own Stats
    order and hidden list. A goal fed by a shown stat is that tile's target.
    """
    from sqlalchemy import func, select

    from packages.core.models.goal import Goal, GoalMeasurement
    from packages.core.models.workspace_stat import WorkspaceStat, WorkspaceStatObservation
    from packages.core.stats.service import (
        STAT_QUICK_VIEW_PREFERENCE_KEY,
        attach_assessment_runtime,
        stat_freshness_status,
    )

    stats = list((await db.execute(select(WorkspaceStat).where(
        WorkspaceStat.entity_id == user.entity_id, WorkspaceStat.workspace_id == workspace.id,
        WorkspaceStat.status == "active",
    ).order_by(WorkspaceStat.created_at.asc(), WorkspaceStat.key.asc()))).scalars().all())
    if refs is not None:
        stats = [stat for stat in stats if f"stat:{stat.key}" in refs]
    else:
        views = (getattr(user, "preferences", None) or {}).get(STAT_QUICK_VIEW_PREFERENCE_KEY)
        view = views.get(workspace.id) if isinstance(views, dict) else None
        view = view if isinstance(view, dict) else {}
        hidden = {str(item) for item in view.get("hidden_stat_ids") or []}
        order = {str(stat_id): index for index, stat_id in enumerate(view.get("ordered_stat_ids") or [])}
        stats = [stat for stat in stats if stat.id not in hidden]
        stats.sort(key=lambda stat: order.get(stat.id, len(order)))

    goals = list((await db.execute(select(Goal).where(
        Goal.entity_id == user.entity_id, Goal.workspace_id == workspace.id,
        Goal.status.in_(("active", "achieved")),
    ).order_by(Goal.priority.asc(), Goal.created_at.asc()))).scalars().all())
    goals_by_stat: dict[str, Any] = {}
    for goal in goals:  # The highest-priority goal on a stat is its target.
        if goal.stat_id:
            goals_by_stat.setdefault(goal.stat_id, goal)
    stat_ids = {stat.id for stat in stats}
    standalone = [goal for goal in goals if goal.stat_id not in stat_ids
                  and (refs is None or f"goal:{goal.goal_key}" in refs)]

    def goal_payload(goal: Goal | None) -> dict[str, Any] | None:
        if goal is None:
            return None
        return {"id": goal.id, "title": goal.title, "target": _number(goal.target_value),
                "baseline": _number(goal.baseline_value), "deadline": goal.deadline,
                "pace_status": goal.pace_status, "status": goal.status}

    def gaps(stat: WorkspaceStat) -> list[str]:
        evidence = (stat.last_assessment or {}).get("evidence") if isinstance(stat.last_assessment, dict) else None
        found = evidence.get("gaps") if isinstance(evidence, dict) else None
        return [str(gap) for gap in found[:3]] if isinstance(found, list) else []

    items: list[dict[str, Any]] = [{
        "ref": f"stat:{stat.key}", "kind": "stat", "id": stat.id, "title": stat.name, "value_type": stat.value_type, "unit": stat.unit,
        "value": _number(stat.current_value), "updated_at": stat.current_value_updated_at,
        "freshness_status": stat_freshness_status(stat), "goal": goal_payload(goals_by_stat.get(stat.id)),
        "collector_type": stat.collector_type, "collector_config": stat.collector_config or {},
        "status": stat.status, "gaps": gaps(stat),
        # What the number means and over which window: the Stat's own definition, shown as the tile's note.
        "description": stat.description or None, "window": stat.window,
    } for stat in stats] + [{
        "ref": f"goal:{goal.goal_key}", "kind": "goal", "id": goal.id, "title": goal.title, "value_type": "number", "unit": None,
        "value": _number(goal.current_value), "updated_at": goal.current_value_updated_at,
        "freshness_status": "fresh" if goal.current_value is not None else "no_data", "goal": goal_payload(goal),
        "collector_type": None, "collector_config": {}, "status": goal.status, "gaps": [],
        "description": goal.description or None, "window": None,
    } for goal in standalone]
    if refs is not None:
        position = {ref: index for index, ref in enumerate(refs)}
        items.sort(key=lambda item: position[item["ref"]])
    for item in items:
        item.pop("ref")
    items = items[:limit]
    # Agent-reviewed stats take their phase (assessing, error, paused) from the
    # assessment Workflow, exactly as the Stats page shows them.
    await attach_assessment_runtime(db, items, user.entity_id, workspace.id)
    for item in items:
        runtime = item.pop("assessment_runtime", None)
        item.pop("collector_config")
        item.pop("status")
        runtime = runtime or {}
        # Enough to open the latest assessment run or start one from the App.
        item["assessment"] = {
            "next_run_at": runtime.get("next_run_at"), "job_id": runtime.get("job_id"),
            "workflow_id": runtime.get("workflow_id"), "run_id": runtime.get("run_id"),
            "run_status": runtime.get("run_status"),
        } if runtime else None
        item["next_assessment_at"] = runtime.get("next_run_at")

    # The last 12 known values per tile, oldest first, for its trend line.
    async def recent(model: Any, key: Any, at: Any, ids: list[str]) -> dict[str, list[dict[str, Any]]]:
        if not ids:
            return {}
        ranked = select(key.label("owner"), at.label("at"), model.value.label("value"), func.row_number().over(
            partition_by=key, order_by=at.desc(),
        ).label("rank")).where(key.in_(ids), model.value.is_not(None)).subquery()
        rows = (await db.execute(select(ranked).where(ranked.c.rank <= 12).order_by(ranked.c.owner, ranked.c.at))).all()
        series: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            series.setdefault(row.owner, []).append({"at": row.at, "value": float(row.value)})
        return series

    stat_series = await recent(
        WorkspaceStatObservation, WorkspaceStatObservation.stat_id, WorkspaceStatObservation.observed_at,
        [item["id"] for item in items if item["kind"] == "stat"],
    )
    goal_series = await recent(
        GoalMeasurement, GoalMeasurement.goal_id, GoalMeasurement.measured_at,
        [item["id"] for item in items if item["kind"] == "goal"],
    )
    for item in items:
        item["series"] = (stat_series if item["kind"] == "stat" else goal_series).get(item["id"], [])
    return items


async def _output_items(
    db: Any, workspace: Any, *, user: Any, section: dict[str, Any], locale: str | None = None,
) -> list[dict[str, Any]]:
    """Recent work started in this Workspace, newest first: Flow runs and requests sent from App entries."""
    source = section["source"] or "workspace"
    limit = section["limit"] or 8
    items = await _flow_output_items(db, workspace, user=user, section=section, locale=locale)
    # A section scoped to one Flow lists that Flow only.
    if source == "workspace":
        items += await _entry_request_items(db, workspace, user=user, limit=limit, locale=locale)
        items.sort(key=lambda item: (item["created_at"], item["run_id"]), reverse=True)
    return items[:limit]


async def _readable_files(db: Any, workspace: Any, *, user: Any, document_ids: set[str]) -> dict[str, dict[str, Any]]:
    """FileRefs of the active Documents the viewer can open, by Document id.

    Work files (an edit recipe, a QA frame) are left out: an output lists what
    was made, not how.
    """
    from sqlalchemy import select

    from packages.core.models.document import Document
    from packages.core.services.document_access import unreadable_document_paths
    from packages.core.services.file_refs import file_ref
    from packages.core.services.generated_artifact_service import is_work_artifact
    from packages.core.services.generated_file_refs import canonical_fs_path
    from packages.core.services.knowledge_visibility import normalize_rel_path

    if not document_ids:
        return {}
    rows = [row for row in (await db.execute(select(
        Document.id, Document.name, Document.fs_path, Document.mime_type, Document.metadata_,
    ).where(
        Document.entity_id == user.entity_id, Document.id.in_(document_ids),
        Document.is_trashed == False,  # noqa: E712
        Document.fs_path.is_not(None),
    ))).all() if not is_work_artifact(row.metadata_)]
    paths = {row.id: canonical_fs_path(row.fs_path) for row in rows}
    unreadable = await unreadable_document_paths(
        db, entity_id=user.entity_id, rel_paths=[path for path in paths.values() if path],
        user_id=user.id, role=getattr(user, "role", None), workspace_id=workspace.id,
    )
    return {
        row.id: file_ref(
            entity_id=user.entity_id, fs_path=paths[row.id], document_id=row.id,
            name=row.name, mime=row.mime_type,
        )
        for row in rows
        if paths[row.id] and normalize_rel_path(paths[row.id]) not in unreadable
    }


def _entry_titles(workspace: Any, locale: str | None = None) -> dict[str, str]:
    """What the App calls each entry: its label, its own title, or its Service's name."""
    from packages.core.services.workspace_app_view import normalize_app_view_config

    model = workspace.operating_model if isinstance(workspace.operating_model, dict) else {}
    try:
        config = normalize_app_view_config(model.get("app_view"))
    except ValueError:
        config = normalize_app_view_config(None)
    config = localized(config, locale)
    titles = {
        f"service:{service.get('key') or service.get('service_key')}": str(service.get("name") or "")
        for service in model.get("services") or [] if isinstance(service, dict)
    }
    titles.update({key: entry["title"] for key, entry in config["entries"].items()})
    titles.update({key: label for key, label in config["labels"].items() if label})
    return titles


def _reply_document_ids(text: str, *, entity_id: str) -> tuple[list[str], list[str]]:
    """The Documents and entity paths a delivered reply links, in reading order."""
    from packages.core.services.file_refs import reply_file_references

    ids: list[str] = []
    paths: list[str] = []
    for ref in reply_file_references(text, entity_id=entity_id):
        if ref.document_id:
            ids.append(ref.document_id)
        elif ref.fs_path and ref.entity_id in {"", entity_id} and ref.fs_path not in paths:
            paths.append(ref.fs_path)
    return ids, paths


async def _entry_request_items(
    db: Any, workspace: Any, *, user: Any, limit: int, locale: str | None = None,
) -> list[dict[str, Any]]:
    """Requests sent from App entries to an Agent, with the reply's state and files.

    Such a request is a chat turn, not a Flow run: its state is the Agent run
    answering it, waiting while it asks the person something, and its files
    are the ones the reply links, or the ones under review while it waits.
    """
    from sqlalchemy import select

    from packages.core.models.document import Document
    from packages.core.models.hitl_request import HitlRequest
    from packages.core.models.runtime_run import RuntimeRun, RuntimeRunStatus
    from packages.core.models.task import Conversation, Message
    from packages.core.services.runtime_human_input import is_awaiting_human_input

    key = Message.meta[WORKSPACE_APP_ACTION_META_KEY].as_string()
    requests = (await db.execute(select(Message.id, Message.created_at, key.label("app_action")).join(
        Conversation, Conversation.id == Message.conversation_id,
    ).where(
        Conversation.entity_id == user.entity_id, Conversation.workspace_id == workspace.id,
        Message.role == "user", key.is_not(None),
    ).order_by(Message.created_at.desc(), Message.id.desc()).limit(limit))).all()
    if not requests:
        return []
    origin = Message.meta["origin_user_message_id"].as_string()
    replies = {row.origin: row for row in (await db.execute(select(
        Message.id, Message.content, origin.label("origin"),
    ).where(Message.role == "assistant", origin.in_([row.id for row in requests])))).all()}
    runs = {run.assistant_message_id: run for run in (await db.execute(select(RuntimeRun).where(
        RuntimeRun.entity_id == user.entity_id,
        RuntimeRun.assistant_message_id.in_([reply.id for reply in replies.values()]),
    ))).scalars()} if replies else {}
    asking = {run.human_input_request_id for run in runs.values() if is_awaiting_human_input(run)}
    questions = {request.id: request for request in (await db.execute(select(HitlRequest).where(
        HitlRequest.entity_id == user.entity_id, HitlRequest.id.in_(asking),
    ))).scalars()} if asking else {}

    linked: dict[str, list[str]] = {}
    linked_paths: dict[str, list[str]] = {}
    for row in requests:
        reply = replies.get(row.id)
        run = runs.get(reply.id) if reply else None
        question = questions.get(run.human_input_request_id) if run is not None else None
        if question is not None:
            # While it waits, what it shows is what it asked the person to review.
            context = question.context if isinstance(question.context, dict) else {}
            asked = (context.get("question") or {}).get("review_files") or []
            linked[row.id] = [str(item.get("document_id")) for item in asked if isinstance(item, dict) and item.get("document_id")]
            linked_paths[row.id] = []
        else:
            linked[row.id], linked_paths[row.id] = _reply_document_ids(reply.content if reply else "", entity_id=user.entity_id)
    wanted_paths = {path for paths in linked_paths.values() for path in paths}
    by_path = dict((await db.execute(select(Document.fs_path, Document.id).where(
        Document.entity_id == user.entity_id, Document.fs_path.in_(wanted_paths),
        Document.is_trashed == False,  # noqa: E712
    ))).all()) if wanted_paths else {}
    for request_id, paths in linked_paths.items():
        linked[request_id] += [by_path[path] for path in paths if path in by_path and by_path[path] not in linked[request_id]]
    files = await _readable_files(db, workspace, user=user, document_ids={i for ids in linked.values() for i in ids})

    titles = _entry_titles(workspace, locale)
    items = []
    for row in requests:
        reply = replies.get(row.id)
        run = runs.get(reply.id) if reply else None
        if run is None:
            status = "completed" if reply is not None and (reply.content or "").strip() else "running"
        elif run.status in RuntimeRunStatus.terminal():
            status = run.status
        else:
            status = "paused" if is_awaiting_human_input(run) else "running"
        run_files = [files[document_id] for document_id in linked[row.id] if document_id in files]
        items.append({
            "kind": "chat", "run_id": run.id if run is not None else row.id,
            # The chat message to open: the reply, or the request until one exists.
            "message_id": reply.id if reply is not None else row.id,
            "workflow_id": "", "binding_id": None, "app_action": row.app_action,
            "title": titles.get(row.app_action, ""), "status": status, "business_outcome": None,
            "result_title": None, "result_summary": None,
            "trigger_source": "app", "created_at": row.created_at,
            "started_at": row.created_at, "completed_at": getattr(run, "completed_at", None),
            "files": run_files[:_FILES_PER_RUN], "file_count": len(run_files),
            "progress": None, "publications": [],
        })
    return items


async def _flow_output_items(
    db: Any, workspace: Any, *, user: Any, section: dict[str, Any], locale: str | None = None,
) -> list[dict[str, Any]]:
    """Recent top-level runs with the files each one delivered.

    Only the final ``__result.files`` is read, as a JSONB path, so listing
    does not load run variables; files the viewer cannot open are dropped.
    """
    from sqlalchemy import select

    from packages.core.models.document import Document
    from packages.core.models.workflow import WorkflowRun
    from packages.core.services import workflow_service
    from packages.core.services.generated_file_refs import canonical_document_id

    from packages.core.models.workflow import WorkflowBinding, WorkflowTemplateInstallation
    from packages.core.stats.review_setup import REVIEW_WORKFLOW_SLUG

    source = section["source"] or "workspace"
    limit = section["limit"] or 8
    binding_id = source.removeprefix("workflow:") if source.startswith("workflow:") else None
    # A Flow's runs include the ones started by hand without its binding (from chat,
    # the Flow page or a builder tool), as the App entry's own history does.
    workflow_id = (await db.execute(select(WorkflowBinding.workflow_id).where(
        WorkflowBinding.id == binding_id, WorkflowBinding.entity_id == user.entity_id,
    ))).scalar_one_or_none() if binding_id else None
    # Over-fetch so dropping hidden runs still fills the section.
    runs = await workflow_service.list_runs(
        db, user.entity_id, workspace_id=workspace.id, binding_id=binding_id,
        workflow_id=workflow_id, include_unbound=workflow_id is not None,
        limit=limit * 3, summary=True, top_level_only=True,
    )
    if not runs:
        return []
    # Blueprint-internal Workflows are hidden from the App's actions; their runs
    # are plumbing, not outputs, so the App never lists them either.
    internal = set((await db.execute(select(WorkflowTemplateInstallation.workflow_id).where(
        WorkflowTemplateInstallation.entity_id == user.entity_id,
        WorkflowTemplateInstallation.workflow_id.in_({run.workflow_id for run in runs}),
        WorkflowTemplateInstallation.source_type == "workspace_blueprint",
        WorkflowTemplateInstallation.installation_metadata["internal"].as_boolean().is_(True),
    ))).scalars())
    # Assessment runs land on the tracking tiles as readings, not as outputs.
    assessments = set((await db.execute(select(WorkflowBinding.id).where(
        WorkflowBinding.entity_id == user.entity_id, WorkflowBinding.workspace_id == workspace.id,
        WorkflowBinding.config["workspace_blueprint_workflow_slug"].as_string() == REVIEW_WORKFLOW_SLUG,
    ))).scalars())
    # A retried run is one result: runs come newest first, so its latest attempt stands for its earlier ones.
    latest, families = [], set()
    for run in runs:
        family = run.lineage_root_run_id or run.id
        if run.workflow_id in internal or run.binding_id in assessments or family in families:
            continue
        families.add(family)
        latest.append(run)
    runs = latest[:limit]
    if not runs:
        return []
    def summary(run: Any) -> dict[str, Any]:
        return run.summary_history_state if isinstance(run.summary_history_state, dict) else {}

    # The App entry each run came from, so the App can reopen the run there.
    started_from = dict((await db.execute(select(
        WorkflowRun.id, WorkflowRun.trigger_data[workflow_service.WORKSPACE_APP_ACTION_KEY].as_string(),
    ).where(WorkflowRun.entity_id == user.entity_id, WorkflowRun.id.in_([run.id for run in runs])))).all())

    # Current summaries carry the delivered files and the result's headline;
    # older ones are completed here from the final result alone.
    from packages.core.services.workflow_run_trace import (
        HISTORY_SUMMARY_SCHEMA_VERSION,
        workflow_run_deliverables,
        workflow_run_result_headline,
    )

    legacy = [run.id for run in runs if int(summary(run).get("schema_version") or 0) < HISTORY_SUMMARY_SCHEMA_VERSION]
    headline = {run.id: summary(run) for run in runs if run.id not in legacy}
    delivered = {run.id: summary(run).get("deliverables") for run in runs if run.id not in legacy}
    if legacy:
        for run_id, result in (await db.execute(select(WorkflowRun.id, WorkflowRun.variables["__result"]).where(
            WorkflowRun.entity_id == user.entity_id, WorkflowRun.id.in_(legacy),
        ))).all():
            variables = {"__result": result} if result is not None else {}
            delivered[run_id] = workflow_run_deliverables(variables, entity_id=user.entity_id)
            headline[run_id] = workflow_run_result_headline(variables)
    # A delivered file named only by its path is matched to its Document.
    wanted_paths = {
        ref["fs_path"] for refs in delivered.values() for ref in refs or []
        if isinstance(ref, dict) and not ref.get("document_id") and ref.get("fs_path")
    }
    by_path = dict((await db.execute(select(Document.fs_path, Document.id).where(
        Document.entity_id == user.entity_id, Document.fs_path.in_(wanted_paths),
        Document.is_trashed == False,  # noqa: E712
    ))).all()) if wanted_paths else {}
    document_ids: dict[str, list[str]] = {}
    for run in runs:
        refs = delivered.get(run.id)
        ids: list[str] = []
        for ref in refs if isinstance(refs, list) else []:
            if not isinstance(ref, dict):
                continue
            document_id = canonical_document_id(ref.get("document_id")) or by_path.get(ref.get("fs_path"), "")
            if document_id and document_id not in ids:
                ids.append(document_id)
        document_ids[run.id] = ids

    files = await _readable_files(
        db, workspace, user=user, document_ids={document_id for ids in document_ids.values() for document_id in ids},
    )

    items = []
    for run in runs:
        run_files = [files[document_id] for document_id in document_ids[run.id] if document_id in files]
        names = run_names_for(run.summary_display_i18n, locale)
        history = localize_run_names(summary(run), names)
        outcome = history.get("business_outcome")
        stages = history.get("stages") if isinstance(history.get("stages"), list) else []
        items.append({
            "run_id": run.id, "workflow_id": run.workflow_id, "binding_id": run.binding_id,
            "app_action": started_from.get(run.id) or None,
            "title": names.get("name") or run.summary_workflow_name or "", "status": run.status,
            # What came out: the result's own title and line.
            "result_title": headline.get(run.id, {}).get("result_title") or None,
            "result_summary": headline.get(run.id, {}).get("result_summary") or None,
            "business_outcome": outcome if isinstance(outcome, str) and outcome else None,
            "trigger_source": run.trigger_source, "created_at": run.created_at,
            "started_at": run.started_at, "completed_at": run.completed_at,
            "files": run_files[:_FILES_PER_RUN], "file_count": len(run_files),
            # Stage progress and publications come straight from the persisted
            # run summary, the same projection Workflow History reads.
            "progress": {
                "stages": [{"name": stage.get("name"), "status": stage.get("status")} for stage in stages[:_MAX_STAGES]],
                "stage_count": len(stages),
                "done_count": sum(stage.get("status") in {"completed", "skipped"} for stage in stages),
                "current": history.get("current_stage") if isinstance(history.get("current_stage"), dict) else None,
            } if stages else None,
            "publications": [
                publication for publication in (history.get("publications") or [])
                if isinstance(publication, dict)
            ][:_FILES_PER_RUN],
        })
    return _without_superseded_asks(items)


def _without_superseded_asks(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop a finished run that only asked for something once its Flow ran again.

    A run that ended asking for input (no topic picked, a brief too thin) is
    answered by starting the Flow again; after that it is history, not
    something the person still needs to do. ``items`` are newest first.
    """
    seen: set[str] = set()
    kept = []
    for item in items:
        # The Flow, however it was started (its App entry or a direct run).
        flow = item.get("workflow_id") or item.get("binding_id") or ""
        # An ask's files are what it asked about (Knowledge to fill in), not
        # something it made, so they go with it.
        superseded = (
            flow in seen and item["status"] in _ENDED_RUN_STATUSES
            and item.get("business_outcome") in {"needs_input", "revision_required"}
        )
        if not superseded:
            kept.append(item)
        seen.add(flow)
    return kept
