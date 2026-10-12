export function spreadsheetAxisOffsets(sizes: number[], fallback: number): number[] {
  const offsets = [0];
  for (let index = 0; index < sizes.length; index += 1) {
    offsets.push(offsets[index] + (sizes[index] || fallback));
  }
  return offsets;
}

export function spreadsheetViewportRange(
  offsets: number[],
  scrollOffset: number,
  viewportSize: number,
  overscan: number,
): { start: number; end: number } {
  const length = Math.max(0, offsets.length - 1);
  if (length === 0) return { start: 0, end: 0 };
  const viewportEnd = Math.max(0, scrollOffset) + Math.max(0, viewportSize);
  const firstAfter = (value: number) => {
    let low = 0;
    let high = offsets.length;
    while (low < high) {
      const middle = Math.floor((low + high) / 2);
      if (offsets[middle] <= value) low = middle + 1;
      else high = middle;
    }
    return low;
  };
  const start = Math.min(length - 1, Math.max(0, firstAfter(scrollOffset) - 1));
  const end = Math.min(length, Math.max(start + 1, firstAfter(viewportEnd)));
  return {
    start: Math.max(0, start - overscan),
    end: Math.min(length, end + overscan),
  };
}
