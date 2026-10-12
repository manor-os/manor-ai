"""Search metadata every discoverable capability declares.

Discovery ranks the English catalog locally (capability_ranker), so whether a
capability is found depends on the words it is registered with. Each catalog
entry therefore declares, in English:

- ``terms``: words people use for it that its name and description lack;
- ``examples``: keyword queries a user's request would be written as.

Tools, Manor operations and MCP providers declare it in
``capability_search_metadata.json``, keyed by catalog id (``tool:create_task``,
``manor_action:list_tasks``, ``provider:gmail``). Skills declare it in their own
``config.json`` as ``{"search": {"terms": [...], "examples": [...]}}``.
tests/test_capability_registration_contract.py requires it for the whole
built-in catalog and ranks every example against that catalog: each must find
its capability in the top 5.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping

METADATA_PATH = Path(__file__).with_name("capability_search_metadata.json")
MIN_EXAMPLES = 3
FINDABLE_WITHIN = 5


@lru_cache(maxsize=1)
def capability_search_metadata() -> dict[str, dict[str, list[str]]]:
    """Declared metadata for tools, operations and providers, by catalog id."""
    data = json.loads(METADATA_PATH.read_text(encoding="utf-8"))
    return {key: value for key, value in data.items() if not key.startswith("_")}


def capability_search_terms(catalog_id: str) -> tuple[str, ...]:
    entry = capability_search_metadata().get(catalog_id) or {}
    return tuple(str(term) for term in entry.get("terms") or ())


def skill_search_metadata(config: Mapping[str, Any] | None) -> dict[str, list[str]]:
    """A skill's ``search`` config, normalized to string lists."""
    raw = (config or {}).get("search") if isinstance(config, Mapping) else None
    raw = raw if isinstance(raw, Mapping) else {}
    return {
        field: [str(item) for item in raw.get(field) or () if str(item).strip()]
        for field in ("terms", "examples")
    }
