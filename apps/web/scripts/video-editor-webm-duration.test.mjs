import assert from "node:assert/strict";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import ts from "typescript";

async function loadMediaDurationHelpers() {
  const sourceUrl = new URL("../src/lib/mediaDuration.ts", import.meta.url);
  const source = await readFile(sourceUrl, "utf8");
  const compiled = ts.transpileModule(source, {
    compilerOptions: {
      module: ts.ModuleKind.ES2022,
      target: ts.ScriptTarget.ES2022,
    },
  }).outputText;

  const tempDir = await mkdtemp(join(tmpdir(), "media-duration-"));
  const tempFile = join(tempDir, "mediaDuration.mjs");
  await writeFile(tempFile, compiled, "utf8");
  const mod = await import(`file://${tempFile}`);
  await rm(tempDir, { recursive: true, force: true });
  return mod;
}

const {
  getPlayableMediaDuration,
  requestMediaDurationProbe,
} = await loadMediaDurationHelpers();

test("uses the media duration when it is finite", () => {
  const media = {
    duration: 42.5,
    currentTime: 0,
    seekable: { length: 0, end: () => 0 },
  };

  assert.equal(getPlayableMediaDuration(media), 42.5);
});

test("uses the seekable endpoint for WebM without duration metadata", () => {
  const media = {
    duration: Infinity,
    currentTime: 0,
    seekable: { length: 1, end: () => 73.25 },
  };

  assert.equal(getPlayableMediaDuration(media), 73.25);
});

test("requests a duration probe for an unbounded WebM", () => {
  const media = {
    duration: Infinity,
    currentTime: 0,
    seekable: { length: 0, end: () => 0 },
  };

  assert.equal(requestMediaDurationProbe(media), true);
  assert.equal(media.currentTime, Number.MAX_SAFE_INTEGER);
});

test("Video Editor rechecks metadata when a WebM duration becomes available", async () => {
  const editorSource = await readFile(
    new URL("../src/pages/VideoEditor.tsx", import.meta.url),
    "utf8",
  );

  assert.match(editorSource, /onDurationChange=\{handleLoadedMetadata\}/);
  assert.match(editorSource, /durationProbeUrlRef\.current/);
  assert.match(editorSource, /getPlayableMediaDuration\(video\)/);
  assert.match(editorSource, /requestMediaDurationProbe\(video\)/);
});
