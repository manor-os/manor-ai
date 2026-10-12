---
name: video-edit
description: Default workflow for editing, composing, or creating a finished video from screenshots, UI captures, uploaded clips, images, audio, or a written brief. Use for video editing, product demos, app walkthroughs, launch promos, explainers, motion graphics, title sequences, captioned videos, social ads, clip trimming/reordering, transitions, overlays, and other multi-scene video work. Use Video Edit for the final composition and approval-gated render; generate standalone AI footage only when a scene truly needs it.
---

# Video Edit

Own the finished video, not just a script, storyboard, or collection of clips.
Build an editable project, produce review frames, and render only the exact
revision that passed review and that the user approved. A request for the
finished or exported video is that approval; only a request to review the
edit before rendering asks for a separate confirmation.

## User-facing contract

- Call the capability **Video Edit**. Do not expose the underlying render
  engine in ordinary progress, review, error, or completion messages unless the
  user asks for implementation details.
- Describe observable stages: prepare assets, edit the project, validate review
  frames, render, and return the editable source plus MP4 (asking for approval
  first only when the user asked to review before rendering).
- Never claim that a video is complete before the render result exists.
- Keep one `session_id` from project start through revisions and final render.
  Do not restart the Sandbox between authoring and review unless the Tool reports
  that it recovered an expired session.

## Route video work here

Use this workflow when the requested deliverable is a composed or edited video,
especially:

- trimming, reordering, joining, reframing, or transitioning uploaded clips;
- adding titles, captions, callouts, overlays, cursor actions, music, or SFX;
- product demos and UI walkthroughs made from real screenshots or recordings;
- promos, explainers, social ads, brand motion, diagrams, and title sequences;
- multi-scene productions that combine footage with designed motion graphics.

Do not replace a composed final with one generic AI-generated clip. Generate
new footage with `generate_file(kind="video")` only for scenes that truly need
photoreal people, environments, or camera footage. Wait for every generated
clip with `wait_media_jobs`, then keep it as a replaceable local asset.

## 1. Resolve the edit

Infer reasonable creative choices from the request and references. Establish:

- audience, message, call to action, aspect ratio, duration, tone, and audio;
- the narrative arc and time budget for each scene;
- which supplied files are source clips, screenshots, visual references, or
  audio;
- the exact editing operations requested: trim, reorder, caption, highlight,
  replace, transition, mix, or animate.

Use authentic product media when supplied. Do not invent replacement UI.
When `video-production` supplies a reviewed `CANVAS.md`, treat its shot IDs,
first/last frames, dialogue, audio cues, and chapter timing as the edit brief;
record any necessary changes back against those IDs in `STORYBOARD.md`.
For designed motion, typography, 3D or beat-driven edits, invoke
built-in `motion-design`, then load each reference it lists that the edit needs
with `invoke_skill(skill_id="motion-design", bundle_file="references/<name>.md")`
and apply it. `read_file` cannot open another skill's references. This skill may
invoke only the guidance skills `motion-design` and `video-type-playbooks`.

## 2. Start the editable Sandbox project

Treat the `video_edit` Tool as the authority for runtime readiness. Do not run
generic shell probes such as `which chrome`, `which google-chrome`, or a host
browser check, and do not downgrade to PIL/ffmpeg because those probes fail.
The isolated image uses headless Chromium at `/usr/bin/chromium`; it does not
provide a `chrome` executable or desktop GUI. Only a structured
`runtime_unavailable` response from `video_edit` means the video runtime is
unavailable.

Choose a concise entity-relative path such as `Videos/<project-slug>` and call:

`video_edit(operation="start", project_path="Videos/<project-slug>", reference_paths=[...])`

Pass attached `/api/v1/fs/...` paths directly in `reference_paths`. Use the
returned `session_id` and `project_path` exactly. Readable references are copied
into the project and delivered to the same active Sandbox.
Before `start` or `push_assets`, resolve every other reference through the
actual `read_file`, `list_files`, or completed media Tool receipt. Pass only
**existing file paths** from those results; `reference_paths` does not accept
directories, guessed locations, document IDs, or placeholder paths. If a
music, voice, or canvas file cannot be resolved, omit that reference and report
the missing file instead of inventing a path. For a revision of an existing
editable project, start with its existing `project_path`; do not copy its own
project directory into `reference_paths`. Use the exact Knowledge `fs_path` for
each external asset that must be staged.
For a `/viewer/<document-id>` link, use
`search_documents(query="<short filename>", detail="details")` and match the
returned `document_id` exactly; the `fs_path` in that record is the file to
stage. `search_documents` searches document names, so do not pass a full
sentence, a Knowledge directory, or a document ID as its `query`. To inspect
an editable project directory, call `list_files(path="<project_path>")`, then
`read_file` on the specific returned source files. An empty filename search
alone does not prove that a supplied project path is absent.
Knowledge may normalize spaces and punctuation in names. Copy the exact
returned `fs_path` or `list_files` path into tool calls; a path printed in a
canvas or earlier chat answer is only a hint until verified.

Read the returned `authoring_contract` before writing source. It is the current
machine-readable contract, including supported assertion schemas and a minimal
HTML/sidecar example. Start, status, sync and recovered sessions expose it again;
do not guess a different framework or schema from a previous project's files.

When another tool, browser flow, or user response produces a new screenshot,
clip, image, voice track, music bed, or data file, deliver it without restarting:

`video_edit(operation="push_assets", session_id="<session_id>", reference_paths=[...])`

The Sandbox has no arbitrary internet access. When it needs something missing,
publish a structured request with `operation="report"` and
`event_type="asset_request"`, acquire the material through Manor's approved
external tools, then call `push_assets`. Use `event_type="progress"`,
`"warning"`, or `"question"` for concise status that the parent Chat should
surface. Poll `operation="status"` or `operation="events"` with
`after_event_seq` to keep the lease alive and receive only new events.

## 3. Author the complete edit

Write or update project source inside the active Sandbox with:

`video_edit(operation="write_files", session_id="<session_id>", files={...})`

`write_files` **replaces each named file in full**. For a revision, read the current
file, apply the change to its complete contents, and submit that complete file.
Never send only a CSS or JavaScript fragment as `index.html`: it destroys the
composition root and makes review fail. After every write, inspect
`missing_project_contract`; fix every listed item before calling `review`.

Split large projects into logical batches. Every successful call writes inside
the Sandbox and atomically synchronizes the same editable files back to Manor.
Do not use general entity `generate_file`/`patch_file` for a session started with
`operation="start"`. Include at minimum:

- `BRIEF.md` — objective, audience, message, deliverable, and constraints;
- `STORYBOARD.md` — timecoded scenes, edits, copy, transitions, and audio;
- `frame.md` — palette, type, spacing, depth, texture, and motion language;
- `video.json` — runtime composition metadata;
- `package.json` — private ESM project pinned to the supported video runtime;
- `index.html` — complete composition using only local assets;
- `index.motion.json` — validation assertions about visible motion, not playback
  tracks or keyframes. Author playback in the HTML's GSAP timeline.

Use this minimal `video.json` shape unless the project needs extra local paths:

```json
{
  "paths": {
    "blocks": "compositions",
    "components": "compositions/components",
    "assets": "assets"
  },
  "media": {"autoProxy": true},
  "authoringSkill": "video-edit"
}
```

Keep `package.json` local and deterministic; it only needs a safe project name,
`"private": true`, and `"type": "module"`. The Sandbox supplies the pinned
render CLI and browser, so do not fetch a runtime dependency from the network.

The standalone render root must have `data-start="0"`, a unique
`data-composition-id`, and explicit `data-duration`, `data-fps`, `data-width`,
and `data-height`. Give it an explicitly resolved pixel size. Every visible
timed child must be a direct child of the root with `class="clip"`, `id`,
`data-start`, `data-duration`, and `data-track-index`. Register exactly one
paused, synchronously created timeline at
`window.__timelines["<data-composition-id>"]`. Declare every timed layer with
runtime `data-*` attributes. Drive animation deterministically from composition
time so seeking and frame rendering reproduce the same state. Do not use live
clocks, unseeded randomness, infinite repeats, network requests, or manual media
playback.

Before the inline timeline, load
`<script src="./assets/vendor/gsap.min.js"></script>`. The Sandbox supplies this
library during review; do not download it. A `local_gsap_import` violation means
the entry HTML lacks this import, not that the rendering engine is unavailable.
Initialize `window.__timelines = window.__timelines || {}` before registering
the timeline. Use the returned minimal HTML example as a structural guide,
while preserving the user's actual clips, text and design.

Use only the supported motion assertion kinds in `index.motion.json`:

```json
{
  "duration": 12,
  "assertions": [
    {"kind": "appearsBy", "selector": "#headline", "bySec": 0.8},
    {"kind": "before", "a": "#headline", "b": "#cta"},
    {"kind": "staysInFrame", "selector": ".product-card"},
    {"kind": "keepsMoving", "withinSelector": ".scene", "maxStaticSec": 2}
  ]
}
```

Never invent assertion kinds such as `exists`. A selector that matches nothing
is a validation failure.

The returned `authoring_contract.motion_assertions.schema` is authoritative for
supported kinds and their required fields. Clip counts, track counts and
keyframes are project data, not assertion kinds. Manor validates **every existing
root `*.motion.json`** file: writing a valid new `index.motion.json` does not fix
an invalid `hook.motion.json` or `timeline.motion.json`. Repair each reported
file/pointer while retaining its other data; do not discard existing files.

`write_files` returning `source_updated` / `source_saved` only confirms saved
source. Check `contract_valid` and `contract_violations` after every batch.
Violations identify the file, rule, pointer, explanation and expected shape.
Tool failures use the shared `tool_error` contract with `error`, `message` and
`details`; diagnostics do not select or execute a repair. Compare the reported
constraints with the current source and choose the next correction within this
Agent loop, preserving the user's content and any already-saved work.
A valid static contract still requires strict review and approved rendering.

For source clips, preserve the original files and express trims, ordering,
speed, transitions, captions, graphics, and audio as editable composition
layers. Keep critical text as HTML/SVG text. Freeze every dependency under
`assets/`; do not load remote scripts, fonts, images, audio, or video.

## Quality bar

- Design each scene around one communication job and one focus point.
- Keep UI authentic and legible; show an action, then its product value.
- Use a disciplined grid, strong hierarchy, intentional negative space, and a
  restrained visual system.
- Use motivated camera moves, masks, depth, typography, and transitions. Let
  actions settle instead of keeping every element moving.
- Vary scene composition without losing continuity. Make the final frame a
  deliberate closing composition.
- Check captions, safe areas, clip boundaries, audio levels, and the first and
  last frame before review.
- Make the canvas audio plan real in the composition: preserve separate voice,
  music, ambience, and Foley tracks; encode intended fades and speech ducking as
  track automation or supported audio controls. A constant `data-volume` does
  not implement a planned dynamic mix. Inspect cut points against audible cues
  and rendered playback, especially when narration crosses a picture cut.
- Probe the final narration file and give its last spoken word room to finish.
  Do not set the composition, narration clip, subtitles, or music bed to end
  before that word; then allow the final brand frame to hold while music fades.
  Inspect both the audio tail and the branded last frame in the exported MP4.
- Before extending a shot to fit narration, probe the source video's real
  duration. Never give a video element a playback window longer than available
  source footage; extract and place an actual still frame for a closing hold.
  Review a frame within that hold so a missing source frame cannot become a
  black or empty end card.

## 4. Review before rendering

Call:

`video_edit(operation="review", session_id="<session_id>")`

Fix every reported issue and review again until the strict gate passes. Return
the review frames, the complete production canvas/assembly map, and `review_sha256`
to the parent Chat; the limited review contact sheet does not replace a shot-by-shot
canvas or explain how the video was generated and assembled. When the user asked
for the finished or exported video, that request approves the revision that passes
this review: continue to render it in the same invocation, without asking again
or waiting for another message. Stop and ask for approval or revisions only when
the user explicitly asked to review or approve the edit before it is rendered.
Never write an approval on the user's behalf in either case.
Inspect the actual closing frames against the canvas before rendering or requesting approval:
the intended action must occur in the source in/out range, the product and brand
must remain legible through the last frame, and any planned end card or CTA must
hold long enough to read. A passed technical gate alone does not prove these
visual requirements. If a frame is wrong, revise the edit and review again.
Do not call `read_file` on binary review PNG/JPEG files to inspect them; that
returns file metadata rather than a visual inspection and needlessly transfers
large images. Use the review's visual artifacts in a capable viewer, or return
their references to the parent Chat for visual review. State plainly when no
visual inspection was possible.
Keep the same session open after a failed review so revisions can use
`write_files` and `review` without rebuilding the Sandbox. Close it after
successful delivery or when the user explicitly cancels the edit.

The Tool runs untrusted project code only in the active network-isolated
Sandbox. Never run the project HTML or render command inside the Manor API or
worker. The review operation first synchronizes any Sandbox-authored changes to
the durable Manor project, then runs the strict gate and captures review frames.

## 5. Render the approved revision

Once the review passes and the revision is approved (by the user's request for the
finished video, or by their explicit reply when they asked to review first), call:

`video_edit(operation="render", session_id="<session_id>", review_sha256="<approved hash>", quality="high", output_name="renders/final.mp4")`

The Tool rejects any source change after review and repeats validation before
rendering. If the user requests a revision, edit the durable source, review
again, and obtain approval for the new hash. Return the registered MP4 and keep
the source project as the editable master.

After delivery, call `video_edit(operation="close", session_id="<session_id>")`
to perform one final source sync and release the Sandbox. If the session expires
before that, the Tool recovers it from the durable Manor source on the next
session operation and reports the recovery as a progress event.

If `video_edit` returns `runtime_unavailable`, report that exact blocker. Never
silently switch to a lower-fidelity preset.
