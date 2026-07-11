export const DIAGRAM_VERSION = "editable_diagram_v1" as const;

export type DiagramVersion = typeof DIAGRAM_VERSION;
export type DiagramUnit = "px" | "in";
export type DiagramShapeKind =
  | "rect"
  | "roundRect"
  | "ellipse"
  | "diamond"
  | "triangle"
  | "hexagon"
  | "parallelogram"
  | "trapezoid"
  | "cylinder"
  | "document"
  | "rightArrow"
  | "downArrow";
export type DiagramElementKind = "shape" | "text" | "connector";
export type DiagramAnchor = "top" | "right" | "bottom" | "left" | "center";
export type DiagramConnectorRouting = "straight" | "elbow" | "orthogonal" | "curve";

export interface DiagramCanvasSpec {
  width: number;
  height: number;
  unit: DiagramUnit;
  originX?: number;
  originY?: number;
}

export interface DiagramTheme {
  fontFamily: string;
  labelFontFamily?: string;
  palette: Record<string, string>;
}

export interface DiagramBaseElement {
  id: string;
  kind: DiagramElementKind;
  name?: string;
  locked?: boolean;
}

export interface DiagramTextStyle {
  fontFamily?: string;
  fontSize?: number;
  fontWeight?: number;
  fontStyle?: "normal" | "italic";
  color?: string;
  align?: "left" | "center" | "right";
}

export interface DiagramShapeElement extends DiagramBaseElement {
  kind: "shape";
  shape: DiagramShapeKind;
  x: number;
  y: number;
  w: number;
  h: number;
  fill?: string;
  stroke?: string;
  strokeWidth?: number;
  strokeDash?: "none" | "dash" | "dot";
  opacity?: number;
  radius?: number;
  text?: string;
  textStyle?: DiagramTextStyle;
}

export interface DiagramTextElement extends DiagramBaseElement {
  kind: "text";
  x: number;
  y: number;
  w: number;
  h: number;
  text: string;
  textStyle?: DiagramTextStyle;
}

export interface DiagramEndpointBinding {
  elementId: string;
  anchor: DiagramAnchor;
}

export interface DiagramConnectorEndpoint {
  x?: number;
  y?: number;
  bind?: DiagramEndpointBinding;
}

export interface DiagramConnectorElement extends DiagramBaseElement {
  kind: "connector";
  from: DiagramConnectorEndpoint;
  to: DiagramConnectorEndpoint;
  controlPoint?: { x: number; y: number };
  routing?: DiagramConnectorRouting;
  stroke?: string;
  strokeWidth?: number;
  strokeDash?: "none" | "dash" | "dot";
  arrowStart?: boolean;
  arrowEnd?: boolean;
  label?: string;
  textStyle?: DiagramTextStyle;
}

export type DiagramElement = DiagramShapeElement | DiagramTextElement | DiagramConnectorElement;

export interface DiagramGroup {
  id: string;
  label?: string;
  elementIds: string[];
}

export interface DiagramConstraint {
  type: "alignX" | "alignY" | "distributeX" | "distributeY";
  elementIds: string[];
}

export interface EditableDiagramDocument {
  version: DiagramVersion;
  id: string;
  title: string;
  canvas: DiagramCanvasSpec;
  theme: DiagramTheme;
  elements: DiagramElement[];
  groups?: DiagramGroup[];
  constraints?: DiagramConstraint[];
  prompt?: string;
}

export interface DiagramBounds {
  x: number;
  y: number;
  w: number;
  h: number;
}

const DEFAULT_THEME: DiagramTheme = {
  fontFamily: "Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif",
  labelFontFamily: "Times New Roman, serif",
  palette: {
    line: "#1c1917",
    accent: "#008cad",
    containerStroke: "#55a9e6",
    cream: "#f5df9b",
    orange: "#f3a77f",
    blueFill: "#bfe1f0",
    paper: "#ffffff",
    text: "#1c1917",
    muted: "#78716c",
  },
};

export function createDiagramId(prefix = "diagram"): string {
  const random = typeof crypto !== "undefined" && "randomUUID" in crypto
    ? crypto.randomUUID().slice(0, 8)
    : Math.random().toString(36).slice(2, 10);
  return `${prefix}_${random}`;
}

export function createDefaultDiagramDocument(title = "Untitled diagram"): EditableDiagramDocument {
  const titleElementId = createDiagramId("title");
  const fuzz = createDiagramId("layer");
  const spatial = createDiagramId("layer");
  const normalize = createDiagramId("layer");
  const defuzz = createDiagramId("layer");
  const output = createDiagramId("output");

  return {
    version: DIAGRAM_VERSION,
    id: createDiagramId(),
    title,
    canvas: { width: 2400, height: 1600, unit: "px", originX: -120, originY: -90 },
    theme: DEFAULT_THEME,
    elements: [
      {
        id: titleElementId,
        kind: "text",
        x: 78,
        y: 34,
        w: 880,
        h: 36,
        text: "Editable system diagram",
        textStyle: { fontSize: 26, fontWeight: 700, color: "#1c1917", align: "center", fontFamily: "Times New Roman, serif" },
      },
      createLayer(fuzz, 210, 105, 670, 68, "Fuzzification Layer"),
      createLayer(spatial, 250, 255, 590, 72, "Spatial Firing Layer"),
      createLayer(normalize, 305, 405, 480, 66, "Normalize Layer"),
      createLayer(defuzz, 210, 540, 670, 68, "Defuzzification Layer"),
      {
        id: output,
        kind: "shape",
        shape: "roundRect",
        x: 915,
        y: 250,
        w: 170,
        h: 88,
        fill: "#bfe1f0",
        stroke: "#1c1917",
        strokeWidth: 2,
        text: "Kalman\nSmoothing",
        textStyle: { fontSize: 25, fontWeight: 700, color: "#1c1917", align: "center", fontFamily: "Times New Roman, serif" },
      },
      connector(fuzz, spatial, "bottom", "top"),
      connector(spatial, normalize, "bottom", "top"),
      connector(normalize, defuzz, "bottom", "top"),
      connector(output, defuzz, "bottom", "right", "elbow"),
    ],
    groups: [
      { id: createDiagramId("group"), label: "Layered model", elementIds: [fuzz, spatial, normalize, defuzz] },
    ],
    constraints: [
      { type: "alignX", elementIds: [fuzz, spatial, normalize, defuzz] },
    ],
  };
}

function createLayer(id: string, x: number, y: number, w: number, h: number, label: string): DiagramShapeElement {
  return {
    id,
    kind: "shape",
    shape: "roundRect",
    x,
    y,
    w,
    h,
    fill: "transparent",
    stroke: "#55a9e6",
    strokeWidth: 2,
    strokeDash: "dash",
    radius: 16,
    text: label,
    textStyle: { fontSize: 24, fontWeight: 700, color: "#1c1917", align: "center", fontFamily: "Times New Roman, serif" },
  };
}

function connector(
  from: string,
  to: string,
  fromAnchor: DiagramAnchor,
  toAnchor: DiagramAnchor,
  routing: DiagramConnectorRouting = "straight",
): DiagramConnectorElement {
  return {
    id: createDiagramId("conn"),
    kind: "connector",
    from: { bind: { elementId: from, anchor: fromAnchor } },
    to: { bind: { elementId: to, anchor: toAnchor } },
    routing,
    stroke: "#008cad",
    strokeWidth: 5,
    arrowEnd: true,
  };
}

export function isDiagramDocument(value: unknown): value is EditableDiagramDocument {
  if (!value || typeof value !== "object") return false;
  const candidate = value as Partial<EditableDiagramDocument>;
  return candidate.version === DIAGRAM_VERSION && Array.isArray(candidate.elements) && Boolean(candidate.canvas);
}

export function parseDiagramDocument(content: string, fallbackTitle = "Untitled diagram"): EditableDiagramDocument {
  if (!content.trim()) return createDefaultDiagramDocument(fallbackTitle);
  try {
    const parsed = JSON.parse(content) as unknown;
    if (isDiagramDocument(parsed)) return normalizeDiagramDocument(parsed, fallbackTitle);
  } catch {
    // Fall through to default document. The raw JSON editor remains available by renaming the file.
  }
  return createDefaultDiagramDocument(fallbackTitle);
}

export function serializeDiagramDocument(document: EditableDiagramDocument): string {
  return `${JSON.stringify(normalizeDiagramDocument(document), null, 2)}\n`;
}

export function normalizeDiagramDocument(
  document: EditableDiagramDocument,
  fallbackTitle = "Untitled diagram",
): EditableDiagramDocument {
  const canvas = {
    width: clampNumber(document.canvas?.width, 320, 10000, 1200),
    height: clampNumber(document.canvas?.height, 180, 10000, 675),
    unit: document.canvas?.unit === "in" ? "in" as const : "px" as const,
    originX: clampNumber(document.canvas?.originX, -20000, 20000, 0),
    originY: clampNumber(document.canvas?.originY, -20000, 20000, 0),
  };
  const theme = {
    ...DEFAULT_THEME,
    ...document.theme,
    palette: { ...DEFAULT_THEME.palette, ...(document.theme?.palette || {}) },
  };
  const elements = (document.elements || []).map((element) => normalizeElement(element, canvas));
  return {
    ...document,
    version: DIAGRAM_VERSION,
    id: document.id || createDiagramId(),
    title: document.title || fallbackTitle,
    canvas,
    theme,
    elements,
    groups: document.groups || [],
    constraints: document.constraints || [],
  };
}

function normalizeElement(element: DiagramElement, canvas: DiagramCanvasSpec): DiagramElement {
  if (element.kind === "connector") {
    return {
      ...element,
      id: element.id || createDiagramId("conn"),
      stroke: element.stroke || DEFAULT_THEME.palette.line,
      strokeWidth: clampNumber(element.strokeWidth, 1, 24, 2),
      routing: element.routing || "straight",
      controlPoint: element.controlPoint
        ? {
            x: clampNumber(element.controlPoint.x, (canvas.originX ?? 0) - canvas.width, (canvas.originX ?? 0) + canvas.width * 2, 0),
            y: clampNumber(element.controlPoint.y, (canvas.originY ?? 0) - canvas.height, (canvas.originY ?? 0) + canvas.height * 2, 0),
          }
        : undefined,
    };
  }
  const originX = canvas.originX ?? 0;
  const originY = canvas.originY ?? 0;
  const bounds = {
    x: clampNumber(element.x, originX - canvas.width, originX + canvas.width * 2, 80),
    y: clampNumber(element.y, originY - canvas.height, originY + canvas.height * 2, 80),
    w: clampNumber(element.w, 12, canvas.width * 2, 160),
    h: clampNumber(element.h, 12, canvas.height * 2, 80),
  };
  if (element.kind === "text") {
    return {
      ...element,
      ...bounds,
      id: element.id || createDiagramId("text"),
      text: element.text || "Text",
      textStyle: normalizeTextStyle(element.textStyle),
    };
  }
  return {
    ...element,
    ...bounds,
    id: element.id || createDiagramId("shape"),
    shape: element.shape || "rect",
    fill: element.fill ?? "#ffffff",
    stroke: element.stroke || DEFAULT_THEME.palette.line,
    strokeWidth: clampNumber(element.strokeWidth, 0, 24, 1),
    radius: clampNumber(element.radius, 0, 120, element.shape === "roundRect" ? 12 : 0),
    textStyle: normalizeTextStyle(element.textStyle),
  };
}

function normalizeTextStyle(style?: DiagramTextStyle): DiagramTextStyle {
  return {
    fontFamily: style?.fontFamily || DEFAULT_THEME.labelFontFamily,
    fontSize: clampNumber(style?.fontSize, 6, 96, 18),
    fontWeight: clampNumber(style?.fontWeight, 100, 900, 400),
    fontStyle: style?.fontStyle === "italic" ? "italic" : "normal",
    color: style?.color || DEFAULT_THEME.palette.text,
    align: style?.align || "center",
  };
}

function clampNumber(value: unknown, min: number, max: number, fallback: number): number {
  const numberValue = typeof value === "number" && Number.isFinite(value) ? value : fallback;
  return Math.max(min, Math.min(max, numberValue));
}

export function getElementBounds(element: DiagramElement): DiagramBounds | null {
  if (element.kind === "connector") return null;
  return { x: element.x, y: element.y, w: element.w, h: element.h };
}

export function getAnchorPoint(bounds: DiagramBounds, anchor: DiagramAnchor): { x: number; y: number } {
  switch (anchor) {
    case "top": return { x: bounds.x + bounds.w / 2, y: bounds.y };
    case "right": return { x: bounds.x + bounds.w, y: bounds.y + bounds.h / 2 };
    case "bottom": return { x: bounds.x + bounds.w / 2, y: bounds.y + bounds.h };
    case "left": return { x: bounds.x, y: bounds.y + bounds.h / 2 };
    case "center":
    default: return { x: bounds.x + bounds.w / 2, y: bounds.y + bounds.h / 2 };
  }
}

export function resolveEndpoint(
  endpoint: DiagramConnectorEndpoint,
  elements: DiagramElement[],
): { x: number; y: number } {
  if (endpoint.bind) {
    const target = elements.find((element) => element.id === endpoint.bind?.elementId);
    const bounds = target ? getElementBounds(target) : null;
    if (bounds) return getAnchorPoint(bounds, endpoint.bind.anchor);
  }
  return { x: endpoint.x ?? 0, y: endpoint.y ?? 0 };
}

export function cloneDiagramDocument(document: EditableDiagramDocument): EditableDiagramDocument {
  return JSON.parse(JSON.stringify(document)) as EditableDiagramDocument;
}
