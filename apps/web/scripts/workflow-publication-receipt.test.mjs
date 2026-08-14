import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const webRoot = path.resolve(here, "..");
const read = (relative) => fs.readFileSync(path.join(webRoot, relative), "utf8");

test("publication receipt is a first-class workflow node", () => {
  const palette = read("src/components/workflows/WorkflowNodePalette.tsx");
  const canvas = read("src/components/workflows/WorkflowCanvas.tsx");
  const config = read("src/components/workflows/WorkflowNodeConfigPanel.tsx");

  assert.match(palette, /"publication_receipt"/);
  assert.match(canvas, /publication_receipt:\s*"RECEIPT"/);
  assert.match(config, /key:\s*"receipt"/);
  assert.match(config, /key:\s*"require_verified"/);
});

test("run detail renders reusable receipt cards", () => {
  const detail = read("src/components/workflows/WorkflowRunDetail.tsx");
  const cards = read("src/components/workflows/PublicationReceiptCards.tsx");

  assert.match(detail, /<PublicationReceiptCards receipts=\{run\.publication_receipts\}/);
  assert.match(cards, /openDetail\(/);
  assert.match(cards, /receipt\.payload_hash/);
  assert.match(cards, /receipt\.evidence/);
  assert.match(cards, /open_publication/);
});

test("chat result card shows verified publication count", () => {
  const card = read("src/components/WorkflowResultCard.tsx");
  const stream = read("src/lib/chatStream.ts");

  assert.match(stream, /publication_summary\?:/);
  assert.match(card, /verified_publications/);
  assert.match(card, /publication_summary\?\.verified/);
});
