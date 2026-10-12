---
name: remotion-markup
description: Apply Remotion animation and effects practices. Use when authoring or reviewing Remotion React markup, timing, transitions, typography, charts, effects, or 3D scenes.
version: 4.0.506
license: MIT adapter; Remotion runtime licensing may vary by use
---

# Remotion Markup

Use when authoring or reviewing Remotion React markup, timing, transitions, typography, charts, effects, or 3D scenes.

This is one of the 12 Skill adapters in Manor's built-in Remotion plugin. Work only after the user has enabled the Remotion plugin. Preserve an existing project's structure and versions; new projects use the pinned dependencies already installed with this Skill.

## Execution contract

- Work in the active isolated sandbox and use only deterministic, frame-driven Remotion behavior.
- Register the root synchronously. Give each composition an explicit id, width, height, fps, duration, and valid default props.
- Drive animation with Remotion frames and sequences. Do not use wall-clock timers, CSS transitions, CSS keyframes, requestAnimationFrame, live render-time network calls, or unseeded randomness.
- Use local approved assets and Remotion media components. Never invent a supplied product screenshot or silently replace missing media.
- Validate with `npx remotion compositions`. For visual work, render stills of the final source and inspect them with sandbox `read_file` before rendering the MP4 or claiming the composition is correct: at least the first frame, each element's entrance, and the last frame. Any source change after an inspection needs new stills inspected again; check that text does not overlap and that every requested element is visible where and when it should be.
- When an MP4 or still is requested, render it with `/usr/bin/chromium`, verify the output exists, and save the requested artifact. Save an editable source bundle separately when requested.
- Do not claim support for hosted rendering, a user-visible Studio URL, or paid services unless that capability is actually connected and verified.

Typical commands:

```bash
npx remotion compositions src/index.ts --browser-executable=/usr/bin/chromium
npx remotion still src/index.ts <composition-id> review.png --frame=<frame> --browser-executable=/usr/bin/chromium
npx remotion render src/index.ts <composition-id> output.mp4 --browser-executable=/usr/bin/chromium
```

Return concrete source and render paths, composition ids, validated frames, and any honest blocker. Remotion has separate runtime license terms; use official sources for licensing claims.
