"""Canonical kinds for Workspace-bound document groups."""
from __future__ import annotations

from enum import StrEnum


class WorkspaceDocumentGroupKind(StrEnum):
    """Stable values stored in ``DocumentGroup.settings.kind``."""

    KNOWLEDGE_NET = "knowledge_net"
    DEFAULT_COLLECTION = "workspace_collection"
    FILE_BUCKET = "workspace_files"
    LEGACY_KNOWLEDGE_FOLDER = "knowledge_folder"


__all__ = ["WorkspaceDocumentGroupKind"]
