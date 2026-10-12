# Transitions

Every transition is tweens on the master timeline, placed at a frame-snapped
cut time `c` (usually `beat(n)` from `references/gsap-timelines.md`). The
outgoing clip's window ends at `c` and the incoming clip's window starts at `c`,
or they overlap by exactly the transition length — no gaps, no stray frames.
Use one transition family per piece plus hard cuts; a different trick on every
cut reads as a template.

## Hard cut on the beat

The default. Put the cut on the beat frame and give the incoming shot its
impact on that same frame (scale 1.08 → 1, or a type slam). Hold for at least
a few beats before the next cut unless the section is deliberately rapid.

## Whip pan

```js
const W = 1920;
tl.to("#a .frame", { x: -1.2 * W, filter: "blur(20px)", duration: 0.28, ease: "power3.in" }, c - 0.28);
tl.fromTo("#b .frame", { x: 1.2 * W, filter: "blur(20px)" },
  { x: 0, filter: "blur(0px)", duration: 0.32, ease: "power3.out" }, c);
```

Blur peaks at 16–24 px exactly on the cut and the direction is consistent
across both sides. Pair it with one whoosh starting about 0.1 s before `c`.

## Match cut (shape or position)

The last frame of A and the first frame of B share an anchor: same centre,
same size, same colour family (a dot becomes a planet, a progress bar becomes a
horizon). Keep the anchor in one constant both scenes read:

```js
const ANCHOR = { x: 960, y: 540, size: 64 };
// Lay both elements out once, centred on the anchor; then animate transforms only.
gsap.set(["#a .dot", "#b .planet"], { position: "absolute", left: ANCHOR.x, top: ANCHOR.y, xPercent: -50, yPercent: -50 });
gsap.set("#a .dot", { width: 16, height: 16 });            // base size, never tweened
gsap.set("#b .planet", { width: 640, height: 640 });
tl.to("#a .dot", { scale: ANCHOR.size / 16, duration: 0.6, ease: "power2.inOut" }, c - 0.6);
tl.fromTo("#b .planet", { scale: ANCHOR.size / 640 },
  { scale: 1, duration: 1.0, ease: "power3.out" }, c);
```

Tween `scale`, not `width` / `height`: size tweens reflow layout and shift the
centre unless the anchor is re-solved every frame.

Check it with review snapshots at `c - 1/30` and `c`: the anchor must not jump.

## Iris

Open the incoming scene with a circular clip. From an off-centre origin, the
radius that covers the frame is the distance to the farthest corner:

```js
const ox = 1440, oy = 300;
const R = Math.max(Math.hypot(ox, oy), Math.hypot(1920 - ox, oy), Math.hypot(ox, 1080 - oy), Math.hypot(1920 - ox, 1080 - oy));
tl.fromTo("#b", { clipPath: `circle(0px at ${ox}px ${oy}px)` },
  { clipPath: `circle(${Math.ceil(R)}px at ${ox}px ${oy}px)`, duration: 0.6, ease: "power2.inOut" }, c);
```

## Liquid wipe

A moving wavy edge, made from polygons with the same point count so core GSAP
interpolates each number. Generate the keyframe polygons once at load from a
pure function of progress:

```js
function wavePolygon(p, points = 24, amp = 0.04) {
  const edge = p * 1.2 - 0.1;                    // sweeps left to right
  const pts = [];
  for (let i = 0; i <= points; i++) {
    const y = i / points;
    const x = edge + amp * Math.sin(y * Math.PI * 3 + p * Math.PI * 4);
    pts.push(`${(x * 100).toFixed(2)}% ${(y * 100).toFixed(2)}%`);
  }
  return `polygon(0% 0%, ${pts.join(", ")}, 0% 100%)`;
}
const keyframes = [0, 0.25, 0.5, 0.75, 1].map((p) => ({ clipPath: wavePolygon(p) }));
tl.set("#b", { clipPath: wavePolygon(0) }, c);
tl.to("#b", { keyframes, duration: 0.8, ease: "none" }, c);
```

## Flash

A white or accent flash peaks for at most 2 frames, ramps out over 4–6 frames,
and never repeats more than 3 times in any second (photosensitivity). A
selected playbook's flash limit is the acceptance check. Prefer an accent-colour
flash at 60–80% opacity to a full white frame. Never put the flash on frame 0:
platforms use the first frame as the cover.
