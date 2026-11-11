import type { WorkspaceAppChartBucket } from "../../lib/api";

/** How a chart reads and lays out its numbers, apart from its markup: the same in its picture, its table and its tooltip. */

function parseDay(key: string): Date | null {
  const match = /^(\d{4})-(\d{2})(?:-(\d{2}))?$/.exec(key);
  return match ? new Date(Date.UTC(Number(match[1]), Number(match[2]) - 1, Number(match[3] || 1))) : null;
}

/** A time bucket in the reader's words: "Nov 2", "Nov 2026"; a weekday or an hour keeps the server's name. */
export function chartCategoryLabel(category: { key: string; label: string }, bucket: WorkspaceAppChartBucket | undefined, locale: string, short = false): string {
  if (bucket !== "day" && bucket !== "week" && bucket !== "month") return category.label;
  const day = parseDay(category.key);
  if (!day) return category.label;
  try {
    return new Intl.DateTimeFormat(locale, bucket === "month"
      ? { month: "short", ...(short ? {} : { year: "numeric" }), timeZone: "UTC" }
      : { month: "short", day: "numeric", timeZone: "UTC" }).format(day);
  } catch {
    return category.label;
  }
}

/** Round bounds and 3–5 ticks that hold every value (and zero for bars and areas). */
export function chartScale(values: number[], fromZero: boolean): { min: number; max: number; ticks: number[] } {
  const finite = values.filter((value) => Number.isFinite(value));
  let low = finite.length ? Math.min(...finite) : 0;
  let high = finite.length ? Math.max(...finite) : 1;
  if (fromZero) { low = Math.min(0, low); high = Math.max(0, high); }
  if (low === high) { high = low + 1; if (!fromZero) low -= 1; }
  const rough = (high - low) / 4;
  const power = 10 ** Math.floor(Math.log10(rough));
  const step = [1, 2, 2.5, 5, 10].map((factor) => factor * power).find((candidate) => candidate >= rough) ?? 10 * power;
  const min = Math.floor(low / step) * step;
  const max = Math.ceil(high / step) * step;
  const count = Math.min(8, Math.round((max - min) / step) + 1);
  return { min, max, ticks: Array.from({ length: count }, (_, index) => Number((min + index * step).toPrecision(12))) };
}

/** Each layer's bottom and top in every column, for a stacked bar or area; a missing value stacks as nothing. */
export function stackSeries(series: Array<{ values: Array<number | null> }>): Array<Array<[number, number]>> {
  const length = Math.max(0, ...series.map((entry) => entry.values.length));
  const floor = Array.from({ length }, () => 0);
  return series.map((entry) => Array.from({ length }, (_, index) => {
    const bottom = floor[index];
    const top = bottom + Math.max(0, entry.values[index] ?? 0);
    floor[index] = top;
    return [bottom, top] as [number, number];
  }));
}

/** A funnel's steps: each count, its share of the first step and what share of the step before reached it. */
export function funnelSteps(values: Array<number | null>): Array<{ value: number; ofFirst: number | null; ofPrevious: number | null }> {
  const counts = values.map((value) => value ?? 0);
  return counts.map((value, index) => ({
    value,
    ofFirst: counts[0] > 0 ? value / counts[0] : null,
    ofPrevious: index === 0 ? null : counts[index - 1] > 0 ? value / counts[index - 1] : null,
  }));
}

/** The arcs of a donut, clockwise from the top, as SVG paths around (cx, cy). A lone slice is a full ring. */
export function donutArcs(values: number[], cx: number, cy: number, outer: number, inner: number): string[] {
  const total = values.reduce((sum, value) => sum + Math.max(0, value), 0);
  if (total <= 0) return values.map(() => "");
  let angle = -Math.PI / 2;
  const point = (radius: number, at: number) => `${(cx + radius * Math.cos(at)).toFixed(2)} ${(cy + radius * Math.sin(at)).toFixed(2)}`;
  return values.map((value) => {
    const share = Math.max(0, value) / total;
    if (share <= 0) return "";
    if (share >= 0.9999) {
      return `M${point(outer, -Math.PI / 2)}A${outer} ${outer} 0 1 1 ${point(outer, Math.PI * 1.5 - 0.0001)}Z`
        + `M${point(inner, -Math.PI / 2)}A${inner} ${inner} 0 1 0 ${point(inner, Math.PI * 1.5 - 0.0001)}Z`;
    }
    const start = angle;
    const end = angle + share * Math.PI * 2;
    angle = end;
    const large = end - start > Math.PI ? 1 : 0;
    return `M${point(outer, start)}A${outer} ${outer} 0 ${large} 1 ${point(outer, end)}L${point(inner, end)}A${inner} ${inner} 0 ${large} 0 ${point(inner, start)}Z`;
  });
}

/** How strong a heatmap cell is, 0–1 of the busiest cell; an empty grid is all 0. */
export function heatLevel(value: number | null, max: number): number {
  return value && max > 0 ? Math.max(0.08, Math.min(1, value / max)) : 0;
}

/** Which category labels fit along an axis of ``width`` pixels: every nth, always the last. */
export function labelEvery(count: number, width: number, labelWidth = 56): number {
  return Math.max(1, Math.ceil(count / Math.max(2, Math.floor(width / labelWidth))));
}

/** The colour of the nth series: ink for one, the chart palette (then its lighter tints) for several. */
export function seriesColor(index: number, count: number): { color: string; opacity: number } {
  if (count <= 1) return { color: "var(--text-strong)", opacity: 1 };
  return { color: `var(--chart-series-${index % 3 + 1})`, opacity: index < 3 ? 1 : 0.55 };
}
