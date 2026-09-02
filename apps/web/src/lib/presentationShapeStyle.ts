import { presentationPointsToCqh } from "./presentationStyleInheritance";

export const PRESENTATION_STROKE_DASHES = [
  "solid",
  "dash",
  "dot",
  "dashDot",
  "lgDash",
  "lgDashDot",
  "lgDashDotDot",
  "sysDash",
  "sysDot",
  "sysDashDot",
  "sysDashDotDot",
] as const;

export type PresentationStrokeDash = typeof PRESENTATION_STROKE_DASHES[number];

const presentationStrokeDashSet = new Set<string>(PRESENTATION_STROKE_DASHES);

export function isPresentationStrokeDash(value: unknown): value is PresentationStrokeDash {
  return typeof value === "string" && presentationStrokeDashSet.has(value);
}

const dashPatterns: Partial<Record<PresentationStrokeDash, number[]>> = {
  dash: [4, 3], dot: [1, 3], dashDot: [4, 3, 1, 3],
  lgDash: [8, 3], lgDashDot: [8, 3, 1, 3], lgDashDotDot: [8, 3, 1, 3, 1, 3],
  sysDash: [3, 1], sysDot: [1, 1], sysDashDot: [3, 1, 1, 1], sysDashDotDot: [3, 1, 1, 1, 1, 1],
};

export function presentationStrokeDash(xml: string): string | undefined {
  const dash = xml.match(/<a:prstDash\b[^>]*\bval="([^"]+)"/i)?.[1];
  return isPresentationStrokeDash(dash) ? dash : undefined;
}

export function presentationStrokeDashArray(dash: string | undefined, width: number): string | undefined {
  return isPresentationStrokeDash(dash)
    ? dashPatterns[dash]?.map((unit) => presentationPointsToCqh(unit * width)).join(" ")
    : undefined;
}

/** DrawingML roundRect uses a fraction of the shorter side, not elliptical percentages. */
export function presentationRoundRectRadius(width: number, height: number, adjustment = 16.667): string {
  const fraction = Math.max(0, Math.min(50, adjustment)) / 100;
  return `calc(min(${width}cqw, ${height}cqh) * ${fraction})`;
}

/** SVG rect radius matching the editor canvas for rect-based DrawingML presets. */
export function presentationOutlineRectRadius(
  preset: string | undefined,
  width: number,
  height: number,
  borderRadius?: number,
  strokeInset = "0px",
): string | number {
  if (preset === "flowChartTerminator") return "999px";
  if (preset === "roundRect") {
    return `max(0px, calc(${presentationRoundRectRadius(width, height, borderRadius)} - ${strokeInset}))`;
  }
  return 0;
}
