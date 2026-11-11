"""What a person should see of a Workflow project.

A producing Workflow writes ``state.display`` with its ``workflow_project``
steps (``state`` or ``patch``), and every App surface reads it before it
falls back to the project's internal keys:

- ``title``: what the project is called (not the brief it started from).
- ``status``: the words for where it stands now ("3 posts ready for review").
- ``summary``: one or two sentences about it.
- ``media``: the images or videos to show, in order; the first is the cover.
- ``deliverables``: the finished files; a project's file list shows these.

A file is a Document id, or ``{"document_id": ..., "label": ...}``.
"""
from __future__ import annotations

import re
from typing import Any

PROJECT_DISPLAY_KEY = "display"
PROJECT_DISPLAY_TEXT_FIELDS = {"title": 160, "status": 80, "summary": 600}
PROJECT_DISPLAY_FILE_FIELDS = {"media": 24, "deliverables": 50}

# The paths an App may read without the producer's state schema declaring them.
PROJECT_DISPLAY_PATHS: dict[tuple[str, ...], str] = {
    (PROJECT_DISPLAY_KEY,): "object",
    **{(PROJECT_DISPLAY_KEY, name): "string" for name in PROJECT_DISPLAY_TEXT_FIELDS},
    **{(PROJECT_DISPLAY_KEY, name): "array" for name in PROJECT_DISPLAY_FILE_FIELDS},
}

_DOCUMENT_ID = re.compile(r"^[A-Za-z0-9]{1,64}$")


def display_file(value: Any) -> dict[str, str] | None:
    """One file reference as ``{"document_id", "label"?}``, or None when it names no Document."""
    label = None
    if isinstance(value, dict):
        label = value.get("label")
        value = value.get("document_id")
    if not isinstance(value, str) or not _DOCUMENT_ID.fullmatch(value.strip()):
        return None
    label = label.strip()[:120] if isinstance(label, str) and label.strip() else None
    return {"document_id": value.strip(), **({"label": label} if label else {})}


def project_display(state: Any) -> dict[str, Any]:
    """The project's display, trimmed to what a page can show; empty values are left out."""
    raw = state.get(PROJECT_DISPLAY_KEY) if isinstance(state, dict) else None
    if not isinstance(raw, dict):
        return {}
    display: dict[str, Any] = {}
    for name, limit in PROJECT_DISPLAY_TEXT_FIELDS.items():
        value = raw.get(name)
        if isinstance(value, str) and value.strip():
            text = " ".join(value.split()) if name != "summary" else value.strip()
            display[name] = text if len(text) <= limit else f"{text[:limit - 1].rstrip()}…"
    for name, limit in PROJECT_DISPLAY_FILE_FIELDS.items():
        files = raw.get(name)
        picked: dict[str, dict[str, str]] = {}
        for item in files if isinstance(files, list) else []:
            file = display_file(item)
            if file is not None and file["document_id"] not in picked:
                picked[file["document_id"]] = file
        if picked:
            display[name] = list(picked.values())[:limit]
    return display


def project_display_failure(state: Any) -> str | None:
    """Why a project state's display cannot be saved, or None.

    Types only: lengths are trimmed when read, so a long title never fails a run.
    """
    raw = state.get(PROJECT_DISPLAY_KEY) if isinstance(state, dict) else None
    if raw is None:
        return None
    if not isinstance(raw, dict):
        return "display must be an object with title, status, summary, media and deliverables"
    for name in PROJECT_DISPLAY_TEXT_FIELDS:
        if raw.get(name) is not None and not isinstance(raw[name], str):
            return f"display.{name} must be text"
    for name in PROJECT_DISPLAY_FILE_FIELDS:
        files = raw.get(name)
        if files is None:
            continue
        if not isinstance(files, list):
            return f"display.{name} must be a list of Document ids or {{document_id, label}} objects"
        for index, item in enumerate(files):
            if display_file(item) is None:
                return f"display.{name}[{index}] must be a Document id or {{document_id, label}}, not {str(item)[:60]!r}"
    return None
