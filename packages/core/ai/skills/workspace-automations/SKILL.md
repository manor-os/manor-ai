---
name: workspace-automations
description: Create, inspect, pause, restore, cancel or run scheduled Workspace Automations, keeping schedule changes separate from App links and Workflow execution.
version: 1.0.0
---

# Workspace Automations

Inspect the current Workspace's jobs with list_scheduled_jobs or
query_scheduled_jobs. Combine status/Workspace/Agent filters in one query;
values within a plural filter are ORed, different filters are ANDed. Use real
job IDs and current revisions. Avoid creating a duplicate when a matching job exists.

create_scheduled_job creates scheduled work with its real executor/Skill and
schedule. Include the user's timezone, frequency and required input. If a
schedule choice is genuinely missing, resolve it before creating the job.
Use the current tool schema for supported update/replacement behavior; do not
invent update_scheduled_job. A Workspace operating-model process or automation
description is metadata and does not create or update a live ScheduledJob.

toggle_scheduled_job pauses/restores the actual job. cancel_scheduled_job
removes it. run_scheduled_job_now requests an immediate run and may have business
effects; editing a schedule or linking the Automation to the App does not imply
authorization to run it now. For an App button targeting an existing job, load
workspace-app and set the internal automation:<job_id> target through a draft.

Follow scheduler approval, lifecycle and credit gates. A created/enabled job is
scheduled, not completed execution. Verify the persisted schedule/enabled state
and inspect returned execution status separately. Private conversational
follow-ups and Agent Initiative have different ownership; do not turn them into
Workspace jobs unless Workspace automation is the user's intended scope.
