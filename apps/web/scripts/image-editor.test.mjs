import assert from "node:assert/strict";
import { build } from "esbuild";
import { pathToFileURL } from "node:url";
import { mkdtemp, rm } from "node:fs/promises";
import os from "node:os";
import path from "node:path";

const repoRoot = path.resolve(import.meta.dirname, "../../..");
const temporaryDirectory = await mkdtemp(path.join(os.tmpdir(), "manor-image-editor-"));
const bundlePath = path.join(temporaryDirectory, "geometry.mjs");

try {
  await build({
    entryPoints: [path.join(repoRoot, "apps/web/src/lib/imageEditorGeometry.ts")],
    outfile: bundlePath,
    bundle: true,
    platform: "node",
    format: "esm",
  });
  const geometry = await import(`${pathToFileURL(bundlePath).href}?${Date.now()}`);

  assert.deepEqual(geometry.imageOutputSize(4032, 3024, 0), { width: 4032, height: 3024 });
  assert.deepEqual(geometry.imageOutputSize(4032, 3024, 90), { width: 3024, height: 4032 });
  assert.deepEqual(geometry.imageOutputSize(4032, 3024, -90), { width: 3024, height: 4032 });
  assert.equal(geometry.normalizeImageQuarterTurn(450), 90);

  const point = geometry.imageCanvasPoint(
    250,
    175,
    { left: 50, top: 25, width: 400, height: 300 },
    { width: 4032, height: 3024 },
  );
  assert.deepEqual(point, { x: 2016, y: 1512 });
  console.log("image editor geometry test passed");
} finally {
  await rm(temporaryDirectory, { recursive: true, force: true });
}
