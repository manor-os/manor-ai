"""What a media provider's own status words mean for our job.

``MediaJobStatus`` already exists on the model (pending → processing →
completed | failed) and stays there — this module deliberately does not
define a second one; two enums for one column is the same defect as two
literals, with more ceremony.

What was missing is the translation layer. Image/video providers each
report progress in their own vocabulary — "succeeded", "complete",
"done", "failure", "canceled" — and the poll loops carried those variants
inline, one hand-written set per loop.

The sets had drifted. The Seedance and generic video pollers recognised
``{"succeeded", "success", "completed"}`` as success while the OpenRouter
poller also accepted ``"complete"`` and ``"done"``; the failure sets
disagreed on ``"failure"`` and the one-L ``"canceled"``. A provider that
answered "done" to the Seedance poller was therefore neither finished nor
failed — the loop kept polling until it timed out, and a video that had
actually been produced was reported as still pending.

So the variants live here once, as data, and the pollers ask a question
instead of matching words.
"""
from __future__ import annotations

from packages.core.models.media_job import MediaJobStatus

#: Terminal states — the job will not change again. ``MediaJobStatus.terminal()``
#: returns the same set as raw values; this is the member-typed form.
MEDIA_JOB_TERMINAL_STATUSES: frozenset[MediaJobStatus] = frozenset(
    {MediaJobStatus.COMPLETED, MediaJobStatus.FAILED}
)

#: Every word a provider has been observed to use for "the asset is ready".
_PROVIDER_SUCCESS_WORDS = frozenset(
    {"completed", "complete", "succeeded", "success", "done", "finished", "ready"}
)

#: …and for "it will never be ready". ``expired`` counts: a job whose
#: result was garbage-collected is not going to arrive.
_PROVIDER_FAILURE_WORDS = frozenset(
    {"failed", "failure", "error", "errored", "cancelled", "canceled", "expired", "rejected"}
)


def coerce_provider_media_status(reported: object) -> MediaJobStatus | None:
    """What a provider's status word means for the job, or ``None``.

    ``None`` means "still running, or a word we don't recognise" — which
    for a poll loop are the same instruction: keep polling until the
    deadline. It deliberately never guesses FAILED from an unknown word,
    since that would abandon a job over an unfamiliar spelling.
    """
    text = str(reported or "").strip().lower()
    if not text:
        return None
    if text in _PROVIDER_SUCCESS_WORDS:
        return MediaJobStatus.COMPLETED
    if text in _PROVIDER_FAILURE_WORDS:
        return MediaJobStatus.FAILED
    return None


__all__ = [
    "MEDIA_JOB_TERMINAL_STATUSES",
    "MediaJobStatus",
    "coerce_provider_media_status",
]
