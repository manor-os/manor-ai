import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const read = (relativePath) =>
  readFileSync(new URL(`../${relativePath}`, import.meta.url), "utf8");

test("DOCX preview HTML is sanitized at every injection source", () => {
  const sanitizer = read("src/lib/sanitizeDocumentHtml.ts");
  const fileViewer = read("src/pages/FileViewer.tsx");
  const embeddedChat = read("src/components/EmbeddedChat.tsx");

  assert.match(sanitizer, /DOMPurify\.sanitize/);
  assert.match(sanitizer, /"script"/);
  assert.match(sanitizer, /"iframe"/);
  assert.match(sanitizer, /FORBID_ATTR:\s*\["style", "srcset"\]/);
  assert.match(fileViewer, /setHtml\(sanitizeDocumentHtml\(result\.value\)\)/);
  assert.match(fileViewer, /setHtml\(sanitizeDocumentHtml\(new TextDecoder\(\)\.decode\(buf\)\)\)/);
  assert.match(embeddedChat, /setDocxHtml\(sanitizeDocumentHtml\(result\.value\)\)/);
});
