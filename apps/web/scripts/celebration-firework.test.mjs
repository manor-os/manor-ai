import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";

const component = await readFile(
  new URL("../src/components/ui/CelebrationFirework.tsx", import.meta.url),
  "utf8",
);
const styles = await readFile(new URL("../src/index.css", import.meta.url), "utf8");
const workspaceDetail = await readFile(
  new URL("../src/pages/WorkspaceDetail.tsx", import.meta.url),
  "utf8",
);

test("workspace creation uses the shared 3D celebration firework", () => {
  assert.ok(workspaceDetail.includes("<CelebrationFirework />"));
  assert.ok(component.includes("const PARTICLE_COUNT = 20"));
  assert.ok(component.includes('aria-hidden="true"'));
  assert.doesNotMatch(component, /Math\.random/);
  assert.doesNotMatch(workspaceDetail, /WorkspaceWelcomeBurst|workspace-welcome-burst/);
});

test("celebration firework animates in depth and respects reduced motion", () => {
  assert.match(styles, /\.celebration-firework\s*\{[\s\S]*?perspective: 120px/);
  assert.match(styles, /\.celebration-firework__scene\s*\{[\s\S]*?transform-style: preserve-3d/);
  assert.match(styles, /@keyframes celebration-firework-particle[\s\S]*?translate3d/);
  assert.match(
    styles,
    /@media \(prefers-reduced-motion: reduce\)[\s\S]*?\.celebration-firework__particle/,
  );
});
