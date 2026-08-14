"""First-party Workspace Stats library.

Library entries are portable collection definitions, never runtime values.
Users and Blueprints install an entry into a Workspace, then may customize its
window, cadence, display name, and filters without mutating the global catalog.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Optional

from packages.core.stats.integration_keys import StatIntegrationKey


@dataclass(frozen=True)
class StatLibraryEntry:
    key: str
    name: str
    description: str
    category: str
    value_type: str
    unit: Optional[str]
    default_window: str
    collector_type: str
    collector_config: dict[str, Any]
    default_cadence: Optional[str]
    freshness_limit_seconds: Optional[int]
    goal_eligible: bool = True
    recommended_goal_role: Optional[str] = None
    integration_key: Optional[StatIntegrationKey] = None

    def __post_init__(self) -> None:
        has_integration_key = self.integration_key is not None
        if (self.collector_type == "integration") != has_integration_key:
            raise ValueError(
                "integration collectors require exactly one integration_key"
            )

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["integration_key"] = (
            self.integration_key.value if self.integration_key else None
        )
        return payload


_ENTRIES = (
    StatLibraryEntry(
        key="workspace.tasks.created",
        name="Tasks created",
        description="Tasks created in the selected time window.",
        category="tasks",
        value_type="number",
        unit="tasks",
        default_window="calendar_week",
        collector_type="workspace_internal",
        collector_config={"metric": "tasks_created"},
        default_cadence="daily",
        freshness_limit_seconds=172800,
        goal_eligible=False,
    ),
    StatLibraryEntry(
        key="workspace.tasks.completed",
        name="Tasks completed",
        description="Tasks completed in the selected time window.",
        category="tasks",
        value_type="number",
        unit="tasks",
        default_window="calendar_week",
        collector_type="workspace_internal",
        collector_config={"metric": "tasks_completed"},
        default_cadence="daily",
        freshness_limit_seconds=172800,
        recommended_goal_role="leading",
    ),
    StatLibraryEntry(
        key="workspace.tasks.completion_rate",
        name="Task completion rate",
        description="Tasks created in the selected window that are completed, divided by all tasks created in that window.",
        category="tasks",
        value_type="percent",
        unit="percent",
        default_window="calendar_week",
        collector_type="workspace_internal",
        collector_config={"metric": "task_completion_rate"},
        default_cadence="daily",
        freshness_limit_seconds=172800,
        recommended_goal_role="leading",
    ),
    StatLibraryEntry(
        key="workspace.tasks.on_time_rate",
        name="On-time completion rate",
        description="Completed tasks with deadlines that finished on or before their deadline.",
        category="tasks",
        value_type="percent",
        unit="percent",
        default_window="rolling_30d",
        collector_type="workspace_internal",
        collector_config={"metric": "on_time_completion_rate"},
        default_cadence="daily",
        freshness_limit_seconds=172800,
        recommended_goal_role="guardrail",
    ),
    StatLibraryEntry(
        key="workspace.tasks.overdue",
        name="Overdue tasks",
        description="Open tasks whose deadline has passed.",
        category="tasks",
        value_type="number",
        unit="tasks",
        default_window="latest",
        collector_type="workspace_internal",
        collector_config={"metric": "overdue_task_count"},
        default_cadence="daily",
        freshness_limit_seconds=172800,
        recommended_goal_role="guardrail",
    ),
    StatLibraryEntry(
        key="workspace.tasks.blocked",
        name="Blocked tasks",
        description="Workspace tasks currently blocked or waiting on a dependency.",
        category="tasks",
        value_type="number",
        unit="tasks",
        default_window="latest",
        collector_type="workspace_internal",
        collector_config={"metric": "blocked_task_count"},
        default_cadence="daily",
        freshness_limit_seconds=172800,
        recommended_goal_role="guardrail",
    ),
    StatLibraryEntry(
        key="workspace.tasks.avg_cycle_time_hours",
        name="Average task cycle time",
        description="Average hours from task start to completion in the selected window.",
        category="tasks",
        value_type="duration",
        unit="hours",
        default_window="rolling_30d",
        collector_type="workspace_internal",
        collector_config={"metric": "avg_task_cycle_time_hours"},
        default_cadence="daily",
        freshness_limit_seconds=172800,
        recommended_goal_role="leading",
    ),
    StatLibraryEntry(
        key="workspace.workflows.runs",
        name="Workflow runs",
        description="Workflow executions started in the selected time window.",
        category="workflows",
        value_type="number",
        unit="runs",
        default_window="calendar_week",
        collector_type="workspace_internal",
        collector_config={"metric": "workflow_run_count"},
        default_cadence="daily",
        freshness_limit_seconds=172800,
        goal_eligible=False,
    ),
    StatLibraryEntry(
        key="workspace.workflows.success_rate",
        name="Workflow success rate",
        description="Completed workflow runs divided by terminal workflow runs.",
        category="workflows",
        value_type="percent",
        unit="percent",
        default_window="rolling_30d",
        collector_type="workspace_internal",
        collector_config={"metric": "workflow_success_rate"},
        default_cadence="daily",
        freshness_limit_seconds=172800,
        recommended_goal_role="guardrail",
    ),
    StatLibraryEntry(
        key="workspace.agents.runs",
        name="Agent runs",
        description="Agent executions started in the selected time window.",
        category="agents",
        value_type="number",
        unit="runs",
        default_window="calendar_week",
        collector_type="workspace_internal",
        collector_config={"metric": "agent_run_count"},
        default_cadence="daily",
        freshness_limit_seconds=172800,
        goal_eligible=False,
    ),
    StatLibraryEntry(
        key="workspace.agents.success_rate",
        name="Agent success rate",
        description="Completed agent executions divided by terminal executions.",
        category="agents",
        value_type="percent",
        unit="percent",
        default_window="rolling_30d",
        collector_type="workspace_internal",
        collector_config={"metric": "agent_success_rate"},
        default_cadence="daily",
        freshness_limit_seconds=172800,
        recommended_goal_role="guardrail",
    ),
    StatLibraryEntry(
        key="workspace.knowledge.ready_documents",
        name="Ready knowledge documents",
        description="Workspace documents ready for retrieval.",
        category="knowledge",
        value_type="number",
        unit="documents",
        default_window="latest",
        collector_type="workspace_internal",
        collector_config={"metric": "knowledge_ready_documents"},
        default_cadence="daily",
        freshness_limit_seconds=172800,
        goal_eligible=False,
    ),
    StatLibraryEntry(
        key="twitter_x.followers_count",
        name="X followers",
        description="Current follower count for the selected X connection.",
        category="social",
        value_type="number",
        unit="followers",
        default_window="latest",
        collector_type="integration",
        collector_config={"metric_key": "followers_count"},
        default_cadence="daily",
        freshness_limit_seconds=172800,
        recommended_goal_role="primary",
        integration_key=StatIntegrationKey.TWITTER_X,
    ),
    StatLibraryEntry(
        key="twitter_x.following_count",
        name="X accounts followed",
        description="Current following count for the selected X connection.",
        category="social",
        value_type="number",
        unit="accounts",
        default_window="latest",
        collector_type="integration",
        collector_config={"metric_key": "following_count"},
        default_cadence="daily",
        freshness_limit_seconds=172800,
        goal_eligible=False,
        integration_key=StatIntegrationKey.TWITTER_X,
    ),
    StatLibraryEntry(
        key="twitter_x.tweet_count",
        name="X posts",
        description="Current lifetime post count for the selected X connection.",
        category="social",
        value_type="number",
        unit="posts",
        default_window="latest",
        collector_type="integration",
        collector_config={"metric_key": "tweet_count"},
        default_cadence="daily",
        freshness_limit_seconds=172800,
        goal_eligible=False,
        integration_key=StatIntegrationKey.TWITTER_X,
    ),
    StatLibraryEntry(
        key="twitter_x.listed_count",
        name="X list memberships",
        description="Current number of X lists containing the selected account.",
        category="social",
        value_type="number",
        unit="lists",
        default_window="latest",
        collector_type="integration",
        collector_config={"metric_key": "listed_count"},
        default_cadence="daily",
        freshness_limit_seconds=172800,
        goal_eligible=False,
        integration_key=StatIntegrationKey.TWITTER_X,
    ),
)

_BY_KEY = {entry.key: entry for entry in _ENTRIES}


def list_library_entries() -> list[StatLibraryEntry]:
    return list(_ENTRIES)


def get_library_entry(key: str) -> Optional[StatLibraryEntry]:
    return _BY_KEY.get(str(key or "").strip())
