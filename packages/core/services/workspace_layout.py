"""Minimal Knowledge layout; directories are created only for actual content.

    Knowledge root/                           files without a Workspace
    Workspaces/_by_id/<artifact folder id>/    immutable Workspace storage
        <Blueprint or user directory>/        open, not a global registry
        <existing ledger name>/               shared Workspace state
        workflow/<lineage root run id>/       real Workflow execution outputs
        tasks/<task id>/<type>/                Task execution outputs

DocumentFolder projects physical Workspace files into Workspaces/<name>.
Ordinary files have no automatic type directory outside an execution archive.
Workflow retries reuse their lineage directory; existing canonical references
and historical execution directories remain valid.

Files never move once written. "Which of these is the deliverable" is
answered by ``ArtifactRole`` in the document's metadata and surfaced in the
generated index — moving a finished file into a ``deliverables/`` directory
would break every reference an earlier step already handed downstream.
"""
from __future__ import annotations

from enum import Enum


WORKFLOW_ARTIFACT_SEGMENT = "workflow"
TASK_ARTIFACT_SEGMENT = "tasks"
WORKSPACE_LEDGER_DIRECTORIES = frozenset({
    "topic-ledger", "content-ledger", "finance-ledger",
    "recruiting-ledger", "relationship-ledger",
})
WORKSPACE_RESERVED_DIRECTORIES = WORKSPACE_LEDGER_DIRECTORIES | {
    WORKFLOW_ARTIFACT_SEGMENT, TASK_ARTIFACT_SEGMENT,
}


class WorkspaceArtifactDir(str, Enum):
    """The type buckets inside a task archive.

    A tool that writes generated output picks one of these. Adding a bucket
    is a deliberate change here, not an accident in a call site.
    """

    IMAGES = "images"
    VIDEOS = "videos"
    AUDIO = "audio"
    DOCUMENTS = "documents"
    PRESENTATIONS = "presentations"
    SPREADSHEETS = "spreadsheets"
    CODE = "code"
    ARTIFACTS = "artifacts"

    @classmethod
    def values(cls) -> frozenset[str]:
        return frozenset(member.value for member in cls)


class ArtifactRole(str, Enum):
    """What a produced file IS to the task that made it.

    Every writer used to hardcode ``"final"``, so a task's 14 storyboard
    frames and its one finished video were indistinguishable — which is why
    "what did this task actually deliver?" could not be answered without
    opening files.

    DELIVERABLE — what the task set out to produce; surfaced to the user.
    INTERMEDIATE — real work product kept for reuse on retry (scene frames,
      voiceover takes, renders), not what the user asked for.
    REFERENCE — an input snapshot, not output at all.
    """

    DELIVERABLE = "final"
    INTERMEDIATE = "intermediate"
    REFERENCE = "reference"

    @classmethod
    def values(cls) -> frozenset[str]:
        return frozenset(member.value for member in cls)


#: Filename of the per-task index that makes deliverables visible without
#: moving any file. Generated, never hand-edited.
TASK_INDEX_FILENAME = "TASK_OUTPUT.md"
