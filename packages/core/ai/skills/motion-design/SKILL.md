---
name: motion-design
description: Technique guidance for designed motion inside a Video Edit project — deterministic seek-driven animation, GSAP master timelines and beat grids, 3D and 2.5D scenes, word-synced captions and audio mixing, transitions, and platform safe zones. Returns instructions to the calling video-edit or video-production agent; it does not run tools.
---

# Motion Design

**When to use:** the calling agent is authoring or revising a Video Edit project
that needs designed motion — kinetic typography, shape and line animation,
beat-driven cuts, 3D or 2.5D scenes, word-synced captions, or a dynamic audio
mix. Load only the references the edit needs.

**What it returns:** this skill is instructions-only. It hands technique
guidance back to the caller; `video-edit` still owns the `video_edit` session,
source writes, strict review and approval-gated render. Use the project
vocabulary of `video-edit`: `BRIEF.md`, `STORYBOARD.md`, `frame.md`,
`video.json`, `index.html` and `index.motion.json`.

**Priority:** the user's brief, script, assets and facts come first. When a
Video Studio type was explicitly selected, its `video-type-playbooks` reference
sets structure, beats, visual rules and the acceptance checklist; this skill
only says how to build those things reliably. Numbers here are defaults: where
the selected playbook states its own number (loudness range, flash frames,
safe-area band), use the playbook's.

## Runtime facts (do not assume more)

- The Sandbox stages only core GSAP 3.14.2 into the project, at
  `./assets/vendor/gsap.min.js`, and renders with headless Chromium. GSAP
  plugins (SplitText, CustomEase, DrawSVG, MorphSVG and others) are not staged,
  so the composition cannot load them; there is no Three.js or Lottie either.
  Use core GSAP only.
- No network. Never reference a CDN, remote font, remote image or remote
  script. Every dependency is a file inside the project, normally staged with
  `video_edit(operation="push_assets", reference_paths=[...])`; use the
  `project_path` it returns for each staged file.
- Installed fonts: Noto Sans and Noto Serif (Regular, Bold), Noto Sans CJK SC
  and Noto Serif CJK SC (Regular, Bold only), Roboto (Thin to Black, plus
  Condensed), Lato (Hairline to Black), Liberation Sans / Serif / Mono, Caladea
  and Carlito: `@font-face { src: local("Noto Sans CJK SC"); }`, no file needed.
  Any other face: a user-supplied font file staged into the project.
- Inspect before rendering with
  `video_edit(operation="review", snapshot_times=[...])` (up to 12 times per
  review). Render only with the `review_sha256` that passed review and was
  approved (a request for the finished video approves it).
  `render_frame_samples` is for a finished MP4 only.
- `transcribe_video` returns segment timestamps only; word timing is
  interpolated inside a segment. There is no beat detection: build the beat
  grid from a known BPM and a measured first-downbeat offset.

## References

Load a reference by invoking this skill again with its path, for example
`invoke_skill(skill_id="motion-design", bundle_file="references/determinism.md")`;
while next_char_offset is not null, repeat with `char_offset` set to it.
`read_file` cannot open these files; they belong to this skill, not to the
project or Knowledge.

| Need | Reference |
| --- | --- |
| Seek-safe frames, seeded randomness, seamless loops | `references/determinism.md` |
| Master timeline, labels, beat helpers, split text, stagger | `references/gsap-timelines.md` |
| 3D scenes: vendored Three.js, CSS 3D, Canvas 2D/2.5D | `references/threejs.md` |
| Word-synced captions, narration timing, ducking, loudness | `references/captions-and-audio.md` |
| Beat cuts, whip, match cut, iris, liquid wipe, flash | `references/transitions.md` |
| 9:16, 16:9 and 4:5 platform and title safe zones | `references/safe-zones.md` |

Record every technique decision that affects timing (BPM, offset, cut list,
caption grouping, ducking windows) in `STORYBOARD.md` against the canvas shot
IDs, so a revision can reproduce it.
