from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from packages.core.contracts.agent_initiative import (
    DecisionPolicy,
    InitiativeDecision,
    InterruptionPolicy,
)
from packages.core.services.agent_initiative_decision import (
    InitiativeAdmissionRejected,
    _complete_one_shot_initiative,
    _delivery_reservations,
    _exact_runtime_authority,
    admit_scheduled_initiative,
    evaluate_initiative_policy,
    initiative_decision_fingerprint,
    initiative_result_revision_matches,
    initiative_task_marker,
    parse_initiative_decision,
)
from packages.core.services.agent_initiative_delivery import (
    _DELIVERY_LEASE,
    _MAX_DELIVERY_ATTEMPTS,
    _advance_delivery_state,
    _delivery_retry_at,
    _publish_surface_event,
    initiative_notification_body,
    initiative_notification_link,
    initiative_quiet_hours_active,
)


def _decision(**updates) -> InitiativeDecision:
    payload = {
        "version": "1",
        "decision": "notify",
        "message": "A material update happened.",
        "reason_code": "material_update",
        "severity": "info",
        "dedupe_key": "event-1",
        "evidence_refs": [],
    }
    payload.update(updates)
    return InitiativeDecision.model_validate(payload)


def _at_trigger() -> dict:
    return {
        "version": "1",
        "kind": "at",
        "run_at": "2026-09-05T10:00:00+00:00",
        "timezone": "UTC",
    }


def test_decision_parser_is_json_only_and_strict() -> None:
    valid, error = parse_initiative_decision(
        '{"version":"1","decision":"notify","message":"Ready",'
        '"reason_code":"ready","severity":"info","evidence_refs":[]}'
    )
    assert error is None
    assert valid is not None and valid.decision == "notify"

    for invalid in (
        "```json\n{\"version\":\"1\",\"decision\":\"silent\","
        "\"reason_code\":\"none\"}\n```",
        {"version": "1", "decision": "silent", "reason_code": "none", "extra": True},
        {"version": "1", "decision": "silent", "reason_code": "none", "message": "raw"},
    ):
        parsed, validation_error = parse_initiative_decision(invalid)
        assert parsed is None
        assert validation_error


def test_fingerprint_is_normalized_and_evidence_order_independent() -> None:
    left = _decision(
        message="  Material   UPDATE  ",
        dedupe_key=" Event-1 ",
        evidence_refs=[
            {"type": "url", "url": "HTTPS://EXAMPLE.COM/B"},
            {"type": "id", "id": "42"},
        ],
    )
    right = _decision(
        message="material update",
        dedupe_key="event-1",
        evidence_refs=[
            {"type": "ID", "id": "42"},
            {"type": "URL", "url": "https://example.com/b"},
        ],
    )

    assert initiative_decision_fingerprint(left) == initiative_decision_fingerprint(right)


def test_policy_enforces_mute_dedupe_cooldown_rate_and_paused_run_now() -> None:
    now = datetime(2026, 9, 4, 12, tzinfo=timezone.utc)
    policy = DecisionPolicy(
        mode="on_change",
        cooldown_seconds=300,
        max_notifications_per_day=2,
    )
    decision = _decision()
    fingerprint = initiative_decision_fingerprint(decision)

    muted = evaluate_initiative_policy(
        decision=decision,
        initiative_status="muted",
        policy=policy,
        last_fingerprint=None,
        cooldown_until=None,
        window_started_at=None,
        notification_count=0,
        now=now,
    )
    assert (muted.final_decision, muted.reason_code, muted.should_deliver) == (
        "suppressed", "initiative_muted", False,
    )

    duplicate = evaluate_initiative_policy(
        decision=decision,
        initiative_status="active",
        policy=policy,
        last_fingerprint=None,
        reserved_fingerprints={fingerprint},
        cooldown_until=None,
        window_started_at=None,
        notification_count=0,
        now=now,
    )
    assert duplicate.reason_code == "duplicate"

    for mode in ("always", "on_condition"):
        repeated = evaluate_initiative_policy(
            decision=decision,
            initiative_status="active",
            policy=DecisionPolicy(mode=mode),
            last_fingerprint=fingerprint,
            reserved_fingerprints={fingerprint},
            cooldown_until=None,
            window_started_at=None,
            notification_count=0,
            now=now,
        )
        assert repeated.final_decision == "notify"
        assert repeated.should_deliver is True

    cooldown = evaluate_initiative_policy(
        decision=decision,
        initiative_status="active",
        policy=policy,
        last_fingerprint=None,
        cooldown_until=now + timedelta(seconds=1),
        window_started_at=None,
        notification_count=0,
        now=now,
    )
    assert cooldown.reason_code == "cooldown"

    limited = evaluate_initiative_policy(
        decision=decision,
        initiative_status="active",
        policy=policy,
        last_fingerprint=None,
        cooldown_until=None,
        window_started_at=now - timedelta(hours=1),
        notification_count=2,
        now=now,
    )
    assert limited.reason_code == "rate_limited"

    paused_manual = evaluate_initiative_policy(
        decision=decision,
        initiative_status="paused",
        policy=policy,
        last_fingerprint=None,
        cooldown_until=None,
        window_started_at=None,
        notification_count=0,
        now=now,
        allow_paused_delivery=True,
    )
    assert paused_manual.final_decision == "notify"
    assert paused_manual.should_deliver is True


@pytest.mark.asyncio
async def test_pending_delivery_receipts_reserve_cooldown_and_rate_capacity() -> None:
    candidate = _decision().model_dump(mode="json", exclude_none=True)
    statements = []

    class Scalars:
        def all(self):
            return [candidate]

    class Result:
        def scalars(self):
            return Scalars()

    class DB:
        async def execute(self, statement):
            statements.append(statement)
            return Result()

    reservations = await _delivery_reservations(
        DB(),
        initiative_id="initiative-1",
        current_run_id="run-2",
    )
    assert reservations.pending_count == 1
    assert reservations.fingerprints == {
        initiative_decision_fingerprint(_decision())
    }
    params = statements[0].compile().params
    status_values = next(value for key, value in params.items() if "status" in key)
    assert set(status_values) == {"pending", "error", "processing"}

    now = datetime.now(timezone.utc)
    cooldown = evaluate_initiative_policy(
        decision=_decision(dedupe_key="different"),
        initiative_status="active",
        policy=DecisionPolicy(mode="always", cooldown_seconds=60),
        last_fingerprint=None,
        cooldown_until=None,
        window_started_at=None,
        notification_count=0,
        now=now,
        pending_delivery_count=reservations.pending_count,
    )
    assert cooldown.reason_code == "cooldown"
    limited = evaluate_initiative_policy(
        decision=_decision(dedupe_key="different"),
        initiative_status="active",
        policy=DecisionPolicy(
            mode="always",
            cooldown_seconds=0,
            max_notifications_per_day=1,
        ),
        last_fingerprint=None,
        cooldown_until=None,
        window_started_at=None,
        notification_count=0,
        now=now,
        pending_delivery_count=reservations.pending_count,
    )
    assert limited.reason_code == "rate_limited"


def test_silent_model_decision_never_creates_a_delivery_candidate() -> None:
    result = evaluate_initiative_policy(
        decision=_decision(
            decision="silent",
            message=None,
            reason_code="nothing_new",
        ),
        initiative_status="active",
        policy=DecisionPolicy(),
        last_fingerprint=None,
        cooldown_until=None,
        window_started_at=None,
        notification_count=0,
        now=datetime.now(timezone.utc),
    )

    assert result.final_decision == "silent"
    assert result.fingerprint is None
    assert result.should_deliver is False


def test_marker_requires_exact_versioned_runtime_identity() -> None:
    marker = initiative_task_marker({
        "agent_initiative": {
            "version": "1",
            "initiative_id": "initiative-1",
            "initiative_run_id": "run-1",
            "initiative_revision": 2,
            "agent_subscription_id": "subscription-1",
            "owner_user_id": "user-1",
        }
    })
    assert marker is not None
    assert marker.initiative_revision == 2
    assert initiative_task_marker({
        "agent_initiative": {
            "version": "1",
            "initiative_id": "initiative-1",
            "initiative_run_id": "run-1",
            "initiative_revision": True,
            "agent_subscription_id": "subscription-1",
            "owner_user_id": "user-1",
        }
    }) is None


def test_result_revision_fence_allows_only_exact_or_system_one_shot_bump() -> None:
    marker = initiative_task_marker({
        "agent_initiative": {
            "version": "1",
            "initiative_id": "initiative-1",
            "initiative_run_id": "run-1",
            "initiative_revision": 3,
            "agent_subscription_id": "subscription-1",
            "owner_user_id": "user-1",
        }
    })
    assert marker is not None
    run = SimpleNamespace(status="running", trigger_type="cron")
    initiative = SimpleNamespace(
        revision=3,
        status="active",
        trigger_kind="cron",
    )
    assert initiative_result_revision_matches(
        initiative=initiative,
        marker=marker,
        run=run,
    ) is True

    initiative.revision = 4
    assert initiative_result_revision_matches(
        initiative=initiative,
        marker=marker,
        run=run,
    ) is False

    initiative.status = "completed"
    initiative.trigger_kind = "at"
    run.status = "succeeded"
    run.trigger_type = "at"
    assert initiative_result_revision_matches(
        initiative=initiative,
        marker=marker,
        run=run,
        allow_one_shot_completion=True,
    ) is True
    initiative.revision = 5
    assert initiative_result_revision_matches(
        initiative=initiative,
        marker=marker,
        run=run,
        allow_one_shot_completion=True,
    ) is False


def test_task_runner_metadata_preserves_exact_initiative_identity() -> None:
    from packages.core.ai.task_runner import _task_runtime_metadata

    marker = {
        "version": "1",
        "initiative_id": "initiative-1",
        "initiative_run_id": "run-1",
        "initiative_revision": 2,
        "agent_subscription_id": "subscription-1",
        "owner_user_id": "user-1",
    }
    assert _task_runtime_metadata(
        SimpleNamespace(details={"agent_initiative": marker})
    ) == {"agent_initiative": marker}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case,reason_code",
    [
        ("subscription", "initiative_subscription_inactive"),
        ("owner", "initiative_owner_inactive"),
        ("agent", "initiative_agent_inactive"),
        ("conversation", "initiative_conversation_invalid"),
        ("workspace", "initiative_workspace_inactive"),
    ],
)
async def test_runtime_authority_lifecycle_failures_are_rejected_before_execution(
    case: str,
    reason_code: str,
) -> None:
    workspace_id = "workspace-1" if case == "workspace" else None
    agent_id = "agent-1" if case == "agent" else "manor-master"
    initiative = SimpleNamespace(
        agent_subscription_id="subscription-1",
        owner_user_id="user-1",
        entity_id="entity-1",
        workspace_id=workspace_id,
    )
    subscription = SimpleNamespace(
        id="subscription-1",
        agent_id=agent_id,
        owner_user_id="user-1",
        workspace_id=workspace_id,
    )
    owner = SimpleNamespace(role="member")
    conversation = SimpleNamespace(
        id="conversation-1",
        entity_id="entity-1",
        status="active",
        workspace_id=workspace_id,
        scope="workspace_main" if workspace_id else "personal",
        user_id="user-1",
        agent_id=agent_id,
        agent_subscription_id="subscription-1",
    )
    if case == "subscription":
        results = [None]
    elif case == "owner":
        results = [subscription, None]
    elif case == "agent":
        results = [subscription, owner, None]
    elif case == "conversation":
        results = [subscription, owner, None]
    else:
        results = [subscription, owner, conversation, None]

    class Result:
        def __init__(self, value):
            self.value = value

        def scalar_one_or_none(self):
            return self.value

    class DB:
        async def execute(self, _statement):
            return Result(results.pop(0))

    with pytest.raises(InitiativeAdmissionRejected) as exc_info:
        await _exact_runtime_authority(
            DB(),
            initiative=initiative,
            subscription_id="subscription-1",
            owner_user_id="user-1",
            agent_id=agent_id,
            conversation_id="conversation-1",
        )
    assert exc_info.value.reason_code == reason_code


@pytest.mark.asyncio
async def test_runtime_authority_accepts_master_subscription_bound_to_main_chat() -> None:
    initiative = SimpleNamespace(
        agent_subscription_id="subscription-master",
        owner_user_id="user-1",
        entity_id="entity-1",
        workspace_id=None,
    )
    subscription = SimpleNamespace(
        id="subscription-master",
        agent_id="manor-master",
        owner_user_id="user-1",
        workspace_id=None,
    )
    owner = SimpleNamespace(role="member")
    conversation = SimpleNamespace(
        id="conversation-1",
        entity_id="entity-1",
        status="active",
        workspace_id=None,
        scope="channel",
        user_id="user-1",
        agent_id=None,
        agent_subscription_id="subscription-master",
    )

    class Result:
        def __init__(self, value):
            self.value = value

        def scalar_one_or_none(self):
            return self.value

    class DB:
        def __init__(self):
            self.results = [subscription, owner, conversation]

        async def execute(self, _statement):
            return Result(self.results.pop(0))

    resolved_subscription, resolved_owner, resolved_conversation = (
        await _exact_runtime_authority(
            DB(),
            initiative=initiative,
            subscription_id="subscription-master",
            owner_user_id="user-1",
            agent_id="manor-master",
            conversation_id="conversation-1",
        )
    )

    assert resolved_subscription is subscription
    assert resolved_owner is owner
    assert resolved_conversation is conversation


@pytest.mark.asyncio
async def test_runtime_authority_does_not_apply_main_chat_exception_to_custom_agent() -> None:
    initiative = SimpleNamespace(
        agent_subscription_id="subscription-1",
        owner_user_id="user-1",
        entity_id="entity-1",
        workspace_id=None,
    )
    subscription = SimpleNamespace(
        id="subscription-1",
        agent_id="agent-1",
        owner_user_id="user-1",
        workspace_id=None,
    )
    owner = SimpleNamespace(role="member")
    conversation = SimpleNamespace(
        id="conversation-1",
        entity_id="entity-1",
        status="active",
        workspace_id=None,
        scope="channel",
        user_id="user-1",
        agent_id=None,
        agent_subscription_id="subscription-1",
    )

    class Result:
        def __init__(self, value):
            self.value = value

        def scalar_one_or_none(self):
            return self.value

    class DB:
        def __init__(self):
            self.results = [subscription, owner, "agent-1", conversation]

        async def execute(self, _statement):
            return Result(self.results.pop(0))

    with pytest.raises(InitiativeAdmissionRejected) as exc_info:
        await _exact_runtime_authority(
            DB(),
            initiative=initiative,
            subscription_id="subscription-1",
            owner_user_id="user-1",
            agent_id="agent-1",
            conversation_id="conversation-1",
        )

    assert exc_info.value.reason_code == "initiative_conversation_invalid"


def test_generic_scheduled_projection_cannot_project_initiative_output() -> None:
    from packages.core.tasks.ai_tasks import _scheduled_agent_result_projection

    job = SimpleNamespace(execution_type="agent_initiative")
    task = SimpleNamespace(details={})
    assert _scheduled_agent_result_projection(task=task, job=job, result={}) is None

    malformed_marker_task = SimpleNamespace(details={"agent_initiative": {}})
    generic_job = SimpleNamespace(execution_type="agent")
    assert _scheduled_agent_result_projection(
        task=malformed_marker_task,
        job=generic_job,
        result={"response": "must not escape"},
    ) is None


def test_notification_rendering_uses_chat_routes_and_bounded_summary() -> None:
    assert initiative_notification_link(None) == "/chat/history"
    assert initiative_notification_link(None, "conversation/1") == (
        "/chat?conversation=conversation%2F1"
    )
    assert initiative_notification_link("workspace-1") == (
        "/chat?workspace=workspace-1"
    )
    assert initiative_notification_link(
        "workspace-1",
        "workspace/conversation-1",
    ) == (
        "/chat?conversation=workspace%2Fconversation-1"
    )
    long_message = "sensitive-result " * 100
    summary = initiative_notification_body(
        long_message,
        content_mode="summary_and_link",
    )
    assert len(summary) <= 500
    assert summary.endswith("…")
    assert initiative_notification_body(
        long_message,
        content_mode="full",
    ) == long_message.strip()


@pytest.mark.asyncio
async def test_personal_surface_delivery_pushes_conversation_refresh(
    monkeypatch,
) -> None:
    from packages.core.services import realtime

    calls = []

    async def push(user_id, message, *, entity_id):
        calls.append((user_id, entity_id, message))

    monkeypatch.setattr(realtime, "push_conversation_message", push)
    message = SimpleNamespace(
        id="message-1",
        conversation_id="conversation-1",
        message_kind="agent_update",
        author_kind="agent",
    )

    await _publish_surface_event(
        entity_id="entity-1",
        owner_user_id="user-1",
        workspace_id=None,
        message=message,
    )

    assert calls == [(
        "user-1",
        "entity-1",
        {
            "conversation_id": "conversation-1",
            "message_id": "message-1",
            "message_kind": "agent_update",
            "author_kind": "agent",
            "has_pending_action": False,
        },
    )]


@pytest.mark.asyncio
async def test_workspace_surface_realtime_is_acl_scoped_and_exact(
    monkeypatch,
) -> None:
    from packages.core.workspace_chat import service

    published = []

    class FakeRedis:
        async def publish(self, channel, payload):
            published.append((channel, json.loads(payload)))

    async def get_redis():
        return FakeRedis()

    monkeypatch.setattr("packages.core.cache._get_redis", get_redis)
    message = SimpleNamespace(
        id="message-1",
        conversation_id="conversation-1",
        message_kind="agent_update",
        author_kind="agent",
        pending_action=None,
        meta={},
    )

    await service.publish_workspace_chat_message_event(
        "entity-1",
        workspace_id="workspace-1",
        message=message,
    )

    assert published == [(
        "manor:ws_broadcast",
        {
            "target": "workspace",
            "entity_id": "entity-1",
            "workspace_id": "workspace-1",
            "event": "workspace_chat_message",
            "data": {
                "workspace_id": "workspace-1",
                "conversation_id": "conversation-1",
                "message_id": "message-1",
                "message_kind": "agent_update",
                "author_kind": "agent",
                "has_pending_action": False,
                "action_kind": None,
            },
        },
    )]


def test_repeated_deliveries_still_advance_rate_and_cooldown_state() -> None:
    now = datetime(2026, 9, 4, 12, tzinfo=timezone.utc)
    state = SimpleNamespace(
        last_fingerprint=None,
        window_started_at=None,
        notification_count=0,
        last_notified_at=None,
        cooldown_until=None,
        version=1,
    )
    context = SimpleNamespace(
        state=state,
        run=SimpleNamespace(
            candidate_decision=_decision().model_dump(mode="json", exclude_none=True)
        ),
        decision_policy=DecisionPolicy(mode="always", cooldown_seconds=60),
    )

    _advance_delivery_state(context, now)
    _advance_delivery_state(context, now + timedelta(minutes=2))

    assert state.notification_count == 2
    assert state.last_notified_at == now + timedelta(minutes=2)
    assert state.cooldown_until == now + timedelta(minutes=3)
    assert state.version == 3


def test_quiet_hours_keep_inapp_policy_server_owned() -> None:
    custom = InterruptionPolicy.model_validate({
        "version": "1",
        "quiet_hours": {
            "mode": "custom",
            "start": "22:00",
            "end": "08:00",
            "timezone": "America/Los_Angeles",
        },
        "during_quiet_hours": "suppress_external",
    })
    assert initiative_quiet_hours_active(
        custom,
        user_preferences=None,
        now=datetime(2026, 1, 2, 7, tzinfo=timezone.utc),
    ) is True
    assert initiative_quiet_hours_active(
        custom,
        user_preferences=None,
        now=datetime(2026, 1, 2, 20, tzinfo=timezone.utc),
    ) is False

    inherited = InterruptionPolicy()
    assert initiative_quiet_hours_active(
        inherited,
        user_preferences={
            "notifications": {
                "quiet_hours": {"from": "22:00", "to": "08:00", "tz": "UTC"}
            }
        },
        now=datetime(2026, 1, 2, 23, tzinfo=timezone.utc),
    ) is True


@pytest.mark.asyncio
async def test_successful_scheduled_one_shot_completes_and_closes_driver(
    monkeypatch,
) -> None:
    import packages.core.revisions as revisions
    from packages.core.services.scheduler_service import ScheduledJobMutationFactory

    initiative = SimpleNamespace(status="muted", trigger_config=_at_trigger())
    run = SimpleNamespace(id="run-1", status="succeeded", trigger_type="at")
    driver = SimpleNamespace(enabled=True, next_run_at=datetime.now(timezone.utc))
    events: list[tuple[str, object]] = []

    async def bump_revision(_db, record, **kwargs):
        events.append(("initiative", kwargs["patch"]))
        assert record is initiative

    async def apply_locked(_db, record, updates, **_kwargs):
        events.append(("driver", dict(updates)))
        for key, value in updates.items():
            setattr(record, key, value)

    monkeypatch.setattr(revisions, "bump_revision", bump_revision)
    monkeypatch.setattr(ScheduledJobMutationFactory, "apply_locked", apply_locked)

    changed = await _complete_one_shot_initiative(
        object(),
        initiative=initiative,
        driver=driver,
        run=run,
    )

    assert changed is True
    assert initiative.status == "completed"
    assert driver.enabled is False
    assert driver.next_run_at is None
    assert events == [
        ("initiative", {"status": "completed"}),
        ("driver", {"enabled": False, "next_run_at": None}),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("trigger_type,status", [("manual", "succeeded"), ("at", "failed")])
async def test_manual_or_failed_one_shot_does_not_consume_schedule(
    monkeypatch,
    trigger_type: str,
    status: str,
) -> None:
    import packages.core.revisions as revisions
    from packages.core.services.scheduler_service import ScheduledJobMutationFactory

    async def unexpected(*_args, **_kwargs):
        raise AssertionError("one-shot lifecycle must remain unchanged")

    monkeypatch.setattr(revisions, "bump_revision", unexpected)
    monkeypatch.setattr(ScheduledJobMutationFactory, "apply_locked", unexpected)
    next_run = datetime(2026, 9, 5, 10, tzinfo=timezone.utc)
    initiative = SimpleNamespace(status="active", trigger_config=_at_trigger())
    run = SimpleNamespace(id="run-1", status=status, trigger_type=trigger_type)
    driver = SimpleNamespace(enabled=True, next_run_at=next_run)

    changed = await _complete_one_shot_initiative(
        object(),
        initiative=initiative,
        driver=driver,
        run=run,
    )

    assert changed is False
    assert initiative.status == "active"
    assert driver.enabled is True
    assert driver.next_run_at == next_run


@pytest.mark.asyncio
async def test_completed_one_shot_cannot_be_re_admitted() -> None:
    initiative = SimpleNamespace(status="completed")

    class Result:
        def scalar_one_or_none(self):
            return initiative

    class DB:
        async def execute(self, _statement):
            return Result()

    job = SimpleNamespace(
        execution_type="agent_initiative",
        execution_target={
            "managed_by": "agent_initiative",
            "initiative_id": "initiative-1",
            "initiative_revision": 1,
            "subscription_id": "subscription-1",
            "owner_user_id": "user-1",
        },
        agent_initiative_id="initiative-1",
        entity_id="entity-1",
    )
    with pytest.raises(InitiativeAdmissionRejected) as exc_info:
        await admit_scheduled_initiative(
            DB(),
            job=job,
            scheduled_run_id="scheduled-run-2",
            occurrence_key="at:second-scan",
            trigger_type="at",
            now=datetime.now(timezone.utc),
            manual=False,
        )
    assert exc_info.value.reason_code == "initiative_not_active"


@pytest.mark.asyncio
async def test_manual_scheduler_settlement_does_not_disable_delete_after_driver(
    monkeypatch,
) -> None:
    from packages.core.services.scheduler_service import (
        ScheduledJobMutationFactory,
        reconcile_scheduled_job_run_projection,
    )

    run = SimpleNamespace(
        id="scheduled-run-manual",
        status="success",
        trigger_type="manual",
    )
    job = SimpleNamespace(
        job_id="initiative:initiative-1",
        delete_after_run=True,
        enabled=True,
        consecutive_errors=0,
        last_status="running",
    )

    class Result:
        def scalars(self):
            return [run]

    class DB:
        async def execute(self, _statement):
            return Result()

    async def unexpected(*_args, **_kwargs):
        raise AssertionError("manual run-now must not close the one-shot clock")

    monkeypatch.setattr(ScheduledJobMutationFactory, "apply_locked", unexpected)

    auto_paused = await reconcile_scheduled_job_run_projection(
        DB(),
        job,
        finalized_run_id=run.id,
    )

    assert auto_paused is False
    assert job.enabled is True


def test_delivery_recovery_has_bounded_attempts_lease_and_backoff() -> None:
    now = datetime(2026, 9, 5, 12, tzinfo=timezone.utc)

    assert _MAX_DELIVERY_ATTEMPTS == 5
    assert _DELIVERY_LEASE == timedelta(minutes=5)
    assert _delivery_retry_at(now, 1) == now + timedelta(minutes=1)
    assert _delivery_retry_at(now, 2) == now + timedelta(minutes=5)
    assert _delivery_retry_at(now, 3) == now + timedelta(minutes=15)
    assert _delivery_retry_at(now, 4) == now + timedelta(hours=1)
    assert _delivery_retry_at(now, 99) == now + timedelta(hours=1)


def test_initiative_scheduler_uses_primary_model_credit_gate_and_recovery_queue() -> None:
    from packages.core.celery_app import celery_app
    from packages.core.queues import CeleryQueue, TASK_QUEUES
    from packages.core.tasks.scheduler_tasks import (
        _scheduled_job_model_role,
        _scheduled_job_requires_credit_gate,
    )

    job = SimpleNamespace(
        execution_type="agent_initiative",
        execution_target={"complexity": "worker"},
    )
    assert _scheduled_job_model_role(job) == "primary"
    assert _scheduled_job_requires_credit_gate(job) is True
    assert TASK_QUEUES["agent_initiative.deliver"] is CeleryQueue.RECOVERY_V2
    assert TASK_QUEUES["agent_initiative.delivery_sweep"] is CeleryQueue.CONTROL
    assert celery_app.conf.beat_schedule["agent-initiative-delivery-sweep"]["task"] == (
        "agent_initiative.delivery_sweep"
    )
