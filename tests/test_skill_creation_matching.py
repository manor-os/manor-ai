import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from packages.core.models.skill import AgentSkillBinding, Skill
from packages.core.services import agent_provisioning_service
from packages.core.services.agent_provisioning_service import (
    CustomAgentSpec,
    provision_custom_agent,
)
from packages.core.services.skill_generator import generate_skill
from packages.core.services.skill_service import create_skill


@asynccontextmanager
async def _granted_scheduled_skill_claim(job_id: str, revision: int):
    from packages.core.services import workflow_run_execution_claim as claim_service

    yield claim_service._claim(
        f"{job_id}:{revision}",
        "skill-generation-test",
        granted=True,
        reason=claim_service.CLAIM_GRANTED,
    )


@pytest.mark.asyncio
async def test_create_skill_replaces_placeholder_identity(db_session):
    skill = await create_skill(
        db_session,
        entity_id="ent_skill_identity",
        name="unknown",
        system_prompt="# Support Ticket Triage\n\nUse this skill when support tickets need routing.",
        slug="unknown",
        description="Support Ticket Triage",
    )

    assert skill.name == "Support Ticket Triage"
    assert skill.slug == "support_ticket_triage"

    duplicate = await create_skill(
        db_session,
        entity_id="ent_skill_identity",
        name="Support Ticket Triage",
        system_prompt="Handle support tickets.",
        description="Support Ticket Triage",
    )

    assert duplicate.slug == "support_ticket_triage_2"


@pytest.mark.asyncio
async def test_create_skill_cleans_files_when_minio_dir_flush_fails(
    db_session,
    monkeypatch,
):
    from packages.core.services import skill_service

    real_flush = db_session.flush
    flush_count = 0
    cleaned: list[tuple[str, dict, str]] = []

    async def fail_second_flush(*args, **kwargs):
        nonlocal flush_count
        flush_count += 1
        if flush_count == 2:
            raise RuntimeError("persisting minio_dir failed")
        return await real_flush(*args, **kwargs)

    def record_cleanup(skill, config, *, context):
        cleaned.append((skill.id, dict(config), context))

    monkeypatch.setattr(db_session, "flush", fail_second_flush)
    monkeypatch.setattr(
        skill_service,
        "_save_skill_files_to_minio",
        lambda *_args, **_kwargs: "generated-skill-dir",
    )
    monkeypatch.setattr(
        skill_service,
        "_delete_skill_files_best_effort",
        record_cleanup,
    )

    with pytest.raises(RuntimeError, match="persisting minio_dir failed"):
        await create_skill(
            db_session,
            entity_id="ent_skill_flush_cleanup",
            name="Generated report",
            system_prompt="Prepare the report.",
        )
    await db_session.rollback()

    assert flush_count == 2
    assert len(cleaned) == 1
    assert cleaned[0][1]["minio_dir"] == "generated-skill-dir"
    assert cleaned[0][2] == "create rollback"


@pytest.mark.asyncio
async def test_workspace_missing_skill_specs_reuse_llm_selected_existing_skill(
    db_session,
    monkeypatch,
):
    existing = await create_skill(
        db_session,
        entity_id="ent_skill_reuse",
        name="Support Ticket Triage",
        system_prompt="Route support tickets by urgency and owner.",
        slug="support_ticket_triage",
        description="Use this skill when support tickets need triage, routing, and priority labeling.",
        tools=["workspace_search"],
    )

    async def fake_selector(**kwargs):
        requested = kwargs["requested_skill"]
        candidates = kwargs["candidates"]
        assert requested["name"] == "Ticket Routing"
        assert any(candidate["id"] == existing.id for candidate in candidates)
        return json.dumps(
            {
                "reuse": True,
                "skill_id": existing.id,
                "confidence": 0.93,
                "reason": "Existing skill covers support ticket triage.",
            }
        )

    monkeypatch.setattr(
        agent_provisioning_service,
        "_execute_skill_reuse_selector_completion",
        fake_selector,
    )

    result = await provision_custom_agent(
        db_session,
        entity_id="ent_skill_reuse",
        spec=CustomAgentSpec(
            agent_name="Support Agent",
            system_prompt="You are Support Agent. Handle support workflows and stay in scope.",
            source="auto_workspace_setup",
            workspace_id="ws_support",
            workspace_name="Support Ops",
            service_key="support_triage",
            missing_skill_specs=[
                {
                    "name": "Ticket Routing",
                    "system_prompt": "Route support tickets by priority and owner.",
                    "description": "Ticket triage and routing.",
                    "tools": ["workspace_search"],
                }
            ],
        ),
    )

    assert result.created_skills == []
    assert result.bound_skills == ["support_ticket_triage"]

    skills = (await db_session.execute(select(Skill).where(Skill.entity_id == "ent_skill_reuse"))).scalars().all()
    assert [skill.id for skill in skills] == [existing.id]

    binding = (
        await db_session.execute(
            select(AgentSkillBinding).where(
                AgentSkillBinding.agent_id == result.agent_id,
                AgentSkillBinding.skill_id == existing.id,
            )
        )
    ).scalar_one()
    assert binding.status == "active"
    [context] = binding.config["contexts"]
    assert context["source"] == "auto_workspace_setup"
    assert context["agent_id"] == result.agent_id
    assert context["agent_name"] == "Support Agent"
    assert context["workspace_id"] == "ws_support"
    assert context["workspace_name"] == "Support Ops"
    assert context["service_key"] == "support_triage"
    assert context["requested_skill"]["name"] == "Ticket Routing"
    assert context["match"]["type"] == "llm_reuse"
    assert context["match"]["confidence"] == 0.93

    from apps.api.routers.skills import _binding_contexts_for_skills

    contexts = await _binding_contexts_for_skills(
        db_session,
        [existing],
        entity_id="ent_skill_reuse",
    )
    [api_context] = contexts[existing.id]
    assert api_context["binding_id"] == binding.id
    assert api_context["agent_name"] == "Support Agent"
    assert api_context["workspace_name"] == "Support Ops"
    assert api_context["match"]["type"] == "llm_reuse"


@pytest.mark.asyncio
async def test_workspace_missing_skill_specs_preserve_requested_slug(db_session, monkeypatch):
    async def no_existing_skill(**kwargs):
        return json.dumps({"reuse": False, "confidence": 0, "reason": "No matching skill."})

    monkeypatch.setattr(
        agent_provisioning_service,
        "_execute_skill_reuse_selector_completion",
        no_existing_skill,
    )

    result = await provision_custom_agent(
        db_session,
        entity_id="ent_skill_slug_preserve",
        spec=CustomAgentSpec(
            agent_name="Consulting Discovery Agent",
            system_prompt="You qualify consulting opportunities.",
            source="auto_workspace_setup",
            workspace_id="ws_consulting",
            workspace_name="Consulting",
            service_key="client_discovery",
            skill_bindings=["consulting-discovery-brief"],
            missing_skill_specs=[
                {
                    "name": "Consulting Discovery Brief",
                    "slug": "consulting-discovery-brief",
                    "system_prompt": "Prepare concise discovery briefs.",
                    "description": "Discovery workflow.",
                    "tools": ["rag", "workspace_agent", "generate_file"],
                }
            ],
        ),
    )

    skill = (
        await db_session.execute(
            select(Skill).where(
                Skill.entity_id == "ent_skill_slug_preserve",
                Skill.slug == "consulting-discovery-brief",
            )
        )
    ).scalar_one()
    binding = (
        await db_session.execute(
            select(AgentSkillBinding).where(
                AgentSkillBinding.agent_id == result.agent_id,
                AgentSkillBinding.skill_id == skill.id,
            )
        )
    ).scalar_one()

    assert result.created_skills == ["consulting-discovery-brief"]
    assert skill.slug == "consulting-discovery-brief"
    assert binding.status == "active"


@pytest.mark.asyncio
async def test_generated_scheduled_job_skill_records_source_metadata(
    db_session,
    monkeypatch,
):
    async def fake_generation_completion(*args, **kwargs):
        return SimpleNamespace(
            content=json.dumps(
                {
                    "name": "daily-support-summary",
                    "slug": "daily_support_summary",
                    "display_name": "Daily Support Summary",
                    "description": "Use this skill when a scheduled support summary should be produced.",
                    "system_prompt": "# Daily Support Summary\n\nSummarize support work.",
                    "tools": ["workspace_search"],
                    "input_schema": {},
                    "output_format": "markdown",
                    "category": "automation",
                    "tags": ["support"],
                    "complexity": "worker",
                }
            ),
            usage={},
        )

    async def fake_review_completion(*args, **kwargs):
        return SimpleNamespace(content="PASS", usage={})

    monkeypatch.setattr(
        "packages.core.services.skill_generator.runtime_execute_skill_generation_completion",
        fake_generation_completion,
    )
    monkeypatch.setattr(
        "packages.core.services.skill_generator.runtime_execute_skill_review_completion",
        fake_review_completion,
    )

    skill = await generate_skill(
        prompt="Scheduled automation: Daily support summary",
        entity_id="ent_scheduled_skill",
        db=db_session,
        category="automation",
        tags=["auto-generated", "scheduled-job", "job_support_daily"],
        config_overrides={
            "source": "scheduled_job",
            "generation_source": "llm-generated",
            "scheduled_job_id": "job_support_daily",
            "workspace_id": "ws_support",
            "agent_id": "agent_support",
        },
    )

    assert skill.config["source"] == "scheduled_job"
    assert skill.config["generation_source"] == "llm-generated"
    assert skill.config["scheduled_job_id"] == "job_support_daily"
    assert skill.config["workspace_id"] == "ws_support"
    assert skill.config["agent_id"] == "agent_support"


def test_generate_job_skill_credit_exhaustion_stops_before_generation(monkeypatch):
    from packages.core.ai.llm_client import CreditExhaustedError
    from packages.core.tasks import ai_tasks

    job = SimpleNamespace(
        id="job_pk_credit_stop",
        job_id="job_credit_stop",
        entity_id="ent_credit_stop",
        workspace_id="ws_credit_stop",
        user_id="user_credit_stop",
        name="Credit Stop Automation",
        payload_message="Summarize support tickets daily.",
        agent_id="agent_credit_stop",
        execution_type="agent",
        execution_target={},
        enabled=True,
    )

    calls: list[dict[str, object]] = []

    async def fake_assert_credit_available(entity_id, *, source, **kwargs):
        calls.append({"handler": "credit_gate", "entity_id": entity_id, "source": source, **kwargs})
        raise CreditExhaustedError("no credits")

    async def fake_generate_skill(*args, **kwargs):
        calls.append({"handler": "generate_skill"})
        raise AssertionError("generate_skill should not run when credits are exhausted")

    class FakeResult:
        def scalar_one_or_none(self):
            return job

    class FakeDB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return None

        async def execute(self, *_args, **_kwargs):
            return FakeResult()

        async def commit(self):
            calls.append({"handler": "commit"})

    class FakeSessionFactory:
        def __call__(self):
            return FakeDB()

    monkeypatch.setattr(
        "packages.core.database.create_worker_session",
        lambda: FakeSessionFactory(),
    )
    monkeypatch.setattr(
        "packages.core.services.workflow_run_execution_claim."
        "scheduled_job_skill_generation_claim",
        _granted_scheduled_skill_claim,
    )
    monkeypatch.setattr(
        ai_tasks,
        "runtime_assert_credit_available",
        fake_assert_credit_available,
        raising=False,
    )
    monkeypatch.setattr(
        "packages.core.services.skill_generator.generate_skill",
        fake_generate_skill,
    )

    ai_tasks.generate_job_skill.run(
        job.id,
        "Summarize support tickets daily.",
        "Credit Stop Automation",
    )

    assert calls == [
        {
            "handler": "credit_gate",
            "entity_id": "ent_credit_stop",
            "source": "scheduled_job",
            "user_id": "user_credit_stop",
            "workspace_id": "ws_credit_stop",
            "byok": False,
        }
    ]
    assert job.execution_type == "agent"
    assert job.execution_target == {}


def test_generate_job_skill_disabled_job_stops_before_credit_or_provider(
    monkeypatch,
):
    from packages.core.tasks import ai_tasks

    job = SimpleNamespace(
        id="job-pk-disabled-generation",
        enabled=False,
    )

    class FakeResult:
        def scalar_one_or_none(self):
            return job

    class FakeDB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return None

        async def execute(self, *_args, **_kwargs):
            return FakeResult()

    class FakeSessionFactory:
        def __call__(self):
            return FakeDB()

    async def unexpected(*_args, **_kwargs):
        raise AssertionError("disabled generation crossed the billable boundary")

    monkeypatch.setattr(
        "packages.core.database.create_worker_session",
        lambda: FakeSessionFactory(),
    )
    monkeypatch.setattr(
        "packages.core.services.workflow_run_execution_claim."
        "scheduled_job_skill_generation_claim",
        _granted_scheduled_skill_claim,
    )
    monkeypatch.setattr(ai_tasks, "runtime_assert_credit_available", unexpected)
    monkeypatch.setattr(
        "packages.core.services.skill_generator.generate_skill_draft",
        unexpected,
    )

    ai_tasks.generate_job_skill.run(
        job.id,
        "Prepare a report",
        "Disabled automation",
        4,
    )


@pytest.mark.asyncio
async def test_skill_generation_failure_exhaustion_closes_durable_intent(
    db_session,
):
    from packages.core.constants.execution import (
        SCHEDULED_JOB_SKILL_GENERATION_MAX_ATTEMPTS,
    )
    from packages.core.models.base import generate_ulid
    from packages.core.models.scheduler import ScheduledJob
    from packages.core.tasks.ai_tasks import (
        _record_scheduled_job_skill_generation_failure,
    )

    job = ScheduledJob(
        id=generate_ulid(),
        job_id=f"skill-generation-exhaustion:{generate_ulid()}",
        entity_id=generate_ulid(),
        name="Exhausted generation",
        payload_message="Prepare a report",
        agent_id="legacy-agent-label",
        enabled=True,
        revision=6,
        skill_generation_revision=6,
        skill_generation_attempts=(
            SCHEDULED_JOB_SKILL_GENERATION_MAX_ATTEMPTS
        ),
        skill_generation_next_attempt_at=datetime.now(timezone.utc),
    )
    db_session.add(job)
    await db_session.commit()

    exhausted = await _record_scheduled_job_skill_generation_failure(
        job.id,
        6,
        RuntimeError("provider unavailable"),
    )

    assert exhausted is True
    await db_session.refresh(job)
    assert job.skill_generation_revision is None
    assert job.skill_generation_next_attempt_at is None
    assert job.skill_generation_attempts == (
        SCHEDULED_JOB_SKILL_GENERATION_MAX_ATTEMPTS
    )
    assert job.skill_generation_last_error == (
        "RuntimeError: provider unavailable"
    )


def test_generate_job_skill_live_revision_claim_stops_before_provider(monkeypatch):
    from packages.core.services import workflow_run_execution_claim as claim_service
    from packages.core.tasks import ai_tasks

    calls = []

    @asynccontextmanager
    async def held_claim(job_id: str, revision: int):
        calls.append(("claim", job_id, revision))
        yield claim_service._claim(
            f"{job_id}:{revision}",
            "other-worker",
            granted=False,
            reason=claim_service.CLAIM_HELD,
        )

    def unexpected_session():
        raise AssertionError("duplicate delivery must stop before DB/provider work")

    monkeypatch.setattr(
        "packages.core.services.workflow_run_execution_claim."
        "scheduled_job_skill_generation_claim",
        held_claim,
    )
    monkeypatch.setattr(
        "packages.core.database.create_worker_session",
        unexpected_session,
    )

    ai_tasks.generate_job_skill.run(
        "job-pk-single-flight",
        "Prepare the report",
        "Report",
        8,
    )

    assert calls == [("claim", "job-pk-single-flight", 8)]


def test_generate_job_skill_legacy_delivery_claims_current_revision(monkeypatch):
    from packages.core.services import workflow_run_execution_claim as claim_service
    from packages.core.tasks import ai_tasks

    job = SimpleNamespace(id="job-legacy", revision=11)
    calls = []

    class Result:
        def scalar_one_or_none(self):
            return job

    class FakeDB:
        async def __aenter__(self):
            calls.append("session")
            return self

        async def __aexit__(self, *_args):
            return False

        async def execute(self, _statement):
            calls.append("revision-read")
            return Result()

    class Factory:
        def __call__(self):
            return FakeDB()

    @asynccontextmanager
    async def held_claim(job_id: str, revision: int):
        calls.append(("claim", job_id, revision))
        yield claim_service._claim(
            f"{job_id}:{revision}",
            "new-worker",
            granted=False,
            reason=claim_service.CLAIM_HELD,
        )

    monkeypatch.setattr(
        "packages.core.database.create_worker_session",
        lambda: Factory(),
    )
    monkeypatch.setattr(
        "packages.core.services.workflow_run_execution_claim."
        "scheduled_job_skill_generation_claim",
        held_claim,
    )

    ai_tasks.generate_job_skill.run(job.id, "legacy prompt", "Legacy")

    assert calls == [
        "session",
        "revision-read",
        ("claim", job.id, 11),
    ]


def test_generate_job_skill_legacy_delivery_stops_if_revision_advances(monkeypatch):
    from packages.core.tasks import ai_tasks

    revision_snapshot = SimpleNamespace(id="job-legacy-race", revision=11)
    current_job = SimpleNamespace(
        id=revision_snapshot.id,
        job_id="legacy-race",
        entity_id="entity-legacy-race",
        workspace_id=None,
        user_id="user-legacy-race",
        name="Legacy race",
        payload_message="current prompt",
        agent_id="agent-legacy-race",
        execution_target={},
        enabled=True,
        revision=12,
    )
    results = iter((revision_snapshot, current_job))

    class Result:
        def __init__(self, value):
            self.value = value

        def scalar_one_or_none(self):
            return self.value

    class FakeDB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def execute(self, _statement):
            return Result(next(results))

    class Factory:
        def __call__(self):
            return FakeDB()

    async def unexpected_credit(*_args, **_kwargs):
        raise AssertionError("a superseded legacy delivery must not spend credits")

    monkeypatch.setattr(
        "packages.core.database.create_worker_session",
        lambda: Factory(),
    )
    monkeypatch.setattr(
        "packages.core.services.workflow_run_execution_claim."
        "scheduled_job_skill_generation_claim",
        _granted_scheduled_skill_claim,
    )
    monkeypatch.setattr(
        ai_tasks,
        "runtime_assert_credit_available",
        unexpected_credit,
    )

    ai_tasks.generate_job_skill.run(
        revision_snapshot.id,
        "legacy prompt",
        "Legacy race",
    )


def test_generate_job_skill_paused_workspace_stops_before_credit(monkeypatch):
    from packages.core.tasks import ai_tasks

    job = SimpleNamespace(
        id="job-paused-workspace",
        job_id="job-paused-workspace",
        entity_id="entity-paused-workspace",
        workspace_id="workspace-paused",
        user_id="user-paused",
        name="Paused",
        payload_message="Do billable work",
        agent_id="agent-paused",
        execution_target={},
        enabled=True,
        revision=4,
    )
    paused_workspace = SimpleNamespace(
        status="paused",
        deleted_at=None,
    )
    results = iter((job, paused_workspace))

    class Result:
        def __init__(self, value):
            self.value = value

        def scalar_one_or_none(self):
            return self.value

    class FakeDB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def execute(self, _statement):
            return Result(next(results))

    class Factory:
        def __call__(self):
            return FakeDB()

    async def unexpected_credit(*_args, **_kwargs):
        raise AssertionError("paused Workspace must stop before the credit gate")

    monkeypatch.setattr(
        "packages.core.database.create_worker_session",
        lambda: Factory(),
    )
    monkeypatch.setattr(
        "packages.core.services.workflow_run_execution_claim."
        "scheduled_job_skill_generation_claim",
        _granted_scheduled_skill_claim,
    )
    monkeypatch.setattr(
        ai_tasks,
        "runtime_assert_credit_available",
        unexpected_credit,
    )

    ai_tasks.generate_job_skill.run(
        job.id,
        job.payload_message,
        job.name,
        job.revision,
    )


def test_existing_job_skill_generation_is_copy_on_write_after_read_transaction(
    monkeypatch,
):
    from packages.core.services.scheduler_service import ScheduledJobMutationFactory
    from packages.core.tasks import ai_tasks
    from packages.core.models.workspace import Workspace

    events = []
    old_skill_id = "01H00000000000000000000000"
    job = SimpleNamespace(
        id="job-pk-update-skill",
        job_id="job-update-skill",
        entity_id="entity-update-skill",
        workspace_id="workspace-update-skill",
        user_id="user-update-skill",
        name="Updated automation",
        payload_message="new prompt",
        agent_id="agent-update-skill",
        execution_target={"skill_id": old_skill_id, "preserve": True},
        enabled=True,
        revision=9,
        skill_generation_revision=9,
        skill_generation_attempts=0,
        skill_generation_next_attempt_at=None,
        skill_generation_last_error=None,
    )
    new_skill = SimpleNamespace(
        id="01H00000000000000000000001",
        name="Replacement generated Skill",
        system_prompt="replacement procedure",
        config={"complexity": "worker"},
    )

    class Result:
        def __init__(self, value):
            self.value = value

        def scalar_one_or_none(self):
            return self.value

    class FakeDB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def execute(self, _statement):
            entity = _statement.column_descriptions[0].get("entity")
            if entity is Workspace:
                lock_kind = (
                    "workspace-lock"
                    if getattr(_statement, "_for_update_arg", None) is not None
                    else "workspace-read"
                )
                events.append(lock_kind)
            return Result(job)

        async def rollback(self):
            events.append("read-transaction-ended")

        async def commit(self):
            events.append("commit")

    class Factory:
        def __call__(self):
            return FakeDB()

    async def allow_credit(*_args, **_kwargs):
        events.append("credit")

    async def no_byok(*_args, **_kwargs):
        return False

    async def generate_draft(*_args, **_kwargs):
        assert events[-1] == "commit"
        events.append("provider")
        return SimpleNamespace(spec={}, review_rounds=1)

    async def lifecycle_lock(_db, *, entity_id):
        assert entity_id == job.entity_id
        events.append("lifecycle")

    async def persist_draft(*_args, **kwargs):
        assert kwargs["entity_id"] == job.entity_id
        assert "supersedes_skill_id" not in kwargs["config_overrides"]
        events.append("new-skill-write")
        return new_skill

    async def apply(cls, _db, current_job, updates, **kwargs):
        assert current_job is job
        assert updates == {
            "execution_target": {
                "skill_id": new_skill.id,
                "preserve": True,
                "complexity": "worker",
            },
            "execution_type": "skill",
            "execution_script": None,
        }
        assert kwargs["expected_revision"] == job.revision
        events.append("job-cas")
        return SimpleNamespace(job=job)

    async def unexpected_patch(*_args, **_kwargs):
        raise AssertionError("an existing Skill must not be changed in place")

    monkeypatch.setattr(
        "packages.core.database.create_worker_session",
        lambda: Factory(),
    )
    monkeypatch.setattr(
        "packages.core.services.workflow_run_execution_claim."
        "scheduled_job_skill_generation_claim",
        _granted_scheduled_skill_claim,
    )
    monkeypatch.setattr(ai_tasks, "runtime_assert_credit_available", allow_credit)
    monkeypatch.setattr(
        ai_tasks,
        "_scheduled_job_skill_generation_byok",
        no_byok,
    )
    monkeypatch.setattr(
        "packages.core.services.skill_generator.generate_skill_draft",
        generate_draft,
    )
    monkeypatch.setattr(
        "packages.core.services.skill_generator.persist_generated_skill_draft",
        persist_draft,
    )
    monkeypatch.setattr(
        "packages.core.services.reusable_resource_locks."
        "lock_reusable_resource_lifecycle",
        lifecycle_lock,
    )
    monkeypatch.setattr(
        "packages.core.services.skill_service.update_skill",
        unexpected_patch,
    )
    monkeypatch.setattr(
        ScheduledJobMutationFactory,
        "apply",
        classmethod(apply),
    )

    ai_tasks.generate_job_skill.run(
        job.id,
        job.payload_message,
        job.name,
        job.revision,
    )

    assert events == [
        "workspace-read",
        "credit",
        "read-transaction-ended",
        "lifecycle",
        "workspace-lock",
        "commit",
        "provider",
        "lifecycle",
        "workspace-lock",
        "new-skill-write",
        "job-cas",
        "commit",
    ]


def test_generate_job_skill_discards_stale_revision_without_overwrite(monkeypatch):
    from packages.core.revisions import StaleRevisionError
    from packages.core.services.scheduler_service import ScheduledJobMutationFactory
    from packages.core.tasks import ai_tasks

    job = SimpleNamespace(
        id="job-pk",
        job_id="job-id",
        entity_id="entity-1",
        workspace_id="workspace-1",
        user_id="user-1",
        name="Current automation",
        payload_message="current operator prompt",
        agent_id="agent-1",
        execution_target={"preserve": True},
        enabled=True,
        revision=7,
        skill_generation_revision=7,
        skill_generation_attempts=0,
        skill_generation_next_attempt_at=None,
        skill_generation_last_error=None,
    )
    events = []

    class Result:
        def scalar_one_or_none(self):
            return job

    class FakeDB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def execute(self, _statement):
            return Result()

        async def commit(self):
            events.append("commit")

        async def rollback(self):
            events.append("rollback")

    class Factory:
        def __call__(self):
            return FakeDB()

    async def allow_credit(*_args, **_kwargs):
        return None

    async def fake_generate_skill_draft(prompt, *_args, **_kwargs):
        events.append(prompt)
        return SimpleNamespace(spec={}, review_rounds=1)

    async def fake_persist_generated_skill_draft(*_args, **_kwargs):
        return SimpleNamespace(
            id="skill-1",
            name="Generated",
            config={"complexity": "primary"},
            system_prompt="generated system prompt",
        )

    async def stale_apply(cls, _db, selected_job, _updates, **kwargs):
        assert selected_job is job
        assert kwargs["expected_revision"] == 7
        raise StaleRevisionError(
            target_kind="scheduled_job",
            target_id=job.id,
            expected=7,
            actual=8,
        )

    monkeypatch.setattr(
        "packages.core.database.create_worker_session",
        lambda: Factory(),
    )
    monkeypatch.setattr(
        "packages.core.services.workflow_run_execution_claim."
        "scheduled_job_skill_generation_claim",
        _granted_scheduled_skill_claim,
    )
    monkeypatch.setattr(ai_tasks, "runtime_assert_credit_available", allow_credit)
    monkeypatch.setattr(
        "packages.core.services.skill_generator.generate_skill_draft",
        fake_generate_skill_draft,
    )
    monkeypatch.setattr(
        "packages.core.services.skill_generator.persist_generated_skill_draft",
        fake_persist_generated_skill_draft,
    )
    monkeypatch.setattr(
        ScheduledJobMutationFactory,
        "apply",
        classmethod(stale_apply),
    )

    ai_tasks.generate_job_skill.run(
        job.id,
        job.payload_message,
        "Old name",
        job.revision,
    )

    assert events == [
        "rollback",
        "commit",
        "Scheduled automation: Current automation\n\ncurrent operator prompt",
        "rollback",
    ]
    assert job.execution_target == {"preserve": True}


def test_generate_job_skill_never_trusts_ownership_metadata_for_in_place_patch(
    monkeypatch,
):
    from packages.core.services.scheduler_service import ScheduledJobMutationFactory
    from packages.core.tasks import ai_tasks

    job = SimpleNamespace(
        id="job-pk-private-skill",
        job_id="job-private-skill",
        entity_id="entity-private-skill",
        workspace_id="workspace-private-skill",
        user_id="user-private-skill",
        name="Private automation",
        payload_message="Prepare the private report.",
        agent_id="agent-private-skill",
        execution_target={"skill_id": "shared-skill", "preserve": True},
        enabled=True,
        revision=3,
        skill_generation_revision=3,
        skill_generation_attempts=0,
        skill_generation_next_attempt_at=None,
        skill_generation_last_error=None,
    )
    shared_skill = SimpleNamespace(
        id="shared-skill",
        entity_id=job.entity_id,
        version="1.4.2",
        revision=9,
        config={
            "source": "scheduled_job",
            "generation_source": "llm-generated",
            "scheduled_job_pk": job.id,
            "scheduled_job_id": job.job_id,
        },
    )
    generated_skill = SimpleNamespace(
        id="generated-private-skill",
        name="Generated private Skill",
        config={"complexity": "worker"},
        system_prompt="Private generated instructions",
    )
    results = iter((job, job, shared_skill, job, job, job, job))
    captured: dict[str, object] = {}

    class Result:
        def __init__(self, value):
            self.value = value

        def scalar_one_or_none(self):
            return self.value

    class FakeDB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def execute(self, _statement):
            return Result(next(results))

        async def rollback(self):
            return None

        async def commit(self):
            captured["committed"] = True

    class Factory:
        def __call__(self):
            return FakeDB()

    async def allow_credit(*_args, **_kwargs):
        return None

    async def no_byok(*_args, **_kwargs):
        return False

    async def generate_draft(*_args, **_kwargs):
        return SimpleNamespace(spec={}, review_rounds=1)

    async def persist_draft(*_args, **kwargs):
        captured["config_overrides"] = kwargs["config_overrides"]
        captured["persistence_scope"] = {
            "owner_user_id": kwargs["owner_user_id"],
            "workspace_id": kwargs["workspace_id"],
            "visibility": kwargs["visibility"],
            "version": kwargs["version"],
        }
        return generated_skill

    async def unexpected_patch(*_args, **_kwargs):
        raise AssertionError("a shared Skill must never be patched")

    async def lock_lifecycle(*_args, **_kwargs):
        return None

    async def apply(cls, _db, locked_job, updates, **kwargs):
        assert locked_job is job
        captured["updates"] = updates
        captured["expected_revision"] = kwargs["expected_revision"]
        return SimpleNamespace(job=job)

    monkeypatch.setattr(
        "packages.core.database.create_worker_session",
        lambda: Factory(),
    )
    monkeypatch.setattr(
        "packages.core.services.workflow_run_execution_claim."
        "scheduled_job_skill_generation_claim",
        _granted_scheduled_skill_claim,
    )
    monkeypatch.setattr(ai_tasks, "runtime_assert_credit_available", allow_credit)
    monkeypatch.setattr(ai_tasks, "_scheduled_job_skill_generation_byok", no_byok)
    monkeypatch.setattr(
        "packages.core.services.skill_generator.build_skill_update_patch",
        unexpected_patch,
    )
    monkeypatch.setattr(
        "packages.core.services.skill_generator.generate_skill_draft",
        generate_draft,
    )
    monkeypatch.setattr(
        "packages.core.services.skill_generator.persist_generated_skill_draft",
        persist_draft,
    )
    monkeypatch.setattr(
        "packages.core.services.reusable_resource_locks."
        "lock_reusable_resource_lifecycle",
        lock_lifecycle,
    )
    monkeypatch.setattr(
        ScheduledJobMutationFactory,
        "apply",
        classmethod(apply),
    )

    ai_tasks.generate_job_skill.run(
        job.id,
        job.payload_message,
        job.name,
        job.revision,
    )

    assert shared_skill.config == {
        "source": "scheduled_job",
        "generation_source": "llm-generated",
        "scheduled_job_pk": job.id,
        "scheduled_job_id": job.job_id,
    }
    assert captured["expected_revision"] == 3
    assert captured["config_overrides"] == {
        "source": "scheduled_job",
        "generation_source": "llm-generated",
        "scheduled_job_id": job.job_id,
        "scheduled_job_pk": job.id,
        "workspace_id": job.workspace_id,
        "agent_id": job.agent_id,
        "automation_name": job.name,
        "version_family": f"scheduled-job:{job.job_id}",
        "generated_version": "1.4.3",
        "previous_version": "1.4.2",
    }
    assert captured["persistence_scope"] == {
        "owner_user_id": job.user_id,
        "workspace_id": job.workspace_id,
        "visibility": "workspace",
        "version": "1.4.3",
    }
    assert captured["updates"] == {
        "execution_target": {
            "skill_id": generated_skill.id,
            "preserve": True,
            "complexity": "worker",
        },
        "execution_type": "skill",
        "execution_script": None,
    }
    assert captured["committed"] is True


def test_generate_job_skill_skips_obsolete_queued_prompt(monkeypatch):
    from packages.core.tasks import ai_tasks

    job = SimpleNamespace(
        id="job-pk-obsolete",
        job_id="job-id-obsolete",
        entity_id="entity-1",
        workspace_id="workspace-1",
        user_id="user-1",
        name="Current automation",
        payload_message="new operator prompt",
        agent_id="agent-1",
        execution_target={},
        enabled=True,
        revision=4,
        skill_generation_revision=4,
        skill_generation_attempts=0,
        skill_generation_next_attempt_at=None,
        skill_generation_last_error=None,
    )

    class Result:
        def scalar_one_or_none(self):
            return job

    class FakeDB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def execute(self, _statement):
            return Result()

    class Factory:
        def __call__(self):
            return FakeDB()

    async def unexpected(*_args, **_kwargs):
        raise AssertionError("obsolete generation must stop before credit or LLM")

    monkeypatch.setattr(
        "packages.core.database.create_worker_session",
        lambda: Factory(),
    )
    monkeypatch.setattr(
        "packages.core.services.workflow_run_execution_claim."
        "scheduled_job_skill_generation_claim",
        _granted_scheduled_skill_claim,
    )
    monkeypatch.setattr(ai_tasks, "runtime_assert_credit_available", unexpected)
    monkeypatch.setattr(
        "packages.core.services.skill_generator.generate_skill_draft",
        unexpected,
    )

    ai_tasks.generate_job_skill.run(
        job.id,
        "old queued prompt",
        "Old name",
        job.revision - 1,
    )
    ai_tasks.generate_job_skill.run(
        job.id,
        "old queued prompt",
        "Old name",
        job.revision,
    )
