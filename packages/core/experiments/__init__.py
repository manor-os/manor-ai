"""Bounded config experiments (M13).

See ``docs/STRATEGIST_DECISION_LAYER_REDESIGN_ZH.md`` section M13.
"""
from packages.core.experiments.controller import (
    DEFAULT_DURATION_DAYS,
    DEFAULT_ROLLBACK_ON_CONSECUTIVE_FAILURES,
    EXPERIMENT_OVERLAY_KEY,
    ExperimentError,
    ExperimentTargetError,
    check_experiment_guardrails,
    effective_dispatch_config,
    evaluate_experiment,
    start_experiment,
    stop_experiment,
)

__all__ = [
    "DEFAULT_DURATION_DAYS",
    "DEFAULT_ROLLBACK_ON_CONSECUTIVE_FAILURES",
    "EXPERIMENT_OVERLAY_KEY",
    "ExperimentError",
    "ExperimentTargetError",
    "check_experiment_guardrails",
    "effective_dispatch_config",
    "evaluate_experiment",
    "start_experiment",
    "stop_experiment",
]
