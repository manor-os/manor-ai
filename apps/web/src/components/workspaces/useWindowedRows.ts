import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { windowRange } from "./workspaceAppCollection";

/** Draws only the rows of a long list (a table, a list of cards) that are near the screen, with spacers for the
 *  rest, so a list of thousands scrolls as smoothly as one of twenty. Rows keep their own heights: the spacers use
 *  the average of the rows drawn. Lists at or under ``threshold`` rows are drawn whole. */
export function useWindowedRows<T extends HTMLElement>(count: number, { threshold = 150, estimate = 64 } = {}) {
  const ref = useRef<T | null>(null);
  const average = useRef(estimate);
  const enabled = count > threshold;
  const [range, setRange] = useState(() => ({ start: 0, end: enabled ? Math.min(count, 60) : count, before: 0, after: 0 }));
  const update = useCallback(() => {
    const element = ref.current;
    if (!element || !enabled) return;
    const next = windowRange({ top: -element.getBoundingClientRect().top, viewport: window.innerHeight, average: average.current, count });
    setRange((current) => current.start === next.start && current.end === next.end && Math.abs(current.before - next.before) < 1
      && Math.abs(current.after - next.after) < 1 ? current : next);
  }, [count, enabled]);
  useLayoutEffect(() => {
    // The average follows the rows that are drawn, so spacers match them.
    const element = ref.current;
    if (!element || !enabled) return;
    const rows = Array.from(element.querySelectorAll<HTMLElement>("[data-window-row]"));
    if (rows.length) average.current = rows.reduce((sum, row) => sum + row.offsetHeight, 0) / rows.length || estimate;
  });
  useEffect(() => {
    if (!enabled) {
      setRange({ start: 0, end: count, before: 0, after: 0 });
      return undefined;
    }
    let frame = 0;
    const schedule = () => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(update);
    };
    update();
    // Any scrolling container (the page, the App pane) moves the list.
    document.addEventListener("scroll", schedule, true);
    window.addEventListener("resize", schedule);
    return () => {
      cancelAnimationFrame(frame);
      document.removeEventListener("scroll", schedule, true);
      window.removeEventListener("resize", schedule);
    };
  }, [count, enabled, update]);
  return { ref, enabled, ...range };
}
