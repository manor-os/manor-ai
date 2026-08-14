import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const css = readFileSync(new URL("../src/index.css", import.meta.url), "utf8");
const docEditor = readFileSync(new URL("../src/pages/DocEditor.tsx", import.meta.url), "utf8");
const fileViewer = readFileSync(new URL("../src/pages/FileViewer.tsx", import.meta.url), "utf8");
const markdownTable = readFileSync(new URL("../src/components/MarkdownTable.tsx", import.meta.url), "utf8");

test("markdown tables use one accessible horizontal scroll region", () => {
  assert.match(markdownTable, /className="md-table-scroll"/);
  assert.match(markdownTable, /role="region"/);
  assert.match(markdownTable, /tabIndex=\{0\}/);
  assert.match(docEditor, /<MarkdownTable>\{children\}<\/MarkdownTable>/);
  assert.match(fileViewer, /<MarkdownTable>\{children\}<\/MarkdownTable>/);
  assert.match(css, /\.md-table-scroll\s*\{[^}]*overflow-x:\s*auto/s);
  assert.match(css, /\.md-table\s*\{[^}]*width:\s*max-content[^}]*min-width:\s*100%/s);
});

test("split mode stays side by side until the phone breakpoint", () => {
  assert.match(
    css,
    /@media \(max-width:\s*900px\)[\s\S]*?\.markdown-editor-layout--split\s*\{[^}]*grid-template-columns:\s*minmax\(0, 1fr\) minmax\(0, 1fr\)/,
  );
  assert.match(
    css,
    /@media \(max-width:\s*640px\)[\s\S]*?\.markdown-editor-layout--split\s*\{[^}]*grid-template-columns:\s*minmax\(0, 1fr\)/,
  );
});
