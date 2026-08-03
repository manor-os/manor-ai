"""The blueprint publication lifecycle.

A ``WorkspaceBlueprint`` row moves ``draft`` → ``pending_review`` →
``published``, and ``archived`` takes it back off the marketplace. The
vocabulary was previously four module constants inside the blueprints
*router*, which meant everything outside that router — the marketplace
listing, the draft service, the admin MCP, the platform-admin review
endpoints, the architecture tools — spelled the values by hand instead.

Note that "published" and "archived" are also used by unrelated tables
(agent reviews, memories, workspaces). This enum is the blueprint's
vocabulary only; a shared spelling is not a shared meaning.
"""
from __future__ import annotations

from enum import Enum


class BlueprintStatus(str, Enum):
    """Every state a workspace blueprint can hold."""

    #: Being edited by its author; not listed anywhere.
    DRAFT = "draft"

    #: Submitted for platform review, awaiting an admin decision.
    PENDING_REVIEW = "pending_review"

    #: Live on the workspace marketplace and installable.
    PUBLISHED = "published"

    #: Withdrawn from the marketplace. Existing installs are unaffected —
    #: an install is a copy, not a link.
    ARCHIVED = "archived"

    @classmethod
    def values(cls) -> list[str]:
        return [member.value for member in cls]


#: The states from which a blueprint may be edited or submitted — i.e. not
#: currently listed or under review.
BLUEPRINT_EDITABLE_STATUSES: tuple[BlueprintStatus, ...] = (
    BlueprintStatus.DRAFT,
    BlueprintStatus.ARCHIVED,
)
