"""Regression: planning + agent-run Celery tasks must have generous time limits.

They otherwise inherit the global task_soft_time_limit=300 (5 min), which is too
low for complex planning — up to two planner LLM calls now that contract
enforcement is always-on — and for long agentic loops. That produced
"Planning failed after 3 attempts: SoftTimeLimitExceeded()" on heavy tasks.
"""
from packages.core.tasks.ai_tasks import plan_and_run_task, run_agent_task


def test_plan_and_run_task_time_limits():
    assert plan_and_run_task.soft_time_limit and plan_and_run_task.soft_time_limit >= 900
    assert plan_and_run_task.time_limit and plan_and_run_task.time_limit > plan_and_run_task.soft_time_limit


def test_run_agent_task_time_limits():
    assert run_agent_task.soft_time_limit and run_agent_task.soft_time_limit >= 900
    assert run_agent_task.time_limit and run_agent_task.time_limit > run_agent_task.soft_time_limit
