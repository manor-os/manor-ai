---
name: video-edit
description: Default workflow for editing, composing, or creating a finished video from screenshots, UI captures, uploaded clips, images, audio, or a written brief. Use for video editing, product demos, app walkthroughs, launch promos, explainers, motion graphics, title sequences, captioned videos, social ads, clip trimming/reordering, transitions, overlays, and other multi-scene video work. Use Video Edit for the final composition and approval-gated render; generate standalone AI footage only when a scene truly needs it.
---

# Video Edit

Own the finished video, not just a script, storyboard, or collection of clips.
Build an editable project, produce review frames, and render only the exact
revision the user approves.

## User-facing contract

- Call the capability **Video Edit**. Do not expose the underlying render
  engine in ordinary progress, review, error, or completion messages unless the
  user asks for implementation details.
- Describe observable stages: prepare assets, edit the project, validate review
  frames, request approval, render, and return the editable source plus MP4.
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
- `index.motion.json` — machine-readable timing and keyframes.

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

## 4. Review before rendering

Call:

`video_edit(operation="review", session_id="<session_id>")`

Fix every reported issue and review again until the strict gate passes. Return
the review frames and `review_sha256` to the parent Chat, then stop and ask for
approval or revisions. Do not render in the same turn as the initial review.

The Tool runs untrusted project code only in the active network-isolated
Sandbox. Never run the project HTML or render command inside the Manor API or
worker. The review operation first synchronizes any Sandbox-authored changes to
the durable Manor project, then runs the strict gate and captures review frames.

## 5. Render the approved revision

After explicit approval, call:

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
