"""The declared storage layout for everything a workspace task produces.

Before this module the layout existed only as string literals scattered
across the tools that write files — ``"images"`` here, ``"videos"`` there,
``"documents"`` in five more places — so nothing could answer "what IS the
workspace structure?", and a new tool could invent an eighth bucket without
anything noticing. One staging workspace ended up with five parallel
layouts.

The layout::

    Workspaces/_by_id/<artifact folder id>/     workspace physical root
    └── tasks/<task id>/                        one archive per task
        ├── images/  videos/  audio/            generated media
        ├── documents/  presentations/          generated documents
        ├── spreadsheets/  code/
        ├── artifacts/                          anything else written
        └── <model-chosen path>/                nested here, never outside

Files never move once written. "Which of these is the deliverable" is
answered by ``ArtifactRole`` in the document's metadata and surfaced in the
generated index — moving a finished file into a ``deliverables/`` directory
would break every reference an earlier step already handed downstream.
"""
from __future__ import annotations

from enum import Enum


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
