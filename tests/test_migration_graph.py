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

    assert script.get_heads() == ["20260802_04"]
    assert script.get_revision("20260802_04").down_revision == "20260802_03"
    assert script.get_revision("20260802_03").down_revision == "20260802_02"
    assert script.get_revision("20260802_02").down_revision == "20260802_01"
    assert script.get_revision("20260802_01").down_revision == "20260801_01"
    assert script.get_revision("20260801_01").down_revision == "20260731_13"
    assert script.get_revision("20260731_13").down_revision == "20260731_12"
    assert script.get_revision("20260731_12").down_revision == "20260731_11"
    assert script.get_revision("20260731_11").down_revision == "20260731_10"
    assert script.get_revision("20260731_10").down_revision == "20260731_01"
    assert script.get_revision("20260731_01").down_revision == "20260730_02"
    assert script.get_revision("20260730_02").down_revision == "20260730_01"
    assert script.get_revision("20260730_01").down_revision == "20260729_03"
    assert script.get_revision("20260728_03").down_revision == "20260728_02"
    assert script.get_revision("20260728_02").down_revision == "20260728_01"
    assert script.get_revision("20260728_01").down_revision == "20260726_07"
    assert script.get_revision("20260727_01").down_revision == "20260725_02"
    # Re-chained onto 20260727_01: both migrations were authored against
    # 20260725_02 as the head, so leaving them siblings would fork the graph.
    assert script.get_revision("20260726_06").down_revision == "20260727_01"
    assert script.get_revision("20260726_07").down_revision == "20260726_06"
    assert script.get_revision("20260725_02").down_revision == "20260726_05"
    assert script.get_revision("20260725_01").down_revision == "20260724_01"
    assert script.get_revision("20260724_01").down_revision == "20260722_04"
    assert script.get_revision("20260514_01") is not None
    assert script.get_revision("20260706_01") is not None
    assert script.get_revision("20260708_01") is not None
    assert script.get_revision("20260708_01").down_revision == "20260706_01"
    assert script.get_revision("20260715_01") is not None
    assert script.get_revision("20260716_01").down_revision == "20260715_01"
    assert script.get_revision("20260721_01").down_revision == "20260716_01"
    assert script.get_revision("20260722_01").down_revision == "20260721_01"
    assert script.get_revision("20260722_02").down_revision == "20260722_01"
    assert set(script.get_revision("20260722_03").down_revision) == {
        "20260718_01",
        "20260722_02",
    }
    assert script.get_revision("20260722_04").down_revision == "20260722_03"
    assert script.get_revision("20260723_01").down_revision == "20260725_01"
    assert script.get_revision("20260723_02").down_revision == "20260723_01"
    assert script.get_revision("20260723_03").down_revision == "20260723_02"
    assert script.get_revision("20260723_04").down_revision == "20260723_03"
    assert script.get_revision("20260726_01").down_revision == "20260723_04"
    assert script.get_revision("20260726_02").down_revision == "20260726_01"
    assert script.get_revision("20260726_03").down_revision == "20260726_02"
    assert script.get_revision("20260726_04").down_revision == "20260726_03"
    assert script.get_revision("20260726_05").down_revision == "20260726_04"


def test_workflow_run_lineage_migration_does_not_promote_untrusted_trigger_data(
    monkeypatch,
) -> None:
    revision = _script_directory().get_revision("20260731_12").module
    added_columns: list[tuple[str, object]] = []
    created_indexes: list[tuple[str, str, list[str]]] = []
    statements: list[str] = []
    monkeypatch.setattr(
        revision,
        "add_column_if_not_exists",
        lambda table, column: added_columns.append((table, column)),
    )
    monkeypatch.setattr(
        revision,
        "create_index_if_not_exists",
        lambda name, table, columns: created_indexes.append((name, table, columns)),
    )
    monkeypatch.setattr(
        revision.op,
        "execute",
        lambda statement: statements.append(str(statement)),
    )

    revision.upgrade()

    sql = "\n".join(statements)
    column_names = {column.name for table, column in added_columns if table == "workflow_runs"}
    assert {
        "retry_of_run_id",
        "retry_from_step_id",
        "attempt_number",
        "lineage_root_run_id",
        "lineage_is_legacy",
    }.issubset(column_names)
    assert (
        "ix_workflow_runs_lineage_root_run_id",
        "workflow_runs",
        ["lineage_root_run_id"],
    ) in created_indexes
    assert "trigger_data" not in sql


def test_workflow_run_lineage_downgrade_preserves_canonical_retry_fields(
    monkeypatch,
) -> None:
    revision = _script_directory().get_revision("20260731_12").module
    events: list[tuple[str, str]] = []
    monkeypatch.setattr(revision, "index_exists", lambda _name: True)
    monkeypatch.setattr(revision, "column_exists", lambda _table, _column: True)
    monkeypatch.setattr(
        revision.op,
        "execute",
        lambda statement: events.append(("execute", str(statement))),
    )
    monkeypatch.setattr(
        revision.op,
        "drop_index",
        lambda name, **_kwargs: events.append(("drop_index", name)),
    )
    monkeypatch.setattr(
        revision.op,
        "drop_column",
        lambda _table, column: events.append(("drop_column", column)),
    )

    revision.downgrade()

    copy_index = next(index for index, event in enumerate(events) if event[0] == "execute")
    first_column_drop = next(
        index for index, event in enumerate(events) if event[0] == "drop_column"
    )
    downgrade_sql = events[copy_index][1]
    assert copy_index < first_column_drop
    assert "jsonb_build_object" in downgrade_sql
    assert "'retry_of_run_id', retry_of_run_id" in downgrade_sql
    assert "'retry_from_step_id', retry_from_step_id" in downgrade_sql
    assert "'attempt_number', attempt_number" in downgrade_sql
    assert "WHERE lineage_is_legacy IS FALSE" in downgrade_sql


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
