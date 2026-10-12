"""Declare the services that installed Workspaces already subscribe Agents to.

A Blueprint install (R2-4, ``video-studio-v1``) created active Agent
subscriptions for ``product_video.*`` services that the Workspace's
``operating_model.services`` never declared. Every later operation validated
the whole state and failed on those mappings. This forward repair only adds
the missing declarations; it never removes or rewrites an existing service.

Rows must be identical to what the corrected Blueprint installs, or the next
Blueprint upgrade plan reports a local edit on every repaired leaf. So a
declaration comes, in order, from:

1. the shipped Blueprint file the Workspace was installed from (platform
   ``builtin:<slug>`` installs), inserted at that Blueprint's position;
2. the installed Blueprint row in ``workspace_blueprints`` (other installs);
3. the subscription's ``config.service_name``, else the Agent name, appended
   in subscription order.

Step 1 reads the shipped Blueprint JSON at run time, so the rows follow the
Blueprint version of the code running the migration, not a frozen copy. That
is intended: the repaired list must equal what an upgrade to the running
code's Blueprint would install, so the upgrade plan sees no local edit.

A key whose subscriptions disagree on the name (and that no Blueprint
declares) is ambiguous: it is skipped and logged, not guessed. Each repaired
Workspace bumps ``operation_revision`` and gets an ``audit_log`` receipt.
Running it again changes nothing.
"""
from __future__ import annotations

import json
import logging
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import MetaData, Table, inspect, select

from packages.core.models.base import generate_ulid

REVISION = "20261007_03"
AUDIT_ACTION = "workspace.services_declared"
PLATFORM_ID_PREFIX = "builtin:"
SHIPPED_BLUEPRINTS = Path(__file__).resolve().parents[1] / "blueprints" / "configs"
logger = logging.getLogger(__name__)


def _key(row: Any) -> str:
    return str(row.get("key") or row.get("service_key") or "").strip() if isinstance(row, dict) else ""


def _payload_services(payload: Any) -> list[dict]:
    recipe = payload.get("recipe") if isinstance(payload, dict) else None
    model = recipe.get("operating_model") if isinstance(recipe, dict) else None
    services = model.get("services") if isinstance(model, dict) else None
    return [row for row in services or [] if _key(row)]


def shipped_blueprint_services(root: Path = SHIPPED_BLUEPRINTS) -> dict[str, list[dict]]:
    """Platform slug -> its shipped service declarations, in Blueprint order."""
    out: dict[str, list[dict]] = {}
    for path in sorted(root.rglob("*.json")) if root.is_dir() else []:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        slug = (payload.get("manifest") or {}).get("slug") if isinstance(payload, dict) else None
        if isinstance(slug, str) and slug:
            out[slug] = _payload_services(payload)
    return out


def _blueprint_marker(settings: Any) -> tuple[str | None, str | None]:
    marker = settings.get("_blueprint") if isinstance(settings, dict) else None
    if not isinstance(marker, dict):
        return None, None
    return (str(marker.get("blueprint_id") or "").strip() or None,
            str(marker.get("blueprint_slug") or "").strip() or None)


def _installed_blueprint_services(
    connection, blueprints: Table | None, settings: Any, shipped: dict[str, list[dict]],
) -> tuple[list[dict], bool]:
    """(declarations, ordered) for the Blueprint this Workspace came from."""
    blueprint_id, slug = _blueprint_marker(settings)
    platform_slug = (
        blueprint_id[len(PLATFORM_ID_PREFIX):] if blueprint_id and blueprint_id.startswith(PLATFORM_ID_PREFIX)
        else slug if blueprint_id is None else None
    )
    if platform_slug and platform_slug in shipped:
        return shipped[platform_slug], True
    if blueprints is None or not blueprint_id:
        return [], False
    payload = connection.execute(
        select(blueprints.c.payload).where(blueprints.c.id == blueprint_id)
    ).scalar_one_or_none()
    return _payload_services(payload), False


def _with_declarations(services: list, additions: list[dict], blueprint_order: list[str]) -> list:
    """Insert each addition after its nearest preceding Blueprint neighbour."""
    result = list(services)
    for row in additions:
        key = _key(row)
        position = len(result)
        if key in blueprint_order:
            earlier = blueprint_order[:blueprint_order.index(key)]
            present = [i for i, existing in enumerate(result) if _key(existing) in earlier]
            later = blueprint_order[blueprint_order.index(key) + 1:]
            following = [i for i, existing in enumerate(result) if _key(existing) in later]
            if present:
                position = max(present) + 1
            elif following:
                position = min(following)
        result.insert(position, row)
    return result


def plan(connection, *, shipped: dict[str, list[dict]] | None = None) -> dict[str, Any]:
    """What the repair would write, without writing it."""
    tables = set(inspect(connection).get_table_names())
    if not {"workspaces", "agent_subscriptions"} <= tables:
        return {"operations": [], "skipped": []}
    shipped = shipped_blueprint_services() if shipped is None else shipped
    metadata = MetaData()
    workspaces = Table("workspaces", metadata, autoload_with=connection)
    subscriptions = Table("agent_subscriptions", metadata, autoload_with=connection)
    agents = Table("agents", metadata, autoload_with=connection) if "agents" in tables else None
    blueprints = (
        Table("workspace_blueprints", metadata, autoload_with=connection)
        if "workspace_blueprints" in tables else None
    )
    columns = [subscriptions.c.workspace_id, subscriptions.c.service_key, subscriptions.c.config,
               workspaces.c.entity_id]
    joined = subscriptions.join(workspaces, workspaces.c.id == subscriptions.c.workspace_id)
    if agents is not None:
        columns.append(agents.c.name)
        joined = joined.outerjoin(agents, agents.c.id == subscriptions.c.agent_id)
    query = select(*columns).select_from(joined).where(
        subscriptions.c.status == "active",
        subscriptions.c.service_key.is_not(None),
        # The same tenant boundary every Workspace read applies.
        subscriptions.c.entity_id == workspaces.c.entity_id,
        workspaces.c.deleted_at.is_(None),
    ).order_by(subscriptions.c.created_at, subscriptions.c.id)
    subscribed: dict[str, list[tuple[str, dict, str | None]]] = {}
    for row in connection.execute(query).mappings():
        key = str(row["service_key"] or "").strip()
        if key:
            subscribed.setdefault(row["workspace_id"], []).append(
                (key, row["config"] if isinstance(row["config"], dict) else {}, row.get("name"))
            )

    operations, skipped = [], []
    rows = connection.execute(select(
        workspaces.c.id, workspaces.c.entity_id, workspaces.c.operating_model,
        workspaces.c.settings, workspaces.c.operation_revision,
    ).where(workspaces.c.deleted_at.is_(None), workspaces.c.id.in_(list(subscribed) or [""])))
    for workspace in rows.mappings():
        model = workspace["operating_model"]
        services = model.get("services") if isinstance(model, dict) else None
        if not isinstance(services, list) or not services:
            # No declared list: nothing is validated against it, so nothing is missing.
            continue
        declared = {_key(row) for row in services}
        candidates: dict[str, set[str]] = {}
        for key, config, agent_name in subscribed[workspace["id"]]:
            if key in declared:
                continue
            name = str(config.get("service_name") or agent_name or key).strip() or key
            candidates.setdefault(key, set()).add(name)
        if not candidates:
            continue
        source, ordered = _installed_blueprint_services(
            connection, blueprints, workspace["settings"], shipped,
        )
        from_blueprint = {_key(row): row for row in source}
        blueprint_order = [_key(row) for row in source] if ordered else []
        added = []
        # dict keys keep subscription (created_at, id) order for the fallback.
        for key in candidates:
            if key in from_blueprint:
                added.append(deepcopy(from_blueprint[key]))
            elif len(candidates[key]) == 1:
                added.append({"key": key, "name": next(iter(candidates[key]))})
            else:
                skipped.append({"workspace_id": workspace["id"], "service_key": key,
                                "reason": "subscriptions disagree on the service name",
                                "names": sorted(candidates[key])})
        if added:
            operations.append({
                "workspace_id": workspace["id"], "entity_id": workspace["entity_id"],
                "operating_model": {**model, "services": _with_declarations(services, added, blueprint_order)},
                # Open drafts built on the old state are stale: apply refuses
                # them instead of writing the undeclared state back.
                "operation_revision": int(workspace["operation_revision"] or 0) + 1,
                "added": added,
            })
    return {"operations": operations, "skipped": skipped}


def apply(connection, *, shipped: dict[str, list[dict]] | None = None) -> dict[str, Any]:
    """Run inside Alembic's transaction; returns what was written and skipped.

    Workspace chat summaries are cached per API process for five minutes; a
    migration process cannot reach that cache, and deploys restart the pods.
    """
    result = plan(connection, shipped=shipped)
    if not result["operations"] and not result["skipped"]:
        return {**result, "applied": 0}
    metadata = MetaData()
    workspaces = Table("workspaces", metadata, autoload_with=connection)
    tables = set(inspect(connection).get_table_names())
    audit = Table("audit_log", metadata, autoload_with=connection) if "audit_log" in tables else None
    now = datetime.now(timezone.utc)
    for operation in result["operations"]:
        connection.execute(workspaces.update().where(workspaces.c.id == operation["workspace_id"]).values(
            operating_model=operation["operating_model"],
            operation_revision=operation["operation_revision"],
        ))
        if audit is not None:
            connection.execute(audit.insert().values(
                id=generate_ulid(), action=AUDIT_ACTION, entity_id=operation["entity_id"],
                resource_type="workspace", resource_id=operation["workspace_id"],
                details={"revision": REVISION, "added_services": operation["added"]}, created_at=now,
            ))
    for item in result["skipped"]:
        logger.warning("workspace %s: service %r not declared (%s: %s)", item["workspace_id"],
                       item["service_key"], item["reason"], item["names"])
    return {**result, "applied": len(result["operations"])}
