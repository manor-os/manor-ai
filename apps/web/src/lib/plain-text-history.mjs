export const PLAIN_TEXT_HISTORY_MAX_ENTRIES = 100;
export const PLAIN_TEXT_HISTORY_MAX_BYTES = 4 * 1024 * 1024;

const ENTRY_OVERHEAD_BYTES = 96;
const ENTRY_CONTEXT_CODE_UNITS = 32;

export function normalizePlainTextSelection(selection, textLength) {
  const length = Math.max(0, Number.isFinite(textLength) ? Math.trunc(textLength) : 0);
  const rawStart = Number.isFinite(selection?.start) ? Math.trunc(selection.start) : length;
  const rawEnd = Number.isFinite(selection?.end) ? Math.trunc(selection.end) : rawStart;
  const start = Math.max(0, Math.min(rawStart, rawEnd, length));
  const end = Math.max(start, Math.min(Math.max(rawStart, rawEnd), length));
  const direction = selection?.direction;
  return direction === "forward" || direction === "backward" || direction === "none"
    ? { start, end, direction }
    : { start, end };
}

export function createPlainTextHistory(options = {}) {
  return {
    undo: [],
    redo: [],
    undoBytes: 0,
    redoBytes: 0,
    maxEntries: Math.max(1, Math.trunc(options.maxEntries || PLAIN_TEXT_HISTORY_MAX_ENTRIES)),
    maxBytes: Math.max(1, Math.trunc(options.maxBytes || PLAIN_TEXT_HISTORY_MAX_BYTES)),
  };
}

export function resetPlainTextHistory(history) {
  history.undo.length = 0;
  history.redo.length = 0;
  history.undoBytes = 0;
  history.redoBytes = 0;
}

export function canUndoPlainTextHistory(history) {
  return history.undo.length > 0;
}

export function canRedoPlainTextHistory(history) {
  return history.redo.length > 0;
}

function commonChangeRange(previous, next) {
  const sharedLength = Math.min(previous.length, next.length);
  let start = 0;
  while (start < sharedLength && previous.charCodeAt(start) === next.charCodeAt(start)) start += 1;

  let previousEnd = previous.length;
  let nextEnd = next.length;
  while (
    previousEnd > start
    && nextEnd > start
    && previous.charCodeAt(previousEnd - 1) === next.charCodeAt(nextEnd - 1)
  ) {
    previousEnd -= 1;
    nextEnd -= 1;
  }

  return {
    start,
    removedLength: previousEnd - start,
    insertedLength: nextEnd - start,
  };
}

function inputChangeRange(previous, next, beforeSelection, afterSelection) {
  if (afterSelection.start !== afterSelection.end) return null;
  const start = Math.min(beforeSelection.start, afterSelection.start);
  const insertedLength = afterSelection.start - start;
  const removedLength = previous.length + insertedLength - next.length;
  if (
    insertedLength < 0
    || removedLength < 0
    || start + removedLength > previous.length
    || start + insertedLength > next.length
  ) {
    return null;
  }
  return { start, removedLength, insertedLength };
}

function changeContext(previous, next, range) {
  const leftLength = Math.min(range.start, ENTRY_CONTEXT_CODE_UNITS);
  const previousRightStart = range.start + range.removedLength;
  const nextRightStart = range.start + range.insertedLength;
  const rightLength = Math.min(
    previous.length - previousRightStart,
    next.length - nextRightStart,
    ENTRY_CONTEXT_CODE_UNITS,
  );
  const previousLeft = previous.slice(range.start - leftLength, range.start);
  const nextLeft = next.slice(range.start - leftLength, range.start);
  const previousRight = previous.slice(previousRightStart, previousRightStart + rightLength);
  const nextRight = next.slice(nextRightStart, nextRightStart + rightLength);
  if (previousLeft !== nextLeft || previousRight !== nextRight) return null;
  return { leftLength, rightLength, previousRightStart };
}

function entryBytes(removedLength, insertedLength, leftLength, rightLength) {
  return (removedLength + insertedLength + leftLength + rightLength) * 2 + ENTRY_OVERHEAD_BYTES;
}

function copyStringFragment(value, start, length) {
  if (length <= 0) return "";
  const parts = [];
  const chunkSize = 8192;
  for (let offset = 0; offset < length; offset += chunkSize) {
    const size = Math.min(chunkSize, length - offset);
    const codeUnits = new Uint16Array(size);
    for (let index = 0; index < size; index += 1) {
      codeUnits[index] = value.charCodeAt(start + offset + index);
    }
    parts.push(String.fromCharCode(...codeUnits));
  }
  return parts.join("");
}

function trimUndoHistory(history) {
  while (history.undo.length > history.maxEntries || history.undoBytes > history.maxBytes) {
    const removed = history.undo.shift();
    if (!removed) break;
    history.undoBytes -= removed.bytes;
  }
}

export function recordPlainTextHistory(
  history,
  previous,
  next,
  beforeSelection,
  afterSelection,
  options = {},
) {
  if (previous === next) return false;

  const beforeGeneration = options.beforeGeneration;
  const afterGeneration = options.afterGeneration;
  if (
    !Number.isSafeInteger(beforeGeneration)
    || !Number.isSafeInteger(afterGeneration)
    || beforeGeneration === afterGeneration
  ) {
    resetPlainTextHistory(history);
    return false;
  }

  const normalizedBefore = normalizePlainTextSelection(beforeSelection, previous.length);
  const normalizedAfter = normalizePlainTextSelection(afterSelection, next.length);
  let range = options.input
    ? inputChangeRange(previous, next, normalizedBefore, normalizedAfter)
    : null;
  let context = range ? changeContext(previous, next, range) : null;
  if (!range || !context) {
    range = commonChangeRange(previous, next);
    context = changeContext(previous, next, range);
  }
  if (!context) return false;
  const bytes = entryBytes(
    range.removedLength,
    range.insertedLength,
    context.leftLength,
    context.rightLength,
  );

  history.redo.length = 0;
  history.redoBytes = 0;
  if (bytes > history.maxBytes) {
    history.undo.length = 0;
    history.undoBytes = 0;
    return false;
  }

  const entry = {
    start: range.start,
    // Force standalone strings so a tiny delta cannot retain a huge document's
    // backing store through an engine-level sliced-string representation.
    removed: copyStringFragment(previous, range.start, range.removedLength),
    inserted: copyStringFragment(next, range.start, range.insertedLength),
    leftContext: copyStringFragment(previous, range.start - context.leftLength, context.leftLength),
    rightContext: copyStringFragment(previous, context.previousRightStart, context.rightLength),
    beforeSelection: normalizedBefore,
    afterSelection: normalizedAfter,
    beforeLength: previous.length,
    afterLength: next.length,
    beforeGeneration,
    afterGeneration,
    bytes,
  };
  history.undo.push(entry);
  history.undoBytes += bytes;
  trimUndoHistory(history);
  return true;
}

function applyHistoryEntry(current, currentGeneration, entry, direction) {
  const isUndo = direction === "undo";
  const expectedLength = isUndo ? entry.afterLength : entry.beforeLength;
  const expectedGeneration = isUndo ? entry.afterGeneration : entry.beforeGeneration;
  const expectedFragment = isUndo ? entry.inserted : entry.removed;
  const replacement = isUndo ? entry.removed : entry.inserted;
  const leftContextStart = entry.start - entry.leftContext.length;
  const rightContextStart = entry.start + expectedFragment.length;
  if (
    currentGeneration !== expectedGeneration
    || current.length !== expectedLength
    || current.slice(entry.start, entry.start + expectedFragment.length) !== expectedFragment
    || current.slice(leftContextStart, entry.start) !== entry.leftContext
    || current.slice(rightContextStart, rightContextStart + entry.rightContext.length) !== entry.rightContext
  ) {
    return null;
  }
  return {
    text: current.slice(0, entry.start) + replacement + current.slice(entry.start + expectedFragment.length),
    selection: isUndo ? entry.beforeSelection : entry.afterSelection,
    generation: isUndo ? entry.beforeGeneration : entry.afterGeneration,
  };
}

function runHistoryCommand(history, current, currentGeneration, direction) {
  const source = direction === "undo" ? history.undo : history.redo;
  const destination = direction === "undo" ? history.redo : history.undo;
  const sourceBytes = direction === "undo" ? "undoBytes" : "redoBytes";
  const destinationBytes = direction === "undo" ? "redoBytes" : "undoBytes";
  const entry = source.pop();
  if (!entry) return null;
  history[sourceBytes] -= entry.bytes;

  const result = applyHistoryEntry(current, currentGeneration, entry, direction);
  if (!result) {
    resetPlainTextHistory(history);
    return null;
  }

  destination.push(entry);
  history[destinationBytes] += entry.bytes;
  return result;
}

export function undoPlainTextHistory(history, current, currentGeneration) {
  return runHistoryCommand(history, current, currentGeneration, "undo");
}

export function redoPlainTextHistory(history, current, currentGeneration) {
  return runHistoryCommand(history, current, currentGeneration, "redo");
}
