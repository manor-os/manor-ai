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
When reference captions and the canonical soundtrack come from separate
Knowledge Documents, `measure_media_audio_alignment` can measure their local
constant offset using approximately corresponding 5–60 second source spans.
Use the returned `target_time = reference_time + target_time_offset_seconds`
mapping before applying the excerpt offset. It searches within one second of
the supplied anchors; it is not global audio search, forced transcription, or
final media certification. Preserve its source hashes and evidence, and reject
unmatched/ambiguous timing instead of assuming the files share a zero point.
For designed motion, typography, 3D or beat-driven edits, invoke
built-in `motion-design`, then load each reference it lists that the edit needs
with `invoke_skill(skill_id="motion-design", bundle_file="references/<name>.md")`
and apply it. `read_file` cannot open another skill's references. This skill may
invoke only the guidance skills `motion-design` and `video-type-playbooks`.

## Work in parallel

Every response is a full model round, so the number of responses sets most of
the production time:

- Put independent calls in the same response: request every independent image,
  clip, narration line and sound effect together, read every file you need
  together, then make one `wait_media_jobs` call for all pending job ids.
- Do not re-read a file you just wrote or a result you already hold; reuse the
  identifiers and measurements from the tool result.
- Keep dependent steps sequential: a call that needs another call's output
  goes in a later response.

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
each external asset that must be staged. A web URL is never a reference: an
asset from the web (an openly licensed font, image or sound) must first be
saved into Knowledge by the caller (a shell download saved with
`manor push /tmp/<file> <Knowledge path>`); then pass that saved path.
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

When the edit needs a music bed that nobody supplied, take a licensed track
from Manor's catalog: `search_free_music(query="<mood, genre, instruments>")`,
then `import_free_music(track_id=..., source_path=<the project's CANVAS.md or
video path>)`. Probe the imported Knowledge file, normalize
it with `normalize_audio_loudness`, and stage it with `push_assets`; keep its
license and attribution for the credits. When the catalog is unavailable or has
nothing suitable, generate the bed instead: find the model with
`generate_file(kind="search", params={"media_kind": "audio", "purpose": "music"})`,
then `generate_file(kind="audio", prompt="<genre, mood, BPM, structure>",
params={"purpose": "music", "expected_model": "<from search>"})`, and record in
the storyboard why the catalog was skipped. An unavailable optional source is a
reason to take the next route, not a blocker. When neither route works (for
example the catalog is down and no music model is configured), build the bed
from generated ambience or sound effects (`purpose: "ambience"` or `"sfx"`), or
go without music, and list the missing music as an acceptance exception with
the errors you saw. A music bed never blocks delivery of the video.

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

`write_files` **replaces each named file in full**: use it to create files or
rewrite one wholesale. Never send only a CSS or JavaScript fragment as
`index.html`: it destroys the composition root and makes review fail.

For revisions of an existing file (review fixes, timing, copy, styling), use
`patch_files` so only the changed text is sent:

`video_edit(operation="patch_files", session_id="<session_id>", patches=[{"path": "index.html", "edits": [{"old_text": "<exact current text>", "new_text": "<replacement>"}]}])`

Each `old_text` must match the current file exactly once, including whitespace,
unless `replace_all` is true; edits apply in order, each against the result of
the previous one. A missing or ambiguous match fails the whole call and writes
nothing; the error names the file and edit index. The result lists each patched
file's new `size_bytes` and `sha256`. A patch is saved exactly like a write, so it
also changes the source hash and requires a new `review` before `render`.

After every write or patch, inspect `missing_project_contract`; fix every listed
item before calling `review`.

### Strict review rules

Review fails on every **error and warning** (only info passes). Each failing
finding comes back in `diagnostics` with `code`, `file`, `message`, `fix_hint`
and often `manor_hint`; apply those. Write the source so these never fire:

- `timeline_track_too_dense`: at most 3 timed elements per `data-track-index`
  in one HTML file (video/audio, captions and sub-composition hosts exempt).
  Give each layer its own track; past three scenes, one sub-composition per scene.
- `duplicate_media_discovery_risk`: never repeat an `<img>`/`<video>`/`<audio>`
  with the same `src` + `data-start` + `data-duration` in one file (a blurred
  backdrop copy, a stamp thumbnail). Use one element per source window; paint
  an extra image layer as a CSS `background-image` on a `<div>`.
- `composition_file_too_large`: keep each HTML file at most 300 lines.
- `overlapping_clips_same_track`: clips on one track must not overlap in time.
- `invalid_parent_traversal_in_asset_path`: no `../` in any `src`, `href` or
  CSS `url()`, also inside `compositions/`; paths are root-relative (`assets/…`).
- `gsap_exit_missing_hard_kill` / `scene_layer_missing_visibility_kill`: after a
  fade-out add `tl.set(<sel>, {visibility: "hidden"}, <end>)`; never tween
  `visibility`/`display` on a `.clip` itself (`gsap_animates_clip_element`) —
  animate an inner wrapper.
- `gsap_timeline_set_initial_hide`: author initial hidden states in CSS or
  `gsap.set()`, not `tl.set(..., 0)`.
- Fonts: `font_family_without_font_face` and `google_fonts_import` fail. Declare
  a font installed in the Sandbox (motion-design lists them) with `src: local()`:
  `@font-face { font-family: "Noto Sans CJK SC"; font-weight: 700; src: local("Noto Sans CJK SC Bold"); }`.
  It needs no file, license record or glyph check, so never stop for one. Ship any
  other face as a font file under `assets/` with `@font-face`.
  `system_font_will_alias` and `timed_element_missing_visibility_hidden` are info
  only.
- Media: every `<video>`/`<audio>` has `src`, `data-start` and a unique `id`;
  a timed `<video>` is `muted` (sound via a separate `<audio>`) unless
  `data-has-audio="true"`; no `preload="none"`.
- `studio_missing_editable_id`: every timed element or host has an `id`.
- Browser findings: any console/page error, failed request, held text
  overflow/overlap, or contrast below WCAG AA (use `suggestedColor`) fails.

Split compositions across files instead of one huge `index.html`:
scenes as sub-compositions in `compositions/<scene>.html` (a `<template>`-wrapped
root with its own timeline, mounted by a host clip with `data-composition-src`),
and shared styles in local `.css` files referenced by root-relative paths. Manor's
static contract reads only `index.html`: keep its single inline paused root
`gsap.timeline` registration there, plus at least one inline timed
`class="clip"` element (for example a full-length background section), because
sub-composition hosts do not count as timed clips. Every such file is editable
source: synced, hashed into `review_sha256`, and kept for render. Each
sub-composition registers its own paused timeline at
`window.__timelines["<its data-composition-id>"]` inside its template; one
animated only from the root timeline instead puts `data-no-timeline` on its
host element. Review rejects a sub-composition with neither: the render waits
for that timeline and refuses to export (`sub_timeline_readiness_timeout`).

Sub-composition contract. Export waits for `window.__timelines[<id>]` of every
mounted scene and fails with `sub_timeline_readiness_timeout` when one never
registers, which `review` does not catch. For each scene:

- The host in `index.html` carries `data-composition-id`, the
  `data-composition-src` path and the timing; the file's root inside
  `<template>` carries the **same** `data-composition-id`.
- The file's `<style>` and `<script>` go inside the root (scripts in `<head>`
  are dropped). The script registers one paused timeline under that id; GSAP
  comes from `index.html`, so do not load it again.
- A scene with no animation of its own puts `data-no-timeline` on its host.
- Style the scene through its root `id` (`#s01-root .title`), not
  `[data-composition-id=…]`; target GSAP tweens by unique ids.

```html
<!-- index.html, inside the root -->
<div id="s01-host" data-composition-id="s01" data-composition-src="compositions/s01.html"
  data-start="0" data-duration="2" data-track-index="1"></div>
<div id="s02-host" data-composition-id="s02" data-composition-src="compositions/s02.html"
  data-no-timeline data-start="2" data-duration="2" data-track-index="1"></div>

<!-- compositions/s01.html -->
<template id="s01-template">
  <div id="s01-root" data-composition-id="s01" data-width="1920" data-height="1080">
    <h2 id="s01-title" class="title">Scene one</h2>
    <style>#s01-root .title { position: absolute; left: 160px; top: 420px; }</style>
    <script>
      window.__timelines = window.__timelines || {};
      const tl = gsap.timeline({ paused: true });
      tl.fromTo("#s01-title", { x: -40 }, { x: 0, duration: 2, ease: "none" }, 0);
      window.__timelines["s01"] = tl;
    </script>
  </div>
</template>
```

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

The returned authoring contract defines the supported HTML timing attributes.
`data-source-start` on an `<audio>` element, or any other invented `data-*`
attribute, is not an audio source-trim control and is ignored by Video Edit.
Do not claim a source offset unless the project uses a verified editing path
that supports it. For the native `compose_video_timeline` route, use its
documented `audio_tracks[].source_start` or `sourceStart` field instead; this
HTML route currently has no declarative audio-source-trim contract.

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

`write_files` / `patch_files` returning `source_updated` / `source_saved` only confirms saved
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
- Build designed motion scenes in layers: a background (texture, gradient or
  footage with slow drift), the main subject, and a foreground of type and
  accents, each with its own motion and easing. One animated element on a flat
  background is a slide, not motion design.
- Give every scene an entrance (staggered, varied easing), a hold with
  secondary motion (drift, parallax, counters, light sweeps) and a designed
  transition into the next scene (mask wipe, match cut, shared element, camera
  push). Avoid fully static frames longer than one second unless the brief asks
  for stillness.
- Reveal headlines by line, word or character with masks or offsets rather
  than a single fade, and keep them readable during the hold.
- After the first review passes, make one craft pass that upgrades the two
  weakest scenes, then review again.
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

Every file inside the project is part of the reviewed source, so any write
after a passing review invalidates its `review_sha256`. Keep review logs,
acceptance notes and other records outside the project (in the storyboard or
canvas document), and hand off only a hash returned by a review that came after
your last write; if you wrote anything afterwards, review again.

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

A calling skill or brief may cap the number of review passes to bound cost.
The cap limits iteration, not delivery. When it is used up and some revision of
this version passed review, export that revision: if the source changed since,
restore the passed revision's files exactly, review it once more to confirm the
same `review_sha256` (re-confirming already-passed source does not count
against the cap), and render it. Hand it off as delivered with exceptions: each
unresolved review finding from the later attempts becomes an unmet acceptance
criterion with the finding as its evidence. Report `blocked` only when no
revision ever passed review, so there is nothing to render, with the remaining
findings and the last review frames. The cap is the production's own cost
control: never ask the user, an operator or any other authority to raise it or
to grant an exception.

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

After the rendered MP4 is registered, call
`video_edit(operation="close", session_id="<session_id>")` to perform one final
source sync and release the Sandbox. When your part ends at review and a later
step or the user renders, leave the session open and return its `session_id`
with the review hash; closing it there forces the renderer to start over. If the session expires
before that, the Tool recovers it from the durable Manor source on the next
session operation and reports the recovery as a progress event.

If `video_edit` returns `runtime_unavailable`, report that exact blocker. Never
silently switch to a lower-fidelity preset. When it also says `transient: true`,
the Tool already waited out a Sandbox Service restart (`retry_attempts`,
`retried_seconds`) and the project source is intact: retry the same operation
once more before reporting the blocker.

If a render returns `render_resource_limit`, the runtime already sized FFmpeg
and browser concurrency to the Sandbox's own PID, memory and CPU limits and
retried once at its minimum. `reason` (`pid_limit`, `oom`,
`thread_spawn_failed`) and `step` say what ran out where; `retryable: false`
means repeating the identical export fails the same way. Report that blocker,
or, if the edit allows it, reduce how many video layers play at the same time
and review again. There is no extra stderr or Sandbox control to inspect.

If a render returns `render_audio_failed`, the audio mix stopped the export
before frame capture (the renderer will not ship a silent video). `reason` says
why (`source_not_found`, `invalid_media`, `ffmpeg_failed`, ...) and
`missing_media` lists files the page requested but the render could not find.
For `source_not_found` the Tool already repeated the render with every project
file, so the file is genuinely absent: stage it with `push_assets`, point the
element at the returned `project_path`, review and render again. A render
failure does not invalidate the reviewed revision and never calls for edits to
unrelated parts of the composition.

If a review returns `snapshot_timeout` (or a render `render_timeout`), the
Sandbox browser could not load the composition in time, and the runtime already
retried once with doubled timeouts sized to the project's media. `step` says
where (`quality_check`, `snapshot`, `render`); `retryable: true` means the same
request can succeed once the host has capacity. Do not edit the project, drop
snapshot times or change media preload to work around it: run the same
operation again later, and report the blocker if it persists.

If a review returns `snapshot_video_frames_missing`, frame capture could not
copy one or more `<video>` frames into the review frames (they would have shown
black), so no frames were kept; the runtime already retried once.
`video_extraction` shows FFmpeg's direct attempt on each project video. With
`retryable: true` every video extracted directly, so the host was at fault: run
review again without editing the project. With `retryable: false` the error
names either a clip shown past the end of its footage (`videos_past_source_end`:
its slot is longer than the file — shorten its `data-duration`, slow it with
`data-playback-rate`, loop it or use a longer clip, then review again), the
video that cannot be read in the Sandbox (replace or re-encode that asset, then
review again) or, when no video could be read, the Sandbox itself (report the
blocker; do not touch the assets). Apart from a clip past its footage, never
treat a black video in review frames as a composition bug.
