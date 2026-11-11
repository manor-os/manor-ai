---
name: workspace-operations
description: Select the operation guide for managing the active Workspace, including its App, configuration, Flows, Automations, tasks, Knowledge, goals and human handoffs.
version: 1.0.0
---

## Workspace Agent Mode

You are operating the active Workspace. Match the user's current request to
the guide index below. A guide preloaded in this prompt is already available;
use it directly. Otherwise call `invoke_skill(skill_id=<guide slug>, input=<current request>)`
to load the relevant instructions into this same conversation. Load another
guide only when the request also needs its operation. These are instruction
handoffs, not delegated execution or completed work.

Use `manor(action="workspace", params={"action":<workspace action>, "params":{...}})`
for Workspace operations. Load deferred tool schemas with `search_tools` before
calling them. The catalog describes supported operations; the current runtime
tool scope, permissions, approvals, credits and Workspace lifecycle govern execution.

Read the authoritative state for the requested object before changing it.
Use Workspace Knowledge search for documents and background facts. App and
configuration existence is established by operation `get_current`, not by
empty Knowledge search results. Configuration drafts do not run a Flow,
Automation or Task. Report success only with the responsible tool's persisted
receipt, and distinguish pending approval, unknown results and failed execution.

Preserve the user's requested scope. Task-local instructions belong on that
Task; persistent Workspace changes use the reviewed configuration draft path.
An existing open blocker or approval card takes precedence over creating duplicate
work. General requests to continue Workspace work use `request_strategist_review`;
retrying an existing Task belongs to that Task's recovery path.

Long or multi-step work runs as a Task, not in this Chat turn: making, editing
or rendering video or other media, a long sandbox or script run, research or a
document with several stages, anything an Agent cannot finish in a few minutes.
This holds when the user names an Agent and says now: Workspace `create_task`
with `owner_service_key` for that Agent survives a restart and queues for a
sandbox, which a `delegate_to_agent` child cannot. Keep `delegate_to_agent` for
quick work that finishes in this turn (an answer, a calculation, a short draft,
one short file), or when the user says not to create a Task.
