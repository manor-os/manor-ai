import assert from "node:assert/strict";
import test from "node:test";

import {
  canRedoPlainTextHistory,
  canUndoPlainTextHistory,
  createPlainTextHistory,
  recordPlainTextHistory,
  redoPlainTextHistory,
  undoPlainTextHistory,
} from "../src/lib/plain-text-history.mjs";

test("undo and redo restore the selection used by the next edit", () => {
  const history = createPlainTextHistory();
  const before = "plain text";
  const after = `${before}X`;
  assert.equal(recordPlainTextHistory(
    history,
    before,
    after,
    { start: before.length, end: before.length },
    { start: after.length, end: after.length },
    { input: true, beforeGeneration: 1, afterGeneration: 2 },
  ), true);

  const undone = undoPlainTextHistory(history, after, 2);
  assert.deepEqual(undone, {
    text: before,
    selection: { start: before.length, end: before.length },
    generation: 1,
  });
  const redone = redoPlainTextHistory(history, undone.text, undone.generation);
  assert.deepEqual(redone, {
    text: after,
    selection: { start: after.length, end: after.length },
    generation: 2,
  });

  const typedAfterRedo = `${redone.text.slice(0, redone.selection.start)}Y${redone.text.slice(redone.selection.end)}`;
  assert.equal(typedAfterRedo, "plain textXY");
});

test("programmatic AI replacements share the same undo chain", () => {
  const history = createPlainTextHistory();
  const original = "Draft";
  const typed = "Draft!";
  const aiRevision = "Polished draft.";
  recordPlainTextHistory(history, original, typed, { start: 5, end: 5 }, { start: 6, end: 6 }, {
    input: true,
    beforeGeneration: 1,
    afterGeneration: 2,
  });
  recordPlainTextHistory(history, typed, aiRevision, { start: 6, end: 6 }, { start: 6, end: 6 }, {
    beforeGeneration: 2,
    afterGeneration: 3,
  });

  const undoAi = undoPlainTextHistory(history, aiRevision, 3);
  assert.equal(undoAi.text, typed);
  const undoTyping = undoPlainTextHistory(history, undoAi.text, undoAi.generation);
  assert.equal(undoTyping.text, original);
  assert.equal(redoPlainTextHistory(history, undoTyping.text, undoTyping.generation).text, typed);
});

test("history keeps deltas within the configured memory and entry budgets", () => {
  const history = createPlainTextHistory({ maxEntries: 3, maxBytes: 512 });
  let value = "";
  let generation = 1;
  for (const character of "abcdef") {
    const next = value + character;
    const nextGeneration = generation + 1;
    recordPlainTextHistory(
      history,
      value,
      next,
      { start: value.length, end: value.length },
      { start: next.length, end: next.length },
      { input: true, beforeGeneration: generation, afterGeneration: nextGeneration },
    );
    value = next;
    generation = nextGeneration;
    assert.ok(history.undoBytes <= history.maxBytes);
    assert.ok(history.undo.length <= history.maxEntries);
  }
  assert.equal(history.undo.length, 3);

  assert.equal(recordPlainTextHistory(
    history,
    value,
    "x".repeat(500),
    { start: value.length, end: value.length },
    { start: 500, end: 500 },
    { beforeGeneration: generation, afterGeneration: generation + 1 },
  ), false);
  assert.equal(canUndoPlainTextHistory(history), false);
  assert.equal(canRedoPlainTextHistory(history), false);
});

test("a generation mismatch rejects same-length stale content outside the context window", () => {
  const history = createPlainTextHistory();
  const before = `${"a".repeat(80)}bc${"d".repeat(80)}`;
  const after = `${"a".repeat(80)}bXc${"d".repeat(80)}`;
  const stale = `z${after.slice(1)}`;
  recordPlainTextHistory(history, before, after, { start: 81, end: 81 }, { start: 82, end: 82 }, {
    input: true,
    beforeGeneration: 1,
    afterGeneration: 2,
  });
  assert.equal(undoPlainTextHistory(history, stale, 3), null);
  assert.equal(canUndoPlainTextHistory(history), false);
  assert.equal(canRedoPlainTextHistory(history), false);
});

test("an invalid selection-derived range falls back to the exact common change", () => {
  const history = createPlainTextHistory();
  recordPlainTextHistory(
    history,
    "abcdef",
    "Zbcdef",
    { start: 3, end: 3 },
    { start: 4, end: 4 },
    { input: true, beforeGeneration: 1, afterGeneration: 2 },
  );
  assert.equal(undoPlainTextHistory(history, "Zbcdef", 2).text, "abcdef");
});

test("undo and redo preserve a backward selection", () => {
  const history = createPlainTextHistory();
  recordPlainTextHistory(
    history,
    "selection",
    "sXon",
    { start: 1, end: 7, direction: "backward" },
    { start: 2, end: 2, direction: "none" },
    { input: true, beforeGeneration: 1, afterGeneration: 2 },
  );
  const undone = undoPlainTextHistory(history, "sXon", 2);
  assert.deepEqual(undone.selection, {
    start: 1,
    end: 7,
    direction: "backward",
  });
  assert.deepEqual(redoPlainTextHistory(history, "selection", undone.generation).selection, {
    start: 2,
    end: 2,
    direction: "none",
  });
});

test("stored deltas preserve exact UTF-16 code units", () => {
  const history = createPlainTextHistory();
  const original = `a${String.fromCharCode(0xD800)}b`;
  const edited = `${original}!`;
  recordPlainTextHistory(history, original, edited, { start: 3, end: 3 }, { start: 4, end: 4 }, {
    input: true,
    beforeGeneration: 1,
    afterGeneration: 2,
  });
  assert.equal(undoPlainTextHistory(history, edited, 2).text, original);
});
