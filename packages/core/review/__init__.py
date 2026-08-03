"""ReviewRun / Snapshot / Watermark (M2).

Public surface:

* ``begin_review`` — claim the single running-review slot + freeze snapshot.
* ``mark_review_skipped`` / ``complete_review`` / ``fail_review`` — terminal
  transitions (only ``complete_review`` advances the watermark).
* ``latest_succeeded_review`` — the watermark chain lookup.
* ``events_in_window`` — the review's frozen ledger window.
* ``ReviewAlreadyRunning`` — raised when the running slot is taken.
"""
from packages.core.review.service import (
    ReviewAlreadyRunning,
    begin_review,
    complete_review,
    events_in_window,
    fail_review,
    latest_succeeded_review,
    mark_review_skipped,
)

__all__ = [
    "ReviewAlreadyRunning",
    "begin_review",
    "complete_review",
    "events_in_window",
    "fail_review",
    "latest_succeeded_review",
    "mark_review_skipped",
]
