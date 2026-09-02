from __future__ import annotations

import hashlib
import json
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY

import pytest
import sqlalchemy as sa
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import Text, text


ROOT = Path(__file__).resolve().parents[1]


def _script_directory() -> ScriptDirectory:
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "packages/core/migrations"))
    return ScriptDirectory.from_config(config)


def test_alembic_revision_graph_has_no_missing_parent_revisions() -> None:
    """Fail with an actionable error when a migration file is omitted."""
    script = _script_directory()

    try:
        script.get_heads()
    except KeyError as exc:
        missing_revision = exc.args[0] if exc.args else "<unknown>"
        raise AssertionError(
            "Alembic migration graph references missing revision "
            f"{missing_revision!r}; commit the parent migration file together "
            "with the migration that references it."
        ) from exc


def test_alembic_revision_graph_loads_with_single_head() -> None:
    script = _script_directory()

    assert script.get_heads() == ["20260902_01"]
    assert script.get_revision("20260902_01").down_revision == "20260901_05"
    assert script.get_revision("20260901_05").down_revision == "20260901_04"
    assert script.get_revision("20260901_04").down_revision == "20260901_03"
    assert script.get_revision("20260901_03").down_revision == "20260901_02"
    assert script.get_revision("20260901_02").down_revision == "20260831_01"
    assert script.get_revision("20260831_01").down_revision == "20260901_01"
    assert script.get_revision("20260901_01").down_revision == "20260831_03"
    assert script.get_revision("20260831_03").down_revision == "20260831_02"
    assert script.get_revision("20260831_02").down_revision == "20260830_05"
    assert script.get_revision("20260830_05").down_revision == "20260830_04"
    assert script.get_revision("20260830_04").down_revision == "20260830_03"
    assert script.get_revision("20260830_03").down_revision == "20260830_02"
    assert script.get_revision("20260830_02").down_revision == "20260830_01"
    assert script.get_revision("20260829_04").down_revision == "20260829_03"
    assert script.get_revision("20260829_03").down_revision == "20260829_02"
    assert script.get_revision("20260829_02").down_revision == "20260829_01"
    assert script.get_revision("20260829_01").down_revision == "20260828_07"
    assert script.get_revision("20260828_07").down_revision == "20260828_06"
    assert script.get_revision("20260828_06").down_revision == "20260828_05"
    assert script.get_revision("20260828_05").down_revision == "20260828_04"
    assert script.get_revision("20260828_04").down_revision == "20260828_03"
    assert script.get_revision("20260828_03").down_revision == "20260828_02"
    assert script.get_revision("20260828_02").down_revision == "20260828_01"
    assert script.get_revision("20260828_01").down_revision == "20260827_11"
    assert script.get_revision("20260827_11").down_revision == "20260827_10"
    assert script.get_revision("20260827_10").down_revision == (
        "20260827_09",
        "20260826_07",
    )
    assert script.get_revision("20260826_07").down_revision == "20260826_06"
    assert script.get_revision("20260826_06").down_revision == "20260826_05"
    assert script.get_revision("20260826_05").down_revision == "20260826_04"
    assert script.get_revision("20260827_09").down_revision == "20260827_08"
    assert script.get_revision("20260827_08").down_revision == "20260827_07"
    assert script.get_revision("20260827_07").down_revision == "20260827_06"
    assert script.get_revision("20260827_06").down_revision == "20260827_05"
    assert script.get_revision("20260827_05").down_revision == "20260827_04"
    assert script.get_revision("20260827_04").down_revision == "20260827_03"
    assert script.get_revision("20260827_03").down_revision == "20260827_02"
    assert script.get_revision("20260827_02").down_revision == "20260827_01"
    assert script.get_revision("20260827_01").down_revision == "20260826_04"
    assert script.get_revision("20260826_04").down_revision == "20260826_03"
    assert script.get_revision("20260826_03").down_revision == "20260826_02"
    assert script.get_revision("20260826_02").down_revision == "20260826_01"
    assert script.get_revision("20260826_01").down_revision == "20260825_03"
    assert script.get_revision("20260825_03").down_revision == "20260825_02"
    assert script.get_revision("20260825_02").down_revision == "20260825_01"
    assert script.get_revision("20260825_01").down_revision == "20260824_11"
    assert script.get_revision("20260824_11").down_revision == "20260824_10"
    assert script.get_revision("20260824_10").down_revision == "20260824_09"
    assert script.get_revision("20260824_09").down_revision == "20260824_08"
    assert script.get_revision("20260824_08").down_revision == "20260824_07"
    assert script.get_revision("20260824_07").down_revision == "20260824_06"
    assert script.get_revision("20260824_06").down_revision == "20260824_05"
    assert script.get_revision("20260824_05").down_revision == "20260824_04"
    assert script.get_revision("20260824_04").down_revision == "20260824_03"
    assert set(script.get_revision("20260824_03").down_revision) == {
        "20260824_02",
        "20260823_09",
    }
    assert script.get_revision("20260824_02").down_revision == "20260824_01"
    assert script.get_revision("20260824_01").down_revision == "20260823_01"
    assert script.get_revision("20260823_09").down_revision == "20260823_08"
    assert script.get_revision("20260823_08").down_revision == "20260823_07"
    assert script.get_revision("20260823_07").down_revision == "20260823_06"
    assert script.get_revision("20260823_06").down_revision == "20260823_05"
    assert script.get_revision("20260823_05").down_revision == "20260823_04"
    assert script.get_revision("20260823_04").down_revision == "20260823_03"
    assert script.get_revision("20260823_03").down_revision == "20260823_02"
    assert script.get_revision("20260823_02").down_revision == "20260823_01"
    assert script.get_revision("20260823_01").down_revision == "20260822_07"
    assert script.get_revision("20260822_07").down_revision == "20260822_08"
    assert script.get_revision("20260822_08").down_revision == "20260822_06"
    assert script.get_revision("20260822_06").down_revision == "20260822_05"
    assert script.get_revision("20260822_05").down_revision == "20260822_04"
    assert script.get_revision("20260822_04").down_revision == "20260822_03"
    assert script.get_revision("20260822_03").down_revision == "20260822_02"
    assert script.get_revision("20260822_02").down_revision == "20260822_01"
    assert script.get_revision("20260822_01").down_revision == "20260821_06"
    assert script.get_revision("20260821_05").down_revision == "20260821_04"
    assert script.get_revision("20260821_04").down_revision == "20260821_03"
    assert script.get_revision("20260821_03").down_revision == "20260821_02"
    assert script.get_revision("20260821_02").down_revision == "20260821_01"
    assert script.get_revision("20260821_01").down_revision == "20260820_03"
    assert script.get_revision("20260820_03").down_revision == "20260820_02"
    assert script.get_revision("20260820_02").down_revision == "20260820_01"
    assert script.get_revision("20260820_01").down_revision == "20260818_01"
    assert script.get_revision("20260818_01").down_revision == "20260817_02"
    assert set(script.get_revision("20260817_02").down_revision) == {
        "20260815_01",
        "20260817_01",
    }
    assert script.get_revision("20260815_01").down_revision == "20260814_01"
    assert script.get_revision("20260817_01").down_revision == "20260813_03"
    assert script.get_revision("20260814_01").down_revision == "20260813_03"
    assert script.get_revision("20260813_03").down_revision == "20260813_02"
    assert script.get_revision("20260813_02").down_revision == "20260813_01"
    assert script.get_revision("20260813_01").down_revision == "20260812_02"
    assert set(script.get_revision("20260812_02").down_revision) == {
        "20260811_01",
        "20260812_01",
    }
    assert script.get_revision("20260811_01").down_revision == "20260810_02"
    assert set(script.get_revision("20260812_01").down_revision) == {
        "20260808_01",
        "20260810_02",
    }
    assert set(script.get_revision("20260810_02").down_revision) == {
        "20260805_04",
        "20260810_01",
    }
    assert script.get_revision("20260810_01").down_revision == "20260804_01"
    assert script.get_revision("20260805_04").down_revision == "20260805_03"
    assert script.get_revision("20260805_03").down_revision == "20260805_02"
    assert script.get_revision("20260805_02").down_revision == "20260805_01"
    assert script.get_revision("20260805_01").down_revision == "20260804_01"
    assert script.get_revision("20260804_01").down_revision == "20260803_01"
    assert script.get_revision("20260803_01").down_revision == "20260802_05"
    assert script.get_revision("20260802_05").down_revision == "20260802_04"
    assert script.get_revision("20260802_04").down_revision == "20260802_03"
    assert script.get_revision("20260802_03").down_revision == "20260802_02"
    assert script.get_revision("20260802_02").down_revision == "20260802_01"
    assert script.get_revision("20260802_01").down_revision == "20260801_02"
    assert script.get_revision("20260801_02").down_revision == "20260801_01"
    assert script.get_revision("20260801_01").down_revision == "20260731_14"
    assert script.get_revision("20260731_14").down_revision == "20260731_13"
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


def test_user_acquisition_migration_creates_private_attribution_ledger(
    monkeypatch,
) -> None:
    revision = _script_directory().get_revision("20260901_03").module
    created_tables: list[str] = []
    indexes: list[tuple[str, bool]] = []

    monkeypatch.setattr(revision, "table_exists", lambda _table: False)
    monkeypatch.setattr(revision, "index_exists", lambda _index: False)
    monkeypatch.setattr(
        revision.op,
        "create_table",
        lambda table, *_columns: created_tables.append(table),
    )
    monkeypatch.setattr(
        revision.op,
        "create_index",
        lambda name, _table, _columns, **kwargs: indexes.append((
            name,
            bool(kwargs.get("unique")),
        )),
    )

    revision.upgrade()

    assert created_tables == ["user_acquisition_attributions"]
    assert indexes == [
        ("uq_user_acquisition_attributions_user", True),
        ("ix_user_acquisition_attributions_entity", False),
        ("ix_user_acquisition_attributions_created", False),
    ]


def test_google_review_scope_consistency_migration_uses_exact_manifest(
    monkeypatch,
) -> None:
    revision = _script_directory().get_revision("20260901_02").module
    statements: list[sa.sql.elements.TextClause] = []
    monkeypatch.setattr(revision, "table_exists", lambda _table: True)
    monkeypatch.setattr(
        revision,
        "column_exists",
        lambda _table, column: column in {"scopes", "default_config"},
    )
    monkeypatch.setattr(revision.op, "execute", statements.append)

    revision.upgrade()

    updates = {
        statement.compile().params["server_key"]: statement.compile().params["scopes"]
        for statement in statements
        if "server_key" in statement.compile().params
    }
    assert updates == {
        "gmail": "https://www.googleapis.com/auth/gmail.modify",
        "google_calendar": (
            "https://www.googleapis.com/auth/calendar.events,"
            "https://www.googleapis.com/auth/calendar.calendarlist.readonly,"
            "https://www.googleapis.com/auth/calendar.events.freebusy"
        ),
        "google_drive": (
            "https://www.googleapis.com/auth/drive.file,"
            "https://www.googleapis.com/auth/drive.readonly"
        ),
        "youtube": "https://www.googleapis.com/auth/youtube.force-ssl",
    }
    cleanup_sql = str(statements[-1])
    assert "default_config" in cleanup_sql
    assert "- 'oauth_scopes'" in cleanup_sql


def test_whatsapp_exact_binding_migration_fails_closed() -> None:
    migration = (
        ROOT
        / "packages/core/migrations/versions/20260901_01_whatsapp_exact_business_binding.py"
    ).read_text(encoding="utf-8")

    assert "ux_channels_active_whatsapp_config" in migration
    assert "HAVING COUNT(*) > 1" in migration
    assert "SET status = 'inactive'" in migration
    assert "channel_type = 'whatsapp'" in migration
    assert "SET agent_subscription_id = NULL" in migration
    assert "config ->> 'channel_config_id'" in migration
    assert "DROP INDEX IF EXISTS ux_channels_active_whatsapp_config" in migration


def test_channel_binding_owner_backfill_only_repairs_missing_owners(
    monkeypatch,
) -> None:
    revision = _script_directory().get_revision("20260831_02").module
    statements: list[str] = []

    monkeypatch.setattr(revision, "table_exists", lambda _table: True)
    monkeypatch.setattr(revision, "column_exists", lambda *_args: True)
    monkeypatch.setattr(
        revision.op,
        "execute",
        lambda statement: statements.append(str(statement)),
    )

    revision.upgrade()

    assert len(statements) == 1
    statement = statements[0]
    assert "UPDATE channels AS binding" in statement
    assert "binding.user_id IS NULL" in statement
    assert "config.owner_user_id IS NOT NULL" in statement
    assert "binding.entity_id = config.entity_id" in statement
    assert "binding.config ->> 'channel_config_id' = config.id" in statement


def test_document_upload_idempotency_migration_adds_receipt_and_unique_scope(
    monkeypatch,
) -> None:
    revision = _script_directory().get_revision("20260901_05").module
    added_columns: list[tuple[str, str]] = []
    indexes: list[tuple[str, str, list[str], bool, str]] = []

    monkeypatch.setattr(revision, "table_exists", lambda _table: True)
    monkeypatch.setattr(revision, "column_exists", lambda *_args: False)
    monkeypatch.setattr(revision, "index_exists", lambda _index: False)
    monkeypatch.setattr(
        revision.op,
        "add_column",
        lambda table, column: added_columns.append((table, column.name)),
    )
    monkeypatch.setattr(
        revision.op,
        "create_index",
        lambda name, table, columns, **kwargs: indexes.append((
            name,
            table,
            list(columns),
            bool(kwargs.get("unique")),
            str(kwargs.get("postgresql_where")),
        )),
    )

    revision.upgrade()

    assert added_columns == [
        ("documents", "upload_idempotency_key"),
        ("documents", "upload_request_fingerprint"),
    ]
    assert indexes == [(
        "uq_documents_upload_idempotency",
        "documents",
        ["entity_id", "owner_id", "upload_idempotency_key"],
        True,
        "upload_idempotency_key IS NOT NULL",
    )]


def test_tool_discovery_v2_flag_retirement_is_scoped_and_reversible(
    monkeypatch,
) -> None:
    revision = _script_directory().get_revision("20260902_01").module
    statements: list[sa.sql.elements.TextClause] = []

    monkeypatch.setattr(revision, "table_exists", lambda _table: True)
    monkeypatch.setattr(revision.op, "execute", statements.append)

    revision.upgrade()
    revision.downgrade()

    assert len(statements) == 2
    upgrade_sql = str(statements[0])
    downgrade_sql = str(statements[1])
    assert "status = 'archived'" in upgrade_sql
    assert "AND status = 'active'" in upgrade_sql
    assert "status = 'active'" in downgrade_sql
    assert "default_enabled = true" in downgrade_sql
    assert statements[0].compile().params["key"] == "tool_discovery_v2"
    assert statements[1].compile().params["key"] == "tool_discovery_v2"


def test_announcement_delivery_backfill_uses_only_proven_recipients(
    monkeypatch,
) -> None:
    revision = _script_directory().get_revision("20260901_04").module
    created_tables: dict[str, list[str]] = {}
    statements: list[str] = []

    monkeypatch.setattr(revision, "table_exists", lambda _table: False)
    monkeypatch.setattr(revision, "index_exists", lambda _index: False)
    monkeypatch.setattr(
        revision,
        "add_column_if_not_exists",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        revision.op,
        "create_table",
        lambda table, *columns: created_tables.setdefault(
            table,
            [
                column.name for column in columns
                if isinstance(column, sa.Column)
            ],
        ),
    )
    monkeypatch.setattr(revision.op, "create_index", lambda *_args: None)
    monkeypatch.setattr(
        revision.op,
        "execute",
        lambda statement: statements.append(str(statement)),
    )

    revision.upgrade()

    assert created_tables["platform_announcement_deliveries"] == [
        "id",
        "announcement_id",
        "delivered_at",
        "title",
        "body_md",
        "severity",
        "show_in_app",
        "show_as_banner",
        "audience_terms",
        "status",
        "recipient_count",
        "attempt_count",
        "next_attempt_at",
        "last_error",
        "completed_at",
    ]
    assert created_tables["platform_announcement_recipients"] == [
        "delivery_id", "user_id",
    ]
    backfill = "\n".join(statements)
    assert "audience.term = 'all'" in backfill
    assert "audience.term = 'user:' || users.id" in backfill
    assert "platform_announcement_dismissals" in backfill
    assert "platform_announcement_banner_dismissals" in backfill
    assert "LEFT JOIN entities" not in backfill
    assert "audience.term = 'tenant:'" not in backfill
    assert "audience.term = 'plan:'" not in backfill
    assert "audience.term = 'trial'" not in backfill


def test_announcement_delivery_migration_reconciles_existing_snapshot_table(
    monkeypatch,
) -> None:
    revision = _script_directory().get_revision("20260901_04").module
    added_columns: list[tuple[str, str]] = []

    monkeypatch.setattr(revision, "table_exists", lambda _table: True)
    monkeypatch.setattr(revision, "index_exists", lambda _index: True)
    monkeypatch.setattr(
        revision,
        "add_column_if_not_exists",
        lambda table, column: added_columns.append((table, column.name)),
    )
    monkeypatch.setattr(revision.op, "execute", lambda _statement: None)

    revision.upgrade()

    assert added_columns == [
        ("platform_announcements", "delivered_at"),
        ("platform_announcement_deliveries", "status"),
        ("platform_announcement_deliveries", "recipient_count"),
        ("platform_announcement_deliveries", "attempt_count"),
        ("platform_announcement_deliveries", "next_attempt_at"),
        ("platform_announcement_deliveries", "last_error"),
        ("platform_announcement_deliveries", "completed_at"),
    ]


def test_runtime_recovery_schema_repair_restores_all_missing_columns(
    monkeypatch,
) -> None:
    revision = _script_directory().get_revision("20260830_04").module
    added_columns: list[tuple[str, str]] = []
    indexes: list[tuple[str, str, list[str], dict]] = []
    statements: list[str] = []

    monkeypatch.setattr(revision, "table_exists", lambda _table: True)
    monkeypatch.setattr(
        revision,
        "add_column_if_not_exists",
        lambda table, column: added_columns.append((table, column.name)),
    )
    monkeypatch.setattr(
        revision,
        "create_index_if_not_exists",
        lambda name, table, columns, **kwargs: indexes.append(
            (name, table, columns, kwargs)
        ),
    )
    monkeypatch.setattr(
        revision.op,
        "execute",
        lambda statement: statements.append(str(statement)),
    )

    revision.upgrade()

    assert added_columns == [
        ("review_runs", "delivery_id"),
        ("review_runs", "lease_owner"),
        ("review_runs", "lease_expires_at"),
        ("workflow_runs", "terminal_effects_completed_at"),
        ("workflow_runs", "terminal_effects_next_attempt_at"),
        ("workspace_artifact_purge_jobs", "next_attempt_at"),
    ]
    assert {name for name, *_ in indexes} == {
        "uq_review_runs_workspace_delivery_live_or_terminal",
        "ix_workflow_runs_terminal_effects_due",
        "ix_workspace_artifact_purge_due",
    }
    assert len(statements) == 2
    assert "SET terminal_effects_next_attempt_at = CURRENT_TIMESTAMP" in statements[0]
    assert "AND terminal_effects_next_attempt_at IS NULL" in statements[0]
    assert "SET terminal_effects_completed_at = COALESCE" in statements[1]
    assert "AND terminal_effects_next_attempt_at IS NULL" in statements[1]


def test_stamped_schema_repair_replays_migrations_in_order(
    monkeypatch,
) -> None:
    revision = _script_directory().get_revision("20260830_05").module
    imported: list[str] = []
    upgraded: list[str] = []

    def fake_import(module_name: str):
        imported.append(module_name)
        return SimpleNamespace(
            upgrade=lambda: upgraded.append(module_name),
            _check_constraint_exists=lambda _table, _constraint: False,
        )

    monkeypatch.setattr(revision, "import_module", fake_import)
    monkeypatch.setattr(revision, "table_exists", lambda _table: True)
    monkeypatch.setattr(revision, "index_exists", lambda _index: False)
    monkeypatch.setattr(
        revision,
        "column_exists",
        lambda _table, _column: True,
    )

    revision.upgrade()

    assert imported == [
        "packages.core.migrations.versions.20260827_09_blueprint_checkout_attempts",
        "packages.core.migrations.versions.20260827_11_blueprint_checkout_destination",
        "packages.core.migrations.versions.20260828_01_blueprint_checkout_refunds",
        "packages.core.migrations.versions.20260828_02_blueprint_refund_allocations",
        "packages.core.migrations.versions.20260828_03_blueprint_sale_reconciliation",
        "packages.core.migrations.versions.20260828_04_blueprint_sale_recovery_claims",
        "packages.core.migrations.versions.20260828_05_blueprint_payment_recovery",
        "packages.core.migrations.versions.20260828_06_global_payment_intent_owner",
        "packages.core.migrations.versions.20260828_07_product_growth_events",
        "packages.core.migrations.versions.20260829_01_runtime_execution_claims",
    ]
    assert upgraded == imported


def test_stamped_schema_repair_skips_healthy_payment_intent_owner(
    monkeypatch,
) -> None:
    revision = _script_directory().get_revision("20260830_05").module
    upgraded: list[str] = []

    def fake_import(module_name: str):
        return SimpleNamespace(
            upgrade=lambda: upgraded.append(module_name),
            _check_constraint_exists=lambda table, constraint: (
                table == "payment_logs"
                and constraint == "ck_payment_logs_stripe_pi_nonblank"
            ),
        )

    monkeypatch.setattr(revision, "import_module", fake_import)
    monkeypatch.setattr(revision, "table_exists", lambda _table: True)
    monkeypatch.setattr(revision, "column_exists", lambda _table, _column: True)
    monkeypatch.setattr(
        revision,
        "index_exists",
        lambda index: index == "ux_payment_logs_stripe_pi_global",
    )

    revision.upgrade()

    assert revision._PAYMENT_INTENT_OWNER_REPAIR not in upgraded
    assert upgraded == [
        module_name
        for module_name in revision._STAMPED_SCHEMA_REPAIRS
        if module_name != revision._PAYMENT_INTENT_OWNER_REPAIR
    ]


def test_stamped_schema_repair_rejects_silent_drift(
    monkeypatch,
) -> None:
    revision = _script_directory().get_revision("20260830_05").module

    monkeypatch.setattr(
        revision,
        "import_module",
        lambda _module_name: SimpleNamespace(
            upgrade=lambda: None,
            _check_constraint_exists=lambda _table, _constraint: False,
        ),
    )
    monkeypatch.setattr(revision, "index_exists", lambda _index: False)
    monkeypatch.setattr(
        revision,
        "table_exists",
        lambda table: table not in {
            "blueprint_checkout_refunds",
            "product_growth_events",
            "runtime_execution_claims",
        },
    )
    monkeypatch.setattr(
        revision,
        "column_exists",
        lambda table, column: not (
            table == "blueprint_purchases" and column == "seller_entity_id"
        ),
    )

    with pytest.raises(RuntimeError) as exc_info:
        revision.upgrade()

    message = str(exc_info.value)
    assert "blueprint_purchases.seller_entity_id" in message
    assert "blueprint_checkout_refunds" in message
    assert "product_growth_events" in message
    assert "runtime_execution_claims" in message


def test_product_growth_migration_creates_fact_table_and_indexes(
    monkeypatch,
) -> None:
    revision = _script_directory().get_revision("20260828_07").module
    created_tables: list[tuple[str, tuple[object, ...]]] = []
    indexes: list[tuple[str, str, list[str], dict]] = []

    monkeypatch.setattr(revision, "table_exists", lambda _table: False)
    monkeypatch.setattr(
        revision.op,
        "create_table",
        lambda name, *columns: created_tables.append((name, columns)),
    )
    monkeypatch.setattr(
        revision,
        "create_index_if_not_exists",
        lambda name, table, columns, **kwargs: indexes.append(
            (name, table, columns, kwargs)
        ),
    )

    revision.upgrade()

    assert created_tables[0][0] == "product_growth_events"
    assert {
        column.name
        for column in created_tables[0][1]
        if getattr(column, "name", None)
    } >= {
        "entity_id",
        "workspace_id",
        "user_id",
        "milestone",
        "source_kind",
        "source_id",
        "occurred_at",
    }
    assert {name for name, *_ in indexes} == {
        "uq_product_growth_event_source",
        "ix_product_growth_user_milestone_occurred",
    }


def test_runtime_execution_claim_migration_creates_claim_table_and_index(
    monkeypatch,
) -> None:
    revision = _script_directory().get_revision("20260829_01").module
    created_tables: list[tuple[str, tuple[object, ...]]] = []
    indexes: list[tuple[str, str, list[str], dict]] = []

    monkeypatch.setattr(revision, "table_exists", lambda _table: False)
    monkeypatch.setattr(
        revision.op,
        "create_table",
        lambda name, *columns: created_tables.append((name, columns)),
    )
    monkeypatch.setattr(
        revision,
        "create_index_if_not_exists",
        lambda name, table, columns, **kwargs: indexes.append(
            (name, table, columns, kwargs)
        ),
    )

    revision.upgrade()

    assert created_tables[0][0] == "runtime_execution_claims"
    assert {
        column.name
        for column in created_tables[0][1]
        if getattr(column, "name", None)
    } >= {"claim_key", "claim_token", "expires_at"}
    assert indexes == [
        (
            "ix_runtime_execution_claims_expires",
            "runtime_execution_claims",
            ["expires_at"],
            {},
        )
    ]


def test_twilio_voice_session_ownership_migration_adds_backfills_and_indexes(
    monkeypatch,
) -> None:
    revision = _script_directory().get_revision("20260829_04").module
    operations: list[tuple[str, object]] = []

    monkeypatch.setattr(revision, "table_exists", lambda _table: True)
    monkeypatch.setattr(revision, "column_exists", lambda _table, _column: False)
    monkeypatch.setattr(revision, "index_exists", lambda _index: False)
    monkeypatch.setattr(
        revision.op,
        "add_column",
        lambda table, column: operations.append(("add_column", (table, column))),
    )
    monkeypatch.setattr(
        revision.op,
        "execute",
        lambda statement: operations.append(("execute", statement)),
    )
    monkeypatch.setattr(
        revision.op,
        "create_index",
        lambda name, table, columns: operations.append(
            ("create_index", (name, table, columns))
        ),
    )

    revision.upgrade()

    added_columns = [
        value for operation, value in operations if operation == "add_column"
    ]
    assert [column.name for _, column in added_columns] == [
        "owner_user_id",
        "workspace_id",
    ]
    assert all(table == "twilio_voice_call_sessions" for table, _ in added_columns)
    assert all(column.nullable for _, column in added_columns)

    backfill_sql = " ".join(
        str(next(value for operation, value in operations if operation == "execute")).split()
    )
    assert "FROM channel_configs AS config" in backfill_sql
    assert "config.id = session.channel_config_id" in backfill_sql
    assert "owner_user_id = COALESCE(" in backfill_sql
    assert "session.owner_user_id, config.owner_user_id" in backfill_sql
    assert "workspace_id = COALESCE(" in backfill_sql
    assert "session.workspace_id, config.workspace_id" in backfill_sql

    created_indexes = [
        value for operation, value in operations if operation == "create_index"
    ]
    assert created_indexes == [
        (
            "ix_twilio_voice_call_sessions_owner_user_id",
            "twilio_voice_call_sessions",
            ["owner_user_id"],
        ),
        (
            "ix_twilio_voice_call_sessions_workspace_id",
            "twilio_voice_call_sessions",
            ["workspace_id"],
        ),
    ]


def test_twilio_voice_session_ownership_migration_downgrades_in_dependency_order(
    monkeypatch,
) -> None:
    revision = _script_directory().get_revision("20260829_04").module
    operations: list[tuple[str, str]] = []

    monkeypatch.setattr(revision, "table_exists", lambda _table: True)
    monkeypatch.setattr(revision, "column_exists", lambda _table, _column: True)
    monkeypatch.setattr(revision, "index_exists", lambda _index: True)
    monkeypatch.setattr(
        revision.op,
        "drop_index",
        lambda name, **_kwargs: operations.append(("drop_index", name)),
    )
    monkeypatch.setattr(
        revision.op,
        "drop_column",
        lambda _table, column: operations.append(("drop_column", column)),
    )

    revision.downgrade()

    assert operations == [
        ("drop_index", "ix_twilio_voice_call_sessions_workspace_id"),
        ("drop_index", "ix_twilio_voice_call_sessions_owner_user_id"),
        ("drop_column", "workspace_id"),
        ("drop_column", "owner_user_id"),
    ]


def test_discord_installation_columns_and_unique_indexes() -> None:
    from packages.core.models.channel import ChannelConfig, MessageLog

    assert "discord_application_id" in ChannelConfig.__table__.columns
    assert "discord_guild_id" in ChannelConfig.__table__.columns
    assert "whatsapp_phone_number_id" in ChannelConfig.__table__.columns
    channel_indexes = {index.name: index for index in ChannelConfig.__table__.indexes}
    assert set(
        channel_indexes["ux_channel_configs_whatsapp_phone_number_id"].columns.keys()
    ) == {"whatsapp_phone_number_id"}
    assert set(
        channel_indexes["ux_channel_configs_discord_installation"].columns.keys()
    ) == {"discord_application_id", "discord_guild_id"}
    message_indexes = {index.name for index in MessageLog.__table__.indexes}
    assert "uq_message_logs_discord_inbound_interaction" in message_indexes
    assert "uq_message_logs_whatsapp_inbound_event" in message_indexes
    assert "uq_message_logs_wechat_personal_inbound_message" in message_indexes


def test_whatsapp_inbound_receipt_migration_deduplicates_before_adding_index(monkeypatch) -> None:
    revision = _script_directory().get_revision("20260826_01").module
    operations: list[tuple[str, object]] = []

    monkeypatch.setattr(revision, "table_exists", lambda table: table == "message_logs")
    monkeypatch.setattr(revision, "index_exists", lambda _name: False)
    monkeypatch.setattr(revision.op, "execute", lambda statement: operations.append(("execute", statement)))
    monkeypatch.setattr(
        revision.op,
        "create_index",
        lambda name, table, columns, **kwargs: operations.append(
            ("create_index", (name, table, columns, kwargs))
        ),
    )

    revision.upgrade()

    assert "channel_type = 'whatsapp'" in str(operations[0][1])
    assert operations[1] == (
        "create_index",
        (
            "uq_message_logs_whatsapp_inbound_event",
            "message_logs",
            ["channel_config_id", "external_id"],
            {
                "unique": True,
                "postgresql_where": ANY,
            },
        ),
    )


def test_response_surface_submission_migration_adds_durable_unique_event_index(
    monkeypatch,
) -> None:
    revision = _script_directory().get_revision("20260827_05").module
    added_columns: list[tuple[str, object]] = []
    indexes: list[tuple[str, str, list[str], dict]] = []

    monkeypatch.setattr(revision, "table_exists", lambda table: table == "messages")
    monkeypatch.setattr(revision, "column_exists", lambda _table, _column: False)
    monkeypatch.setattr(revision, "index_exists", lambda _index: False)
    monkeypatch.setattr(
        revision.op,
        "add_column",
        lambda table, column: added_columns.append((table, column)),
    )
    monkeypatch.setattr(
        revision.op,
        "create_index",
        lambda name, table, columns, **kwargs: indexes.append(
            (name, table, columns, kwargs)
        ),
    )

    revision.upgrade()

    assert added_columns[0][0] == "messages"
    assert added_columns[0][1].name == "response_surface_event_id"
    assert indexes == [(
        "uq_messages_conversation_response_surface_event",
        "messages",
        ["conversation_id", "response_surface_event_id"],
        {"unique": True, "postgresql_where": ANY},
    )]


def test_latest_schema_repair_replays_idempotent_runtime_and_discord_repairs(
    monkeypatch,
) -> None:
    revision = _script_directory().get_revision("20260827_06").module
    calls: list[str] = []

    class _Repair:
        def __init__(self, name: str) -> None:
            self.name = name

        def upgrade(self) -> None:
            calls.append(self.name)

    monkeypatch.setattr(
        revision,
        "import_module",
        lambda name: _Repair(name),
    )

    revision.upgrade()

    assert calls == list(revision._REPAIR_MODULES)


def test_renumbered_chain_merge_reconciles_reused_channel_revisions(
    monkeypatch,
) -> None:
    revision = _script_directory().get_revision("20260827_10").module
    calls: list[str] = []

    monkeypatch.setattr(
        revision,
        "import_module",
        lambda name: SimpleNamespace(upgrade=lambda: calls.append(name)),
    )

    revision.upgrade()

    assert calls == list(revision._CHANNEL_REPAIR_MODULES)


def test_artifact_cleanup_path_downgrade_preserves_widened_column(monkeypatch) -> None:
    revision = _script_directory().get_revision("20260827_07").module
    alterations = []

    monkeypatch.setattr(
        revision.op,
        "alter_column",
        lambda *args, **kwargs: alterations.append((args, kwargs)),
    )

    revision.downgrade()

    assert alterations == []


def test_external_event_delivery_migration_adds_durable_retry_state(
    monkeypatch,
) -> None:
    revision = _script_directory().get_revision("20260827_08").module
    added_columns: list[tuple[str, object]] = []
    indexes: list[tuple[str, str, list[str], dict]] = []

    monkeypatch.setattr(revision, "table_exists", lambda table: table == "event_logs")
    monkeypatch.setattr(revision, "add_column_if_not_exists", lambda table, column: added_columns.append((table, column)))
    monkeypatch.setattr(
        revision,
        "create_index_if_not_exists",
        lambda name, table, columns, **kwargs: indexes.append(
            (name, table, columns, kwargs)
        ),
    )

    revision.upgrade()

    assert [column.name for _, column in added_columns] == [
        "workspace_id",
        "external_delivery_status",
        "external_delivery_attempt_count",
        "external_delivery_available_at",
        "external_delivery_locked_until",
        "external_delivery_claim_token",
        "external_delivery_delivered_at",
        "external_delivery_completed_sinks",
        "external_delivery_last_error",
    ]
    assert indexes == [
        (
            "ix_event_external_delivery_due",
            "event_logs",
            ["external_delivery_status", "external_delivery_available_at"],
            {"postgresql_where": ANY},
        ),
        (
            "ix_event_external_delivery_lease",
            "event_logs",
            ["external_delivery_status", "external_delivery_locked_until"],
            {"postgresql_where": ANY},
        ),
    ]


def test_blueprint_checkout_attempt_migration_adds_financial_evidence(
    monkeypatch,
) -> None:
    revision = _script_directory().get_revision("20260827_09").module
    created_tables: list[tuple[str, tuple[object, ...]]] = []
    added_columns: list[tuple[str, object]] = []
    indexes: list[tuple[str, str, list[str], dict]] = []

    monkeypatch.setattr(
        revision,
        "table_exists",
        lambda table: table == "blueprint_purchases",
    )
    monkeypatch.setattr(
        revision.op,
        "create_table",
        lambda name, *columns: created_tables.append((name, columns)),
    )
    monkeypatch.setattr(
        revision,
        "add_column_if_not_exists",
        lambda table, column: added_columns.append((table, column)),
    )
    monkeypatch.setattr(
        revision,
        "create_index_if_not_exists",
        lambda name, table, columns, **kwargs: indexes.append(
            (name, table, columns, kwargs)
        ),
    )

    revision.upgrade()

    assert created_tables[0][0] == "blueprint_checkout_attempts"
    attempt_columns = {
        column.name
        for column in created_tables[0][1]
        if getattr(column, "name", None)
    }
    assert {
        "purchase_id",
        "stripe_checkout_session_id",
        "amount_cents",
        "platform_fee_cents",
        "payload_snapshot",
        "blueprint_content_version",
        "status",
    } <= attempt_columns
    assert [column.name for _, column in added_columns] == [
        "stripe_dispute_id",
        "stripe_dispute_status",
        "disputed_amount_cents",
        "dispute_funds_reinstated",
        "last_dispute_event_id",
        "last_dispute_event_created_at",
    ]
    assert {name for name, *_ in indexes} == {
        "ix_blueprint_checkout_attempts_purchase",
        "ix_blueprint_checkout_attempts_blueprint",
        "ux_blueprint_checkout_attempts_session",
        "ix_blueprint_purchases_payment_intent",
    }


def test_blueprint_checkout_destination_migration_freezes_connect_account(
    monkeypatch,
) -> None:
    revision = _script_directory().get_revision("20260827_11").module
    added_columns: list[tuple[str, object]] = []

    monkeypatch.setattr(revision, "table_exists", lambda table: table == "blueprint_checkout_attempts")
    monkeypatch.setattr(
        revision,
        "add_column_if_not_exists",
        lambda table, column: added_columns.append((table, column)),
    )

    revision.upgrade()

    assert [(table, column.name) for table, column in added_columns] == [
        ("blueprint_checkout_attempts", "stripe_destination_account_id"),
        ("blueprint_checkout_attempts", "stripe_checkout_success_url"),
        ("blueprint_checkout_attempts", "stripe_checkout_cancel_url"),
    ]


def test_blueprint_dispute_migration_refuses_unsafe_downgrade(monkeypatch) -> None:
    revision = _script_directory().get_revision("20260827_09").module
    bind = SimpleNamespace(
        execute=lambda _statement: SimpleNamespace(scalar=lambda: True),
    )
    monkeypatch.setattr(revision, "table_exists", lambda _table: True)
    monkeypatch.setattr(revision.op, "get_bind", lambda: bind)

    with pytest.raises(RuntimeError, match="disputes are unresolved"):
        revision.downgrade()


def test_blueprint_refund_migration_adds_seller_snapshot_and_job(
    monkeypatch,
) -> None:
    revision = _script_directory().get_revision("20260828_01").module
    added_columns: list[tuple[str, object]] = []
    created_tables: list[tuple[str, tuple[object, ...]]] = []
    indexes: list[tuple[str, str, list[str], dict]] = []
    statements: list[str] = []

    monkeypatch.setattr(
        revision,
        "table_exists",
        lambda table: table != "blueprint_checkout_refunds",
    )
    monkeypatch.setattr(
        revision,
        "add_column_if_not_exists",
        lambda table, column: added_columns.append((table, column)),
    )
    monkeypatch.setattr(
        revision,
        "create_index_if_not_exists",
        lambda name, table, columns, **kwargs: indexes.append(
            (name, table, columns, kwargs)
        ),
    )
    monkeypatch.setattr(
        revision.op,
        "create_table",
        lambda name, *columns: created_tables.append((name, columns)),
    )
    monkeypatch.setattr(
        revision.op,
        "execute",
        lambda statement: statements.append(str(statement)),
    )

    revision.upgrade()

    assert [(table, column.name) for table, column in added_columns] == [
        ("blueprint_purchases", "seller_entity_id"),
        ("blueprint_purchases", "stripe_destination_account_id"),
        ("blueprint_checkout_attempts", "seller_entity_id"),
    ]
    assert created_tables[0][0] == "blueprint_checkout_refunds"
    refund_columns = {
        column.name
        for column in created_tables[0][1]
        if getattr(column, "name", None)
    }
    assert {
        "stripe_payment_intent_id",
        "stripe_checkout_session_id",
        "refund_evidence",
        "claim_token",
        "next_attempt_at",
    } <= refund_columns
    assert "ux_blueprint_checkout_refunds_payment_intent" in {
        name for name, *_ in indexes
    }
    assert any("seller_entity_id" in statement for statement in statements)


def test_blueprint_refund_migration_refuses_unresolved_downgrade(
    monkeypatch,
) -> None:
    revision = _script_directory().get_revision("20260828_01").module
    bind = SimpleNamespace(
        execute=lambda _statement: SimpleNamespace(scalar=lambda: True),
    )
    monkeypatch.setattr(revision, "table_exists", lambda _table: True)
    monkeypatch.setattr(revision.op, "get_bind", lambda: bind)

    with pytest.raises(RuntimeError, match="refunds need reconciliation"):
        revision.downgrade()


def test_blueprint_refund_allocation_migration_adds_independent_ledgers(
    monkeypatch,
) -> None:
    revision = _script_directory().get_revision("20260828_02").module
    added_columns: list[tuple[str, object]] = []
    indexes: list[tuple[str, str, list[str], dict]] = []
    statements: list[str] = []

    monkeypatch.setattr(revision, "table_exists", lambda _table: True)
    monkeypatch.setattr(
        revision,
        "add_column_if_not_exists",
        lambda table, column: added_columns.append((table, column)),
    )
    monkeypatch.setattr(
        revision,
        "create_index_if_not_exists",
        lambda name, table, columns, **kwargs: indexes.append(
            (name, table, columns, kwargs)
        ),
    )
    monkeypatch.setattr(
        revision.op,
        "execute",
        lambda statement: statements.append(str(statement)),
    )

    revision.upgrade()

    assert [(table, column.name) for table, column in added_columns] == [
        ("blueprint_purchases", "seller_recovery_attempted_at"),
        ("blueprint_checkout_refunds", "platform_fee_cents"),
        ("blueprint_checkout_refunds", "transfer_reversed_amount_cents"),
        (
            "blueprint_checkout_refunds",
            "platform_fee_refunded_amount_cents",
        ),
    ]
    assert {name for name, *_ in indexes} == {
        "ix_blueprint_purchases_seller_recovery",
        "ix_blueprint_checkout_refunds_session_buyer",
    }
    assert len(statements) == 2


def test_blueprint_sale_reconciliation_migration_adds_rotation_cursor(
    monkeypatch,
) -> None:
    revision = _script_directory().get_revision("20260828_03").module
    added_columns: list[tuple[str, object]] = []
    indexes: list[tuple[str, str, list[str], dict]] = []

    monkeypatch.setattr(revision, "table_exists", lambda _table: True)
    monkeypatch.setattr(
        revision,
        "add_column_if_not_exists",
        lambda table, column: added_columns.append((table, column)),
    )
    monkeypatch.setattr(
        revision,
        "create_index_if_not_exists",
        lambda name, table, columns, **kwargs: indexes.append(
            (name, table, columns, kwargs)
        ),
    )

    revision.upgrade()

    assert [(table, column.name) for table, column in added_columns] == [
        ("blueprint_purchases", "allocation_recovery_attempted_at"),
    ]
    assert indexes == [(
        "ix_blueprint_purchases_allocation_recovery",
        "blueprint_purchases",
        ["seller_entity_id", "allocation_recovery_attempted_at"],
        {},
    )]


def test_blueprint_sale_recovery_migration_adds_fenced_retry_state(
    monkeypatch,
) -> None:
    revision = _script_directory().get_revision("20260828_04").module
    added_columns: list[tuple[str, object]] = []
    indexes: list[tuple[str, str, list[str], dict]] = []

    monkeypatch.setattr(revision, "table_exists", lambda _table: True)
    monkeypatch.setattr(
        revision,
        "add_column_if_not_exists",
        lambda table, column: added_columns.append((table, column)),
    )
    monkeypatch.setattr(
        revision,
        "create_index_if_not_exists",
        lambda name, table, columns, **kwargs: indexes.append(
            (name, table, columns, kwargs)
        ),
    )

    revision.upgrade()

    assert [column.name for _, column in added_columns] == [
        "seller_recovery_next_attempt_at",
        "seller_recovery_claim_token",
        "seller_recovery_claim_expires_at",
        "seller_recovery_retry_count",
        "seller_recovery_last_error",
    ]
    assert indexes == [(
        "ix_blueprint_purchases_seller_recovery_due",
        "blueprint_purchases",
        [
            "seller_entity_id",
            "seller_recovery_next_attempt_at",
            "seller_recovery_claim_expires_at",
        ],
        {},
    )]


def test_blueprint_payment_recovery_migration_adds_owner_and_allocation_claims(
    monkeypatch,
) -> None:
    revision = _script_directory().get_revision("20260828_05").module
    added_columns: list[tuple[str, object]] = []
    indexes: list[tuple[str, str, list[str], dict]] = []

    monkeypatch.setattr(revision, "table_exists", lambda _table: True)
    monkeypatch.setattr(
        revision,
        "add_column_if_not_exists",
        lambda table, column: added_columns.append((table, column)),
    )
    monkeypatch.setattr(
        revision,
        "create_index_if_not_exists",
        lambda name, table, columns, **kwargs: indexes.append(
            (name, table, columns, kwargs)
        ),
    )

    revision.upgrade()

    assert [(table, column.name) for table, column in added_columns] == [
        ("blueprint_checkout_attempts", "stripe_payment_intent_id"),
        ("blueprint_purchases", "allocation_recovery_next_attempt_at"),
        ("blueprint_purchases", "allocation_recovery_claim_token"),
        ("blueprint_purchases", "allocation_recovery_claim_expires_at"),
        ("blueprint_purchases", "allocation_recovery_retry_count"),
        ("blueprint_purchases", "allocation_recovery_last_error"),
    ]
    assert indexes[0][0:3] == (
        "ux_blueprint_checkout_attempts_payment_intent",
        "blueprint_checkout_attempts",
        ["stripe_payment_intent_id"],
    )
    assert indexes[0][3]["unique"] is True
    assert indexes[1] == (
        "ix_blueprint_purchases_allocation_recovery_due",
        "blueprint_purchases",
        [
            "seller_entity_id",
            "allocation_recovery_next_attempt_at",
            "allocation_recovery_claim_expires_at",
        ],
        {},
    )


def test_payment_log_payment_intent_owner_is_globally_unique() -> None:
    from packages.core.models.billing import PaymentLog

    indexes = {index.name: index for index in PaymentLog.__table__.indexes}
    owner_index = indexes["ux_payment_logs_stripe_pi_global"]
    constraints = {
        constraint.name: constraint
        for constraint in PaymentLog.__table__.constraints
    }

    assert owner_index.unique is True
    assert list(owner_index.columns.keys()) == ["stripe_payment_intent_id"]
    assert "stripe_payment_intent_id ~ '^pi_[A-Za-z0-9_]+$'" in str(
        constraints["ck_payment_logs_stripe_pi_nonblank"].sqltext
    )


def test_payment_intent_owner_migration_quarantines_all_conflicts(
    monkeypatch,
) -> None:
    revision = _script_directory().get_revision("20260828_06").module
    statements: list[str] = []
    dropped: list[tuple[str, str]] = []
    indexes: list[tuple[str, str, list[str], dict]] = []
    constraints: list[tuple[str, str, str]] = []

    monkeypatch.setattr(revision, "table_exists", lambda table: table == "payment_logs")
    monkeypatch.setattr(
        revision,
        "index_exists",
        lambda name: name in {
            "ux_payment_logs_entity_stripe_pi",
            "ix_payment_logs_stripe_pi",
        },
    )
    monkeypatch.setattr(
        revision,
        "_check_constraint_exists",
        lambda _table, _name: False,
    )
    monkeypatch.setattr(
        revision.op,
        "execute",
        lambda statement: statements.append(str(statement)),
    )
    monkeypatch.setattr(
        revision.op,
        "drop_index",
        lambda name, table_name: dropped.append((name, table_name)),
    )
    monkeypatch.setattr(
        revision.op,
        "create_index",
        lambda name, table, columns, **kwargs: indexes.append(
            (name, table, columns, kwargs)
        ),
    )
    monkeypatch.setattr(
        revision.op,
        "create_check_constraint",
        lambda name, table, condition: constraints.append(
            (name, table, condition)
        ),
    )

    revision.upgrade()

    assert statements[0] == "LOCK TABLE payment_logs IN SHARE ROW EXCLUSIVE MODE"
    assert "stripe_payment_intent_id !~ '^pi_[A-Za-z0-9_]+$'" in statements[1]
    assert "invalidStripePaymentIntentId" in statements[1]
    assert "HAVING count(*) > 1" in statements[2]
    assert "GROUP BY stripe_payment_intent_id" in statements[2]
    assert "conflictingStripePaymentIntentId" in statements[2]
    assert "stripe_payment_intent_id = NULL" in statements[2]
    assert constraints == [(
        "ck_payment_logs_stripe_pi_nonblank",
        "payment_logs",
        "stripe_payment_intent_id IS NULL "
        "OR stripe_payment_intent_id ~ '^pi_[A-Za-z0-9_]+$'",
    )]
    assert dropped == [
        ("ux_payment_logs_entity_stripe_pi", "payment_logs"),
        ("ix_payment_logs_stripe_pi", "payment_logs"),
    ]
    assert indexes == [(
        "ux_payment_logs_stripe_pi_global",
        "payment_logs",
        ["stripe_payment_intent_id"],
        {"unique": True, "postgresql_where": ANY},
    )]


def test_billing_refund_migration_expands_audit_ids_and_indexes_due_jobs(
    monkeypatch,
) -> None:
    revision = _script_directory().get_revision("20260827_04").module
    altered_columns: list[tuple[str, str, dict]] = []
    statements: list[str] = []

    monkeypatch.setattr(revision, "table_exists", lambda _table: True)
    monkeypatch.setattr(revision, "index_exists", lambda _index: False)
    monkeypatch.setattr(
        revision.op,
        "alter_column",
        lambda table, column, **kwargs: altered_columns.append(
            (table, column, kwargs)
        ),
    )
    monkeypatch.setattr(
        revision.op,
        "execute",
        lambda statement: statements.append(str(statement)),
    )

    revision.upgrade()

    assert altered_columns[0][:2] == ("audit_log", "resource_id")
    assert altered_columns[0][2]["type_"].length == 255
    sql = "\n".join(statements)
    assert "ix_payment_logs_duplicate_refund_due" in sql
    assert "duplicateRefundNextAttemptAt" in sql
    assert "duplicate_subscription_checkout" in sql
    assert "duplicateSubscriptionCancellationStatus" in sql
    assert "duplicateRefundRequirement" in sql
    assert "duplicateCreatorRewardReversalStatus" in sql
    assert "plan-duplicate-cancel:migrated:" in sql
    assert "to_jsonb(CURRENT_TIMESTAMP)" in sql
    assert "metadata ? 'duplicateRefundNextAttemptAt'" in sql
    assert "status <> 'refunded'" not in sql


def test_billing_refund_migration_refuses_lossy_audit_id_downgrade(
    monkeypatch,
) -> None:
    revision = _script_directory().get_revision("20260827_04").module

    monkeypatch.setattr(revision, "table_exists", lambda _table: True)
    monkeypatch.setattr(revision, "index_exists", lambda _index: False)
    monkeypatch.setattr(revision, "_has_long_audit_resource_ids", lambda: True)

    with pytest.raises(RuntimeError, match="longer than 26"):
        revision.downgrade()


@pytest.mark.asyncio
async def test_billing_refund_legacy_backfill_sql_executes(
    db_session,
    monkeypatch,
) -> None:
    from packages.core.models.base import generate_ulid
    from packages.core.models.billing import PaymentLog
    from packages.core.models.user import Entity

    revision = _script_directory().get_revision("20260827_04").module
    statements: list[str] = []
    entity = Entity(id=generate_ulid(), name="Legacy Refund Migration Org")
    pending = PaymentLog(
        id=generate_ulid(),
        entity_id=entity.id,
        amount=4900,
        currency="usd",
        status="succeeded",
        event_type="checkout.session.completed",
        stripe_payment_intent_id="pi_legacy_migration_pending",
        meta={
            "source": "duplicate_subscription_checkout",
            "stripeRefundStatus": "pending",
            "duplicateRefunded": False,
        },
    )
    completed = PaymentLog(
        id=generate_ulid(),
        entity_id=entity.id,
        amount=4900,
        currency="usd",
        status="refunded",
        event_type="checkout.session.completed",
        stripe_payment_intent_id="pi_legacy_migration_completed",
        meta={
            "source": "duplicate_subscription_checkout",
            "stripeRefundStatus": "succeeded",
            "duplicateRefunded": True,
        },
    )
    db_session.add_all([entity, pending, completed])
    await db_session.flush()

    monkeypatch.setattr(revision, "table_exists", lambda _table: True)
    monkeypatch.setattr(revision, "index_exists", lambda _index: True)
    monkeypatch.setattr(revision.op, "alter_column", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        revision.op,
        "execute",
        lambda statement: statements.append(str(statement)),
    )

    revision.upgrade()

    updates = [statement for statement in statements if statement.startswith("UPDATE")]
    assert len(updates) == 3
    for statement in updates:
        await db_session.execute(sa.text(statement))
    db_session.expire_all()
    await db_session.refresh(pending)
    await db_session.refresh(completed)

    assert pending.meta["duplicateSubscriptionCancellationStatus"] == "succeeded"
    assert pending.meta["duplicateRefundRequirement"] == "required"
    assert pending.meta["duplicateRefundPaymentIntentId"] == (
        "pi_legacy_migration_pending"
    )
    assert pending.meta["duplicateSubscriptionCancellationKey"] == (
        f"plan-duplicate-cancel:migrated:{pending.id}"
    )
    assert "duplicateRefundNextAttemptAt" in pending.meta
    assert pending.meta["duplicateCreatorRewardReversalStatus"] == "pending"
    assert completed.meta["duplicateSubscriptionCancellationStatus"] == "succeeded"
    assert completed.meta["duplicateRefundRequirement"] == "required"
    assert completed.meta["duplicateCreatorRewardReversalStatus"] == "pending"
    assert "duplicateRefundNextAttemptAt" in completed.meta
    await db_session.rollback()


def test_telegram_channel_migration_adds_a_unique_bot_identity(monkeypatch) -> None:
    revision = _script_directory().get_revision("20260825_01").module
    operations: list[tuple[str, object]] = []

    monkeypatch.setattr(revision, "table_exists", lambda table: table == "channel_configs")
    monkeypatch.setattr(
        revision,
        "add_column_if_not_exists",
        lambda table, column: operations.append(("add_column", (table, column))),
    )
    monkeypatch.setattr(revision, "index_exists", lambda _name: False)
    monkeypatch.setattr(
        revision.op,
        "create_index",
        lambda name, table, columns, **kwargs: operations.append(
            ("create_index", (name, table, columns, kwargs))
        ),
    )

    revision.upgrade()

    assert operations[0][0] == "add_column"
    table, column = operations[0][1]
    assert table == "channel_configs"
    assert column.name == "telegram_bot_id"
    index_name, table, columns, kwargs = operations[1][1]
    assert index_name == "ux_channel_configs_telegram_bot_id"
    assert table == "channel_configs"
    assert columns == ["telegram_bot_id"]
    assert kwargs["unique"] is True


def test_whatsapp_route_migration_skips_existing_route_collisions(monkeypatch) -> None:
    revision = _script_directory().get_revision("20260826_02").module
    operations: list[tuple[str, object]] = []

    monkeypatch.setattr(revision, "table_exists", lambda table: table == "channel_configs")
    monkeypatch.setattr(
        revision,
        "add_column_if_not_exists",
        lambda table, column: operations.append(("add_column", (table, column))),
    )
    monkeypatch.setattr(revision, "index_exists", lambda _name: False)
    monkeypatch.setattr(
        revision.op,
        "execute",
        lambda statement: operations.append(("execute", statement)),
    )
    monkeypatch.setattr(
        revision.op,
        "create_index",
        lambda name, table, columns, **kwargs: operations.append(
            ("create_index", (name, table, columns, kwargs))
        ),
    )

    revision.upgrade()

    backfill_sql = str(next(value for kind, value in operations if kind == "execute"))
    assert "HAVING COUNT(*) = 1" in backfill_sql
    assert "NOT EXISTS" in backfill_sql
    assert "existing.whatsapp_phone_number_id" in backfill_sql


def test_workspace_membership_dedupe_prefers_unexpired_active_rows(monkeypatch) -> None:
    revision = _script_directory().get_revision("20260824_10").module
    statements: list[str] = []

    monkeypatch.setattr(revision, "table_exists", lambda _table: True)
    monkeypatch.setattr(revision, "index_exists", lambda _index: False)
    monkeypatch.setattr(
        revision.op,
        "execute",
        lambda statement: statements.append(str(statement)),
    )
    monkeypatch.setattr(revision.op, "create_index", lambda *_args, **_kwargs: None)

    revision.upgrade()

    sql = "\n".join(statements)
    assert "status = 'active' AND (expires_at IS NULL OR expires_at > CURRENT_TIMESTAMP)" in sql


def test_creator_partial_reversal_migration_backfills_historical_reversals(
    monkeypatch,
) -> None:
    revision = _script_directory().get_revision("20260824_08").module
    added_columns: list[tuple[str, sa.Column]] = []
    altered_columns: list[tuple[str, str, dict]] = []
    executed: list[str] = []

    monkeypatch.setattr(revision, "table_exists", lambda _table: True)
    monkeypatch.setattr(
        revision,
        "add_column_if_not_exists",
        lambda table, column: added_columns.append((table, column)),
    )
    monkeypatch.setattr(
        revision.op,
        "alter_column",
        lambda table, column, **kwargs: altered_columns.append(
            (table, column, kwargs)
        ),
    )
    monkeypatch.setattr(
        revision.op,
        "execute",
        lambda statement: executed.append(str(statement)),
    )

    revision.upgrade()

    assert [(table, column.name) for table, column in added_columns] == [
        ("creator_payout_attempts", "amount_reversed_cents"),
    ]
    assert altered_columns[0][:2] == ("creator_reward_entries", "status")
    sql = "\n".join(executed).lower()
    assert "set amount_reversed_cents = amount_cash_cents" in sql
    assert "status = 'reversed'" in sql


def test_creator_partial_reversal_migration_refuses_unsafe_downgrade(
    monkeypatch,
) -> None:
    revision = _script_directory().get_revision("20260824_08").module
    dropped_columns: list[tuple[str, str]] = []

    class PartialReversalResult:
        @staticmethod
        def scalar() -> bool:
            return True

    monkeypatch.setattr(revision, "table_exists", lambda _table: True)
    monkeypatch.setattr(
        revision.op,
        "get_bind",
        lambda: SimpleNamespace(
            execute=lambda _statement: PartialReversalResult()
        ),
    )
    monkeypatch.setattr(
        revision,
        "drop_column_if_exists",
        lambda table, column: dropped_columns.append((table, column)),
    )

    with pytest.raises(RuntimeError, match="partially reversed Creator payouts"):
        revision.downgrade()

    assert dropped_columns == []


def test_task_status_changed_at_compat_migration_repairs_existing_databases(
    monkeypatch,
) -> None:
    revision = _script_directory().get_revision("20260823_08").module
    added_columns: list[tuple[str, sa.Column]] = []
    executed: list[str] = []

    monkeypatch.setattr(
        revision,
        "add_column_if_not_exists",
        lambda table, column: added_columns.append((table, column)),
    )
    monkeypatch.setattr(revision, "table_exists", lambda _table: True)
    monkeypatch.setattr(
        revision,
        "column_exists",
        lambda _table, _column: True,
    )
    monkeypatch.setattr(
        revision.op,
        "execute",
        lambda statement: executed.append(str(statement)),
    )

    revision.upgrade()

    assert [(table, column.name) for table, column in added_columns] == [
        ("tasks", "status_changed_at"),
    ]
    assert "COALESCE(updated_at, created_at)" in "\n".join(executed)


def test_chat_feedback_lifecycle_migration_adds_cascading_foreign_keys(
    monkeypatch,
) -> None:
    revision = _script_directory().get_revision("20260823_09").module
    executed: list[str] = []
    created: list[tuple] = []
    created_indexes: list[tuple[str, str, list[str]]] = []

    monkeypatch.setattr(revision, "table_exists", lambda _table: True)
    monkeypatch.setattr(
        revision,
        "foreign_key_exists",
        lambda _table, _constraint: False,
    )
    monkeypatch.setattr(
        revision.op,
        "execute",
        lambda statement: executed.append(str(statement)),
    )
    monkeypatch.setattr(
        revision.op,
        "create_foreign_key",
        lambda *args, **kwargs: created.append((*args, kwargs)),
    )
    monkeypatch.setattr(
        revision,
        "create_index_if_not_exists",
        lambda name, table, columns: created_indexes.append(
            (name, table, columns)
        ),
    )

    revision.upgrade()

    assert "DELETE FROM chat_message_feedback" in "\n".join(executed)
    assert [item[0] for item in created] == [
        "fk_chat_feedback_user",
        "fk_chat_feedback_conversation",
        "fk_chat_feedback_message",
    ]
    assert all(item[-1]["ondelete"] == "CASCADE" for item in created)
    assert created_indexes == [
        ("ix_chat_feedback_user", "chat_message_feedback", ["user_id"]),
    ]


def test_document_comment_resource_type_migration_canonicalizes_aliases(
    monkeypatch,
) -> None:
    revision = _script_directory().get_revision("20260824_03").module
    executed: list[str] = []

    monkeypatch.setattr(revision, "table_exists", lambda _table: True)
    monkeypatch.setattr(
        revision.op,
        "execute",
        lambda statement: executed.append(str(statement)),
    )

    revision.upgrade()

    sql = "\n".join(executed).lower()
    assert "update comments" in sql
    assert "lower(trim(resource_type))" in sql
    assert "set resource_type = 'document'" in sql


def test_document_comment_resource_index_migration_is_nonblocking(
    monkeypatch,
) -> None:
    revision = _script_directory().get_revision("20260824_05").module
    executed: list[str] = []
    catalog_queries: list[tuple[str, dict[str, str]]] = []
    autocommit_events: list[str] = []

    class InvalidIndexResult:
        @staticmethod
        def scalar_one_or_none() -> bool:
            return True

    class Bind:
        @staticmethod
        def execute(statement, params):
            catalog_queries.append((str(statement), params))
            return InvalidIndexResult()

    @contextmanager
    def autocommit_block():
        autocommit_events.append("enter")
        yield
        autocommit_events.append("exit")

    monkeypatch.setattr(revision, "table_exists", lambda _table: True)
    monkeypatch.setattr(
        revision.op,
        "get_context",
        lambda: SimpleNamespace(autocommit_block=autocommit_block),
    )
    monkeypatch.setattr(revision.op, "get_bind", lambda: Bind())
    monkeypatch.setattr(
        revision.op,
        "execute",
        lambda statement: executed.append(str(statement)),
    )

    revision.upgrade()
    revision.downgrade()

    sql = "\n".join(executed).lower()
    assert autocommit_events == ["enter", "exit", "enter", "exit"]
    assert len(catalog_queries) == 1
    catalog_sql, catalog_params = catalog_queries[0]
    assert "pg_catalog.pg_index" in catalog_sql
    assert "indisvalid" in catalog_sql
    assert catalog_params == {"index_name": "ix_comments_resource_normalized"}
    assert "create index concurrently if not exists" in sql
    assert sql.count("drop index concurrently if exists") == 2
    assert executed[0].strip().lower().startswith("drop index concurrently")
    assert executed[1].strip().lower().startswith("create index concurrently")


def test_document_comment_resource_index_migration_keeps_valid_index(
    monkeypatch,
) -> None:
    revision = _script_directory().get_revision("20260824_05").module
    executed: list[str] = []

    class ValidIndexResult:
        @staticmethod
        def scalar_one_or_none() -> bool:
            return False

    @contextmanager
    def autocommit_block():
        yield

    monkeypatch.setattr(revision, "table_exists", lambda _table: True)
    monkeypatch.setattr(
        revision.op,
        "get_context",
        lambda: SimpleNamespace(autocommit_block=autocommit_block),
    )
    monkeypatch.setattr(
        revision.op,
        "get_bind",
        lambda: SimpleNamespace(
            execute=lambda _statement, _params: ValidIndexResult()
        ),
    )
    monkeypatch.setattr(
        revision.op,
        "execute",
        lambda statement: executed.append(str(statement)),
    )

    revision.upgrade()

    assert len(executed) == 1
    assert "create index concurrently if not exists" in executed[0].lower()
    assert "drop index" not in executed[0].lower()


def test_chat_feedback_subject_migration_normalizes_completion_targets(
    monkeypatch,
) -> None:
    revision = _script_directory().get_revision("20260824_04").module
    added_columns: list[tuple[str, sa.Column]] = []
    executed: list[str] = []
    altered_columns: list[tuple[str, str, dict]] = []
    created_indexes: list[tuple[str, str, list[str], bool]] = []

    monkeypatch.setattr(revision, "table_exists", lambda _table: True)
    monkeypatch.setattr(
        revision,
        "add_column_if_not_exists",
        lambda table, column: added_columns.append((table, column)),
    )
    monkeypatch.setattr(
        revision.op,
        "execute",
        lambda statement: executed.append(str(statement)),
    )
    monkeypatch.setattr(
        revision.op,
        "alter_column",
        lambda table, column, **kwargs: altered_columns.append(
            (table, column, kwargs)
        ),
    )
    monkeypatch.setattr(
        revision,
        "create_index_if_not_exists",
        lambda name, table, columns, *, unique=False: created_indexes.append(
            (name, table, columns, unique)
        ),
    )

    revision.upgrade()

    sql = "\n".join(executed).lower()
    assert [(table, column.name) for table, column in added_columns] == [
        ("chat_message_feedback", "target_kind"),
        ("chat_message_feedback", "target_id"),
        ("chat_message_feedback", "task_id"),
        ("chat_message_feedback", "plan_id"),
    ]
    assert "jsonb_array_elements" in sql
    assert "jsonb_typeof(message.refs) = 'array'" in sql
    assert "from execution_plans as plan" in sql
    assert "set target_kind = 'task_completion'" in sql
    assert "and plan.task_id is not null" in sql
    assert "then 'completion'" in sql
    assert "target_id" in sql
    assert "user_id" in sql
    assert [(table, column) for table, column, _kwargs in altered_columns] == [
        ("chat_message_feedback", "target_kind"),
    ]
    assert all(kwargs["nullable"] is False for _, _, kwargs in altered_columns)
    assert created_indexes == [
        (
            "ux_chat_feedback_target_user",
            "chat_message_feedback",
            ["target_kind", "target_id", "user_id"],
            True,
        ),
    ]


def test_chat_feedback_rolling_compat_migration_restores_legacy_writes(
    monkeypatch,
) -> None:
    revision = _script_directory().get_revision("20260824_09").module
    altered_columns: list[tuple[str, str, dict]] = []
    executed: list[str] = []

    monkeypatch.setattr(revision, "table_exists", lambda _table: True)
    monkeypatch.setattr(
        revision.op,
        "alter_column",
        lambda table, column, **kwargs: altered_columns.append(
            (table, column, kwargs)
        ),
    )
    monkeypatch.setattr(
        revision.op,
        "execute",
        lambda statement: executed.append(str(statement)),
    )

    revision.upgrade()
    assert [(table, column) for table, column, _kwargs in altered_columns] == [
        ("chat_message_feedback", "target_id"),
    ]
    assert isinstance(altered_columns[0][2]["existing_type"], sa.String)
    assert altered_columns[0][2]["existing_type"].length == 64
    assert altered_columns[0][2]["nullable"] is True
    assert executed == []

    revision.downgrade()
    assert "set target_id = message_id" in "\n".join(executed).lower()
    assert altered_columns[-1][2]["nullable"] is False


def test_legacy_completion_feedback_migration_backfills_canonical_subjects(
    monkeypatch,
) -> None:
    revision = _script_directory().get_revision("20260824_11").module
    executed: list[str] = []
    altered_columns: list[tuple[str, str, dict]] = []

    monkeypatch.setattr(revision, "table_exists", lambda _table: True)
    monkeypatch.setattr(
        revision.op,
        "execute",
        lambda statement: executed.append(str(statement)),
    )
    monkeypatch.setattr(
        revision.op,
        "alter_column",
        lambda table, column, **kwargs: altered_columns.append(
            (table, column, kwargs)
        ),
    )

    revision.upgrade()

    sql = "\n".join(executed).lower()
    assert "jsonb_each_text" in sql
    assert "task_completion_feedback" in sql
    assert "latest_task_completion_feedback" in sql
    assert "completion_message_lineage_cleanup" in sql
    assert "completion_feedback_projection_anchor" in sql
    assert "task complete([[:space:]]|$)" in sql
    assert "conversation_scope = 'workspace_main'" in sql
    assert "pg_temp.try_timestamptz" in sql
    assert "feedback_recorded_at" in sql
    assert sql.index("feedback_recorded_at desc nulls last") < sql.index(
        "is_latest_projection desc"
    )
    assert "then jsonb_build_object" in sql
    assert "legacy_completion_feedback_backfill" in sql
    assert "canonical_message_id" in sql
    assert "set conversation_id = anchor.conversation_id" in sql
    assert "on conflict (target_kind, target_id, user_id)" in sql
    assert "runtime_evidence" in sql
    assert "'target_id', legacy.target_id" in sql
    assert "'task_id', lineage.task_id" in sql
    assert "'plan_id', lineage.plan_id" in sql
    assert "join chat_message_feedback as feedback" in sql
    assert "'rating', canonical.rating" in sql
    assert "'helpful', case canonical.rating" in sql
    assert "updated_at = canonical.updated_at" in sql
    assert "insert into runtime_evidence" in sql
    assert "'feedback_target_kind', 'none'" in sql
    assert "partition by" in sql
    assert "details->>'target_kind'" in sql
    assert "details->>'target_id'" in sql
    assert "where ranked.rank > 1" in sql
    assert "set target_id = message_id" in sql
    assert "set mutation_sequence = 1" in sql
    assert [(table, column) for table, column, _ in altered_columns] == [
        ("chat_message_feedback", "target_kind"),
        ("chat_message_feedback", "target_id"),
        ("chat_message_feedback", "mutation_sequence"),
    ]
    assert all(kwargs["nullable"] is False for _, _, kwargs in altered_columns)
    assert altered_columns[0][2]["server_default"] is None


def test_legacy_completion_feedback_migration_works_without_evidence_table(
    monkeypatch,
) -> None:
    revision = _script_directory().get_revision("20260824_11").module
    executed: list[str] = []

    monkeypatch.setattr(
        revision,
        "table_exists",
        lambda table: table != "runtime_evidence",
    )
    monkeypatch.setattr(
        revision.op,
        "execute",
        lambda statement: executed.append(str(statement)),
    )
    monkeypatch.setattr(revision.op, "alter_column", lambda *_args, **_kwargs: None)

    revision.upgrade()

    sql = "\n".join(executed).lower()
    assert "jsonb_each_text" in sql
    assert "null::timestamptz as observed_at" in sql
    assert "update runtime_evidence" not in sql


@pytest.mark.asyncio
@pytest.mark.parametrize("preexisting_canonical", [False, True])
@pytest.mark.parametrize("preexisting_evidence", [False, True])
async def test_legacy_completion_feedback_migration_keeps_feedback_and_evidence_current(
    client,
    db_session,
    preexisting_canonical: bool,
    preexisting_evidence: bool,
) -> None:
    from datetime import timedelta

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import select

    from packages.core.models.base import generate_ulid
    from packages.core.models.chat_feedback import ChatMessageFeedback
    from packages.core.models.execution import ExecutionPlan
    from packages.core.models.runtime_learning import RuntimeEvidence
    from packages.core.models.task import Conversation, Message, Task

    registration = await client.post(
        "/api/v1/auth/register",
        json={
            "username": "legacy_feedback_migration",
            "email": "legacy_feedback_migration@test.com",
            "password": "MigrationPass123!",
            "entity_name": "Legacy Feedback Migration",
        },
    )
    assert registration.status_code == 200, registration.text
    headers = {"Authorization": f"Bearer {registration.json()['access_token']}"}
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    workspace = await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": "Legacy Feedback Migration"},
    )
    workspace_body = workspace.json()

    conversation_id = generate_ulid()
    thread_conversation_id = generate_ulid()
    task_id = generate_ulid()
    plan_id = generate_ulid()
    evidence_message_id = generate_ulid()
    latest_message_id = generate_ulid()
    legacy_thread_message_id = generate_ulid()
    baseline = datetime(2026, 8, 24, 12, tzinfo=timezone.utc)
    old_recorded_at = baseline + timedelta(minutes=30)
    latest_recorded_at = baseline + timedelta(hours=1)
    thread_recorded_at = baseline + timedelta(hours=1, minutes=30)
    canonical_recorded_at = baseline + timedelta(hours=2)
    rows = [
        Conversation(
            id=conversation_id,
            entity_id=workspace_body["entity_id"],
            workspace_id=workspace_body["id"],
            title="Legacy duplicate completion receipts",
            channel="workspace",
            scope="workspace_main",
        ),
        Conversation(
            id=thread_conversation_id,
            entity_id=workspace_body["entity_id"],
            workspace_id=workspace_body["id"],
            title="Legacy completion thread projection",
            channel="workspace",
            scope="workspace_thread",
            thread_ref_kind="plan",
            thread_ref_id=plan_id,
        ),
        Task(
            id=task_id,
            entity_id=workspace_body["entity_id"],
            workspace_id=workspace_body["id"],
            title="Legacy completion",
            status="completed",
        ),
        ExecutionPlan(
            id=plan_id,
            entity_id=workspace_body["entity_id"],
            workspace_id=workspace_body["id"],
            task_id=task_id,
            status="completed",
        ),
        Message(
            id=evidence_message_id,
            conversation_id=conversation_id,
            role="assistant",
            content="Completion receipt with an evidence row.",
            author_kind="agent",
            message_kind="agent_update",
            refs=[
                {"type": "plan", "id": plan_id},
                {"type": "task", "id": task_id},
            ],
            meta={
                "feedback_target_kind": "task_completion",
                "task_completion_feedback": {me["id"]: "up"},
                "latest_task_completion_feedback": {
                    "user_id": me["id"],
                    "rating": "up",
                    "recorded_at": old_recorded_at.isoformat(),
                },
            },
            created_at=baseline + timedelta(minutes=20),
        ),
        Message(
            id=latest_message_id,
            conversation_id=conversation_id,
            role="assistant",
            content="Older receipt rated later after evidence failed.",
            author_kind="agent",
            message_kind="agent_update",
            refs=[
                {"type": "plan", "id": plan_id},
                {"type": "task", "id": task_id},
            ],
            meta={
                "feedback_target_kind": "task_completion",
                "task_completion_feedback": {me["id"]: "down"},
                "latest_task_completion_feedback": {
                    "user_id": me["id"],
                    "rating": "down",
                    "recorded_at": latest_recorded_at.isoformat(),
                },
            },
            created_at=baseline,
        ),
        Message(
            id=legacy_thread_message_id,
            conversation_id=thread_conversation_id,
            role="assistant",
            content="✅ **Task complete — Legacy thread receipt**",
            author_kind="agent",
            message_kind="agent_update",
            refs=[
                {"type": "plan", "id": plan_id},
                {"type": "task", "id": task_id},
            ],
            meta={
                "task_completion_feedback": {me["id"]: "down"},
                "latest_task_completion_feedback": {
                    "user_id": me["id"],
                    "rating": "down",
                    "recorded_at": thread_recorded_at.isoformat(),
                },
            },
            created_at=baseline + timedelta(hours=3),
        ),
    ]
    if preexisting_evidence:
        rows.append(
            RuntimeEvidence(
                entity_id=workspace_body["entity_id"],
                workspace_id=workspace_body["id"],
                user_id=me["id"],
                conversation_id=conversation_id,
                message_id=evidence_message_id,
                task_id=task_id,
                evidence_type="task_completion_feedback",
                source="workspace_chat",
                status="succeeded",
                summary="Legacy helpful feedback",
                details={
                    "rating": "up",
                    "task_id": task_id,
                    "plan_id": plan_id,
                },
                metrics={"helpful": 1},
                created_at=old_recorded_at,
                updated_at=old_recorded_at,
            )
        )
    db_session.add_all(rows)
    await db_session.flush()
    if preexisting_canonical:
        db_session.add(
            ChatMessageFeedback(
                entity_id=workspace_body["entity_id"],
                user_id=me["id"],
                conversation_id=conversation_id,
                message_id=latest_message_id,
                target_kind="task_completion",
                target_id=plan_id,
                task_id=task_id,
                plan_id=plan_id,
                rating="up",
                mutation_sequence=7,
                meta={"source": "canonical_writer"},
                created_at=latest_recorded_at,
                updated_at=canonical_recorded_at,
            )
        )
    await db_session.commit()

    revision = _script_directory().get_revision("20260824_11").module

    def run_upgrade(sync_session) -> None:
        context = MigrationContext.configure(sync_session.connection())
        with Operations.context(context):
            revision.upgrade()

    await db_session.run_sync(run_upgrade)
    db_session.expire_all()

    feedback = (
        await db_session.execute(
            select(ChatMessageFeedback).where(
                ChatMessageFeedback.target_kind == "task_completion",
                ChatMessageFeedback.target_id == plan_id,
                ChatMessageFeedback.user_id == me["id"],
            )
        )
    ).scalar_one()
    expected_rating = "up" if preexisting_canonical else "down"
    expected_updated_at = (
        canonical_recorded_at
        if preexisting_canonical
        else thread_recorded_at
    )
    assert feedback.rating == expected_rating
    assert feedback.message_id == evidence_message_id
    assert feedback.updated_at == expected_updated_at

    evidence = (
        await db_session.execute(
            select(RuntimeEvidence).where(
                RuntimeEvidence.workspace_id == workspace_body["id"],
                RuntimeEvidence.user_id == me["id"],
                RuntimeEvidence.evidence_type == "task_completion_feedback",
                RuntimeEvidence.details["target_id"].as_string() == plan_id,
            )
        )
    ).scalar_one()
    assert evidence.message_id == evidence_message_id
    assert evidence.details["rating"] == expected_rating
    assert evidence.metrics["helpful"] == (1 if preexisting_canonical else 0)
    assert evidence.updated_at == expected_updated_at

    messages = {
        message.id: message
        for message in (
            await db_session.execute(
                select(Message).where(
                    Message.id.in_((
                        evidence_message_id,
                        latest_message_id,
                        legacy_thread_message_id,
                    ))
                )
            )
        ).scalars()
    }
    assert (
        messages[evidence_message_id].meta["feedback_target_kind"]
        == "task_completion"
    )
    assert messages[latest_message_id].meta["feedback_target_kind"] == "none"
    assert (
        messages[legacy_thread_message_id].meta["feedback_target_kind"]
        == "none"
    )
    assert all(
        "task_completion_feedback" not in (message.meta or {})
        and "latest_task_completion_feedback" not in (message.meta or {})
        for message in messages.values()
    )

    # The newest legacy mutation came from the retired Plan-thread receipt,
    # but the durable subject must survive deletion of that projection.
    from packages.core.services.conversation_lifecycle import delete_conversation

    assert await delete_conversation(
        db_session,
        thread_conversation_id,
        workspace_body["entity_id"],
    )
    await db_session.commit()
    assert await db_session.get(ChatMessageFeedback, feedback.id) is not None
    assert await db_session.get(RuntimeEvidence, evidence.id) is not None


def test_blueprint_purchase_refund_migration_backfills_historical_logs(
    monkeypatch,
) -> None:
    revision = _script_directory().get_revision("20260822_08").module
    added_columns: list[tuple[str, sa.Column]] = []
    executed: list[str] = []

    monkeypatch.setattr(
        revision,
        "add_column_if_not_exists",
        lambda table, column: added_columns.append((table, column)),
    )
    monkeypatch.setattr(revision, "table_exists", lambda _table: True)
    monkeypatch.setattr(
        revision,
        "column_exists",
        lambda _table, _column: True,
    )
    monkeypatch.setattr(
        revision.op,
        "execute",
        lambda statement: executed.append(str(statement)),
    )

    revision.upgrade()

    assert [(table, column.name) for table, column in added_columns] == [
        ("blueprint_purchases", "refunded_amount_cents"),
        ("blueprint_purchases", "transfer_reversed_amount_cents"),
        ("blueprint_purchases", "platform_fee_refunded_amount_cents"),
    ]
    assert added_columns[0][1].nullable is False
    assert added_columns[1][1].nullable is True
    assert added_columns[2][1].nullable is True
    assert added_columns[1][1].server_default is None
    assert added_columns[2][1].server_default is None
    sql = "\n".join(executed)
    assert "status = 'refunded'" in sql
    assert "payment_logs" in sql
    assert "amountRefunded" in sql
    assert "purchaseId" in sql


@pytest.mark.asyncio
async def test_blueprint_purchase_refund_backfill_recovers_partial_and_full(
    db_session,
) -> None:
    revision = _script_directory().get_revision("20260822_08").module
    await db_session.execute(text("""
        CREATE TEMP TABLE blueprint_purchases (
            id varchar(26) PRIMARY KEY,
            amount_cents integer NOT NULL,
            refunded_amount_cents integer NOT NULL DEFAULT 0,
            transfer_reversed_amount_cents integer,
            platform_fee_refunded_amount_cents integer,
            status varchar(20) NOT NULL
        ) ON COMMIT DROP
    """))
    await db_session.execute(text("""
        CREATE TEMP TABLE payment_logs (
            id varchar(26) PRIMARY KEY,
            metadata jsonb NOT NULL
        ) ON COMMIT DROP
    """))
    await db_session.execute(text("""
        INSERT INTO blueprint_purchases (
            id, amount_cents, refunded_amount_cents, status
        ) VALUES
            ('purchase-partial', 5000, 0, 'completed'),
            ('purchase-full', 5000, 0, 'refunded'),
            ('purchase-malformed', 5000, 0, 'completed')
    """))
    for log_id, metadata in [
        (
            "log-partial",
            {
                "source": "blueprint_purchase",
                "purchaseId": "purchase-partial",
                "amountRefunded": 1250,
            },
        ),
        (
            "log-malformed",
            {
                "source": "blueprint_purchase",
                "purchaseId": "purchase-malformed",
                "amountRefunded": "not-a-number",
            },
        ),
    ]:
        await db_session.execute(
            text("""
                INSERT INTO payment_logs (id, metadata)
                VALUES (:id, CAST(:metadata AS jsonb))
            """),
            {"id": log_id, "metadata": json.dumps(metadata)},
        )

    await db_session.execute(revision._FULL_REFUND_BACKFILL)
    await db_session.execute(revision._PAYMENT_LOG_REFUND_BACKFILL)

    rows = {
        row.id: (
            row.refunded_amount_cents,
            row.transfer_reversed_amount_cents,
            row.platform_fee_refunded_amount_cents,
        )
        for row in (await db_session.execute(text("""
        SELECT id, refunded_amount_cents,
               transfer_reversed_amount_cents,
               platform_fee_refunded_amount_cents
        FROM blueprint_purchases
        ORDER BY id
    """))).all()
    }
    assert rows == {
        "purchase-full": (5000, None, None),
        "purchase-malformed": (0, None, None),
        "purchase-partial": (1250, None, None),
    }


def test_avatar_url_migration_widens_user_and_staff_columns(monkeypatch) -> None:
    revision = _script_directory().get_revision("20260820_03").module
    altered_columns: list[tuple[str, str, dict]] = []

    monkeypatch.setattr(revision, "column_exists", lambda _table, _column: True)
    monkeypatch.setattr(
        revision.op,
        "alter_column",
        lambda table, column, **kwargs: altered_columns.append(
            (table, column, kwargs)
        ),
    )

    revision.upgrade()

    assert [(table, column) for table, column, _kwargs in altered_columns] == [
        ("users", "avatar_url"),
        ("staff", "avatar_url"),
    ]
    assert all(
        isinstance(kwargs["type_"], Text)
        for _table, _column, kwargs in altered_columns
    )


def test_goal_identity_migration_restores_contract_keys_before_fallback(
    monkeypatch,
) -> None:
    revision = _script_directory().get_revision("20260822_06").module
    statements: list[str] = []
    indexes: list[tuple[str, list[str], dict]] = []

    monkeypatch.setattr(revision, "add_column_if_not_exists", lambda *_args: None)
    monkeypatch.setattr(revision, "column_exists", lambda *_args: True)
    monkeypatch.setattr(
        revision.op,
        "execute",
        lambda statement: statements.append(str(statement)),
    )
    monkeypatch.setattr(revision.op, "alter_column", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        revision,
        "create_index_if_not_exists",
        lambda name, _table, columns, **kwargs: indexes.append(
            (name, columns, kwargs)
        ),
    )

    revision.upgrade()

    assert len(statements) == 4
    assert "jsonb_array_elements" in statements[0]
    assert "operating_model -> 'goals'" in statements[0]
    assert "UPDATE goals" in statements[1]
    assert "ORDER BY created_at, id" in statements[2]
    assert "WHILE EXISTS" in statements[2]
    assert "assigned.assigned_key = candidate_key" in statements[2]
    assert "SET operating_model = jsonb_set" in statements[3]
    assert [name for name, _columns, _kwargs in indexes] == [
        "uq_goals_workspace_goal_key",
        "uq_goals_entity_goal_key",
    ]
    assert all(kwargs["unique"] is True for _name, _columns, kwargs in indexes)


def test_creator_campaign_migration_canonicalizes_legacy_profile_aliases() -> None:
    revision = _script_directory().get_revision("20260822_07").module

    legacy_rows = [
        {
            "id": "participant-1",
            "campaign_id": "campaign-1",
            "profile_url": "https://twitter.com/Creator_Name?ref=legacy",
        },
        {
            "id": "participant-2",
            "campaign_id": "campaign-2",
            "profile_url": "https://m.instagram.com/Creator.Name/",
        },
        {
            "id": "participant-3",
            "campaign_id": "campaign-3",
            "profile_url": "https://youtube.com/@CreatorName/videos",
        },
    ]
    updates = revision._canonical_profile_updates(legacy_rows)

    assert [row["profile_url"] for row in updates] == [
        "https://x.com/creator_name",
        "https://instagram.com/creator.name",
        "https://youtube.com/@creatorname",
    ]


def test_creator_campaign_migration_quarantines_canonical_collision() -> None:
    revision = _script_directory().get_revision("20260822_07").module

    legacy_rows = [
        {
            "id": "participant-1",
            "campaign_id": "campaign-1",
            "profile_url": "https://twitter.com/Creator_Name",
            "profile_fingerprint": "legacy-fingerprint-1",
            "status": "active",
            "status_reason": None,
        },
        {
            "id": "participant-2",
            "campaign_id": "campaign-1",
            "profile_url": "https://x.com/creator_name?source=legacy",
            "profile_fingerprint": "legacy-fingerprint-2",
            "status": "pending_verification",
            "status_reason": None,
        },
    ]
    updates = revision._canonical_profile_updates(legacy_rows)

    assert [row["profile_url"] for row in updates] == [
        row["profile_url"] for row in legacy_rows
    ]
    assert [row["profile_fingerprint"] for row in updates] == [
        "legacy-fingerprint-1",
        "legacy-fingerprint-2",
    ]
    assert {row["status"] for row in updates} == {"suspended"}
    assert all("conflicts" in row["status_reason"] for row in updates)


def test_creator_campaign_migration_quarantines_old_video_url_without_aborting() -> None:
    revision = _script_directory().get_revision("20260822_07").module
    profile_url = "https://youtu.be/legacy-video-id"
    updates = revision._canonical_profile_updates([
        {
            "id": "participant-video",
            "campaign_id": "campaign-1",
            "profile_url": profile_url,
            "profile_fingerprint": "",
            "status": "active",
            "status_reason": None,
        },
    ])

    assert updates == [{
        "id": "participant-video",
        "profile_url": profile_url,
        "profile_fingerprint": hashlib.sha256(profile_url.encode("utf-8")).hexdigest(),
        "status": "suspended",
        "status_reason": (
            "Creator profile requires re-verification after identity normalization upgrade"
        ),
    }]


@pytest.mark.asyncio
async def test_goal_identity_migration_deduplicates_legacy_scope_collisions(
    db_session,
) -> None:
    revision = _script_directory().get_revision("20260822_06").module
    await db_session.execute(text("""
        CREATE TEMP TABLE workspaces (
            id varchar(26) PRIMARY KEY,
            operating_model jsonb NOT NULL
        ) ON COMMIT DROP
    """))
    await db_session.execute(text("""
        CREATE TEMP TABLE goals (
            id varchar(26) PRIMARY KEY,
            entity_id varchar(26) NOT NULL,
            workspace_id varchar(26),
            goal_key varchar(100),
            title varchar(255) NOT NULL DEFAULT 'Legacy goal',
            description text,
            metric_key varchar(100) NOT NULL,
            target_value numeric(20, 4) NOT NULL DEFAULT 0,
            baseline_value numeric(20, 4),
            deadline date,
            measurement_source jsonb,
            measurement_cadence varchar(64),
            priority smallint NOT NULL DEFAULT 3,
            created_at timestamptz NOT NULL
        ) ON COMMIT DROP
    """))

    long_prefix = "x" * 100
    workspace_contract = {
        "goals": [
            {"goal_key": " duplicate ", "metric_key": "shared_metric"},
            {"goal_key": "duplicate", "metric_key": "shared_metric"},
            {"goal_key": "duplicate", "metric_key": "shared_metric"},
            {"goal_key": f"{long_prefix}a", "metric_key": "long_metric"},
            {"goal_key": f"{long_prefix}b", "metric_key": "long_metric"},
        ]
    }
    for workspace_id, operating_model in (
        ("workspace-1", workspace_contract),
        ("workspace-2", {
            "goals": [{
                "goal_key": "duplicate",
                "metric_key": "shared_metric",
            }],
        }),
    ):
        await db_session.execute(
            text("""
                INSERT INTO workspaces (id, operating_model)
                VALUES (:id, CAST(:operating_model AS jsonb))
            """),
            {
                "id": workspace_id,
                "operating_model": json.dumps(operating_model),
            },
        )

    created_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows = [
        ("workspace-goal-1", "entity-1", "workspace-1", None, "shared_metric", created_at),
        ("workspace-goal-2", "entity-1", "workspace-1", None, "shared_metric", created_at.replace(day=2)),
        ("workspace-goal-3", "entity-1", "workspace-1", None, "shared_metric", created_at.replace(day=3)),
        ("workspace-goal-4", "entity-1", "workspace-1", None, "long_metric", created_at.replace(day=4)),
        ("workspace-goal-5", "entity-1", "workspace-1", None, "long_metric", created_at.replace(day=5)),
        ("other-workspace", "entity-1", "workspace-2", None, "shared_metric", created_at),
        ("entity-goal-1", "entity-1", None, None, "entity_metric", created_at),
        ("entity-goal-2", "entity-1", None, None, "entity_metric", created_at.replace(day=2)),
        ("other-entity", "entity-2", None, None, "entity_metric", created_at),
    ]
    for row in rows:
        await db_session.execute(
            text("""
                INSERT INTO goals (
                    id, entity_id, workspace_id, goal_key, metric_key, created_at
                ) VALUES (
                    :id, :entity_id, :workspace_id, :goal_key, :metric_key, :created_at
                )
            """),
            {
                "id": row[0],
                "entity_id": row[1],
                "workspace_id": row[2],
                "goal_key": row[3],
                "metric_key": row[4],
                "created_at": row[5],
            },
        )

    await db_session.execute(text(revision.RESTORE_CONTRACT_GOAL_KEYS_SQL))
    await db_session.execute(text(revision.FILL_MISSING_GOAL_KEYS_SQL))
    await db_session.execute(text(revision.DEDUPLICATE_GOAL_KEYS_SQL))
    await db_session.execute(text(revision.SYNC_WORKSPACE_CONTRACT_GOAL_KEYS_SQL))

    keys = dict((await db_session.execute(
        text("SELECT id, goal_key FROM goals")
    )).all())
    assert keys["workspace-goal-1"] == "duplicate"
    assert keys["workspace-goal-2"] == "duplicate_2"
    assert keys["workspace-goal-3"] == "duplicate_3"
    assert keys["workspace-goal-4"] == long_prefix
    assert keys["workspace-goal-5"] == f"{'x' * 98}_2"
    assert keys["other-workspace"] == "duplicate"
    assert keys["entity-goal-1"] == "entity_metric"
    assert keys["entity-goal-2"] == "entity_metric_2"
    assert keys["other-entity"] == "entity_metric"
    assert all(0 < len(goal_key) <= 100 for goal_key in keys.values())

    repaired_contract = (await db_session.execute(text("""
        SELECT operating_model -> 'goals'
        FROM workspaces
        WHERE id = 'workspace-1'
    """))).scalar_one()
    assert [goal["goal_key"] for goal in repaired_contract] == [
        "duplicate",
        "duplicate_2",
        "duplicate_3",
        long_prefix,
        f"{'x' * 98}_2",
    ]

    # These mirror the migration's final constraints. Creating them proves the
    # repaired legacy rows cannot block the schema upgrade.
    await db_session.execute(text("""
        CREATE UNIQUE INDEX test_goals_workspace_goal_key
        ON goals (workspace_id, goal_key)
        WHERE workspace_id IS NOT NULL
    """))
    await db_session.execute(text("""
        CREATE UNIQUE INDEX test_goals_entity_goal_key
        ON goals (entity_id, goal_key)
        WHERE workspace_id IS NULL
    """))


@pytest.mark.asyncio
async def test_goal_identity_migration_preserves_rank_and_reconciles_cardinality(
    db_session,
) -> None:
    revision = _script_directory().get_revision("20260822_06").module
    await db_session.execute(text("""
        CREATE TEMP TABLE workspaces (
            id varchar(26) PRIMARY KEY,
            operating_model jsonb NOT NULL
        ) ON COMMIT DROP
    """))
    await db_session.execute(text("""
        CREATE TEMP TABLE goals (
            id varchar(26) PRIMARY KEY,
            entity_id varchar(26) NOT NULL,
            workspace_id varchar(26),
            goal_key varchar(100),
            title varchar(255) NOT NULL,
            description text,
            metric_key varchar(100) NOT NULL,
            target_value numeric(20, 4) NOT NULL,
            baseline_value numeric(20, 4),
            deadline date,
            measurement_source jsonb,
            measurement_cadence varchar(64),
            priority smallint NOT NULL,
            created_at timestamptz NOT NULL
        ) ON COMMIT DROP
    """))

    await db_session.execute(
        text("""
            INSERT INTO workspaces (id, operating_model)
            VALUES ('workspace-mixed', CAST(:operating_model AS jsonb))
        """),
        {
            "operating_model": json.dumps({
                "goals": [
                    {"title": "Trial signups", "metric_key": "signup_count"},
                    {
                        "title": "Paid signups",
                        "goal_key": "paid_signups",
                        "metric_key": "signup_count",
                    },
                    {
                        "title": "Future paid signups",
                        "goal_key": "paid_signups",
                        "metric_key": "signup_count",
                    },
                ]
            }),
        },
    )
    created_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows = [
        (
            "trial-goal", None, "Trial signups", "signup_count", 100,
            created_at,
        ),
        (
            "paid-goal", None, "Paid signups", "signup_count",
            "9999999999999999.9999",
            created_at.replace(day=2),
        ),
        (
            "runtime-only-goal", None, "Retained users", "retention_count", 50,
            created_at.replace(day=3),
        ),
    ]
    for goal_id, goal_key, title, metric_key, target_value, row_created_at in rows:
        await db_session.execute(
            text("""
                INSERT INTO goals (
                    id, entity_id, workspace_id, goal_key, title, description,
                    metric_key, target_value, baseline_value, deadline,
                    measurement_source, measurement_cadence, priority, created_at
                ) VALUES (
                    :id, 'entity-mixed', 'workspace-mixed', :goal_key, :title,
                    NULL, :metric_key, :target_value, 0, NULL, NULL, 'daily', 3,
                    :created_at
                )
            """),
            {
                "id": goal_id,
                "goal_key": goal_key,
                "title": title,
                "metric_key": metric_key,
                "target_value": target_value,
                "created_at": row_created_at,
            },
        )

    await db_session.execute(text(revision.RESTORE_CONTRACT_GOAL_KEYS_SQL))
    await db_session.execute(
        text("""
            UPDATE goals
            SET baseline_value = CAST(:baseline_value AS numeric)
            WHERE id = 'paid-goal'
        """),
        {"baseline_value": "1234567890123456.7890"},
    )
    await db_session.execute(text(revision.FILL_MISSING_GOAL_KEYS_SQL))
    await db_session.execute(text(revision.DEDUPLICATE_GOAL_KEYS_SQL))
    await db_session.execute(text(revision.SYNC_WORKSPACE_CONTRACT_GOAL_KEYS_SQL))

    runtime_keys = dict((await db_session.execute(
        text("SELECT id, goal_key FROM goals ORDER BY created_at")
    )).all())
    assert runtime_keys == {
        "trial-goal": "signup_count",
        "paid-goal": "paid_signups",
        "runtime-only-goal": "retention_count",
    }

    repaired_contract = (await db_session.execute(text("""
        SELECT operating_model -> 'goals'
        FROM workspaces
        WHERE id = 'workspace-mixed'
    """))).scalar_one()
    assert [goal["goal_key"] for goal in repaired_contract] == [
        "signup_count",
        "paid_signups",
        "paid_signups_2",
        "retention_count",
    ]
    assert repaired_contract[-1]["title"] == "Retained users"
    assert repaired_contract[1]["target_value"] == "9999999999999999.9999"
    assert repaired_contract[1]["baseline_value"] == "1234567890123456.7890"
    assert repaired_contract[-1]["target_value"] == "50.0000"


def test_active_document_path_migration_deduplicates_before_unique_index(
    monkeypatch,
) -> None:
    revision = _script_directory().get_revision("20260821_02").module
    operations: list[tuple[str, object]] = []

    monkeypatch.setattr(revision, "table_exists", lambda _table: True)
    monkeypatch.setattr(revision, "index_exists", lambda _index: False)
    monkeypatch.setattr(
        revision.op,
        "execute",
        lambda statement: operations.append(("execute", str(statement))),
    )
    monkeypatch.setattr(
        revision.op,
        "create_index",
        lambda name, table, columns, **kwargs: operations.append(
            ("create_index", (name, table, columns, kwargs))
        ),
    )

    revision.upgrade()

    assert [operation for operation, _payload in operations] == [
        "execute",
        "create_index",
    ]
    cleanup_sql = str(operations[0][1])
    assert "ROW_NUMBER() OVER" in cleanup_sql
    assert "is_trashed = true" in cleanup_sql
    assert "fs_path = NULL" in cleanup_sql
    assert "restore_blocked_reason" in cleanup_sql
    assert "duplicate_filesystem_projection" in cleanup_sql
    index_name, table, columns, kwargs = operations[1][1]
    assert index_name == "uq_documents_entity_fs_path_active"
    assert table == "documents"
    assert columns == ["entity_id", "fs_path"]
    assert kwargs["unique"] is True
    assert "is_trashed = false" in str(kwargs["postgresql_where"])


def test_slack_event_migration_deduplicates_before_unique_index(monkeypatch) -> None:
    revision = _script_directory().get_revision("20260824_02").module
    operations: list[tuple[str, object]] = []

    monkeypatch.setattr(
        revision,
        "table_exists",
        lambda table: table == "message_logs",
    )
    monkeypatch.setattr(
        revision.op,
        "execute",
        lambda statement: operations.append(("execute", str(statement))),
    )
    monkeypatch.setattr(
        revision,
        "create_index_if_not_exists",
        lambda name, table, columns, **kwargs: operations.append(
            ("create_index", (name, table, columns, kwargs))
        ),
    )

    revision.upgrade()

    assert [operation for operation, _payload in operations] == [
        "execute",
        "create_index",
    ]
    cleanup_sql = str(operations[0][1])
    assert "ROW_NUMBER() OVER" in cleanup_sql
    assert "PARTITION BY channel_config_id, external_id" in cleanup_sql
    assert "channel_type = 'slack'" in cleanup_sql
    assert "direction = 'inbound'" in cleanup_sql
    assert "duplicate_rank > 1" in cleanup_sql


def test_slack_event_migration_backfills_binding_owner(monkeypatch) -> None:
    revision = _script_directory().get_revision("20260824_02").module
    statements: list[str] = []

    monkeypatch.setattr(
        revision,
        "table_exists",
        lambda table: table in {"channels", "channel_configs"},
    )
    monkeypatch.setattr(revision, "column_exists", lambda _table, _column: True)
    monkeypatch.setattr(
        revision.op,
        "execute",
        lambda statement: statements.append(str(statement)),
    )

    revision.upgrade()

    combined = "\n".join(statements)
    assert "UPDATE channels AS binding" in combined
    assert "SET user_id = channel_config.owner_user_id" in combined
    assert "binding.type = 'slack'" in combined
    assert "binding.user_id IS NULL" in combined


def test_durable_content_ownership_migration_backfills_and_abandons_orphans(
    monkeypatch,
) -> None:
    revision = _script_directory().get_revision("20260821_03").module
    statements: list[str] = []
    added_columns: list[tuple[str, str]] = []

    monkeypatch.setattr(revision, "table_exists", lambda _table: True)
    monkeypatch.setattr(
        revision,
        "add_column_if_not_exists",
        lambda table, column: added_columns.append((table, column.name)),
    )
    monkeypatch.setattr(revision, "create_index_if_not_exists", lambda *_args, **_kw: None)
    monkeypatch.setattr(revision.op, "execute", lambda statement: statements.append(str(statement)))

    revision.upgrade()

    assert ("sites", "created_by_user_id") in added_columns
    combined = "\n".join(statements)
    assert "document.owner_id IS NULL" in combined
    assert "site.created_by_user_id IS NULL" in combined
    assert "UPDATE workspace_drafts" in combined
    assert "user_memberships" in combined
    assert "membership.status = 'active'" in combined
    assert "COUNT(DISTINCT user_id) = 1" in combined
    assert "status IN ('active', 'ready')" in combined
    assert "SET status = 'abandoned'" in combined


def test_durable_runtime_repair_replays_schema_when_all_tables_are_missing(
    monkeypatch,
) -> None:
    script = _script_directory()
    revision = script.get_revision("20260820_01").module
    upgrades: list[None] = []

    monkeypatch.setattr(revision, "table_exists", lambda _table: False)
    monkeypatch.setattr(
        revision,
        "import_module",
        lambda _module_name: SimpleNamespace(upgrade=lambda: upgrades.append(None)),
    )

    revision.upgrade()

    assert upgrades == [None]
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


def test_workspace_event_repair_recreates_table_and_indexes(monkeypatch) -> None:
    revision = _script_directory().get_revision("20260810_02").module
    created_tables: list[str] = []
    created_indexes: list[tuple[str, str, list[str], bool]] = []

    monkeypatch.setattr(revision, "table_exists", lambda _table: False)
    monkeypatch.setattr(
        revision.op,
        "create_table",
        lambda name, *_columns: created_tables.append(name),
    )
    monkeypatch.setattr(
        revision,
        "create_index_if_not_exists",
        lambda name, table, columns, **kwargs: created_indexes.append(
            (name, table, columns, bool(kwargs.get("unique")))
        ),
    )

    revision.upgrade()

    assert created_tables == ["workspace_events"]
    assert len(created_indexes) == 5
    assert (
        "uq_workspace_events_idempotency",
        "workspace_events",
        ["entity_id", "idempotency_key"],
        True,
    ) in created_indexes


def test_workspace_event_repair_preserves_existing_table(monkeypatch) -> None:
    revision = _script_directory().get_revision("20260810_02").module
    created_tables: list[str] = []
    created_indexes: list[str] = []

    monkeypatch.setattr(revision, "table_exists", lambda _table: True)
    monkeypatch.setattr(
        revision.op,
        "create_table",
        lambda name, *_columns: created_tables.append(name),
    )
    monkeypatch.setattr(
        revision,
        "create_index_if_not_exists",
        lambda name, *_args, **_kwargs: created_indexes.append(name),
    )

    revision.upgrade()

    assert created_tables == []
    assert len(created_indexes) == 5


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


def test_workflow_execution_snapshot_migration_backfills_existing_runs(
    monkeypatch,
) -> None:
    revision = _script_directory().get_revision("20260814_01").module
    added_columns: list[tuple[str, object]] = []
    statements: list[str] = []
    from packages.core.migrations import helpers as migration_helpers

    monkeypatch.setattr(migration_helpers, "column_exists", lambda *args, **kwargs: False)
    monkeypatch.setattr(
        revision.op,
        "add_column",
        lambda table, column: added_columns.append((table, column)),
    )
    monkeypatch.setattr(
        revision.op,
        "execute",
        lambda statement: statements.append(str(statement)),
    )

    revision.upgrade()

    assert [(table, column.name) for table, column in added_columns] == [
        ("workflow_runs", "execution_snapshot"),
    ]
    sql = "\n".join(statements)
    assert "UPDATE workflow_runs" in sql
    assert "workflow_definitions" in sql
    assert "execution_snapshot" in sql


def test_staff_consolidation_migration_bootstraps_missing_staff_table(
    monkeypatch,
) -> None:
    revision = _script_directory().get_revision("20260422_03").module
    existing_tables = {"staff_members"}
    created_tables: list[str] = []

    def create_table(name, *_columns, **_kwargs):
        created_tables.append(name)
        existing_tables.add(name)

    monkeypatch.setattr(revision, "table_exists", existing_tables.__contains__)
    monkeypatch.setattr(revision.op, "create_table", create_table)
    monkeypatch.setattr(revision, "add_column_if_not_exists", lambda *_args: None)
    monkeypatch.setattr(revision, "create_index_if_not_exists", lambda *_args: None)
    monkeypatch.setattr(revision.op, "execute", lambda *_args: None)
    monkeypatch.setattr(revision.op, "drop_table", lambda *_args: None)

    revision.upgrade()

    assert "staff" in created_tables
    assert "staff_roles" in created_tables


def test_scheduler_recovery_migration_keeps_nested_terminal_effects_pending(
    monkeypatch,
) -> None:
    revision = _script_directory().get_revision("20260830_02").module
    statements: list[str] = []

    class Bind:
        dialect = SimpleNamespace(name="sqlite")

        def execute(self, statement, *_args, **_kwargs):
            statements.append(str(statement))
            return SimpleNamespace(mappings=lambda: SimpleNamespace(fetchmany=lambda _n: []))

    monkeypatch.setattr(revision, "table_exists", lambda table: table == "workflow_runs")
    monkeypatch.setattr(revision, "column_exists", lambda *_args: True)
    monkeypatch.setattr(revision.op, "get_bind", lambda: Bind())

    revision._backfill_workflow_terminal_effects()

    sql = "\n".join(statements)
    assert "trigger_data ? 'parent_run_id'" in sql
    assert "trigger_data ->> 'parent_step_id'" in sql
    assert "SET terminal_effects_next_attempt_at = CURRENT_TIMESTAMP" in sql


def test_runtime_approval_legacy_blob_cleanup_migration_purges_metadata(
    monkeypatch,
) -> None:
    revision = _script_directory().get_revision("20260817_02").module
    statements: list[str] = []
    monkeypatch.setattr(
        revision.op,
        "execute",
        lambda statement: statements.append(str(statement)),
    )

    revision.upgrade()

    sql = "\n".join(statements)
    assert "UPDATE conversations" in sql
    assert "runtime_approvals" in sql
    assert "\"metadata\" - 'runtime_approvals'" in sql


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
