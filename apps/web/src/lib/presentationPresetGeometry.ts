export type PresentationPolygonPoint = readonly [number, number];

/** Presets that both the native file engine and browser editor can represent consistently. */
export const PRESENTATION_EDITABLE_PRESETS = [
  "rect",
  "roundRect",
  "ellipse",
  "triangle",
  "rtTriangle",
  "diamond",
  "pentagon",
  "hexagon",
  "octagon",
  "parallelogram",
  "trapezoid",
  "chevron",
  "rightArrow",
  "leftArrow",
  "upArrow",
  "downArrow",
  "leftRightArrow",
  "star4",
  "star5",
  "star6",
  "plus",
  "flowChartProcess",
  "flowChartDecision",
  "flowChartTerminator",
  "flowChartInputOutput",
  "wedgeRectCallout",
  "wedgeRoundRectCallout",
  "wedgeEllipseCallout",
  "line",
] as const;

export type PresentationEditablePreset = typeof PRESENTATION_EDITABLE_PRESETS[number];

const presentationEditablePresetSet = new Set<string>(PRESENTATION_EDITABLE_PRESETS);

export function isPresentationEditablePreset(value: unknown): value is PresentationEditablePreset {
  return typeof value === "string" && presentationEditablePresetSet.has(value);
}

function regularPolygonPoints(sides: number, rotation = -90): PresentationPolygonPoint[] {
  return Array.from({ length: sides }, (_, index) => {
    const angle = ((rotation + (360 * index) / sides) * Math.PI) / 180;
    return [50 + Math.cos(angle) * 50, 50 + Math.sin(angle) * 50] as const;
  });
}

function regularStarPoints(points: number, innerRadius: number): PresentationPolygonPoint[] {
  return Array.from({ length: points * 2 }, (_, index) => {
    const angle = ((-90 + (180 * index) / points) * Math.PI) / 180;
    const radius = index % 2 === 0 ? 50 : innerRadius;
    return [50 + Math.cos(angle) * radius, 50 + Math.sin(angle) * radius] as const;
  });
}

/** Bounded presets rendered identically by the PPT editor and read-only viewer. */
export function presentationPresetPolygonPoints(preset?: string): PresentationPolygonPoint[] | undefined {
  const fixed: Record<string, PresentationPolygonPoint[]> = {
    triangle: [[50, 0], [100, 100], [0, 100]],
    rtTriangle: [[0, 0], [100, 100], [0, 100]],
    diamond: [[50, 0], [100, 50], [50, 100], [0, 50]],
    flowChartDecision: [[50, 0], [100, 50], [50, 100], [0, 50]],
    parallelogram: [[25, 0], [100, 0], [75, 100], [0, 100]],
    flowChartInputOutput: [[25, 0], [100, 0], [75, 100], [0, 100]],
    trapezoid: [[20, 0], [80, 0], [100, 100], [0, 100]],
    pentagon: [[50, 0], [100, 38], [81, 100], [19, 100], [0, 38]],
    hexagon: [[25, 0], [75, 0], [100, 50], [75, 100], [25, 100], [0, 50]],
    octagon: [[29, 0], [71, 0], [100, 29], [100, 71], [71, 100], [29, 100], [0, 71], [0, 29]],
    chevron: [[0, 0], [70, 0], [100, 50], [70, 100], [0, 100], [30, 50]],
    homePlate: [[0, 0], [75, 0], [100, 50], [75, 100], [0, 100]],
    rightArrow: [[0, 25], [62, 25], [62, 0], [100, 50], [62, 100], [62, 75], [0, 75]],
    leftArrow: [[38, 0], [38, 25], [100, 25], [100, 75], [38, 75], [38, 100], [0, 50]],
    upArrow: [[50, 0], [100, 38], [75, 38], [75, 100], [25, 100], [25, 38], [0, 38]],
    downArrow: [[25, 0], [75, 0], [75, 62], [100, 62], [50, 100], [0, 62], [25, 62]],
    leftRightArrow: [[0, 50], [22, 15], [22, 34], [78, 34], [78, 15], [100, 50], [78, 85], [78, 66], [22, 66], [22, 85]],
    plus: [[35, 0], [65, 0], [65, 35], [100, 35], [100, 65], [65, 65], [65, 100], [35, 100], [35, 65], [0, 65], [0, 35], [35, 35]],
    wedgeRectCallout: [[0, 0], [100, 0], [100, 76], [61, 76], [50, 100], [43, 76], [0, 76]],
    wedgeRoundRectCallout: [[8, 0], [92, 0], [100, 8], [100, 68], [92, 76], [61, 76], [50, 100], [43, 76], [8, 76], [0, 68], [0, 8]],
    wedgeEllipseCallout: [[50, 0], [78, 8], [96, 29], [100, 50], [96, 68], [78, 84], [61, 88], [50, 100], [44, 86], [22, 84], [4, 68], [0, 50], [4, 29], [22, 8]],
  };
  if (preset && fixed[preset]) return fixed[preset];
  if (preset === "star4") return regularStarPoints(4, 20);
  if (preset === "star5") return regularStarPoints(5, 22);
  if (preset === "star6") return regularStarPoints(6, 25);
  if (preset === "heptagon") return regularPolygonPoints(7);
  if (preset === "decagon") return regularPolygonPoints(10);
  if (preset === "dodecagon") return regularPolygonPoints(12);
  return undefined;
}

export function presentationPresetClipPath(preset?: string): string | undefined {
  const points = presentationPresetPolygonPoints(preset);
  return points ? `polygon(${points.map(([x, y]) => `${x}% ${y}%`).join(", ")})` : undefined;
}

export function presentationPresetPointsAttribute(preset?: string): string | undefined {
  return presentationPresetPolygonPoints(preset)?.map(([x, y]) => `${x},${y}`).join(" ");
}
