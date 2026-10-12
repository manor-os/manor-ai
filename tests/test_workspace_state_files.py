from datetime import datetime, timezone
from types import SimpleNamespace

from packages.core.services.workspace_state_files import WorkspaceFileEntry, _render_files_md, _render_state_md


def test_render_files_md_exposes_document_id_and_origin() -> None:
    workspace = SimpleNamespace(id="ws_1", name="Launch Workspace")
    files = [
        WorkspaceFileEntry(
            key="doc_1",
            name="campaign-hero.png",
            description="Final campaign hero image",
            location="assets/campaign-hero.png",
            document_id="doc_1",
            source="replicate, role=final",
            task_id="task_1",
            task_title="Generate campaign visuals",
            updated_at=datetime(2026, 6, 25, tzinfo=timezone.utc),
        )
    ]

    rendered = _render_files_md(
        workspace,
        now=datetime(2026, 6, 25, tzinfo=timezone.utc),
        files=files,
    )

    assert "| Name | Document ID | What | Storage Location | Display Path | Origin | Updated |" in rendered
    assert "doc_1" in rendered
    assert "assets/campaign-hero.png" in rendered
    assert "task=Generate campaign visuals" in rendered


def test_render_files_md_puts_the_storage_location_before_the_display_path() -> None:
    """Agents copy the first path they see: the one tools accept comes first."""

    workspace = SimpleNamespace(id="ws_1", name="Video Studio Eval R1")
    files = [
        WorkspaceFileEntry(
            key="doc_1",
            name="launch-taglines.md",
            description="Launch taglines",
            location="Workspaces/_by_id/f1/launch-taglines.md",
            display_location="Workspaces/Video Studio Eval R1/launch-taglines.md",
            document_id="doc_1",
        )
    ]

    rendered = _render_files_md(workspace, now=datetime(2026, 6, 25, tzinfo=timezone.utc), files=files)

    [row] = [line for line in rendered.splitlines() if line.startswith("| launch-taglines.md")]
    cells = [cell.strip() for cell in row.strip("|").split("|")]
    assert cells[3] == "Workspaces/_by_id/f1/launch-taglines.md"
    assert cells[4] == "Workspaces/Video Studio Eval R1/launch-taglines.md"



def test_render_state_md_does_not_treat_replanned_plans_as_live() -> None:
    workspace = SimpleNamespace(
        id="ws_1",
        name="JR First Release",
        status="active",
        kind="automatic",
    )
    goals = [
        SimpleNamespace(
            title="Campaign pack",
            current_value=None,
            target_value=100,
            pace_status="unknown",
            deadline=None,
        )
    ]
    completed_task = SimpleNamespace(
        id="task_completed",
        title="Distribution checklist",
        status="completed",
        owner_service_key="docs",
        actual_output={},
    )
    failed_task = SimpleNamespace(
        id="task_failed",
        title="Rights draft",
        status="failed",
        owner_service_key="docs",
        actual_output={},
    )
    live_task = SimpleNamespace(
        id="task_live",
        title="Campaign pack",
        status="in_progress",
        owner_service_key="docs",
        actual_output={},
    )
    historical = [
        SimpleNamespace(
            id="plan_replanned",
            status="replanned",
            task_id="task_completed",
            execution_mode="auto",
        ),
        SimpleNamespace(
            id="plan_failed",
            status="failed",
            task_id="task_failed",
            execution_mode="auto",
        ),
    ]

    rendered = _render_state_md(
        workspace,
        now=datetime(2026, 9, 20, tzinfo=timezone.utc),
        goals=goals,
        subscriptions=[],
        recent_tasks=[completed_task, failed_task],
        recent_plans=historical,
        active_batches=[],
        file_count=3,
    )

    current_work, _, rest = rendered.partition("## Recent Tasks")
    assert "_No active work batch._" in current_work
    assert "_No live plans." in current_work
    assert "plan_replanned" not in current_work
    assert "status=replanned (terminal history)" in rest
    assert "task=`task_completed`, task_status=completed" in rest
    assert "status=failed (terminal history)" in rest
    assert "task_status=failed" in rest

    live_plan = SimpleNamespace(
        id="plan_running",
        status="running",
        task_id="task_live",
        execution_mode="auto",
    )
    live_rendered = _render_state_md(
        workspace,
        now=datetime(2026, 9, 20, tzinfo=timezone.utc),
        goals=goals,
        subscriptions=[],
        recent_tasks=[live_task],
        recent_plans=[live_plan],
        active_batches=[],
        file_count=3,
    )
    live_current, _, _ = live_rendered.partition("## Recent Tasks")
    assert "_No live plans." not in live_current
    assert "`plan_running` status=running" in live_current
    assert "task_status=in_progress" in live_current
    assert "(terminal history)" not in live_current


def test_format_plans_labels_replanned_as_terminal_history() -> None:
    from packages.core.ai.runtime.strategist import _format_plans

    ctx = SimpleNamespace(
        recent_tasks=[
            SimpleNamespace(id="task_completed", status="completed"),
            SimpleNamespace(id="task_live", status="in_progress"),
        ],
        recent_plans=[
            SimpleNamespace(
                id="plan_replanned",
                status="replanned",
                task_id="task_completed",
                execution_mode="auto",
                cost_tracking={},
            ),
            SimpleNamespace(
                id="plan_running",
                status="running",
                task_id="task_live",
                execution_mode="auto",
                cost_tracking={},
            ),
        ],
    )
    rendered = _format_plans(ctx)
    assert "Live plans:" in rendered
    assert "plan_running status=running" in rendered
    assert "task_status=in_progress" in rendered
    assert "Historical plans (not in-flight):" in rendered
    assert "plan_replanned status=replanned (terminal history)" in rendered
    assert "task_status=completed" in rendered

    empty_live = _format_plans(SimpleNamespace(
        recent_tasks=[SimpleNamespace(id="task_completed", status="completed")],
        recent_plans=[
            SimpleNamespace(
                id="plan_replanned",
                status="replanned",
                task_id="task_completed",
                execution_mode="auto",
                cost_tracking={},
            )
        ],
    ))
    assert "Live plans: none." in empty_live
    assert "`replanned` is terminal history" in empty_live
