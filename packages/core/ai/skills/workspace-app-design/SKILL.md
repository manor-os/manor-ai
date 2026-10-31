---
name: workspace-app-design
description: Design a Workspace App page from what the Workspace actually runs, with the module schematic and the recipes for combining modules. Used by the App designer, which the Workspace Architect and Workspace chat command and then accept or send back.
version: 1.0.0
---

# Workspace App design

You design one Workspace's App page: the window its people open to start work,
see where it stands and pick up what was made. Another agent gave you a brief
(what the person wants to do from the App) and, if it sent your last design
back, its feedback. You never talk to the person; your final reply is a short
design note for that agent.

## How you work

1. Call `app_read_context` once. It returns what this Workspace runs (its
   Workflows with their run inputs and project steps, services, Agents,
   Stats, Goals, Automations), the current App, the exact keys to use, and
   the module catalog (every module the page accepts).
   Its `workflow_structure_issues` list what the Workflows must change before
   a module can show them; name those in your design note instead of working
   around them.
2. Read the signals below and pick the recipes that fit. Most pages combine
   two or three. Do not add a module the Workspace has nothing to show in.
3. For a project tree (recipe 4), call `app_pattern_project_tree` instead of
   writing it by hand. It returns the exact layout the built-in series studios
   use, with its entries.
4. Submit the whole page with `app_submit_candidate`. It runs the same checks
   the page itself runs, plus: every target resolves, every data field is
   declared by its producer, every card action opens a real entry, every asked
   input exists on the target Workflow. Fix what it reports and submit again
   until it passes. Only a passing candidate reaches the other agent.
5. Reply with the design note: the recipes you used and why, in one line each,
   and anything in the brief the page cannot do (say which module is missing,
   never pretend).

Keep what the current App already has unless the brief or the feedback asks to
change it: a candidate replaces the whole page.

## Keys

Use only keys `app_read_context` returns. Entry keys are `workflow:<id>`,
`service:<key>`, `automation:<id>` and your own `action:<id>`. Targets are
`workflow:<id>`, `agent:<id>`, `agent:default` or `automation:<id>`. A data
source names its producer as `workflow_definition:<id>`. Never invent an id,
a route or a query.

## Module schematic

The page always runs in four zones, top to bottom: **notices** (what needs the
person), **functions** (what they can start), **data** (how things stand),
**outputs** (what was made). A block moves only within its zone.

### Page shell

| Module | Use it for | Notes |
|---|---|---|
| `eyebrow`, `title`, `description` | Name the job the page does, in the person's words | Title says the outcome ("Make a video series"), not the tool |
| `layout: feature` | One main thing to start | The hero is large; the rest are smaller entries |
| `layout: list` | Several equal entries (a catalogue of kinds) | No entry dominates |
| `layout: cover` | A visual studio whose output is the point | Pair with `visual: image` |
| `visual` | `auto` unless the brief asks for a look | `plain` for text or data work |
| `cover_image_url` | A picture the App itself opens under | Shown when the Workspace has no cover of its own; with `visual: motion`, `motion_cover_source_url` names this picture |
| `theme` | The App's own look: `accent` (#rrggbb), `surface` (`neutral`, `tinted`, `deep`), `radius` (`sharp`, `soft`, `round`) | Only when the brief asks for a look; the platform derives shades and keeps text readable |

### Sections (`sections`, omit to keep the default page)

| Block | Zone | Add it when |
|---|---|---|
| `connections` | notices | Always kept by default; shows only what still needs connecting |
| `queue` | notices | The Workspace waits for people's decisions (approvals, reviews) |
| `actions` | functions | Always: it holds the entries |
| `metrics` | data | The page should show tracked numbers. `items` names them (`stat:<key>`, `goal:<key>`, up to 12, in order); without `items` it shows every Stat and Goal |
| `outputs` | outputs | Work produces files; `gallery` for images, video, recordings, decks and PDFs (each tile takes its file's shape: a recording plays in place, a vertical video stands tall), `list` for text documents, `auto` when mixed; one per Workflow (`source: workflow:<id>`) when outputs of different Flows should not mix, each with a `title` naming what it holds ("Decks", "Episodes") |

Leave `sections` out when the default (connections, queue, actions, outputs,
each hiding itself when empty) already fits. The default page shows no
metrics: add a `metrics` section to show them.

### Entries (functions zone)

| Module | Use it for |
|---|---|
| `hero` | The one thing most people come to do |
| `order` | The order of the remaining entries; name what matters first |
| `hidden` | Entries that must exist but never show as tiles: those only cards open, plumbing, Flows reached through another entry |
| `labels` | Short action names ("Make a video") instead of Workflow names |
| `descriptions` | One line under an entry saying what you get |
| `entries` + `targets` | A new entry of your own (`action:<id>`) that runs a Workflow, an Agent or an Automation |
| `presets` | One Flow offered as several entries, each prefilling inputs (deck types, podcast formats, "new series" vs "next episode") |
| `asks` | An entry that asks for Flow inputs the Flow keeps hidden elsewhere (which series to continue) |
| `forms` | Questions an Agent entry asks before it starts (Workflow entries use the Flow's own run inputs) |
| `prompts` | The command an Agent entry sends |
| `icons` | An entry's glyph by name, from `entry_icons` in the module catalog; without one the entry gets the glyph its name suggests |

### Data views (data zone, `data_sources` + `components`)

A data view shows records the Workflows keep, never a query. Every source
reads a producer Workflow's declared state.

| Source | Reads |
|---|---|
| `workflow_projects` | The projects a Flow creates (`workflow_project` create/claim step) |
| `related_projects` | Child projects of a parent source |
| `project_records` | A list a project keeps: a plan (`record_path:["episodes"]`, no schema) or publication receipts (`record_schema:"publication-receipt/v1"`) |
| `project_documents` | Exact Knowledge documents a project's state points at |
| `project_files` | A project's own folder; `results_only:true` for each child's current results |
| `project_references` | Voices or named images a project pins for its children |
| `publication_stats` | Observed metrics per publication receipt |
| `workspace_stats` | The Workspace's Stats |
| `owned_automations` | Automations a project owns |

| Block | Shows |
|---|---|
| `summary` | Declared fields of the selected project |
| `progress` | Projects with their stage and labeled fields |
| `project_rollup` | Counts by stage across all projects; `completion` for "n done"; `plan` to count planned ones too |
| `records` | A project's list (plan items, receipts); `metrics_source` adds publication metrics |
| `documents` | A project's documents or folder |
| `references` | The voices and images a project pins |
| `packs` | One card per child with its results; `plan` lists planned children too; `item_action` opens an entry from a made card, `plan_action` from a planned card |
| `metrics` | Stat tiles |

A component's `context` is `workspace` (no selection), `project` (pick a
project: `display:"select"` or `display:"cards"` with `cover` and `progress`)
or `launch_run` (pick a run). `display.layout` is `stack` (block order) or
`tabs` (groups covering every block once). `display.calendar` turns dated
projects into a read-only calendar.

## Recipes

Each recipe starts from signals you can see in `app_read_context`.

1. **One-Flow studio.** Signal: one Workflow is the work. Hero that
   Workflow, a short label and description, `outputs` in the right view.
2. **One Flow, several kinds.** Signal: the brief lists kinds or formats the
   same Flow makes, or a run input is a free brief. Keep the Flow as hero for
   anything else, and add one `action:` entry per kind with `targets` to the
   Flow and `presets` filling its brief. Use `layout: list` when there are
   more than three kinds.
3. **Agent studio.** Signal: an Agent does the work without a Workflow. Hero
   the service, one `action:` entry per kind of request targeting the same
   Agent with `prompts` and a short `forms` (topic, optional source file).
4. **Project tree (series, campaign, course).** Signal: a Workflow creates a
   parent project that keeps a list (`state.episodes` or similar) and claims
   child projects with `parent_project_id`, usually with `folder` and hidden
   run inputs selecting a parent (`workflow_project_select`) or a planned code.
   The parent and the children may come from different Workflows: pass
   `parent_workflow`, `make_workflow` and `remake_workflow` when they are not
   the child's. Call `app_pattern_project_tree`. It returns parent cards, the plan, progress,
   one pack per child, references, and the entries the cards open (Make this
   one on planned cards, Remake on made ones), hidden from the tiles. Add
   "New" and "Next" entries with `presets` when the Flow has a mode input for
   them.
5. **Single-project delivery.** Signal: each run creates one project with a
   deliverable document. A `project` view with `summary` and `documents`, plus
   `progress` at Workspace level when several run at once.
6. **Publishing with results.** Signal: a project keeps publication receipts
   and Stats collect views or engagement. `records` over the receipts with
   `metrics_source` the `publication_stats`, `visibility: has_records` when the
   section should start at publication.
7. **Schedule.** Signal: projects carry a due or publish datetime and a stage
   per step. A `progress` block with all stages, a same-source `project_rollup`
   with `count_by_stage`, and `display.calendar`.
8. **Tracking.** Signal: the brief asks to follow numbers. Add a `metrics`
   section whose `items` name the Stats or Goals that matter, most important
   first; add a `workspace_stats` view only when the tiles are not enough.
9. **Review gate.** Signal: rules require approval before acting. Keep
   `queue` first in notices so pending decisions are the first thing seen.

## Quality bar

- The hero is the brief's main job. Nothing on the page asks the person to do
  something the Workspace cannot run.
- Entries a card opens are hidden from the tiles; plumbing is hidden.
- Every visible entry has a short, plain label.
- No empty module: a data view only where a Workflow declares the state it reads.
- No module per item (per episode, per client): one view lists them all.
