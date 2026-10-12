"""Plan executor deliveries require the durable outbox generation fence."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from packages.core.plans.executor import PlanExecutor


def _cycle_claim():
    return SimpleNamespace(
        event_id="E1",
        plan_id="P1",
        wake_generation="G1",
        claim_token="C1",
    )


@pytest.mark.parametrize(
    ("event_id", "wake_generation"),
    [
        (None, None),
        ("E1", None),
        (None, "G1"),
        ("", "G1"),
        ("E1", ""),
    ],
)
def test_generationless_run_plan_fails_before_claim_or_execution(
    monkeypatch,
    event_id,
    wake_generation,
):
    from packages.core.tasks import ai_tasks

    monkeypatch.setattr(
        ai_tasks,
        "_claim_plan_cycle_delivery",
        lambda **_kwargs: pytest.fail("invalid delivery must not claim an outbox row"),
    )
    monkeypatch.setattr(
        PlanExecutor,
        "run_cycle",
        lambda *_args, **_kwargs: pytest.fail("invalid delivery must not execute"),
    )

    assert ai_tasks.run_plan(
        "P1",
        outbox_event_id=event_id,
        wake_generation=wake_generation,
    ) == {
        "plan_id": "P1",
        "status": "invalid_plan_cycle_delivery",
        "next_action": "stop",
    }


def test_run_plan_ack_failure_does_not_fail_committed_business_state(monkeypatch):
    from packages.core.tasks import ai_tasks

    async def fake_cycle(self, plan_id):
        return {"plan_id": plan_id, "status": "completed", "next_action": "stop"}

    monkeypatch.setattr(PlanExecutor, "run_cycle", fake_cycle)
    monkeypatch.setattr(
        ai_tasks,
        "_claim_plan_cycle_delivery",
        lambda **_kwargs: (_cycle_claim(), "claimed"),
    )
    monkeypatch.setattr(
        ai_tasks,
        "_settle_plan_cycle_delivery",
        lambda **_kwargs: (_ for _ in ()).throw(ConnectionError("ack database down")),
    )
    failed: list[tuple] = []
    monkeypatch.setattr(
        ai_tasks,
        "_mark_plan_failed",
        lambda *args, **kwargs: failed.append((args, kwargs)),
    )

    result = ai_tasks.run_plan(
        "P1",
        outbox_event_id="E1",
        wake_generation="G1",
    )

    assert result == {"plan_id": "P1", "status": "completed", "next_action": "stop"}
    assert failed == []


def test_stale_heavy_generation_never_enters_plan_executor(monkeypatch):
    from packages.core.tasks import ai_tasks

    called: list[str] = []

    async def forbidden_cycle(self, plan_id):
        called.append(plan_id)
        raise AssertionError("stale HEAVY delivery crossed the generation fence")

    monkeypatch.setattr(PlanExecutor, "run_cycle", forbidden_cycle)
    monkeypatch.setattr(
        ai_tasks,
        "_claim_plan_cycle_delivery",
        lambda **_kwargs: (None, "stale_generation"),
    )

    result = ai_tasks.run_plan(
        "P1",
        outbox_event_id="E1",
        wake_generation="G1",
    )

    assert result == {
        "plan_id": "P1",
        "status": "stale_plan_cycle_delivery",
        "reason": "stale_generation",
        "next_action": "stop",
    }
    assert called == []


def test_duplicate_heavy_claim_loser_never_enters_plan_executor(monkeypatch):
    from packages.core.tasks import ai_tasks

    async def forbidden_cycle(self, plan_id):
        raise AssertionError(f"duplicate HEAVY executed Plan {plan_id}")

    monkeypatch.setattr(PlanExecutor, "run_cycle", forbidden_cycle)
    monkeypatch.setattr(
        ai_tasks,
        "_claim_plan_cycle_delivery",
        lambda **_kwargs: (None, "execution_claim_held"),
    )
    monkeypatch.setattr(
        ai_tasks,
        "_settle_plan_cycle_delivery",
        lambda **_kwargs: pytest.fail("claim loser must not settle the generation"),
    )

    result = ai_tasks.run_plan(
        "P1",
        outbox_event_id="E1",
        wake_generation="G1",
    )

    assert result == {
        "plan_id": "P1",
        "status": "plan_cycle_execution_claim_held",
        "reason": "execution_claim_held",
        "next_action": "stop",
    }


def test_run_plan_retry_releases_exact_claim_before_requeue(monkeypatch):
    from packages.core.tasks import ai_tasks

    class RetrySignal(Exception):
        pass

    async def failed_cycle(self, plan_id):
        raise RuntimeError(f"transient failure for {plan_id}")

    monkeypatch.setattr(PlanExecutor, "run_cycle", failed_cycle)
    monkeypatch.setattr(
        ai_tasks,
        "_claim_plan_cycle_delivery",
        lambda **_kwargs: (_cycle_claim(), "claimed"),
    )
    released: list[tuple[object, float]] = []
    monkeypatch.setattr(
        ai_tasks,
        "_release_plan_cycle_delivery",
        lambda claim, *, delay_seconds: released.append((claim, delay_seconds)) or True,
    )
    monkeypatch.setattr(
        ai_tasks.run_plan,
        "retry",
        lambda **_kwargs: RetrySignal(),
    )

    with pytest.raises(RetrySignal):
        ai_tasks.run_plan(
            "P1",
            outbox_event_id="E1",
            wake_generation="G1",
        )

    assert released == [(_cycle_claim(), 30)]
