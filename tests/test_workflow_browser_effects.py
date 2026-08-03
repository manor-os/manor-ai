import pytest

from packages.core.ai.runtime.browser_effects import (
    BrowserEffectContractError,
    BrowserEffectDecision,
    BrowserEffectRecord,
    BrowserEffectStatus,
    BrowserEffectTransitionError,
    browser_effect_execution_decision,
    transition_browser_effect,
    validate_browser_effect_record,
)


def _effect(**overrides):
    record = {
        "effect_id": "effect-1",
        "scene_id": "scene-1",
        "action": "click_element",
        "precondition": {"url": "/marketplace"},
        "expected_postcondition": {"text": "Installed"},
        "status": "not_started",
        "evidence": [],
        "attempt_count": 0,
    }
    record.update(overrides)
    return record


def test_validate_browser_effect_record_normalizes_status_and_evidence():
    effect = validate_browser_effect_record(_effect(
        status="failed",
        evidence=[{"effect_observed": False, "snapshot_id": "snapshot-1"}],
        attempt_count=1,
    ))

    assert effect.effect_id == "effect-1"
    assert effect.status is BrowserEffectStatus.FAILED
    assert effect.evidence == ({"effect_observed": False, "snapshot_id": "snapshot-1"},)
    assert effect.attempt_count == 1


@pytest.mark.parametrize(
    "record",
    [
        None,
        _effect(effect_id=""),
        _effect(scene_id=""),
        _effect(action=""),
        _effect(status="completed"),
        _effect(precondition=[]),
        _effect(expected_postcondition=[]),
        _effect(evidence=["snapshot-1"]),
        _effect(attempt_count=-1),
        _effect(attempt_count=True),
    ],
)
def test_validate_browser_effect_record_rejects_malformed_values(record):
    with pytest.raises(BrowserEffectContractError):
        validate_browser_effect_record(record)


def test_validate_browser_effect_record_revalidates_record_instances():
    invalid = BrowserEffectRecord(
        effect_id="effect-1",
        scene_id="scene-1",
        action="click_element",
        precondition={},
        expected_postcondition={},
        status=BrowserEffectStatus.FAILED,
        evidence=(),
        attempt_count=-1,
    )

    with pytest.raises(BrowserEffectContractError):
        validate_browser_effect_record(invalid)


def test_not_started_effect_is_executable():
    effect = validate_browser_effect_record(_effect())

    assert browser_effect_execution_decision(effect) is BrowserEffectDecision.EXECUTE


def test_observed_complete_effect_is_reused():
    effect = validate_browser_effect_record(_effect(
        status="observed_complete",
        evidence=[{"effect_observed": True, "snapshot_id": "snapshot-2"}],
        attempt_count=1,
    ))

    assert browser_effect_execution_decision(effect) is BrowserEffectDecision.REUSE


def test_failed_effect_retries_only_with_absence_evidence():
    retryable = validate_browser_effect_record(_effect(
        status="failed",
        evidence=[{"effect_observed": False}],
        attempt_count=1,
    ))
    unproven = validate_browser_effect_record(_effect(
        status="failed",
        evidence=[{"error": "connection closed"}],
        attempt_count=1,
    ))

    assert browser_effect_execution_decision(retryable) is BrowserEffectDecision.RETRY
    assert (
        browser_effect_execution_decision(unproven)
        is BrowserEffectDecision.OBSERVE_OR_PAUSE
    )


@pytest.mark.parametrize("status", ["in_progress", "unknown"])
def test_unresolved_effect_never_retries_automatically(status):
    effect = validate_browser_effect_record(_effect(status=status, attempt_count=1))

    assert (
        browser_effect_execution_decision(effect)
        is BrowserEffectDecision.OBSERVE_OR_PAUSE
    )


def test_effect_transition_tracks_attempt_and_terminal_evidence():
    started = transition_browser_effect(
        validate_browser_effect_record(_effect()),
        BrowserEffectStatus.IN_PROGRESS,
    )
    completed = transition_browser_effect(
        started,
        BrowserEffectStatus.OBSERVED_COMPLETE,
        evidence={"effect_observed": True, "snapshot_id": "snapshot-2"},
    )

    assert started.attempt_count == 1
    assert completed.attempt_count == 1
    assert completed.status is BrowserEffectStatus.OBSERVED_COMPLETE
    assert completed.evidence[-1]["snapshot_id"] == "snapshot-2"


def test_failed_effect_transitions_to_retry_only_after_absence_is_observed():
    failed = validate_browser_effect_record(_effect(status="failed", attempt_count=1))
    proven_absent = validate_browser_effect_record(_effect(
        status="failed",
        evidence=[{"effect_observed": False}],
        attempt_count=1,
    ))

    with pytest.raises(BrowserEffectTransitionError):
        transition_browser_effect(failed, BrowserEffectStatus.IN_PROGRESS)

    retrying = transition_browser_effect(
        proven_absent,
        BrowserEffectStatus.IN_PROGRESS,
    )
    assert retrying.attempt_count == 2
    assert retrying.status is BrowserEffectStatus.IN_PROGRESS


@pytest.mark.parametrize(
    ("target", "observation", "decision"),
    [
        (
            BrowserEffectStatus.OBSERVED_COMPLETE,
            True,
            BrowserEffectDecision.REUSE,
        ),
        (
            BrowserEffectStatus.FAILED,
            False,
            BrowserEffectDecision.RETRY,
        ),
    ],
)
def test_unknown_effect_can_be_resolved_by_observation(target, observation, decision):
    unknown = validate_browser_effect_record(_effect(status="unknown", attempt_count=1))

    resolved = transition_browser_effect(
        unknown,
        target,
        evidence={"effect_observed": observation, "snapshot_id": "snapshot-3"},
    )

    assert resolved.status is target
    assert browser_effect_execution_decision(resolved) is decision


@pytest.mark.parametrize(
    ("current", "target"),
    [
        ("unknown", "in_progress"),
        ("observed_complete", "failed"),
        ("not_started", "observed_complete"),
        ("in_progress", "not_started"),
    ],
)
def test_effect_transition_rejects_illegal_state_changes(current, target):
    effect = validate_browser_effect_record(_effect(status=current, attempt_count=1))

    with pytest.raises(BrowserEffectTransitionError):
        transition_browser_effect(effect, target)
