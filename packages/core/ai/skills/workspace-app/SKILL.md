---
name: workspace-app
description: Configure the Workspace App layout, main action, forms and internal links to Agent Chat, Workflow or Automation.
version: 1.0.10
---

# Workspace App

First call `manor(action="workspace", params={"action":"operation", "params":{"action":"get_current"}})`.
Read `app_view`, `available_actions`, `workflow_options`, `agent_options`,
`automation_options` and `attachable_workflow_options`. Empty Knowledge results
do not prove an App or Workflow is missing. Unattached saved Workflows can still
be available to connect. Do not select an unrelated Workflow merely because it exists.

To design or redesign the page (which entries it offers, what cards open,
which data views and sections it shows), do not lay it out yourself: command
the App designer with operation `design_app` and
`brief:"<what the person wants to do from the App>"`. It reads what the
Workspace runs, composes the page from the App module schematic and recipes,
and returns a checked candidate as an `app_view.update` draft with its design
notes. It also shows the person the draft's review card, which previews the
page with their real data; they approve or reject it there. Tell them who the
page is for and what its first screen shows. If the candidate misses the
request, call `design_app` again with `feedback` and that `draft_id`: the old
card closes and a new one replaces it.

The card and the design notes say how the page's self-check went. To run it
yourself, call operation `test_app`, with `draft_id` for a draft's page. It reads
every view as this person, without changing anything, and says which one failed,
why and how to fix it. A view also fails when its cards would show people an
unfilled `{{...}}` template, a bare id, an internal key such as
`awaiting_approval` or raw data, and a field showing a Workflow's fixed
choices (`enum` values) without labels for each: fix the field or the Workflow step that writes it. It proves reads work, not that clicking works, so never
tell the person a button was tested. A view with `no_data` has nothing to read
yet, which is not a failure.

Design the page for the people who use it. When the request names neither a
person nor a change (the App's edit button asks "redesign this page around
the people who use it"), start with operation `app_audience`. It returns the
Workspace's purpose, its people and roles, what waits on them, its projects by
stage and how the page is used. Ask the person with `request_input` who the
page is mainly for: up to three options, each a person or role with what they
act on first ("Account owner — pending approvals and finished files first",
"Editor — drafts waiting for notes"), recommending the one with the most
waiting on them. Then call `design_app` with the person's request as `brief`
and the chosen option as `audience`. An explicit change ("put approvals
first", "hide Research") goes straight to `design_app` without asking.

For small named changes (titles, descriptions, visible/order settings, primary action, input forms
and internal execution targets), create a configuration draft using `app_view.update`.
Use returned action keys for existing entries and catalog IDs for runner references.
New entries do not require a Workspace Service: add
`entries:{"action:<unique id>":{"title":"Entry name"}}`, with a typed target in
`targets`. Agent command text goes in `prompts:{"action:<id>":"Instruction"}`;
input questions go in `forms`. Multiple entries can use the same Agent with
different commands. Remove a custom entry with `entries:{"action:<id>":null}`;
this removes only its App display settings and preserves the runner.
To target a saved Workflow, add the entry and `workflow_binding.attach` with
its `action_key` in the same draft. Do not invent or create a Service for an App entry. Internal targets use `agent:<id>`,
`agent:default`, `workflow:<binding_id>` or `automation:<job_id>`. Switching
Run with must update the action's target and matching configuration together.
Preserve other actions and the user's existing layout. Read the tool's live
patch contract for collection replacement semantics before supplying arrays.

To connect an unattached Workflow, use `workflow_binding.attach` with its actual
`workflow_id`, optional existing `action_key` (or `service_key`, never both), and
`primary:true` when it should be the main action. The reviewed apply creates
the binding and App action; it does not deploy or run the Workflow.

Data sections use declared `data_sources` and `data_view` components in the same
App configuration. Project sources can set `label_fields:[["title"],["edition"]]`
to compose readable names from up to three declared string fields in the producer
`state_schema`. Do not use internal project keys as display names. Omit
`launch_workflows` (or use `[]`) for a selector of actual projects only; include
launch mappings only when the Page needs attempts that have not created a project.
Data views are not tied to campaigns or publishing. Choose a context label and
sources for the actual tracked object (for example a delivery or inspection).
One Workspace can have several independent components for different Workflows.
Ordinary `workflow_projects`, `related_projects`, and typed `project_records`
can render as `records` without platforms, publication receipts or Stats.
Set `visibility:{block:<records block key>,when:"has_records"}` to show only
contexts with records in that declared source. Omit visibility when the section
should also show work in progress. Workflow completion is not a universal gate.

A project that contains projects (a series and its episodes, a campaign and
its slots) is one data view: a `workflow_projects` source for the parent, a
`related_projects` source for its children, `project_files` for the files in
the parent's folder and its children's folders (a documents block groups them
by subfolder, exactly as the Workspace files show them) and `project_references`
for what the parent pins for every child: narrator voices
(`reference_kind:"voices"`) and named images such as a key visual or a
character sheet (`reference_kind:"images"`). A `references` block shows each
one, with the image itself, and which project holds it. The
producing Flow names the folders: `workflow_project` create or claim with
`folder:{name:"{{plan.title}}"}`, and a child with
`folder:{name:..., under:"{{parent.state.project_root}}"}`; its `state_schema`
declares `project_root` as a string. A voice is pinned where the parent's state
declares an empty slot, `references:{voices:{narrator:{}}}`, and every child's
narration reuses it. A parent that declares `references:{images:{}}` keeps every
image made inside it under a name (`generate_file` image `workspace_asset_key`,
saved in its `references/` folder); children pass
`params.reference_assets:["<name>"]` to image or video generation to use exactly
those files. Set `context:{kind:"project",source:"parents",display:"cards",
cover:"<references block of images>",progress:"<project_rollup block with
completion>"}` to list the parents as cards (cover, name, children done); one
opens that parent's blocks, and a `packs` block over `project_files` with
`results_only:true` shows each child as a cover card that opens its results.
Give that block `plan:"<project_records source of the parent's plan list>"` to
list every planned child in the plan's order (one not started yet is a card
too), and `item_action:{action:"action:<entry>",input:"episode_id",
label:"Remake"}` to open that App entry from a started child's card with the
child chosen; the `project_rollup` that counts children takes the same
`plan` so its total is every planned child. Give the same block
`plan_action:{action:"action:<entry>",label:"Make this episode",
inputs:{series_id:"parent",episode_code:"code"}}` to open an App entry from a
planned card, its Flow inputs (one to four) filled from the card's `parent`
project or its plan `code`. The entries the cards open are ordinary App entries
(`entries` with a Workflow in `targets`, and `asks:{"action:<entry>":["episode_id"]}`
for the hidden Flow inputs they ask for); list them in `hidden` so they show on
the cards only, never as tiles of their own. A Flow that makes a child again opens it with `workflow_project`
`production:"take"`: the run works in `<child folder>/takes/<run>`, read as
`{{child.production_root}}`, and records it on the child when done; the card
then shows only that take. Do not add a section per Workspace for this; configure
these sources.

A `progress` block (a calendar's scheduled items) takes the same `item_action`.
A `progress` or `summary` card may carry up to four more in `item_actions`. Each
one may fill its form from the record (`prefill: {<Flow input>: <source field>}`,
so an edit starts from the current values) or ask first and then run in place
(`confirm: "<question>"`, for taking something off or cancelling it).
A `progress` block over `workflow_projects` that lists records people add by
hand (a menu, a catalogue) takes `add_action: {action: "workflow:<add flow>",
label: "Add a dish"}`: an Add button in the list's header that opens that
entry exactly as its tile does (same form, same approvals). The entry may
stay a tile too.
For an action about the item's results (refresh this post's counts), give it a
`result_action` of the same shape: it shows as a small icon beside the results
and runs the entry in place, so the entry's other inputs must not need answers.

Project sources may declare `stages:["scheduled","ready"]` and
`order:{field:["due_at"],type:"datetime",direction:"asc"}`. The ordering field
must be a declared string path in the producer schema. Use timezone-aware ISO
timestamps; missing or invalid older values sort last. Stage filters apply to
all dependent views, including related records. Keep a source unfiltered when
both pending and completed results must remain visible.

By default a project execution link reads the project's current `last_run_id`.
When a later status check could replace that pointer, a project source may
declare `execution_run_id_path:["publishing","run_id"]`. The path must be a
string field declared by the producing Workflow's `state_schema`; the App then
links only to that same Workspace's referenced run. For a canonical native
retry lineage it shows the latest retry attempt; it does not fall back to
`last_run_id` when the value is absent or invalid. This is a read projection,
not a retry, launch, approval, or recovery control.

Use a `project_rollup` block in an existing project-context data view for a
summary across all matching projects, not just the current page:
`{key:"schedule_summary",type:"project_rollup",source:"scheduled_items",count_by_stage:true,first:{stages:["scheduled","ready"]}}`.
`first` requires the source's datetime order and selects the earliest matching
item, including overdue items. Its preview contains the label, stage, ordering
date and linked execution. Use `first.stages` to choose pending work without
hiding completed results from other blocks that share the source.

A `project_rollup` can declare
`completion:{stages:["delivered"],label:"Delivered"}`; the App derives the
completed count from that block's returned `total` and `counts_by_stage`.
This is display configuration only and does not advance a Workflow Project.
`project_rollup` and `progress` may declare
`stage_labels:{ready:"Materials ready",leased:"Work in progress"}` to map
stored stage values to human-readable copy. Labels do not establish that work
was generated, published, delivered, or otherwise completed. Use an optional
`empty_message` on any data block only for an honest configured empty state;
it must not imply output or evidence that the configured sources do not have.

A `progress` block can use the same labeled field declarations as `summary`
below, in any context, when its source is a project source; a Workspace-level
list of projects then shows each row's labeled fields and skips empty ones
marked `hide_when_empty`. Legacy field-path arrays remain supported but show
raw state keys. Business stage and current
Workflow run status are separate. An Automation displays its enabled, disabled
or ended state separately from its last run's result, next check and stop date.
Do not interpret a completed check as completion of the tracked project.

For publication results, use a `records` block with
`source:<project_records source>` and `metrics_source:<publication_stats source>`.
For publications with different applicable metrics, set the records block's
`metrics_display` to `configured`. It shows only metrics with matching Stat
definitions for that exact publication; configured metrics without observations
remain visible as unavailable. A publication with no matching definitions remains
pending tracking. Omit the option (or use `declared`) to retain placeholders for
all declared source metric keys. This setting is editable in Page data sections
and travels with the Blueprint.
The Stats source must name this records source as its `parent` and a project
ancestor as its `subject`. This keeps each publication and its observations
together, even when several works use the same platform. A completed Workflow
alone is not proof of publication: the records source reads durable verified
`publication-receipt/v1` records, and missing observations remain unavailable.
For a project section that starts at publication, set
`visibility:{block:"published",when:"has_records"}`. It filters the selector
and records block to verified publication receipts; missing, pending,
unavailable, and error metrics remain
visible. Use `when:"has_observations"` only when the section must wait for an
active matching collector and a real observation (zero counts qualify). Both
rules are portable App configuration and remain editable in Page.

Configure display metadata with
`record_display:{title:{source:<project owner or ancestor>,field:["title"]},fields:[{label:"Outcome",source:<project owner or ancestor>,field:["outcome"],labels:{pass:"Passed"}}]}`.
Each field must be declared in that source's `fields` and the producer
Workflow's `state_schema`. Keep these references in App configuration so Page
edits and Blueprint export/install preserve them. Optional `labels` maps stored
status values to domain copy; do not bake business status meanings into the UI.
Add `record_display.media:[{source:<project_documents source>,label:"Evidence"}]`
to show up to four associated documents inside each record. A media source's
parent must be the record's declared project owner or ancestor. Its
`document_path` must resolve to a canonical Knowledge Document ID. The App
resolves each record's exact project reference and enforces Document access;
never bind a campaign-wide first image, filename match, filesystem path or
arbitrary image URL. Image previews use the shared thumbnail service; other
files retain their viewer entry. Configured media labels describe the actual
role, not an assumed publication status. Showing an asset does not prove it
was externally applied. Hide a redundant standalone documents block through
Page display settings only when those documents already appear with records.
Preserve the main action and
existing Page display choices. App bindings do not create collectors: the
Workflow must configure Stats and deploy collection/reporting schedules before
launching work that can publish.

For a selected project context, add a `summary` block to the same project-scoped
component as its related records so they share one project selector. Its source
must exactly equal the component project source. Remove a component-level
record visibility condition if this context should appear before publication.
Use a separate summary component only for an independent project selection.
Use fields such as
`{label:"Target date",field:["timeline","target_at"],format:"date",hide_when_empty:true}`.
Formats include `text`, `status`, `date`, `link`, `link_list`, and
`document` (the App design guide and `module_catalog` list every one, such as
`percent`, `duration`, `date_range`, `tags`, `member`, `audio`, `images`,
`clips` and `timeline`, and the media options such as `aspect` and
`transcript_field`; a `gallery` block's `media` presentation is described
there too, and so is `preview`, which shows a `progress`, `records` or
`gallery` block's records as the posts they will be on Instagram, 小红书, X,
LinkedIn and the other platforms, with a calendar's `lane_field` for a
platform × day schedule; so are a `progress` block's `timeline`, `gantt`,
`places` and `tree` views, its `group_by`, `select`, `export` and `totals`, a
field's `bar` and the `sparkline` format, the `chart` block (line, bar,
stacked, donut, funnel, leaderboard, heatmap, scatter over records or Stats,
with a Goal's target line) and `display.layout: "grid"` for a dashboard);
`status` may map stored values with `labels`. A field whose
Workflow saves a fixed choice (an `enum` on a run input, an input wait or the
`state_schema`) needs `labels` for every value, or `format:"status"` with the
block's `stage_labels` naming them. Link lists are arrays
of `{label,url}` and only safe public HTTP(S) URLs render as links. A `document`
field holds a stored Document ID and opens that Document in the viewer. Summary fields
must be declared source paths backed by the producer `state_schema`; they cannot
query another Project, Automation, or arbitrary URL. Keep an empty summary
component out of the page rather than adding placeholder content.

Use create_draft with nonempty patches, then validate_draft/preview_diff when
needed. The returned review card owns approval; apply_draft only after approval.
After apply, re-read get_current and verify the target, primary action and layout.
If multiple workflows fit and the intended one is unclear, ask for the choice
before drafting a connection. An unavailable target requires explaining the
specific missing binding or configuration, not declaring that the whole App is absent.
