# 3D and 2.5D scenes

## Choose the route

Three.js is **not** installed in the Sandbox and cannot be loaded from a CDN.

1. **Vendored Three.js** — only when a local build already exists in Knowledge
   (supplied by the user or saved by an earlier project). Stage it with
   `video_edit(operation="push_assets", reference_paths=[...])` and load it from
   the `project_path` the call returns (under `assets/references/`). Never copy
   it into `assets/vendor/`: that folder is excluded from the synced project
   and from `review_sha256`.
2. **CSS 3D** — planes with `perspective` and `translateZ` on a
   `transform-style: preserve-3d` rig, animated by GSAP `rotationY` / `z`:
   tunnels, card stacks, extruded type. Sharpest for text (keep text planes
   within about 35 degrees of facing the camera).
3. **Canvas 2D / 2.5D** — painter's-order layers, depth-scaled parallax, fog by
   depth: landscapes, weather and particles without Three.js.

Record the chosen route and why in `STORYBOARD.md`; never call 2.5D real 3D.

## Drive the frame from the seeked time

Canvas and WebGL are not tweened by GSAP, and some engine seeks (verification
and audio sampling) suppress timeline callbacks. Draw from the runtime's
`hf-seek` window event, which fires on every seek with the composition time.
When a frame depends on something asynchronous (an image decoding, a font
loading), hold the capture with `event.detail.waitUntil(promise)`, called
synchronously inside the listener:

```js
function drawAt(t) { /* pure: camera, sky, water, particles all from t */ }
const ready = Promise.all([skyImage.decode(), document.fonts.ready]);  // started once, at load
window.addEventListener("hf-seek", (event) => {
  event.detail.waitUntil(ready.then(() => drawAt(event.detail.time)));
});
ready.then(() => drawAt(0));
```

Never fetch or decode on seek. Keep this in a local script such as `world.js`;
`index.html` still registers exactly one `gsap.timeline`.

## Vendored Three.js pattern

Load a classic (non-module) build with a plain script tag before the scene
script, so `window.THREE` exists when the scene code runs:

```html
<script src="./assets/vendor/gsap.min.js"></script>
<script src="./assets/references/three.min.js"></script>  <!-- exactly the returned project_path -->
<script src="./world.js"></script>
```

push_assets renames a file whose name collides with a different staged file
(it appends a short hash), so copy each returned `project_path` exactly. That
also breaks module builds whose imports name a sibling file (for example
`three.module.js` importing `three.core.js`) if the sibling was renamed. Prefer
a single-file classic build (published up to r160); with a module-only build use
`<script type="module">` and confirm every sibling kept its original name.

```js
const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, preserveDrawingBuffer: true });
renderer.setPixelRatio(1);
renderer.setSize(1920, 1080, false);
scene.fog = new THREE.FogExp2(0x0b1320, 0.018);
function drawAt(t) {
  const p = camPath(t);                 // Catmull-Rom through timed keys, pure in t
  camera.position.set(p.x, p.y, p.z);
  camera.lookAt(target);
  water.material.uniforms.uTime.value = t;
  renderer.render(scene, camera);
}
```

- Ocean: 3–5 Gerstner waves in the vertex shader, fixed directions and
  wavelengths, total steepness below 1; time enters only through `uTime`.
- Noise: hash-based or simplex noise seeded by constants, never `Math.random()`.
- Post: grain (hash of pixel and frame index), vignette and a 2.39:1 letterbox,
  drawn last in the same pass so tone and grain match.

## Canvas 2D / 2.5D pattern

```js
const ctx = canvas.getContext("2d");
function drawAt(t) {
  ctx.clearRect(0, 0, W, H);
  for (const layer of LAYERS) {           // far to near; built once from a seeded generator
    const shift = camX(t) * layer.depth;  // nearer layers move more
    ctx.globalAlpha = 1 - layer.fog;      // fog grows with distance
    ctx.drawImage(layer.bitmap, -shift, layer.y);
  }
  drawRain(t);                            // closed-form: y = (y0 + speed * t) mod H
}
```

Pre-render static layers into offscreen canvases at load; per frame, only
transform and composite.

## Render budget

The render must finish well inside the 30-minute Sandbox render timeout: 40 s
at 1080p30 is 1200 frames, so aim for 0.5–0.75 s per frame. Time a review of
5–6 snapshots including the heaviest moment and cut particles, fog passes or
offscreen resolution before approval. Keep a flash peak to 2 frames (a selected
playbook's flash limit is the acceptance check).
