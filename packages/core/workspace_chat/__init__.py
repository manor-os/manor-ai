"""Workspace chat — agent group chat per workspace.

Reuses the existing ``conversations`` + ``messages`` tables (extended
with workspace-chat columns by 20260424_02). Each workspace gets one
``scope='workspace_main'`` conversation; long plans can spawn child
``scope='workspace_thread'`` rows so per-plan chatter doesn't drown
the main feed.

Producers (PlanExecutor, measurement service, Strategist) call into
``notifiers`` to post the right kind of message; a thin router exposes
the same data to the web UI and the resolve-pending-action flow.
"""
from packages.core.workspace_chat.service import (
    ensure_main_conversation,
    spawn_thread,
    post_message,
    list_messages,
    resolve_pending_action,
)
from packages.core.workspace_chat.notifiers import (
    notify_plan_started,
    notify_plan_completed,
    notify_plan_failed,
    notify_step_done,
    notify_step_failed,
    notify_step_needs_human,
    notify_goal_measured,
    notify_goal_pace_changed,
    notify_goal_achieved,
)

__all__ = [
    "ensure_main_conversation",
    "spawn_thread",
    "post_message",
    "list_messages",
    "resolve_pending_action",
    "notify_plan_started",
    "notify_plan_completed",
    "notify_plan_failed",
    "notify_step_done",
    "notify_step_failed",
    "notify_step_needs_human",
    "notify_goal_measured",
    "notify_goal_pace_changed",
    "notify_goal_achieved",
]
