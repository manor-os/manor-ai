"""Runtime-scoped collection and reporting tools for Workspace Stats."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select

from packages.core.ai.runtime.tool_context import runtime_tool_call_context_from_kwargs
from packages.core.ai.runtime.tool_error_contract import tool_error_payload
from packages.core.models.workspace_stat import WorkspaceStat
from packages.core.services.workspace_access import (
    lock_workspace_access_boundary,
    user_can_read_workspace_id,
    user_can_write_workspace_id,
)
from packages.core.stats.publication_reporting import compare_publication_stats, list_publication_observations
from packages.core.stats.service import StatError, collect_stat, create_stat, normalize_public_post_config


def _schema(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
        },
    }


# Values one call may record: a page of posts, three counters each.
_MAX_OBSERVATIONS = 60

_SCHEMAS = [
    _schema(
        "configure_workspace_stat",
        "Create one immutable-identity Workspace Stat, or reuse the existing Stat when its key or (for public_post) "
        "collection definition matches; name, description and freshness never block reuse. Returns stat_id and "
        "revision. Use the native update API to change an existing definition.",
        {
            "key": {"type": "string"},
            "name": {"type": "string"},
            "description": {"type": "string"},
            "value_type": {"type": "string"},
            "unit": {"type": "string"},
            "window": {"type": "string"},
            "collector_type": {"type": "string"},
            "collector_config": {
                "type": "object",
                "description": "public_post requires platform, public_url, external_post_id, metric_key "
                "(views/likes/comments, or TikTok shares). Include verified account_id/account_handle when known "
                "and subject {kind, id} for subject-scoped collection/reporting. Other collectors use their native config.",
            },
            "collection_cadence": {"type": "string"},
            "freshness_limit_seconds": {"type": ["integer", "null"], "minimum": 60},
        },
        ["key", "name", "collector_type", "collector_config"],
    ),
    _schema(
        "collect_workspace_stats",
        "Collect configured Workspace Stats using their native collectors.",
        {
            "stat_ids": {"type": "array", "items": {"type": "string"}},
            "subject": {"type": "object"},
            "key_prefix": {"type": "string"},
            "after_id": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 20},
        },
        [],
    ),
    _schema(
        "record_workspace_stat_observations",
        "Record values a Workflow measured itself (an API's counts) on manual Workspace Stats, without an agent "
        "assessment. Each entry names its Stat by key; include its definition (name, value_type, unit, "
        "collector_config) to configure the Stat on first use. A null value needs gaps saying why. "
        "The same idempotency_key never records twice.",
        {
            "observations": {
                "type": "array", "minItems": 1, "maxItems": _MAX_OBSERVATIONS,
                "items": {
                    "type": "object",
                    "properties": {
                        "key": {"type": "string"},
                        "value": {"type": ["number", "null"]},
                        "idempotency_key": {"type": "string"},
                        "evidence": {"type": "object"},
                        "gaps": {"type": "array", "items": {"type": "string"}},
                        "definition": {"type": "object"},
                    },
                    "required": ["key", "value", "idempotency_key"],
                },
            },
        },
        ["observations"],
    ),
    _schema(
        "delete_workspace_stats",
        "Permanently delete one family of Workspace Stats, with all their observations and collection "
        "schedules: every Stat whose key starts with key_prefix (such as publication.<id>.), or every Stat "
        "about one subject {kind, id} (such as a campaign project). Give exactly one.",
        {"key_prefix": {"type": "string", "minLength": 3}, "subject": {"type": "object"}},
        [],
    ),
    _schema(
        "query_workspace_stat_observations",
        "Compare configured stat observations for a reporting window.",
        {
            "stat_ids": {"type": "array", "items": {"type": "string"}},
            "window_start": {"type": "string"},
            "window_end": {"type": "string"},
            "window": {"enum": ["previous_calendar_week"]},
            "timezone": {"type": "string"},
            "subject": {"type": "object"},
            "key_prefix": {"type": "string"},
            "after_id": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 20},
        },
        [],
    ),
]


def _freshness_limit(params: dict[str, Any]) -> tuple[bool, int | None]:
    """Mirror the Stats API freshness contract for direct tool invocations."""

    if "freshness_limit_seconds" not in params:
        return False, None
    value = params["freshness_limit_seconds"]
    if value is None:
        return True, None
    if isinstance(value, bool) or not isinstance(value, int) or value < 60:
        raise StatError("freshness_limit_seconds must be an integer of at least 60 seconds or null")
    return True, value


def _configured_stat_response(
    stat: WorkspaceStat,
    *,
    created: bool,
    freshness_requested: bool,
    requested_freshness_limit_seconds: int | None,
) -> dict[str, Any]:
    response: dict[str, Any] = {
        "stat_id": stat.id,
        "key": stat.key,
        "revision": stat.revision,
        "created": created,
        "freshness_limit_seconds": stat.freshness_limit_seconds,
    }
    if freshness_requested:
        response["requested_freshness_limit_seconds"] = requested_freshness_limit_seconds
        if stat.freshness_limit_seconds != requested_freshness_limit_seconds:
            response["operational_policy_mismatch"] = {
                "field": "freshness_limit_seconds",
                "actual": stat.freshness_limit_seconds,
                "requested": requested_freshness_limit_seconds,
                "update_required": True,
            }
        else:
            response["operational_policy_mismatch"] = None
    return response


async def _selected_stats(
    db, *, entity_id: str, workspace_id: str, params: dict[str, Any]
) -> tuple[list[WorkspaceStat], str | None]:
    stat_ids, subject, prefix = (
        params.get("stat_ids"),
        params.get("subject"),
        str(params.get("key_prefix") or "").strip(),
    )
    selectors = int(bool(stat_ids)) + int(subject is not None) + int(bool(prefix))
    if selectors != 1:
        raise StatError("provide exactly one of stat_ids, subject, or key_prefix")
    if stat_ids is not None and (not isinstance(stat_ids, list) or not stat_ids or len(stat_ids) > 20):
        raise StatError("stat_ids must contain 1 to 20 IDs")
    limit = len(stat_ids) if stat_ids else int(params.get("limit") or 20)
    if not 1 <= limit <= 20:
        raise StatError("limit must be from 1 to 20")
    after_id = str(params.get("after_id") or "").strip()
    if stat_ids and after_id:
        raise StatError("after_id is only valid with subject or key_prefix")
    query = select(WorkspaceStat).where(
        WorkspaceStat.entity_id == entity_id, WorkspaceStat.workspace_id == workspace_id
    )
    if stat_ids:
        query = query.where(WorkspaceStat.id.in_(stat_ids))
    if prefix:
        query = query.where(WorkspaceStat.key.startswith(prefix, autoescape=True))
    if subject is not None:
        query = query.where(WorkspaceStat.collector_config.contains({"subject": subject}))
    if after_id:
        query = query.where(WorkspaceStat.id > after_id)
    stats = list((await db.execute(query.order_by(WorkspaceStat.id).limit(limit + 1))).scalars())
    has_more = len(stats) > limit
    stats = stats[:limit]
    if stat_ids and {stat.id for stat in stats} != set(stat_ids):
        raise StatError("one or more stats are unavailable in this workspace")
    if not stats and stat_ids:
        raise StatError("selector matches no workspace stats")
    return stats, (stats[-1].id if has_more else None)


def _window(params: dict[str, Any]) -> tuple[str, str, str | None]:
    if params.get("window") == "previous_calendar_week":
        zone = ZoneInfo(str(params.get("timezone") or "UTC"))
        local = datetime.now(UTC).astimezone(zone)
        end = (local - timedelta(days=local.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        start = end - timedelta(days=7)
        iso_year, iso_week, _ = start.date().isocalendar()
        return (
            start.astimezone(UTC).isoformat(),
            end.astimezone(UTC).isoformat(),
            f"{iso_year}-W{iso_week:02d}",
        )
    if not params.get("window_start") or not params.get("window_end"):
        raise StatError("window_start and window_end are required unless window=previous_calendar_week")
    return str(params["window_start"]), str(params["window_end"]), None


async def _context(db, entity_id: str, kwargs: dict[str, Any], *, write: bool):
    context = runtime_tool_call_context_from_kwargs(kwargs, entity_id=entity_id)
    if not context.workspace_id or not context.user_id:
        raise PermissionError("an authenticated Workspace runtime is required")
    workspace = await lock_workspace_access_boundary(db, workspace_id=context.workspace_id, entity_id=entity_id)
    from packages.core.services.workspace_autonomy import workspace_allows_configured_work

    if not workspace_allows_configured_work(workspace):
        raise PermissionError("Workspace is unavailable")
    allowed = user_can_write_workspace_id if write else user_can_read_workspace_id
    if not await allowed(db, entity_id=entity_id, workspace_id=context.workspace_id, user_id=context.user_id):
        raise PermissionError("actor is not authorized for this Workspace")
    return context


_DEFINITION_FIELDS = (
    "name", "description", "value_type", "unit", "window", "collector_type", "collector_config",
    "collection_cadence", "freshness_limit_seconds",
)


async def _configure(db, *, entity_id: str, workspace_id: str, params: dict[str, Any]) -> tuple[WorkspaceStat, bool]:
    """Reuse the Stat this definition names, or create it; returns (stat, created)."""
    key = str(params["key"]).strip().lower()
    existing = await db.scalar(
        select(WorkspaceStat).where(WorkspaceStat.workspace_id == workspace_id, WorkspaceStat.key == key)
    )
    definition = {field: params.get(field) for field in _DEFINITION_FIELDS if field in params}
    if definition.get("collector_type") == "public_post":
        definition["collector_config"] = normalize_public_post_config(definition["collector_config"])
    # Display text and freshness policy never change what a Stat measures,
    # so a reworded request reuses the Stat instead of conflicting with it.
    identity = {
        field: value
        for field, value in definition.items()
        if field not in {"name", "description", "freshness_limit_seconds"}
    }
    if definition.get("collector_type") == "public_post":
        identity = {
            "value_type": "number", "unit": None, "window": "latest", "collection_cadence": None,
            **identity,
        }
        if existing is None:
            # _context holds the Workspace lock through commit, serializing
            # concurrent reconciliation without a scenario-specific identity.
            existing = await db.scalar(select(WorkspaceStat).where(
                WorkspaceStat.entity_id == entity_id,
                WorkspaceStat.workspace_id == workspace_id,
                *(getattr(WorkspaceStat, field) == value for field, value in identity.items()),
            ).order_by(WorkspaceStat.created_at, WorkspaceStat.id).limit(1))
    if existing is not None:
        if existing.status != "active":
            raise StatError("stat is inactive; use the native update API to change its status")
        if any(getattr(existing, field) != value for field, value in identity.items()):
            raise StatError("stat key already exists with a different definition; use the native update API")
        return existing, False
    stat = await create_stat(db, entity_id=entity_id, workspace_id=workspace_id, key=key, install_schedule=False,
                             **definition)
    return stat, True



async def _record_observations(db, *, entity_id: str, workspace_id: str, params: dict[str, Any]) -> list[dict]:
    """Write values a Workflow measured itself (an API's counts): no agent judges them.

    Each entry names its Stat by key and may carry the Stat's definition, so the
    first reading also configures it. Only manually collected Stats take values
    this way; a collector or assessment Stat keeps its own path.
    """
    entries = params.get("observations")
    if not isinstance(entries, list) or not 1 <= len(entries) <= _MAX_OBSERVATIONS:
        raise StatError(f"observations must contain 1 to {_MAX_OBSERVATIONS} entries")
    from packages.core.stats.service import record_observation

    recorded = []
    for entry in entries:
        if not isinstance(entry, dict) or not str(entry.get("key") or "").strip():
            raise StatError("each observation needs a stat key")
        idempotency_key = str(entry.get("idempotency_key") or "").strip()
        if not idempotency_key or len(idempotency_key) > 200:
            raise StatError("each observation needs an idempotency_key of at most 200 characters")
        definition = entry.get("definition")
        # An existing Stat keeps its own definition; the entry's only configures a missing one.
        stat = await db.scalar(select(WorkspaceStat).where(
            WorkspaceStat.entity_id == entity_id, WorkspaceStat.workspace_id == workspace_id,
            WorkspaceStat.key == str(entry["key"]).strip().lower(),
        ))
        if stat is None:
            if not isinstance(definition, dict) or (definition.get("collector_type") or "manual") != "manual":
                raise StatError(f"stat {entry['key']!r} is not configured; include its manual definition")
            stat, _ = await _configure(db, entity_id=entity_id, workspace_id=workspace_id,
                                       params={**definition, "collector_type": "manual", "key": entry["key"]})
        if stat.collector_type != "manual":
            raise StatError(f"stat {stat.key!r} is collected by {stat.collector_type}, not recorded")
        value = entry.get("value")
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))):
            raise StatError(f"value for {stat.key!r} must be a number or null")
        evidence = entry.get("evidence") if isinstance(entry.get("evidence"), dict) else {}
        gaps = entry.get("gaps")
        if gaps:
            evidence = {**evidence, "gaps": [str(gap) for gap in gaps][:20]}
        observation = await record_observation(
            db, stat, value=value, source="workflow", evidence=evidence or None,
            idempotency_key=f"workflow:{idempotency_key}",
        )
        recorded.append({"key": stat.key, "stat_id": stat.id, "observation_id": observation.id,
                         "value": float(observation.value) if observation.value is not None else None,
                         "observed_at": observation.observed_at.isoformat()})
    return recorded


async def _delete_stats(db, *, entity_id: str, workspace_id: str, params: dict[str, Any]) -> int:
    """Delete one family of Stats (a key prefix, or one subject's) with observations and schedules."""
    prefix = str(params.get("key_prefix") or "").strip().lower()
    subject = params.get("subject")
    if bool(prefix) == (subject is not None):
        raise StatError("provide exactly one of key_prefix or subject")
    # A selector names a family of Stats (publication.<post>., or one campaign's); never the whole Workspace.
    if prefix and (len(prefix) < 3 or "." not in prefix):
        raise StatError("key_prefix must name a family of stats, like publication.<id>.")
    if subject is not None and (not isinstance(subject, dict) or not str(subject.get("kind") or "").strip()
                                or not str(subject.get("id") or "").strip()):
        raise StatError("subject needs a kind and an id")
    from packages.core.stats.service import delete_stat

    selector = (WorkspaceStat.key.startswith(prefix, autoescape=True) if prefix
                else WorkspaceStat.collector_config.contains({"subject": {"kind": subject["kind"], "id": subject["id"]}}))
    stats = list((await db.execute(select(WorkspaceStat).where(
        WorkspaceStat.entity_id == entity_id, WorkspaceStat.workspace_id == workspace_id, selector,
    ).limit(1001))).scalars())
    if len(stats) > 1000:
        raise StatError("the selector matches more than 1000 stats; name a narrower family")
    for stat in stats:
        await delete_stat(db, stat)
    return len(stats)


def _handler(name: str):
    async def handle(entity_id: str = "", **kwargs: Any) -> str:
        from packages.core.database import async_session

        async with async_session() as db:
            try:
                context = await _context(db, entity_id, kwargs, write=name != "query_workspace_stat_observations")
                if name == "configure_workspace_stat":
                    freshness_requested, requested_freshness_limit_seconds = _freshness_limit(kwargs)
                    stat, created = await _configure(db, entity_id=entity_id, workspace_id=context.workspace_id,
                                                     params=kwargs)
                    await db.commit()
                    return json.dumps(_configured_stat_response(
                        stat,
                        created=created,
                        freshness_requested=freshness_requested,
                        requested_freshness_limit_seconds=requested_freshness_limit_seconds,
                    ))
                if name == "record_workspace_stat_observations":
                    recorded = await _record_observations(
                        db, entity_id=entity_id, workspace_id=context.workspace_id, params=kwargs)
                    await db.commit()
                    return json.dumps({"observations": recorded}, default=str)
                if name == "delete_workspace_stats":
                    deleted = await _delete_stats(db, entity_id=entity_id, workspace_id=context.workspace_id,
                                                  params=kwargs)
                    await db.commit()
                    return json.dumps({"deleted": deleted})
                stats, next_cursor = await _selected_stats(
                    db, entity_id=entity_id, workspace_id=context.workspace_id, params=kwargs
                )
                pagination = {"has_more": next_cursor is not None, "next_cursor": next_cursor}
                if name == "collect_workspace_stats":
                    observations = [await collect_stat(db, stat) for stat in stats]
                    await db.commit()
                    return json.dumps(
                        {
                            "observations": [
                                {
                                    "stat_id": row.stat_id, "observation_id": row.id,
                                    "value": float(row.value) if row.value is not None else None,
                                    "source": row.source, "observed_at": row.observed_at.isoformat(),
                                    "evidence": row.evidence,
                                }
                                for row in observations
                            ],
                            **pagination,
                        }
                    )
                window_start, window_end, reporting_period_key = _window(kwargs)
                rows = await list_publication_observations(
                    db,
                    entity_id=entity_id,
                    workspace_id=context.workspace_id,
                    stat_ids=[stat.id for stat in stats],
                    window_end=window_end,
                )
                response = {
                    **compare_publication_stats(stats, rows, window_start=window_start, window_end=window_end),
                    **pagination,
                }
                if reporting_period_key is not None:
                    response["reporting_period_key"] = reporting_period_key
                return json.dumps(
                    response,
                    default=str,
                )
            except (PermissionError, StatError, ValueError) as exc:
                await db.rollback()
                return json.dumps(tool_error_payload(tool_name=name, code="workspace_stat_invalid", message=str(exc)))

    return handle


def get_tools() -> list[tuple[dict, Any]]:
    return [(schema, _handler(schema["function"]["name"])) for schema in _SCHEMAS]
