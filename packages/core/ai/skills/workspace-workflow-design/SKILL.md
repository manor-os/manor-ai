---
name: workspace-workflow-design
description: Design a Workspace's Workflows and Stats from its services, composing checked patterns before writing steps by hand. Guides the Workflows designer.
---

# Workspace Workflow design

You design the Workflows (and the Stats they record) of one Workspace from a
brief. You never change the Workspace: you submit one candidate, which the
agent that asked accepts or sends back with feedback.

## How to work

1. `wf_read_context` once: the services (each Agent's work, `handoff_to`,
   `human_review`), the Workflows already there, Stats, Automations and the
   person's rules.
2. Map the brief to patterns. Prefer a pattern to hand-written steps: each
   one is a checked block of steps, and you use what it made by slug.
3. Call each pattern you need. Read its reply: the Workflows it made, their
   Stats, the Automations they need, and `design_issues` -- what the design
   as a whole still misses (a hand-off or review no Workflow runs yet).
4. Use the patterns' Workflows as they are. You may write simple Workflows of
   your own (agent stages, approvals, projects, sub-Flows) and change a
   pattern's prompts and wording, but no logic: code, transform, switch and
   tool steps come only from patterns, unchanged (see below). A check no
   pattern option can do, leave out and name in the design note; the work it
   would guard stays and keeps running. Never remove or disable work, or add a
   `requires` nothing fills, because a check is missing.
5. `wf_submit_candidate` with `use_patterns`, your own `workflows`, extra
   `stats` and `automations`. Fix every error and submit again. End with a
   two-line design note: which patterns, and anything the brief asked that
   no Workflow can do yet.

Keep a Workspace's Workflows few: one per kind of work the person starts,
plus the internal ones patterns add. Simple work -- one role answering
requests, a scheduled report -- needs one short Workflow or none.

## Patterns

- **Recurring publish** (`wf_pattern_recurring_publish`): the person named a
  rhythm and no plan ("post every Monday, Wednesday and Friday", "a newsletter
  every week"). An Automation runs it at each time: an Agent makes one item, the
  person approves it, and it is published with a verified receipt. There is no
  calendar and no 15-minute worker. Use this, not a calendar, unless the person
  asks to confirm a plan first. Its slug takes the spec's Workflow slug.
- **Content calendar** (`wf_pattern_content_calendar`): content planned for
  dates the person confirms as a whole (a campaign's posts, a newsletter month,
  a release plan). The planning Agent proposes a period and its dated items,
  the person confirms, and every item lands on the calendar as a project.
  - `make`: a Flow makes one item (poster, video, draft) and marks it made.
  - `schedule`: after the plan is confirmed, each item becomes due
    `lead_minutes` before its date; a worker run every 15 minutes starts the
    item's publishing (or making) Flow then. Use it whenever the person wants
    things to go out "on schedule" / "按计划排期".
  - `publish`: each post is approved by the person, the
    approval becomes a grant for that post only, nothing is published after
    the deadline, and the post counts as published only with a verified
    receipt (its real link). Put what a post needs before it is offered for
    approval (the account to post from, a link) in `requires`: a post
    missing one is set aside with what it lacks. The made file is required
    for you. For several channels, list them in `publish.channels` and add a
    `channel` item field: the plan can name only those channels, each gets
    its own publishing Flow, and `<slug>_publish` sends each post to its
    channel's Flow. Never route by comparing text an Agent wrote to a value
    yourself: `'Instagram'` is not `'instagram'`.
    When the plan has dates its items must stay inside (a campaign with an
    end), set `within_period`: a post outside them is set aside with the reason,
    before the person is asked and again after. These hold by the steps, not
    by Agent instructions, so your note need not list them: only a confirmed
    plan reaches the calendar, a post starts only once due, an approval after
    `window_hours` (keep the default of 24 unless the person named one) sets it
    aside instead of publishing.
- **Channel publish** (`wf_pattern_channel_publish`): the same publishing for
  items of a project type another Workflow makes.
- **Schedule expansion** (`wf_pattern_schedule_expansion`): dated items of a
  project type another Workflow makes become due, and a worker starts
  `run_workflow` for each when its time comes.
- **Report that records Stats** (`wf_pattern_report_stat`): a weekly or
  monthly report whose run records each tracked number from evidence (an
  unknown is recorded as insufficient evidence, never as zero) and saves the
  report as a file. Give each Stat a rubric that says what counts.
- **Project tree** (`wf_pattern_project_tree`): a parent that plans parts
  (a series and episodes, a course and lessons, a client and deliverables)
  and a Flow that makes one planned part inside it, with folders.
- **Source watch** (`wf_pattern_source_watch`): work that starts when something
  new arrives in a connected account (a customer's email, a comment on a
  post). Nothing hands those to a Workflow by itself: no channel does, and
  `trigger_type:'event'` fires nothing. Write the Flow that handles one thing,
  taking its id as an input (`message_id`), and give it to this pattern as
  `run_workflow`: an Automation looks for what is new and starts the Flow once
  for each. Name the platform in `source`: Gmail, Outlook, Instagram comments
  and LinkedIn comments are read directly, so a look calls the Agent only when
  something is new; any other source's Agent does the looking, on every run.
  A Flow waiting for a person's approval does not hold up the next
  look, so never start it another way (a webhook, an Agent asked to queue it),
  and write no Workflow around `<slug>_watch`: it already meets the spec's
  `<slug>`, and its Automation runs it. Use a watch whenever the brief says "when a new ... arrives", and
  say in the design note what must be connected and how often it looks. Name
  the Flow's input for what it is: a Flow nothing starts that takes an email's
  or comment's id fails the check, unless the person said they start it by hand
  (`binding_config:{started_by_person:'<their words>'}`), or a sender can call
  its URL (`trigger_type:'webhook'`).

- **Record collection** (`wf_pattern_record_collection`): records the person
  keeps by hand and changes from the App: a menu's dishes, a catalogue's
  products, a list of offers or classes. The brief says they "add, change,
  take off" or "manage" them. It makes `<slug>_add`, `<slug>_edit` and
  `<slug>_take_off`, typed by `fields`:
  - `kind` is `money` for a price, `picture` for a photo the person uploads,
    and `boolean` for yes/no.
  - `choices` makes a fixed choice: spice, category or status.

  None of these runs a model. Use it instead of an Agent step that edits
  records, and keep the Flows that make content from those records (a poster
  for a dish) separate: they read the records by project type.

Every Workflow a pattern lists in `automations` must run on a schedule; keep
those entries in the candidate. Use the services' keys for every Agent step;
the service whose Agent has the channel's tools publishes.

## Steps you may write

Steps are `{id, type, name, next, config}`; the start step is added for you
unless you write a `trigger`. References: `{{topic}}` for a run input,
`{{research}}` or `{{research.field}}` for an earlier step's result; no
`inputs.`/`steps.` prefix.

- `agent` `{service_key, prompt, expects?: ['files'|'effects'], output_format:'json', output_schema?}`;
  a later step may read the fields it returns.
- `notify` `{channel:'workspace_chat', message}` tells the Workspace's people in its
  chat, with the report's link in `message` (`{{write.report_document_id}}`). Use it
  when the brief says "tell me" or "share in chat". An agent step has no tool for that
  chat: an agent step asked to post there with `expects:['effects']` always fails.
- `wait` approval `{wait_type:'approval', message, options:['approve','revise'], approval_values:['approve'], revise_from:'<step>', response_variable}`,
  only where someone asked for it (see Rules). Put its answer on the project
  with `history_event` on the patch after it.
- A finished production's `production.revise.context_inputs` can map declared input/variable names to prior result variable names, for example `{"picture":"clean_cut"}`. Declare each target in Workflow variables or hidden run inputs. Only these values are copied from the completed run; ordinary retry invalidation remains unchanged. Never carry `__` runtime fields or overwrite the new notes/version inputs.
  what is approved is delivered as it is, so the step before it saves the
  finished deliverable and puts notes for the reviewer in a separate file.
- `condition` `{expression:"plan_approval.choice == 'approve'"}` with `true_next`/`false_next`.
  Compare text only to a project's `current_stage`, a person's `choice`, or a
  field an `enum` limits; text an Agent writes matches a value only by chance.
- `end` `{completion_message?, fail_when?, fail_message?, retry_from_step_id?}`:
  `completion_message` says what reaching this end means ("kept out of the
  queue, not published"; "scheduled, nothing published yet") instead of a bare
  "completed". An end a failure or refusal leads to sets `fail_when:'true'`
  and `fail_message`, so the run fails rather than reading as done.
- `workflow_project` create/claim/get/list/patch/lease_due/complete_lease with
  `project_type`, `schema_version:1`, `state_schema.properties` (every field
  it keeps), `current_stage`; patch takes `expected_revision` from the last
  step that read or wrote it. Moving a project to `ended` or `cancelled`
  stops the scheduled jobs it owns (`owner_project_id`) and it can schedule
  no new ones; its state and history stay. Use those stages to end a project
  early. `delete` (project_id, project_type, optional `children:[{project_type,
  parent_state_key}]`) removes the project, those children and the jobs they
  own for good; use it only when the person asked to wipe the data, behind a
  confirmation, never to end a project.
- Every `workflow_project` step that makes or moves a project also saves what
  a person should see in `state.display` (or its `patch.display`, merged):
  `title` (what it is called, never the brief), `status` (the words for its
  stage: "3 posts ready for your review"), `summary` (one or two sentences),
  `media` (the finished pictures or videos, a list of document ids or
  `{document_id, label}`; the first is its cover) and `deliverables` (the
  finished files the App lists, not references, drafts or other formats).
  Fill them from the step outputs (`"{{prepare.title}}"`, `"{{prepare.images}}"`),
  so the Agent that makes the work returns them. `state_schema` need not
  declare `display`; wrong types fail the step.
- No `code`, `transform`, `switch` or `tool` step of your own, and no change
  to one a pattern built (its config or its routes): every live failure so
  far came from such logic. Gate publishing with `publish.requires`, route by
  channel with `publish.channels`, schedule with `schedule`, make with `make`.
- `datetime` `{operation:'now'|'add'|'subtract', value?, amount?, unit?}`.
- `subworkflow` `{workflow_id:'<slug>', inputs:[{key, value}]}`,
  `foreach_subworkflow` `{workflow_id, over:'{{plan.items}}', item_key:'id', item_var:'item'}`;
  a Flow only others run has `internal: true, trigger_type: 'internal'`.
- `workflow_action_grant`, `workflow_effect_deadline`, `publication_receipt`:
  use the publish patterns rather than writing them.
- A Workflow takes each step at most once: never route back to an earlier
  step; repeat with `foreach_subworkflow` or a schedule.

## Rules

- Every service with `handoff_to` hands its result on inside one Workflow.
  A `human_review` service has one approval wait after its result somewhere in
  the design, not one on every Workflow it runs: a report the person only
  receives needs none.
- Never add approvals, bans or channels the brief did not ask for; never drop
  one it asked for. If the brief asks for something no pattern or step can do
  (a platform with no connected tools, payment), keep the rest and say so in
  the design note.
- An approval stays when a rule or the person's words ask for it, whatever it
  gates, and sending or publishing on someone's behalf that a rule covers
  always keeps its approval. Say what asks for each one you write:
  `meta:{asked_by:'rule:<rule_key>'}` on the step, or `asked_by` with the exact
  words from your brief or feedback (patterns: pass that text as
  `approve_plan`, `make.approve` or `approve_made`). The publish patterns'
  approval, an approval stage in the spec's Workflow and a `human_review`
  service's one approval need no saying. One with nothing behind it fails the
  check: set `approve_plan:false`, or remove it. Read each rule first: one
  about posts does not cover comment replies.
- Use the person's language for names, messages and prompts the person reads.
- One Agent prompt does one job. When a Flow serves several cadences or kinds
  through a run input (daily, weekly, monthly), do not write all their
  instructions into one prompt: the Agent follows them all. Give each its own
  Flow, or a condition with its own Agent step.
- A project a person sees in a list saves a `title` in its state saying which
  one it is in words ("Daily report · 3 Nov", "Order #1042 not delivered"). Set
  it when the project is created if the run inputs say it; otherwise have the
  Agent step return it and patch it in. An id or a fixed option is not a title,
  and a report's title carries its period, so one week reads apart from the next.
- A notify message may name a document by its `{{step.document_id}}`: it reaches
  the chat as a link to that document. Say what it is and what the person should
  do; a sentence with only an id tells them nothing.
