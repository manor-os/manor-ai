# GSAP timelines and beat grids

## One master timeline

`index.html` registers exactly one paused timeline at
`window.__timelines["<data-composition-id>"]`, and the renderer seeks it. The
static gate counts the literal text `gsap.timeline` in `index.html` — comments
included — and requires exactly one. Build every scene into that master with
labels instead of creating child timelines:

```js
window.__timelines = window.__timelines || {};
const tl = gsap.timeline({ paused: true });
const FPS = 30;
const snap = (s) => Math.round(s * FPS) / FPS;
const SCENES = [            // mirrors the clip sections' data-start / data-duration
  { id: "s01-hook", start: 0, dur: 3.75 },
  { id: "s02-lines", start: 3.75, dur: 3.75 },
];
SCENES.forEach((s) => tl.addLabel(s.id, snap(s.start)));
tl.from("#s01-hook .word", { yPercent: 110, autoAlpha: 0, duration: 0.5, ease: "power3.out", stagger: 0.06 }, "s01-hook");
tl.to("#s02-lines path", { strokeDashoffset: 0, duration: 1.2, ease: "power2.inOut" }, "s02-lines+=0.25");
window.__timelines["main"] = tl;
```

Each scene is one sub-composition in the `video-edit` sense: a direct
`class="clip"` child of the root with its own `id`, `data-start`,
`data-duration` and `data-track-index`, plus one labelled block of the master
timeline. Keep the `SCENES` table and the HTML attributes in step; a scene whose
label and clip window disagree shows motion in an invisible clip.

Pad the timeline to the full composition length so seeking near the end is
stable: `tl.set({}, {}, snap(totalDuration));`.

## Beat grid

There is no beat detection. Take the BPM from the user, the music's metadata or
the generation prompt, and measure the first downbeat once (for example the end
of leading silence reported by `analyze_audio` on the music stem, then confirmed
by ear or against review frames). Record both in `STORYBOARD.md`.

```js
const BPM = 128, OFFSET = 0.12;         // seconds to the first downbeat
const beat = (n) => snap(OFFSET + (n * 60) / BPM);
const bar = (n) => beat(n * 4);
tl.addLabel("drop", bar(14));
tl.fromTo("#slam", { scale: 1.6, autoAlpha: 0 }, { scale: 1, autoAlpha: 1, duration: 0.18, ease: "power4.out" }, beat(56));
```

Land impacts on the beat frame; let anticipation start one or two frames early
if the motion needs it. Never space cuts at a uniform interval and call it beat
sync — derive each one from `beat(n)`.

## Split text and stagger

The SplitText plugin is not staged into the project. Split in markup, or with a
deterministic splitter that runs once at load (after fonts are ready, see
`references/determinism.md`) before the timeline is built:

```js
function splitWords(el) {
  const text = el.textContent.trim();
  el.setAttribute("aria-label", text);
  const isCjk = /[㐀-鿿]/.test(text);
  const parts = isCjk ? Array.from(text) : text.split(/\s+/);
  el.innerHTML = parts.map((p) => `<span class="word" aria-hidden="true">${p}</span>`).join(isCjk ? "" : " ");
}
```

Give `.word` `display: inline-block` so transforms apply; wrap each word in an
`overflow: hidden` mask span for a clean rise. Use `stagger: {each: 0.05, from: "center"}`
or `"start"`/`"end"`; never `"random"`.

## Transforms without collisions

- Do not tween the raw `transform` string, and do not tween an element whose
  CSS already carries a layout transform (`translate(-50%, -50%) rotate(-8deg)`).
  Put layout on a wrapper and animate the inner element, or express the base
  with `gsap.set(el, {xPercent: -50, yPercent: -50, rotation: -8})` and animate
  other properties.
- Several `from()` tweens on the same property: give the later ones
  `immediateRender: false`, otherwise seeking backwards shows the wrong start state.
- Animate `x`, `y`, `scale`, `rotation`, `autoAlpha`, `clipPath`,
  `strokeDashoffset` and CSS variables. Avoid layout properties (`width`, `top`)
  on large trees; they are slow and can reflow text.
- Core GSAP interpolates strings whose numbers line up, so `clipPath` polygons
  and SVG paths morph without plugins when both states have the same number of
  points and commands. A path's `d` is an attribute, not a CSS property, so tween
  it through `attr`: `tl.to("#wave", {attr: {d: waveB}, duration: 0.8}, "s03")`.

## Easing

`power2`/`power3` in-out for moves, `power4.out` or `expo.out` for impacts,
`back.out(1.4)` for a single overshoot, `sine.inOut` for drift, `"none"` for
linear camera and progress drives. CustomEase is a plugin and is not staged.
Every scene should settle for at least a few beats so the eye can land.
