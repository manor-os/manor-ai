"""The watchers the sweep runs, in order."""
from __future__ import annotations

from packages.core.attention.contracts import Watcher


def registered_watchers() -> list[Watcher]:
    from packages.core.attention.watchers.account_broken import AccountBrokenWatcher
    from packages.core.attention.watchers.decision_pending import DecisionPendingWatcher
    from packages.core.attention.watchers.needs_input import NeedsInputWatcher
    from packages.core.attention.watchers.work import WorkBlockedWatcher, WorkOverdueWatcher
    from packages.core.attention.watchers.workspace_state import (
        AutomationPausedWatcher,
        BudgetThresholdWatcher,
        GoalOffPaceWatcher,
        SetupIncompleteWatcher,
    )

    watchers: list[Watcher] = [
        NeedsInputWatcher(),
        DecisionPendingWatcher(),
        AccountBrokenWatcher(),
        SetupIncompleteWatcher(),
        AutomationPausedWatcher(),
        BudgetThresholdWatcher(),
        GoalOffPaceWatcher(),
        WorkBlockedWatcher(),
        WorkOverdueWatcher(),
    ]
    keys = [watcher.key for watcher in watchers]
    assert len(keys) == len(set(keys)), f"duplicate watcher keys: {keys}"
    return watchers
