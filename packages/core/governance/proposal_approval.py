"""Atomic operator edits to the canonical Proposal standing approvals."""
from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.workspace import Workspace


def clear_legacy_proposal_approval_source(workspace: Workspace) -> None:
    """An operator, not the former scheduler/simulation, now owns this grant."""
    settings = dict(workspace.settings or {})
    strategist = dict(settings.get("strategist") or {})
    strategist.pop("auto_approve_proposals", None)
    strategist.pop("auto_approve_proposals_source", None)
    strategist.pop("proposal_task_approval_source", None)
    settings["strategist"] = strategist
    workspace.settings = settings


async def set_proposal_approval_actions(
    db: AsyncSession, *, entity_id: str, workspace_id: str,
    action_keys: tuple[str, ...], enabled: bool, changed_by: str | None = None,
) -> Workspace:
    """Reject retired writes before changing any approval or proposal."""
    raise ValueError("Standing proposal approval is retired; use Execution & approvals settings with a current version")
