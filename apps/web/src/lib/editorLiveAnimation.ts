import {
  parseDelimitedText,
  serializeDelimitedText,
  type DelimitedTextFormat,
} from "./delimitedText";

export type EditorLiveTextFrame = {
  content: string;
  selectionStart: number;
  selectionEnd: number;
  phase: "select" | "delete" | "type" | "settle";
  delayMs: number;
};

export type EditorLiveDelimitedFrame = Pick<
  EditorLiveTextFrame,
  "phase" | "delayMs"
> & {
  rows: unknown[][];
};

type EditorLiveTextTransition = {
  prefix: string;
  removedUnits: string[];
  insertedUnits: string[];
  suffix: string;
};

function buildTransition(before: string, after: string): EditorLiveTextTransition {
  const beforeUnits = Array.from(before);
  const afterUnits = Array.from(after);
  let prefixLength = 0;
  while (
    prefixLength < beforeUnits.length
    && prefixLength < afterUnits.length
    && beforeUnits[prefixLength] === afterUnits[prefixLength]
  ) {
    prefixLength += 1;
  }

  let suffixLength = 0;
  while (
    suffixLength < beforeUnits.length - prefixLength
    && suffixLength < afterUnits.length - prefixLength
    && beforeUnits[beforeUnits.length - suffixLength - 1]
      === afterUnits[afterUnits.length - suffixLength - 1]
  ) {
    suffixLength += 1;
  }

  return {
    prefix: beforeUnits.slice(0, prefixLength).join(""),
    removedUnits: beforeUnits.slice(prefixLength, beforeUnits.length - suffixLength),
    insertedUnits: afterUnits.slice(prefixLength, afterUnits.length - suffixLength),
    suffix: suffixLength > 0 ? beforeUnits.slice(beforeUnits.length - suffixLength).join("") : "",
  };
}

function frameStep(length: number, maxFrames: number) {
  return Math.max(1, Math.ceil(length / Math.max(1, maxFrames)));
}

/**
 * Produces a short, bounded sequence that looks like a person selecting,
 * deleting, and typing in the existing editor. The final frame always equals
 * `after`, while long edits are sampled so previews stay fast.
 */
export function buildEditorLiveTextFrames(
  before: string,
  after: string,
  maxFramesPerPhase = 36,
): EditorLiveTextFrame[] {
  if (before === after) {
    return [{
      content: after,
      selectionStart: after.length,
      selectionEnd: after.length,
      phase: "settle",
      delayMs: 0,
    }];
  }

  const transition = buildTransition(before, after);
  const start = transition.prefix.length;
  const removed = transition.removedUnits.join("");
  const frames: EditorLiveTextFrame[] = [{
    content: before,
    selectionStart: start,
    selectionEnd: start + removed.length,
    phase: "select",
    delayMs: removed ? 260 : 90,
  }];

  const deleteStep = frameStep(transition.removedUnits.length, maxFramesPerPhase);
  for (
    let remaining = Math.max(0, transition.removedUnits.length - deleteStep);
    transition.removedUnits.length > 0;
    remaining = Math.max(0, remaining - deleteStep)
  ) {
    const retained = transition.removedUnits.slice(0, remaining).join("");
    frames.push({
      content: `${transition.prefix}${retained}${transition.suffix}`,
      selectionStart: start + retained.length,
      selectionEnd: start + retained.length,
      phase: "delete",
      delayMs: 28,
    });
    if (remaining === 0) break;
  }

  const typeStep = frameStep(transition.insertedUnits.length, maxFramesPerPhase);
  for (
    let written = Math.min(typeStep, transition.insertedUnits.length);
    transition.insertedUnits.length > 0;
    written = Math.min(transition.insertedUnits.length, written + typeStep)
  ) {
    const inserted = transition.insertedUnits.slice(0, written).join("");
    frames.push({
      content: `${transition.prefix}${inserted}${transition.suffix}`,
      selectionStart: start + inserted.length,
      selectionEnd: start + inserted.length,
      phase: "type",
      delayMs: 24,
    });
    if (written === transition.insertedUnits.length) break;
  }

  const last = frames[frames.length - 1];
  if (last.content !== after || last.phase !== "settle") {
    const caret = transition.prefix.length + transition.insertedUnits.join("").length;
    frames.push({
      content: after,
      selectionStart: caret,
      selectionEnd: caret,
      phase: "settle",
      delayMs: 0,
    });
  }
  return frames;
}

function sameDelimitedRow(left: unknown[], right: unknown[]) {
  const width = Math.max(left.length, right.length);
  for (let column = 0; column < width; column += 1) {
    if (!Object.is(left[column] ?? "", right[column] ?? "")) return false;
  }
  return true;
}

function delimitedCellCount(rows: unknown[][]) {
  return rows.reduce((total, row) => total + row.length, 0);
}

function delimitedFrameBudget(rows: unknown[][], requested: number) {
  const cells = delimitedCellCount(rows);
  if (cells > 5_000) return Math.min(requested, 6);
  if (cells > 1_000) return Math.min(requested, 12);
  return requested;
}

/**
 * Streams only the changed row region. Unchanged rows are structurally shared,
 * so a small edit in a large sheet does not materialize the entire grid for
 * every character frame.
 */
export function* createEditorLiveDelimitedFrameStream(
  beforeRows: unknown[][],
  afterRows: unknown[][],
  format: DelimitedTextFormat,
  maxFramesPerPhase = 36,
): Generator<EditorLiveDelimitedFrame> {
  let prefixRows = 0;
  while (
    prefixRows < beforeRows.length
    && prefixRows < afterRows.length
    && sameDelimitedRow(beforeRows[prefixRows], afterRows[prefixRows])
  ) {
    prefixRows += 1;
  }

  let suffixRows = 0;
  while (
    suffixRows < beforeRows.length - prefixRows
    && suffixRows < afterRows.length - prefixRows
    && sameDelimitedRow(
      beforeRows[beforeRows.length - suffixRows - 1],
      afterRows[afterRows.length - suffixRows - 1],
    )
  ) {
    suffixRows += 1;
  }

  if (prefixRows === beforeRows.length && prefixRows === afterRows.length) {
    yield { rows: afterRows, phase: "settle", delayMs: 0 };
    return;
  }

  const beforeRegionEnd = beforeRows.length - suffixRows;
  const afterRegionEnd = afterRows.length - suffixRows;
  const beforeRegion = beforeRows.slice(prefixRows, beforeRegionEnd);
  const afterRegion = afterRows.slice(prefixRows, afterRegionEnd);
  const addedRowCount = Math.max(0, afterRows.length - beforeRows.length);

  if (addedRowCount > 0) {
    const blankRows = beforeRows.slice();
    blankRows.splice(
      prefixRows,
      0,
      ...Array.from({ length: addedRowCount }, (_, offset) => (
        Array(Math.max(1, afterRows[prefixRows + offset]?.length || 1)).fill("")
      )),
    );
    yield { rows: blankRows, phase: "type", delayMs: 90 };
  }

  const totalCells = Math.max(delimitedCellCount(beforeRows), delimitedCellCount(afterRows));
  if (totalCells > 50_000 || Math.max(beforeRows.length, afterRows.length) > 2_000) {
    yield { rows: afterRows, phase: "settle", delayMs: 0 };
    return;
  }

  const regionFormat = { ...format, finalLineEnding: false };
  const beforeText = serializeDelimitedText(beforeRegion, regionFormat);
  const afterText = serializeDelimitedText(afterRegion, regionFormat);
  const frameBudget = delimitedFrameBudget(
    beforeRows.length >= afterRows.length ? beforeRows : afterRows,
    maxFramesPerPhase,
  );
  let lastContent: string | null = null;
  for (const frame of buildEditorLiveTextFrames(beforeText, afterText, frameBudget)) {
    if (addedRowCount > 0 && frame.content === beforeText) continue;
    if (frame.content === lastContent && frame.phase !== "settle") continue;
    lastContent = frame.content;

    if (frame.content === afterText) {
      yield { rows: afterRows, phase: frame.phase, delayMs: frame.delayMs };
      continue;
    }

    const regionRows = frame.content === ""
      ? []
      : parseDelimitedText(frame.content, regionFormat).rows;
    while (addedRowCount > 0 && regionRows.length < afterRegion.length) {
      const rowIndex = prefixRows + regionRows.length;
      regionRows.push(Array(Math.max(1, afterRows[rowIndex]?.length || 1)).fill(""));
    }
    yield {
      rows: [
        ...beforeRows.slice(0, prefixRows),
        ...regionRows,
        ...beforeRows.slice(beforeRegionEnd),
      ],
      phase: frame.phase,
      delayMs: frame.delayMs,
    };
  }
}

export function buildEditorLiveDelimitedFrames(
  beforeRows: unknown[][],
  afterRows: unknown[][],
  format: DelimitedTextFormat,
  maxFramesPerPhase = 36,
): EditorLiveDelimitedFrame[] {
  return Array.from(createEditorLiveDelimitedFrameStream(
    beforeRows,
    afterRows,
    format,
    maxFramesPerPhase,
  ));
}
