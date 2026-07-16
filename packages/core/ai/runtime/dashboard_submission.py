from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Iterator

from packages.core.ai.runtime.dashboard_module_validation import (
    dashboard_module_code_hash,
)


DASHBOARD_SUBMIT_TOOL_NAME = "dashboard_submit_module"


@dataclass
class DashboardSubmissionCapture:
    submission: dict[str, Any] | None = None
    validated_code_hashes: set[str] = field(default_factory=set)


_dashboard_submission_capture: ContextVar[DashboardSubmissionCapture | None] = (
    ContextVar("dashboard_submission_capture", default=None)
)


@contextmanager
def runtime_capture_dashboard_submission() -> Iterator[DashboardSubmissionCapture]:
    capture = DashboardSubmissionCapture()
    token = _dashboard_submission_capture.set(capture)
    try:
        yield capture
    finally:
        _dashboard_submission_capture.reset(token)


def runtime_record_dashboard_submission(submission: dict[str, Any]) -> None:
    capture = _dashboard_submission_capture.get()
    if capture is None:
        raise RuntimeError("Dashboard submission tool is not active for this turn")
    if capture.submission is not None:
        raise RuntimeError("Dashboard layout was already submitted for this turn")
    capture.submission = dict(submission)


def runtime_record_dashboard_validation(code: dict[str, Any]) -> bool:
    capture = _dashboard_submission_capture.get()
    if capture is None:
        return False
    capture.validated_code_hashes.add(dashboard_module_code_hash(code))
    return True


def runtime_unvalidated_dashboard_changes(
    module_changes: list[dict[str, Any]],
) -> list[int]:
    capture = _dashboard_submission_capture.get()
    validated = capture.validated_code_hashes if capture is not None else set()
    missing: list[int] = []
    for index, change in enumerate(module_changes):
        if not isinstance(change, dict) or change.get("action") not in {"create", "update"}:
            continue
        code = change.get("code")
        if not isinstance(code, dict) or dashboard_module_code_hash(code) not in validated:
            missing.append(index)
    return missing
