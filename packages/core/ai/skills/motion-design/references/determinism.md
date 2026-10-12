# Determinism: every frame is a pure function of t

The renderer seeks the composition to a time `t` and captures one frame. Frames
are captured out of order, by several workers, and again at render time after
review. So the picture at `t` must depend on `t` and constant data only — never
on what was drawn before, how long the page has been open, or call order.

## Forbidden in timed code

- `Date.now()`, `new Date()`, `performance.now()`, `requestAnimationFrame`,
  `setTimeout` / `setInterval` as animation clocks.
- `Math.random()`, `gsap.utils.random()`, and `stagger: {from: "random"}`:
  each worker builds its own timeline, so unseeded randomness differs per worker.
- `repeat: -1`, `yoyo` with infinite repeat, CSS `animation` / `transition` for
  timed motion. Use a finite GSAP repeat or a pure periodic function instead.
- State that accumulates across frames: velocities integrated per frame,
  particle arrays advanced by `dt`, trails that read the previous canvas.
- Visual state computed in `onUpdate`, `onStart` or `tl.call()`: some render
  seeks suppress timeline callbacks, so the frame comes out stale.

## Seeded randomness

Use a small seeded generator and seed it from stable ids, never from time:

```js
function mulberry32(seed) {
  return function () {
    seed |= 0; seed = (seed + 0x6D2B79F5) | 0;
    let r = Math.imul(seed ^ (seed >>> 15), 1 | seed);
    r = (r + Math.imul(r ^ (r >>> 7), 61 | r)) ^ r;
    return ((r ^ (r >>> 14)) >>> 0) / 4294967296;
  };
}
function hashString(text) {
  let h = 2166136261;
  for (const ch of text) h = Math.imul(h ^ ch.codePointAt(0), 16777619);
  return h >>> 0;
}
// Build all random layout once, at load, in a fixed order.
const rand = mulberry32(hashString("s04-particles"));
const particles = Array.from({ length: 240 }, () => ({ x: rand(), y: rand(), phase: rand() * Math.PI * 2 }));
```

Per-frame jitter (grain, flicker, handheld shake) must be a function of the
frame index, not of a running generator:
`const frameRand = mulberry32(hashString("grain") ^ Math.round(t * fps));`.
Express position as a formula of `t`: `x = x0 + v * t`, `y = A * Math.sin(2 * Math.PI * f * t + phase)`.
For a particle field, compute each particle from its seed and `t`; wrap with a
modulo instead of respawning (`y = ((y0 + speed * t) % 1 + 1) % 1`).

## Seamless loops

For a loop of length `L` seconds:

- Every periodic term needs a whole number of cycles per loop: use
  `f = k / L` with integer `k`.
- A spring or wobble built from impulses (a kick on each beat, then a damped
  response) returns to rest only if the impulses on each channel sum to zero.
  Keep separate lists for x, y, rotation and scale and check
  `kicks.reduce((a, k) => a + k.delta, 0) === 0` for each one; otherwise the
  element drifts and the loop point pops.
- A visibility window that crosses the loop boundary must wrap:

```js
function visibleInLoop(t, start, duration, loop) {
  const local = (((t - start) % loop) + loop) % loop;
  return local < duration;
}
```

## Fonts and media

- When text uses a staged `@font-face` file, build anything that splits or
  measures text only after the font is ready, or the layout is computed with a
  fallback font and the words shift when the real face loads:
  `document.fonts.ready.then(() => { splitWords(title); buildTimeline(); });`
  Register the master timeline synchronously; add the font-dependent tweens to
  it inside that callback, before the first review.
- Never set `currentTime`, call `play()` or `pause()` on `<video>` / `<audio>`
  elements. The framework owns media playback and syncs each clip to the
  composition time from its `data-start` / `data-duration`; manual seeking
  desynchronises picture and sound.

## Frame grid

Snap every authored time to the frame grid: `const snap = (s) => Math.round(s * fps) / fps;`.
Put cuts, `data-start` and `data-duration` values on frame boundaries so a clip
does not appear for one stray frame.

## Proof

Include a pair of adjacent frame times in one
`video_edit(operation="review", snapshot_times=[...])` call, for example
`[3.0, 3.0333]`, to confirm motion is smooth and present. After render, sample
the same times from the MP4 with `render_frame_samples`: review and render
frames must match visually. Allow for video encoding (slight softness, colour
banding, compression noise); a different position, word, colour or missing
element is a real mismatch and means a hidden clock, unseeded randomness or
accumulated state.
