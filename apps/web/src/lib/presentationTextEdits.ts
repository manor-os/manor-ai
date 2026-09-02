export interface PresentationTextEditSpan {
  originalStart: number;
  originalEnd: number;
  editedStart: number;
  editedEnd: number;
}

export type PresentationTextSourceMap = Array<number | null>;

type PresentationTextDiffStep = "delete" | "equal" | "insert";

const MAX_PRESENTATION_TEXT_EDIT_DISTANCE = 512;

function presentationTextDiffSteps(before: string[], after: string[]): PresentationTextDiffStep[] | null {
  const maximumDistance = Math.min(before.length + after.length, MAX_PRESENTATION_TEXT_EDIT_DISTANCE);
  const frontier = new Map<number, number>([[1, 0]]);
  const trace: Array<Map<number, number>> = [];

  for (let distance = 0; distance <= maximumDistance; distance += 1) {
    trace.push(new Map(frontier));
    for (let diagonal = -distance; diagonal <= distance; diagonal += 2) {
      const deletePosition = frontier.get(diagonal - 1) ?? Number.NEGATIVE_INFINITY;
      const insertPosition = frontier.get(diagonal + 1) ?? Number.NEGATIVE_INFINITY;
      let beforeIndex = diagonal === -distance || (diagonal !== distance && deletePosition < insertPosition)
        ? Math.max(0, insertPosition)
        : Math.max(0, deletePosition + 1);
      let afterIndex = beforeIndex - diagonal;
      while (
        beforeIndex < before.length
        && afterIndex < after.length
        && before[beforeIndex] === after[afterIndex]
      ) {
        beforeIndex += 1;
        afterIndex += 1;
      }
      frontier.set(diagonal, beforeIndex);
      if (beforeIndex < before.length || afterIndex < after.length) continue;

      const steps: PresentationTextDiffStep[] = [];
      let x = before.length;
      let y = after.length;
      for (let backtrackDistance = trace.length - 1; backtrackDistance >= 0; backtrackDistance -= 1) {
        const previousFrontier = trace[backtrackDistance];
        const currentDiagonal = x - y;
        const previousDeletePosition = previousFrontier.get(currentDiagonal - 1) ?? Number.NEGATIVE_INFINITY;
        const previousInsertPosition = previousFrontier.get(currentDiagonal + 1) ?? Number.NEGATIVE_INFINITY;
        const previousDiagonal = currentDiagonal === -backtrackDistance
          || (currentDiagonal !== backtrackDistance && previousDeletePosition < previousInsertPosition)
          ? currentDiagonal + 1
          : currentDiagonal - 1;
        const previousX = Math.max(0, previousFrontier.get(previousDiagonal) ?? 0);
        const previousY = previousX - previousDiagonal;
        while (x > previousX && y > previousY) {
          steps.push("equal");
          x -= 1;
          y -= 1;
        }
        if (backtrackDistance === 0) break;
        if (x === previousX) {
          steps.push("insert");
          y -= 1;
        } else {
          steps.push("delete");
          x -= 1;
        }
      }
      return steps.reverse();
    }
  }
  return null;
}

function presentationTextUniqueAnchors(before: string[], after: string[]): Array<[number, number]> {
  const positions = (characters: string[]) => {
    const result = new Map<string, { count: number; index: number }>();
    characters.forEach((character, index) => {
      const current = result.get(character);
      result.set(character, { count: (current?.count || 0) + 1, index });
    });
    return result;
  };
  const beforePositions = positions(before);
  const afterPositions = positions(after);
  const candidates = Array.from(beforePositions.entries())
    .filter(([character, position]) => position.count === 1 && afterPositions.get(character)?.count === 1)
    .map(([character, position]) => [position.index, afterPositions.get(character)!.index] as [number, number])
    .sort((left, right) => left[0] - right[0]);
  if (candidates.length < 2) return candidates;

  const previous = new Array<number>(candidates.length).fill(-1);
  const tails: number[] = [];
  for (let index = 0; index < candidates.length; index += 1) {
    const afterIndex = candidates[index][1];
    let low = 0;
    let high = tails.length;
    while (low < high) {
      const middle = Math.floor((low + high) / 2);
      if (candidates[tails[middle]][1] < afterIndex) low = middle + 1;
      else high = middle;
    }
    if (low > 0) previous[index] = tails[low - 1];
    tails[low] = index;
  }
  const anchors: Array<[number, number]> = [];
  let cursor = tails[tails.length - 1];
  while (cursor != null && cursor >= 0) {
    anchors.push(candidates[cursor]);
    cursor = previous[cursor];
  }
  return anchors.reverse();
}

export function presentationTextEditSpans(original: string, edited: string): PresentationTextEditSpan[] {
  if (original === edited) return [];
  const originalCharacters = Array.from(original);
  const editedCharacters = Array.from(edited);
  let sharedPrefix = 0;
  while (
    sharedPrefix < originalCharacters.length
    && sharedPrefix < editedCharacters.length
    && originalCharacters[sharedPrefix] === editedCharacters[sharedPrefix]
  ) sharedPrefix += 1;

  let sharedSuffix = 0;
  while (
    sharedSuffix < originalCharacters.length - sharedPrefix
    && sharedSuffix < editedCharacters.length - sharedPrefix
    && originalCharacters[originalCharacters.length - 1 - sharedSuffix]
      === editedCharacters[editedCharacters.length - 1 - sharedSuffix]
  ) sharedSuffix += 1;

  const originalMiddle = originalCharacters.slice(sharedPrefix, originalCharacters.length - sharedSuffix);
  const editedMiddle = editedCharacters.slice(sharedPrefix, editedCharacters.length - sharedSuffix);
  const steps = presentationTextDiffSteps(originalMiddle, editedMiddle);
  if (!steps) {
    const anchors = presentationTextUniqueAnchors(originalMiddle, editedMiddle);
    if (anchors.length) {
      const spans: PresentationTextEditSpan[] = [];
      let originalCursor = 0;
      let editedCursor = 0;
      for (const [originalAnchor, editedAnchor] of anchors) {
        const nested = presentationTextEditSpans(
          originalMiddle.slice(originalCursor, originalAnchor).join(""),
          editedMiddle.slice(editedCursor, editedAnchor).join(""),
        );
        spans.push(...nested.map((span) => ({
          originalStart: span.originalStart + sharedPrefix + originalCursor,
          originalEnd: span.originalEnd + sharedPrefix + originalCursor,
          editedStart: span.editedStart + sharedPrefix + editedCursor,
          editedEnd: span.editedEnd + sharedPrefix + editedCursor,
        })));
        originalCursor = originalAnchor + 1;
        editedCursor = editedAnchor + 1;
      }
      const nested = presentationTextEditSpans(
        originalMiddle.slice(originalCursor).join(""),
        editedMiddle.slice(editedCursor).join(""),
      );
      spans.push(...nested.map((span) => ({
        originalStart: span.originalStart + sharedPrefix + originalCursor,
        originalEnd: span.originalEnd + sharedPrefix + originalCursor,
        editedStart: span.editedStart + sharedPrefix + editedCursor,
        editedEnd: span.editedEnd + sharedPrefix + editedCursor,
      })));
      return spans;
    }
    return [{
      originalStart: sharedPrefix,
      originalEnd: originalCharacters.length - sharedSuffix,
      editedStart: sharedPrefix,
      editedEnd: editedCharacters.length - sharedSuffix,
    }];
  }

  const spans: PresentationTextEditSpan[] = [];
  let originalOffset = sharedPrefix;
  let editedOffset = sharedPrefix;
  let active: PresentationTextEditSpan | null = null;
  const finishActiveSpan = () => {
    if (!active) return;
    spans.push(active);
    active = null;
  };
  for (const step of steps) {
    if (step === "equal") {
      finishActiveSpan();
      originalOffset += 1;
      editedOffset += 1;
      continue;
    }
    active ||= {
      originalStart: originalOffset,
      originalEnd: originalOffset,
      editedStart: editedOffset,
      editedEnd: editedOffset,
    };
    if (step === "delete") {
      originalOffset += 1;
      active.originalEnd = originalOffset;
    } else {
      editedOffset += 1;
      active.editedEnd = editedOffset;
    }
  }
  finishActiveSpan();
  return spans;
}

export function reconcilePresentationTextSourceMap(
  currentText: string,
  nextText: string,
  currentSourceMap?: PresentationTextSourceMap,
  editSpans: PresentationTextEditSpan[] = presentationTextEditSpans(currentText, nextText),
): PresentationTextSourceMap {
  const currentCharacters = Array.from(currentText);
  const currentMap = currentSourceMap?.length === currentCharacters.length
    ? currentSourceMap
    : currentCharacters.map((_character, index) => index);
  const nextMap: PresentationTextSourceMap = [];
  let currentCursor = 0;
  let nextCursor = 0;
  for (const span of editSpans) {
    const unchangedLength = span.originalStart - currentCursor;
    nextMap.push(...currentMap.slice(currentCursor, currentCursor + unchangedLength));
    nextMap.push(...new Array(span.editedEnd - span.editedStart).fill(null));
    currentCursor = span.originalEnd;
    nextCursor = span.editedEnd;
  }
  nextMap.push(...currentMap.slice(currentCursor));
  const nextLength = Array.from(nextText).length;
  if (nextMap.length !== nextLength || nextCursor > nextLength) {
    return new Array(nextLength).fill(null);
  }
  return nextMap;
}

export function presentationTextEditSpansFromSourceMap(
  original: string,
  edited: string,
  sourceMap: PresentationTextSourceMap | undefined,
): PresentationTextEditSpan[] | null {
  if (!sourceMap) return null;
  const originalCharacters = Array.from(original);
  const editedCharacters = Array.from(edited);
  if (sourceMap.length !== editedCharacters.length) return null;
  let previousSource = -1;
  for (let index = 0; index < sourceMap.length; index += 1) {
    const source = sourceMap[index];
    if (source == null) continue;
    if (
      source <= previousSource
      || source < 0
      || source >= originalCharacters.length
      || originalCharacters[source] !== editedCharacters[index]
    ) return null;
    previousSource = source;
  }

  const spans: PresentationTextEditSpan[] = [];
  let originalCursor = 0;
  let editedCursor = 0;
  for (let editedIndex = 0; editedIndex < sourceMap.length; editedIndex += 1) {
    const source = sourceMap[editedIndex];
    if (source == null) continue;
    if (source > originalCursor || editedIndex > editedCursor) {
      spans.push({
        originalStart: originalCursor,
        originalEnd: source,
        editedStart: editedCursor,
        editedEnd: editedIndex,
      });
    }
    originalCursor = source + 1;
    editedCursor = editedIndex + 1;
  }
  if (originalCursor < originalCharacters.length || editedCursor < editedCharacters.length) {
    spans.push({
      originalStart: originalCursor,
      originalEnd: originalCharacters.length,
      editedStart: editedCursor,
      editedEnd: editedCharacters.length,
    });
  }
  return spans.filter((span) => (
    span.originalStart !== span.originalEnd || span.editedStart !== span.editedEnd
  ));
}

export function presentationTextEditSpansForSource(
  original: string,
  edited: string,
  sourceMap?: PresentationTextSourceMap,
): PresentationTextEditSpan[] {
  return presentationTextEditSpansFromSourceMap(original, edited, sourceMap)
    || presentationTextEditSpans(original, edited);
}

function normalizedPresentationTextSourceMap(
  text: string,
  sourceMap?: PresentationTextSourceMap,
): PresentationTextSourceMap {
  const characters = Array.from(text);
  if (sourceMap?.length === characters.length) {
    let previousSource = -1;
    const valid = sourceMap.every((source) => {
      if (source == null) return true;
      if (!Number.isInteger(source) || source < 0 || source <= previousSource) return false;
      previousSource = source;
      return true;
    });
    if (valid) return sourceMap;
  }
  return characters.map((_character, index) => index);
}

export function rebasePresentationTextSourceMap(
  baselineText: string,
  currentText: string,
  baselineSourceMap?: PresentationTextSourceMap,
  currentSourceMap?: PresentationTextSourceMap,
): PresentationTextSourceMap {
  const baselineCharacters = Array.from(baselineText);
  const currentCharacters = Array.from(currentText);
  const baselineMap = normalizedPresentationTextSourceMap(baselineText, baselineSourceMap);
  const currentMap = normalizedPresentationTextSourceMap(currentText, currentSourceMap);
  const baselineIndexBySource = new Map<number, number>();
  baselineMap.forEach((source, index) => {
    if (source != null) baselineIndexBySource.set(source, index);
  });

  const anchors: Array<{ baselineIndex: number; currentIndex: number }> = [];
  let previousBaselineIndex = -1;
  currentMap.forEach((source, currentIndex) => {
    if (source == null) return;
    const baselineIndex = baselineIndexBySource.get(source);
    if (
      baselineIndex == null
      || baselineIndex <= previousBaselineIndex
      || baselineCharacters[baselineIndex] !== currentCharacters[currentIndex]
    ) return;
    anchors.push({ baselineIndex, currentIndex });
    previousBaselineIndex = baselineIndex;
  });

  const rebased: PresentationTextSourceMap = new Array(currentCharacters.length).fill(null);
  let baselineCursor = 0;
  let currentCursor = 0;
  const reconcileGap = (baselineEnd: number, currentEnd: number) => {
    const gapMap = reconcilePresentationTextSourceMap(
      baselineCharacters.slice(baselineCursor, baselineEnd).join(""),
      currentCharacters.slice(currentCursor, currentEnd).join(""),
    );
    gapMap.forEach((source, offset) => {
      rebased[currentCursor + offset] = source == null ? null : baselineCursor + source;
    });
  };
  for (const anchor of anchors) {
    reconcileGap(anchor.baselineIndex, anchor.currentIndex);
    rebased[anchor.currentIndex] = anchor.baselineIndex;
    baselineCursor = anchor.baselineIndex + 1;
    currentCursor = anchor.currentIndex + 1;
  }
  reconcileGap(baselineCharacters.length, currentCharacters.length);
  return rebased;
}

export function reconcilePresentationTextRuns<T extends { text: string }>(
  runs: T[] | undefined,
  currentText: string,
  nextText: string,
  nextSourceMap?: PresentationTextSourceMap,
  currentSourceMap?: PresentationTextSourceMap,
): T[] | undefined {
  if (!runs?.length || !nextText) return undefined;
  const currentCharacters = runs.flatMap((run) => {
    const { text: _text, ...formatting } = run;
    return Array.from(run.text, (character) => ({ character, formatting }));
  });
  if (currentCharacters.map(({ character }) => character).join("") !== currentText) return undefined;

  const nextCharacters = Array.from(nextText);
  const nextFormatting: Array<Omit<T, "text"> | undefined> = new Array(nextCharacters.length);
  let currentCursor = 0;
  let nextCursor = 0;
  const sourceMap = nextSourceMap
    ? rebasePresentationTextSourceMap(currentText, nextText, currentSourceMap, nextSourceMap)
    : undefined;
  for (const span of presentationTextEditSpansForSource(currentText, nextText, sourceMap)) {
    const unchangedLength = span.originalStart - currentCursor;
    for (let offset = 0; offset < unchangedLength; offset += 1) {
      nextFormatting[nextCursor + offset] = currentCharacters[currentCursor + offset]?.formatting as Omit<T, "text">;
    }
    const insertedFormatting = currentCharacters[Math.max(0, span.originalStart - 1)]?.formatting
      || currentCharacters[span.originalStart]?.formatting
      || {};
    for (let index = span.editedStart; index < span.editedEnd; index += 1) {
      nextFormatting[index] = insertedFormatting as Omit<T, "text">;
    }
    currentCursor = span.originalEnd;
    nextCursor = span.editedEnd;
  }
  for (let offset = 0; currentCursor + offset < currentCharacters.length; offset += 1) {
    nextFormatting[nextCursor + offset] = currentCharacters[currentCursor + offset].formatting as Omit<T, "text">;
  }

  const equalFormatting = (left: Omit<T, "text">, right: Omit<T, "text">) => (
    JSON.stringify(left) === JSON.stringify(right)
  );
  const reconciled: T[] = [];
  nextCharacters.forEach((character, index) => {
    const formatting = (nextFormatting[index] || {}) as Omit<T, "text">;
    const previous = reconciled[reconciled.length - 1];
    if (previous) {
      const { text: _text, ...previousFormatting } = previous;
      if (equalFormatting(previousFormatting as Omit<T, "text">, formatting)) {
        previous.text += character;
        return;
      }
    }
    reconciled.push({ text: character, ...formatting } as T);
  });
  return reconciled;
}
