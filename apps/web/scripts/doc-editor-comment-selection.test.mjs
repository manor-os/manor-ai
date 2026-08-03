import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";

const editorSource = await readFile(
  new URL("../src/pages/DocEditor.tsx", import.meta.url),
  "utf8",
);

test("markdown preview comments use the rendered selection instead of a stale source selection", () => {
  assert.match(
    editorSource,
    /ref=\{markdownPreviewRef\}[\s\S]*?onMouseUp=\{refreshMarkdownPreviewCommentAnchor\}[\s\S]*?onKeyUp=\{refreshMarkdownPreviewCommentAnchor\}/,
  );
  assert.match(
    editorSource,
    /commentSelectionSurfaceRef\.current === "markdown-preview"[\s\S]*?surfaceSelectionAnchor\(markdownPreviewRef\.current, mode, docName\)[\s\S]*?if \(previewAnchor\) return previewAnchor/,
  );
});

test("the last editor surface wins over a stale browser preview selection", () => {
  assert.match(
    editorSource,
    /const refreshEditorCommentAnchor[\s\S]*?commentSelectionSurfaceRef\.current = "editor"[\s\S]*?refreshCommentAnchor\(\)/,
  );
  assert.match(
    editorSource,
    /const refreshMarkdownPreviewCommentAnchor[\s\S]*?commentSelectionSurfaceRef\.current = "markdown-preview"[\s\S]*?refreshCommentAnchor\(\)/,
  );
  assert.match(
    editorSource,
    /ref=\{markdownRef\}[\s\S]*?onSelect=\{refreshEditorCommentAnchor\}/,
  );
});

test("opening comments preserves and captures the exact active selection before focus changes", () => {
  assert.match(
    editorSource,
    /const preserveCommentSelection[\s\S]*?event\.preventDefault\(\)[\s\S]*?refreshCommentAnchor\(\)/,
  );
  assert.match(
    editorSource,
    /onMouseDown=\{preserveCommentSelection\}[\s\S]*?onClick=\{\(\) => \{[\s\S]*?refreshCommentAnchor\(\)/,
  );
});

test("rendered comment anchors store the selected range text itself", () => {
  assert.match(
    editorSource,
    /const quote = trimCommentQuote\(range\.toString\(\)\)[\s\S]*?type: "rendered_text_selection"[\s\S]*?quote/,
  );
});
