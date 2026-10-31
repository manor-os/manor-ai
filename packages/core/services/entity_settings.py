"""Atomic writes to ``entities.settings`` (JSONB).

``Entity.settings`` mixes hot usage counters with billing identity
(``stripe_subscription_id``, webhook dedupe ids, pending plan changes). A
read-modify-write of the whole dict from a usage path that did not lock the
row can overwrite a concurrent webhook's commit with a stale copy. Usage paths
therefore change only their own keys, in one SQL statement that operates on
the row's current committed value.
"""
from __future__ import annotations

import re
from typing import Any, Mapping, Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value
from sqlalchemy.orm.util import identity_key

from packages.core.database_values import dumps_storable_json

_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")


def _check_key(key: str) -> str:
    if not _KEY_RE.match(key):
        raise ValueError(f"invalid settings key: {key!r}")
    return key


def _sync_loaded_entity(db: AsyncSession, entity_id: str, settings: Any) -> None:
    """Refresh a loaded Entity's settings without marking it dirty."""
    from packages.core.models.user import Entity

    instance = db.sync_session.identity_map.get(identity_key(Entity, entity_id))
    if instance is not None and isinstance(settings, dict):
        set_committed_value(instance, "settings", settings)


async def increment_entity_settings_counters(
    db: AsyncSession,
    entity_id: str,
    increments: Mapping[str, int | float],
    *,
    round_digits: Optional[int] = None,
) -> Optional[dict]:
    """Add ``increments`` to numeric settings keys in one UPDATE.

    Missing or non-numeric values count as zero. Returns the new settings
    dict, or None when the entity does not exist.
    """
    if not increments:
        return None
    # Pending ORM changes must reach the row before the statement reads it.
    await db.flush()
    params: dict[str, Any] = {"entity_id": entity_id}
    pairs: list[str] = []
    for index, (key, delta) in enumerate(increments.items()):
        _check_key(key)
        params[f"d{index}"] = delta
        current = (
            f"CASE WHEN jsonb_typeof(settings->'{key}') = 'number' "
            f"THEN (settings->>'{key}')::numeric ELSE 0 END"
        )
        value = f"{current} + CAST(:d{index} AS numeric)"
        if round_digits is not None:
            value = f"round({value}, {int(round_digits)})"
        pairs.append(f"'{key}', to_jsonb({value})")
    row = (await db.execute(
        text(
            "UPDATE entities SET settings = COALESCE(settings, '{}'::jsonb) "
            f"|| jsonb_build_object({', '.join(pairs)}) "
            "WHERE id = :entity_id RETURNING settings"
        ),
        params,
    )).first()
    if row is None:
        return None
    _sync_loaded_entity(db, entity_id, row[0])
    return row[0]


async def merge_entity_settings(
    db: AsyncSession,
    entity_id: str,
    patch: Mapping[str, Any],
) -> Optional[dict]:
    """Shallow-merge ``patch`` into settings in one UPDATE."""
    if not patch:
        return None
    for key in patch:
        _check_key(key)
    await db.flush()
    row = (await db.execute(
        text(
            "UPDATE entities SET settings = COALESCE(settings, '{}'::jsonb) "
            "|| CAST(:patch AS jsonb) WHERE id = :entity_id RETURNING settings"
        ),
        {"entity_id": entity_id, "patch": dumps_storable_json(dict(patch))},
    )).first()
    if row is None:
        return None
    _sync_loaded_entity(db, entity_id, row[0])
    return row[0]
