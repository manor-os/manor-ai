// Paint interval. Combined with the slice sizes below this lands around
// 55-150 chars/sec — brisk enough that a long answer never feels stuck,
// slow enough to read along. It used to be 18ms with slices that GREW with
// the backlog, so a long reply painted at ~1300 chars/sec: the answers that
// most need reading arrived fastest.
export const TYPEWRITER_TICK_MS = 36;

export function nextTypewriterSlice(text: string): [string, string] {
  if (text.length === 0) return ["", ""];

  // The pace only needs to distinguish four bounded thresholds. Iterating the
  // full remaining backlog on every tick made draining a long response quadratic.
  let length = 0;
  for (const _char of text) {
    length += 1;
    if (length > 1000) break;
  }

  // The backlog still nudges the pace — a burst should not take minutes to
  // drain — but gently, and the ceiling stays inside reading speed rather
  // than dumping the screen.
  const count =
    length > 1000 ? 6 :
      length > 400 ? 4 :
        length > 160 ? 3 :
          2;
  let end = 0;
  let codeUnitEnd = 0;

  // Keep a trailing run of spaces/newlines with the same paint so markdown
  // does not look like it stalls between words.
  for (const char of text) {
    if (end >= count && (end >= count + 8 || !/\s/.test(char))) break;
    end += 1;
    codeUnitEnd += char.length;
  }

  return [text.slice(0, codeUnitEnd), text.slice(codeUnitEnd)];
}
