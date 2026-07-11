from __future__ import annotations

import sys
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory


ROOT = Path(__file__).resolve().parents[1]


def _script_directory() -> ScriptDirectory:
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "packages/core/migrations"))
    return ScriptDirectory.from_config(config)


def test_alembic_revision_graph_loads_with_single_head() -> None:
    script = _script_directory()

    assert script.get_heads() == ["20260708_01"]
    assert script.get_revision("20260514_01") is not None
    assert script.get_revision("20260706_01") is not None
    assert script.get_revision("20260708_01") is not None
    assert script.get_revision("20260708_01").down_revision == "20260706_01"


def test_commerce_repair_merge_keeps_both_parent_revisions() -> None:
    script = _script_directory()

    merge_revision = script.get_revision("20260516_01")

    assert merge_revision is not None
    assert set(merge_revision.down_revision) == {"20260514_01", "20260515_01"}


def test_default_entity_plan_migration_follows_personal_plan_linearly() -> None:
    script = _script_directory()

    personal_plan = script.get_revision("20260602_04")
    model_provider_keys = script.get_revision("20260605_01")
    default_entity_plan = script.get_revision("20260605_02")
    user_memberships = script.get_revision("20260606_01")

    assert personal_plan is not None
    assert model_provider_keys.down_revision == "20260602_04"
    assert default_entity_plan.down_revision == "20260605_01"
    assert user_memberships.down_revision == "20260605_02"
