#!/usr/bin/env node
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const root = new URL("../", import.meta.url);
const read = (path) => readFile(new URL(path, root), "utf8");

test("shared media insertion reuses Manor Chat and Knowledge APIs", async () => {
  const source = await read("src/lib/mediaInsertion.ts");
  assert.match(source, /api\.chat\.stream\(/);
  assert.match(source, /chatMode:\s*kind/);
  assert.match(source, /chatMode:\s*"research"/);
  assert.match(source, /api\.documents\.list\(/);
  assert.match(source, /api\.documents\.upload\(/);
  assert.match(source, /api\.documents\.createFromUrl\(/);
});

test("shared media dialog exposes upload, Knowledge, online, and generation sources", async () => {
  const source = await read("src/components/MediaInsertDialog.tsx");
  for (const key of ["knowledge", "upload", "online", "generate"]) {
    assert.match(source, new RegExp(`key: "${key}"`));
  }
  assert.match(source, /allowedKinds = \["image", "video"\]/);
});

test("presentation, document, PDF, website, and video editors share the media dialog", async () => {
  const [documentEditor, fileViewer, videoEditor] = await Promise.all([
    read("src/pages/DocEditor.tsx"),
    read("src/pages/FileViewer.tsx"),
    read("src/pages/VideoEditor.tsx"),
  ]);
  assert.ok((documentEditor.match(/<MediaInsertDialog/g) || []).length >= 2);
  assert.match(documentEditor, /mode === "code"/);
  assert.match(fileViewer, /<MediaInsertDialog/);
  assert.match(videoEditor, /<MediaInsertDialog/);
});

test("large Knowledge libraries scroll without collapsing media cards", async () => {
  const css = await read("src/index.css");
  assert.match(css, /\.media-insert-grid\s*\{[\s\S]*grid-auto-rows:\s*max-content;/);
  assert.match(css, /\.media-insert-grid\s*\{[\s\S]*overflow:\s*auto;/);
});
