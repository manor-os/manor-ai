"""Minimal Knowledge layout; directories are created only for actual content.

    Knowledge root/                           files without a Workspace
    Workspaces/_by_id/<artifact folder id>/    immutable Workspace storage
        <Blueprint or user directory>/        open, not a global registry
        <existing ledger name>/               shared Workspace state
        workflow/<lineage root run id>/       real Workflow execution outputs
        tasks/<task id>/<type>/                Task execution outputs
        projects/<project>/<child project>/    a WorkflowProject's own files

A WorkflowProject that names a folder (``state.project_root``) keeps what it
makes there, at the Workspace home whichever run or Task makes it, so a series
and its episodes read as one tree: ``projects/<series>/<episode>/``.

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
PROJECT_ARTIFACT_SEGMENT = "projects"
# A project made again keeps each version in a take folder of its own.
PROJECT_TAKES_SEGMENT = "takes"
WORKSPACE_LEDGER_DIRECTORIES = frozenset({
    "topic-ledger", "content-ledger", "finance-ledger",
    "recruiting-ledger", "relationship-ledger",
})
WORKSPACE_RESERVED_DIRECTORIES = WORKSPACE_LEDGER_DIRECTORIES | {
    WORKFLOW_ARTIFACT_SEGMENT, TASK_ARTIFACT_SEGMENT, PROJECT_ARTIFACT_SEGMENT,
}


def project_production_root(project_root: object) -> str | None:
    """The run's project folder when the runtime assigned it (``projects/...``).

    A production made inside such a project keeps its files there, on every
    retry. A root a Blueprint writes itself is not one, so its run keeps the
    run folder.
    """
    root = str(project_root or "").strip().strip("/")
    return root if root.startswith(f"{PROJECT_ARTIFACT_SEGMENT}/") else None


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
