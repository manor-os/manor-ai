"""Persisted states of a reviewed Workspace configuration change."""

from enum import Enum


class WorkspaceOperationDraftStatus(str, Enum):
    OPEN = "open"
    APPLIED = "applied"
    DISCARDED = "discarded"

    __str__ = str.__str__
    __format__ = str.__format__
