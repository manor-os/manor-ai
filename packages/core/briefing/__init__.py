"""Daily briefing — Demo B's "8AM inbox digest" feature.

Different shape from Strategist:

  * **Strategist** runs weekly, proposes new Tasks. Output goes to
    workspace_chat as a "proposal" message kind with task ids attached.
    Cadence is medium (Mon 9am default).
  * **Briefing** runs daily, surfaces what changed since you went to
    bed. Output goes to workspace_chat as a "briefing" message kind
    with attached drafts the operator can approve / edit / skip.
    Cadence is fast (8am daily default).

Both share the same cycle pattern (ScheduledJob → Celery → service →
chat), but their prompts, output shapes, and chat affordances differ
enough that bundling them into one module would muddy both.

Layout:

  service.py       generate_briefing() — gather signals → triage → post
  inbox.py         pluggable inbox source (Gmail by default; others added
                   per-provider as needed)
  triage.py        single Claude call that classifies each message and
                   drafts replies for actionable ones
  prompt.py        prompt template + JSON-out parser
  schema.py        Pydantic Briefing / BriefingItem
  scheduling.py    install_briefing_schedule() — daily cron
"""
from packages.core.briefing.service import (
    generate_briefing,
    BriefingError,
)
from packages.core.briefing.schema import (
    Briefing,
    BriefingItem,
    BriefingAction,
)
from packages.core.briefing.scheduling import (
    install_briefing_schedule,
    remove_briefing_schedule,
)

__all__ = [
    "generate_briefing",
    "BriefingError",
    "Briefing",
    "BriefingItem",
    "BriefingAction",
    "install_briefing_schedule",
    "remove_briefing_schedule",
]
