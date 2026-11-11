---
name: workspace-tasks
description: Create or inspect one-off Tasks, delegate Workspace services, append task-local requirements and recover existing work without duplicating it.
version: 1.0.0
---

# Workspace Tasks and Service Collaboration

Inspect existing work and open blockers before creating or retrying a Task.
Use Workspace search for scoped task/agent lookup and task details for actual
status, outputs and recovery state. A Task coordinator owns final status.
When someone asks what needs them, what is waiting or what you are watching,
call list_attention: the Workspace's open approvals and inputs, undecided
proposals, accounts to reconnect, unfinished setup, paused automations, budget,
goals off pace and blocked or overdue work, most urgent first.

For concrete one-off work, use Workspace create_task. task.prepare is reserved
for tasks included in a reviewed configuration change; it queues creation on
approved apply and is not an immediate execution shortcut. Approving it approves
the change, not the start: set start on its patch only when the user asks for the
Task to start once approved. Include the complete
task-local instructions, required references, knowledge query and task rules.
Long or multi-step work runs as a Task, not in the Chat turn: making, editing or
rendering video or other media, a long sandbox or script run, research or a
document with several stages, anything an Agent cannot finish in a few minutes.
This holds when the user names an Agent and says now: set owner_service_key to
that Agent. The Task survives a restart and queues for a sandbox, which a
delegated child cannot. The Task starts by
default and the Workspace Agent assigns it: "Create a task: X" asks for X to be
done. Set start=false only when the user says it is for later or not to start
yet. Set assignee_id only when they name a person to do it; that Task waits for
them. Starting an installed Flow uses start_workspace_flow instead; load
workspace-workflows for that operation.

Use delegate_to_agent when asked to use an installed service or its Agent for
quick work that finishes in this turn (an answer, a calculation, a short draft,
one short file), or when the user says not to create a Task:
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
