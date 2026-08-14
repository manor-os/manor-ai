import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";

const readWebSource = (path) => readFile(new URL(`../src/${path}`, import.meta.url), "utf8");

test("shared pane splitter supports pointer, keyboard, reset, and persisted sizing", async () => {
  const source = await readWebSource("components/ui/ResizablePaneGroup.tsx");

  assert.match(source, /role="separator"/);
  assert.match(source, /aria-orientation="vertical"/);
  assert.match(source, /setPointerCapture/);
  assert.match(source, /ArrowLeft/);
  assert.match(source, /ArrowRight/);
  assert.match(source, /onDoubleClick=\{resetSizes\}/);
  assert.match(source, /window\.localStorage\.setItem/);
});

test("project tree lazily lists folders and opens only code-like files", async () => {
  const source = await readWebSource("components/code/CodeProjectExplorer.tsx");

  assert.match(source, /api\.fs\.list\(path\)/);
  assert.match(source, /enabled: expanded/);
  assert.match(source, /role="tree"/);
  assert.match(source, /role="treeitem"/);
  assert.match(source, /isCodeLikeFile/);
  assert.doesNotMatch(source, />\s*EXPLORER\s*</);
});

test("code workspace keeps linked tabs editable, autosaved, and recoverable", async () => {
  const source = await readWebSource("lib/useCodeProjectWorkspace.ts");

  assert.match(source, /api\.fs\.read\(path\)/);
  assert.match(source, /api\.fs\.write\(path, contentToSave\)/);
  assert.match(source, /AUXILIARY_AUTOSAVE_DELAY = 3000/);
  assert.match(source, /saveAll/);
  assert.match(source, /readError: true/);
  assert.match(source, /tab\.loading \|\| tab\.readError/);
  assert.match(source, /status: "error"/);
  assert.match(source, /previewTextOverrides/);
});

test("document code mode composes files, tabs, editor, and preview as resizable panes", async () => {
  const source = await readWebSource("pages/DocEditor.tsx");

  assert.match(source, /useCodeProjectWorkspace/);
  assert.match(source, /<CodeProjectExplorer/);
  assert.match(source, /<ResizablePaneGroup/);
  assert.match(source, /id: "files"/);
  assert.match(source, /id: "source"/);
  assert.match(source, /id: "preview"/);
  assert.match(source, /role="tablist"/);
  assert.match(source, /codeWorkspace\.previewTextOverrides/);
  assert.match(source, /Preview ready/);
});

test("IDE workspace keeps the tree dark and stacks panes without horizontal overflow on narrow screens", async () => {
  const source = await readWebSource("components/code/CodeProjectWorkspace.css");

  assert.match(source, /\.doc-editor-project-pane,[\s\S]*\.code-project-explorer[\s\S]*background: #1c1917/);
  assert.match(source, /@media \(max-width: 900px\)/);
  assert.match(source, /grid-template-columns: minmax\(0, 1fr\)/);
  assert.match(source, /\.doc-editor-ide-workspace \.resizable-pane-group__handle \{[\s\S]*display: none/);
});

test("HTML preview can resolve unsaved linked text from the code workspace", async () => {
  const source = await readWebSource("lib/useHtmlPreviewDocument.ts");

  assert.match(source, /textOverrides: Record<string, string>/);
  assert.match(source, /hasOwnProperty\.call\(textOverrides, path\)/);
  assert.match(source, /readPreviewAsset\(path, textOverrides\)/);
  assert.match(source, /overrideKey/);
});
