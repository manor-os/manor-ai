from __future__ import annotations

from sqlalchemy import UniqueConstraint

from packages.core.constants.blueprints import is_runtime_derived_scheduled_job
from packages.core.models import (
    AgentInitiative,
    AgentInitiativeDelivery,
    AgentInitiativeRun,
    AgentInitiativeState,
    AgentSubscription,
    ScheduledJob,
)
from packages.core.revisions import (
    AGENT_INITIATIVE_CONTENT_REVISION_FIELDS,
    SCHEDULED_JOB_CONTENT_REVISION_FIELDS,
    target_kind_for,
)


def _index(table, name: str):
    return next(index for index in table.indexes if index.name == name)


def test_agent_initiative_models_register_the_runtime_tables() -> None:
    assert AgentInitiative.__table__.name == "agent_initiatives"
    assert AgentInitiativeState.__table__.name == "agent_initiative_states"
    assert AgentInitiativeRun.__table__.name == "agent_initiative_runs"
    assert AgentInitiativeDelivery.__table__.name == "agent_initiative_deliveries"

    assert set(AgentInitiative.__table__.columns.keys()) >= {
        "id",
        "entity_id",
        "workspace_id",
        "agent_subscription_id",
        "owner_user_id",
        "instruction",
        "trigger_config",
        "condition_config",
        "decision_policy",
        "delivery_policy",
        "interruption_policy",
        "status",
        "revision",
    }
    assert AgentInitiativeState.__table__.primary_key.columns.keys() == [
        "initiative_id"
    ]

    occurrence = _index(
        AgentInitiativeRun.__table__, "uq_agent_initiative_runs_occurrence"
    )
    assert occurrence.unique is True
    assert [column.name for column in occurrence.columns] == [
        "initiative_id",
        "occurrence_key",
    ]

    idempotency = _index(
        AgentInitiativeDelivery.__table__,
        "uq_agent_initiative_deliveries_idempotency",
    )
    assert idempotency.unique is True
    assert [column.name for column in idempotency.columns] == ["idempotency_key"]
    assert set(AgentInitiativeDelivery.__table__.columns.keys()) >= {
        "attempt_count",
        "next_attempt_at",
        "locked_until",
        "claim_token",
    }
    due = _index(
        AgentInitiativeDelivery.__table__,
        "ix_agent_initiative_deliveries_due",
    )
    assert [column.name for column in due.columns] == [
        "status",
        "next_attempt_at",
    ]
    lease = _index(
        AgentInitiativeDelivery.__table__,
        "ix_agent_initiative_deliveries_lease",
    )
    assert [column.name for column in lease.columns] == [
        "status",
        "locked_until",
    ]


def test_personal_subscription_and_driver_use_partial_unique_indexes() -> None:
    personal = _index(
        AgentSubscription.__table__, "uq_agent_subscriptions_personal_owner"
    )
    assert personal.unique is True
    assert [column.name for column in personal.columns] == [
        "entity_id",
        "agent_id",
        "owner_user_id",
    ]
    assert str(personal.dialect_options["postgresql"]["where"]) == (
        "workspace_id IS NULL AND owner_user_id IS NOT NULL"
    )

    driver = _index(
        ScheduledJob.__table__, "uq_scheduled_jobs_agent_initiative"
    )
    assert driver.unique is True
    assert str(driver.dialect_options["postgresql"]["where"]) == (
        "agent_initiative_id IS NOT NULL"
    )


def test_workspace_subscription_uses_service_binding_constraints() -> None:
    constraints = {
        constraint.name: constraint
        for constraint in AgentSubscription.__table__.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    workspace_service = constraints[
        "uq_agent_subscriptions_workspace_service_agent"
    ]
    assert [column.name for column in workspace_service.columns] == [
        "entity_id",
        "agent_id",
        "workspace_id",
        "service_key",
    ]

    workspace_default = _index(
        AgentSubscription.__table__,
        "uq_agent_subscriptions_workspace_default_agent",
    )
    assert workspace_default.unique is True
    assert [column.name for column in workspace_default.columns] == [
        "entity_id",
        "agent_id",
        "workspace_id",
    ]
    assert str(workspace_default.dialect_options["postgresql"]["where"]) == (
        "workspace_id IS NOT NULL AND service_key IS NULL"
    )


def test_agent_initiative_revision_and_blueprint_boundaries_are_registered() -> None:
    assert target_kind_for(AgentInitiative()) == "agent_initiative"
    assert {
        "name",
        "instruction",
        "trigger_config",
        "condition_config",
        "decision_policy",
        "delivery_policy",
        "interruption_policy",
        "status",
    } <= AGENT_INITIATIVE_CONTENT_REVISION_FIELDS
    assert "agent_initiative_id" in SCHEDULED_JOB_CONTENT_REVISION_FIELDS
    assert is_runtime_derived_scheduled_job(
        job_id="initiative:01JTEST",
        execution_type="agent_initiative",
    )
