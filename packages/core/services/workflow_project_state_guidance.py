"""What a WorkflowProject keeps for the person following the work.

Every prompt that writes Workflows reads this one text -- the Architect's own
workflow step and the Workflows designer's skill -- so the two cannot drift.
``workspace_workflow_designer.project_state_issues`` checks the same rules.
"""
from __future__ import annotations

FOLLOWABLE_PROJECT_STATE = """\
Declare in ``state_schema.properties`` every state field the App
should show. The App can only show what the project keeps, so keep
what a person following the work wants to see, each written by the
step after the one that produces it:
  - what identifies it: the run inputs (client, topic, deadline)
    in the create ``state``.
  - every file a stage saves, not only the final one: a
    ``<name>_document_id`` field (research_document_id,
    draft_document_id, deliverable_document_id) patched from
    ``{{<agent step>.<name>_document_id}}``. Reading that field
    makes the Agent step return the id of the file it saved; a field
    named otherwise (``final_file``) shows as text, not as a file. Many
    files of one kind (clips, images) stay in the project's folder.
  - what a stage reports that the person checks (a count, a score,
    notes to the reviewer) as ``{{<agent step>.<field>}}``.
  - each decision: on the patch after an approval wait,
    ``history_event:{kind:'review',decision:'{{final_approval.choice}}',
    note:'{{final_approval.note}}'}`` with ``history:{type:'array'}``
    in state_schema; the runner appends it to ``state.history``
    with its timestamp. A wait step's own id has no fields: its
    answer is only ``<response_variable>.choice`` and ``.note``
    (empty when the person wrote none), and no variable holds when
    it was decided. Never have an Agent or code step build the log.
  - key dates: due dates from run inputs; a date the run reaches
    (delivered, published) from a ``datetime`` step
    (``config:{operation:'now'}``) read as ``{{<that step>}}``,
    never a time an Agent reports.
A list entry other than history goes in ``list_appends:[{path,
key, items}]`` on a patch; ``key`` names the field that tells items
apart."""
