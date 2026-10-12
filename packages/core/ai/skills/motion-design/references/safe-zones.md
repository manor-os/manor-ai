# Safe zones

Platform apps draw their own UI over the video. Keep every word, logo, price,
CTA and caption inside the safe area; backgrounds and motion may run full bleed.
When a destination platform is named, the `marketing-platform` overlay supplied
to `video-production` takes precedence over these defaults.

## 9:16 — 1080 × 1920 (short-video feeds)

| Reserved band | Pixels | Covered by |
| --- | --- | --- |
| Top | y 0–220 | status bar, tabs, search, live badge |
| Bottom | y 1520–1920 (400 px) | caption, account name, music ticker, input bar |
| Right | x 940–1080 (140 px) | like / comment / share / avatar rail |
| Left margin | x 0–60 | edge crop on some devices |

Text-safe rectangle: **x 60–940, y 220–1520**. Put the hook in the upper third
of that rectangle (y 260–700) so it reads before the eye drops to the
platform caption. Never imitate platform UI (likes, comments, sales counters).

## 16:9 — 1920 × 1080

- Title-safe: 5% inset on every side → x 96–1824, y 54–1026.
- Action-safe (important motion and faces): 3.5% inset → x 67–1853, y 38–1042.
- Captions: a dedicated lower band; the `topic_explainer` playbook reserves
  y 880–1060 for captions and keeps key graphics at y ≤ 880.
- With a 2.39:1 letterbox the picture area is y 138–942; titles stay inside it.

## 4:5 — 1080 × 1350 (feed posts)

Standalone 4:5: title-safe 5% → x 54–1026, y 68–1282; also keep the bottom 10%
(y 1215–1350) free of essential text, because feed apps overlay caption and
CTA chips there. Text-safe rectangle: **x 54–1026, y 68–1215**.

Derived from a 9:16 master by centre crop: the crop window is
y (1920 − 1350) / 2 = 285 to 1635 of the master. Text that must survive both
deliveries lives in the intersection of both safe areas, in master coordinates:

```text
x: max(60, 54) .. min(940, 1026)            -> 60 .. 940
y: max(220, 285 + 68) .. min(1520, 285 + 1215) -> 353 .. 1500
```

Lay out the master inside that box when one edit feeds both formats. If the
layout cannot fit, author a separate 4:5 composition instead of cropping.

## Implementing and checking

Declare the safe rectangle as CSS variables on the root and position text
against them, so a format change is one edit:

```css
#main { --safe-l: 60px; --safe-r: 140px; --safe-t: 220px; --safe-b: 400px; }
.caption { position: absolute; left: var(--safe-l); right: var(--safe-r); bottom: var(--safe-b); }
```

- Add `staysInFrame` assertions in `index.motion.json` for key text selectors;
  they catch overflow off the frame, not intrusion into a reserved band.
- Pick `video_edit(operation="review", snapshot_times=[...])` times where the
  most text is on screen (hook, densest caption, CTA, end card) and check each
  text box against the numbers above.
- Long translations grow: test the longest language variant, and reduce size
  or rewrap rather than letting text cross a safe edge.
