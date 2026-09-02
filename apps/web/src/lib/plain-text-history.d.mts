export interface PlainTextSelection {
  start: number;
  end: number;
  direction?: "forward" | "backward" | "none";
}

export interface PlainTextHistoryEntry {
  start: number;
  removed: string;
  inserted: string;
  leftContext: string;
  rightContext: string;
  beforeSelection: PlainTextSelection;
  afterSelection: PlainTextSelection;
  beforeLength: number;
  afterLength: number;
  beforeGeneration: number;
  afterGeneration: number;
  bytes: number;
}

export interface PlainTextHistory {
  undo: PlainTextHistoryEntry[];
  redo: PlainTextHistoryEntry[];
  undoBytes: number;
  redoBytes: number;
  maxEntries: number;
  maxBytes: number;
}

export interface PlainTextHistoryOptions {
  maxEntries?: number;
  maxBytes?: number;
}

export interface PlainTextHistoryResult {
  text: string;
  selection: PlainTextSelection;
  generation: number;
}

export const PLAIN_TEXT_HISTORY_MAX_ENTRIES: number;
export const PLAIN_TEXT_HISTORY_MAX_BYTES: number;
export function normalizePlainTextSelection(selection: PlainTextSelection | null | undefined, textLength: number): PlainTextSelection;
export function createPlainTextHistory(options?: PlainTextHistoryOptions): PlainTextHistory;
export function resetPlainTextHistory(history: PlainTextHistory): void;
export function canUndoPlainTextHistory(history: PlainTextHistory): boolean;
export function canRedoPlainTextHistory(history: PlainTextHistory): boolean;
export function recordPlainTextHistory(
  history: PlainTextHistory,
  previous: string,
  next: string,
  beforeSelection: PlainTextSelection,
  afterSelection: PlainTextSelection,
  options: {
    input?: boolean;
    beforeGeneration: number;
    afterGeneration: number;
  },
): boolean;
export function undoPlainTextHistory(
  history: PlainTextHistory,
  current: string,
  currentGeneration: number,
): PlainTextHistoryResult | null;
export function redoPlainTextHistory(
  history: PlainTextHistory,
  current: string,
  currentGeneration: number,
): PlainTextHistoryResult | null;
