---
name: workspace-tasks
description: Create or inspect one-off Tasks, delegate Workspace services, append task-local requirements and recover existing work without duplicating it.
version: 1.0.0
---

# Workspace Tasks and Service Collaboration

Inspect existing work and open blockers before creating or retrying a Task.
Use Workspace search for scoped task/agent lookup and task details for actual
status, outputs and recovery state. A Task coordinator owns final status.

For concrete one-off work, use Workspace create_task. task.prepare is reserved
for tasks included in a reviewed configuration change; it queues creation on
approved apply and is not an immediate execution shortcut. Include the complete
task-local instructions, required references, knowledge query and task rules.
Set start=true when the user asks to do/prepare/run the work now; leave it false
when they explicitly request a Task for later. Starting an installed Flow uses
start_workspace_flow instead; load workspace-workflows for that operation.

Use delegate_to_agent when asked to use an installed service or its Agent now:
pass the agent's name exactly as that tool lists it, and the task. Independent
tasks for different agents go in one call's tasks list and run at the same time;
a task that needs another's result goes in a later call. Without that tool, use
delegate_service with its real service_key/agent_subscription_id and prompt (or
params.tasks for several services); if the key is not in context, search category agents.
Answers of one call share one result; ask for longer output as a file. An answer
marked "Cut here" continues in storage: read the rest with read_delegation_result
and its run_id before quoting or using it, never reconstruct it. The service uses its own tool and MCP scope.
Follow-ups reuse that service and returned session_id; include new corrections
and still-applicable constraints. Another service cannot see its private history.

Append requirements to an existing Task using update_task_runtime. Do not store
them only in chat, change Workspace policy or start a duplicate Task. Rules must
constrain the downstream action they concern, rather than an unrelated task-create gate.

To start a pending Task (one drafted earlier without starting), call start_task
with its task_id. It runs with its own Agents and delivers its own output: do not
redo its work in Chat or create a second Task for it.

When the user asks that some kind of action wait for their review in this
conversation ("writes to Knowledge need my approval", "stop at the approval
card"), call hold_for_approval with those categories (files covers Knowledge)
before doing the work. The held action then stops at a card; the user's words
alone stop nothing. A lasting Workspace requirement is a rule (add_rule).

If the user's reply answers an open blocker, call answer_task_blocker with its
request_id and the actual answer. A bare retry request is not a blocker answer;
login blockers use their dedicated controls. If recovery is unavailable, report
the returned blocker and required action. Do not rebuild deliverables in Chat
or claim completion without the Task/service's verified result and durable outputs.
