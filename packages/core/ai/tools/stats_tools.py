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
        query = query.where(WorkspaceStat.key.startswith(prefix))
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


def _handler(name: str):
    async def handle(entity_id: str = "", **kwargs: Any) -> str:
        from packages.core.database import async_session

        async with async_session() as db:
            try:
                context = await _context(db, entity_id, kwargs, write=name != "query_workspace_stat_observations")
                if name == "configure_workspace_stat":
                    key = str(kwargs["key"]).strip().lower()
                    freshness_requested, requested_freshness_limit_seconds = _freshness_limit(kwargs)
                    existing = await db.scalar(
                        select(WorkspaceStat).where(
                            WorkspaceStat.workspace_id == context.workspace_id, WorkspaceStat.key == key
                        )
                    )
                    definition = {
                        field: kwargs.get(field)
                        for field in (
                            "name",
                            "description",
                            "value_type",
                            "unit",
                            "window",
                            "collector_type",
                            "collector_config",
                            "collection_cadence",
                            "freshness_limit_seconds",
                        )
                        if field in kwargs
                    }
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
                                WorkspaceStat.workspace_id == context.workspace_id,
                                *(getattr(WorkspaceStat, field) == value for field, value in identity.items()),
                            ).order_by(WorkspaceStat.created_at, WorkspaceStat.id).limit(1))
                    if existing is not None:
                        if existing.status != "active":
                            raise StatError("stat is inactive; use the native update API to change its status")
                        if any(getattr(existing, field) != value for field, value in identity.items()):
                            raise StatError(
                                "stat key already exists with a different definition; use the native update API"
                            )
                        return json.dumps(_configured_stat_response(
                            existing,
                            created=False,
                            freshness_requested=freshness_requested,
                            requested_freshness_limit_seconds=requested_freshness_limit_seconds,
                        ))
                    stat = await create_stat(
                        db,
                        entity_id=entity_id,
                        workspace_id=context.workspace_id,
                        key=key,
                        install_schedule=False,
                        **definition,
                    )
                    await db.commit()
                    return json.dumps(_configured_stat_response(
                        stat,
                        created=True,
                        freshness_requested=freshness_requested,
                        requested_freshness_limit_seconds=requested_freshness_limit_seconds,
                    ))
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
