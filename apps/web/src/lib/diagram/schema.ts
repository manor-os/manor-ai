export const DIAGRAM_VERSION = "editable_diagram_v1" as const;
export const MAX_DIAGRAM_ELEMENTS = 10_000;
export const MAX_DIAGRAM_CANVAS_DIMENSION = 100_000;

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
export type DiagramConnectorMarker = "arrow" | "circle" | "cross";

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
  markerStart?: DiagramConnectorMarker;
  markerEnd?: DiagramConnectorMarker;
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

function decodeMermaidEntities(value: string): string {
  if (typeof DOMParser !== "undefined") {
    const parsed = new DOMParser().parseFromString(`<body>${value}</body>`, "text/html");
    return parsed.body.textContent || "";
  }

  const namedEntities: Record<string, string> = {
    amp: "&",
    apos: "'",
    gt: ">",
    lt: "<",
    nbsp: "\u00a0",
    quot: '"',
  };
  return value.replace(/&(#(?:x[\da-f]+|\d+)|[a-z][\da-z]+);/gi, (entity, reference: string) => {
    if (!reference.startsWith("#")) return namedEntities[reference.toLowerCase()] ?? entity;
    const hexadecimal = reference[1]?.toLowerCase() === "x";
    const codePoint = Number.parseInt(reference.slice(hexadecimal ? 2 : 1), hexadecimal ? 16 : 10);
    if (!Number.isFinite(codePoint) || codePoint < 0 || codePoint > 0x10ffff) return "\ufffd";
    try {
      return String.fromCodePoint(codePoint);
    } catch {
      return "\ufffd";
    }
  });
}

function decodeMermaidLabel(value: string): string {
  let decoded = decodeMermaidEntities(value.replace(/<br\s*\/?\s*>/gi, " ")).trim();
  if (decoded.length >= 2 && decoded[0] === decoded.at(-1) && /["'`]/.test(decoded[0])) {
    decoded = decoded.slice(1, -1);
  }
  return decoded
    .replace(/\s+/g, " ")
    .trim();
}

type MermaidFlowDirection = "TD" | "BT" | "LR" | "RL";
type MermaidFlowNode = { id: string; label: string; shape: DiagramShapeKind };
type MermaidFlowEdge = {
  from: string;
  to: string;
  label?: string;
  markerStart?: DiagramConnectorMarker;
  markerEnd?: DiagramConnectorMarker;
};

function mermaidEdgeMarkers(value: string): {
  markerStart?: DiagramConnectorMarker;
  markerEnd?: DiagramConnectorMarker;
} {
  const token = value.replace(/\s+/g, "");
  const markerForCharacter = (character: string | undefined): DiagramConnectorMarker | undefined => {
    if (character === "<" || character === ">") return "arrow";
    if (character === "o") return "circle";
    if (character === "x") return "cross";
    return undefined;
  };
  return {
    markerStart: markerForCharacter(token[0]),
    markerEnd: markerForCharacter(token.at(-1)),
  };
}

function splitMermaidStatements(value: string): string[] {
  const statements: string[] = [];
  let current = "";
  let quote = "";
  let escaped = false;
  let depth = 0;
  const pushCurrent = () => {
    const statement = current.trim();
    if (statement) statements.push(statement);
    current = "";
  };

  Array.from(value).forEach((character) => {
    if (quote) {
      current += character;
      if (escaped) {
        escaped = false;
      } else if (character === "\\") {
        escaped = true;
      } else if (character === quote) {
        quote = "";
      }
      return;
    }
    const previous = current.at(-1) || "";
    if (/["'`]/.test(character) && (character !== "'" || !/[A-Za-z0-9]/.test(previous))) {
      quote = character;
      current += character;
      return;
    }
    if (/[\[{(]/.test(character)) depth += 1;
    if (/[\]})]/.test(character)) depth = Math.max(0, depth - 1);
    if (depth === 0 && (character === ";" || character === "\n" || character === "\r")) {
      pushCurrent();
      return;
    }
    current += character;
  });
  pushCurrent();
  return statements;
}

function parseMermaidFlow(source: string): {
  direction: MermaidFlowDirection;
  nodes: MermaidFlowNode[];
  edges: MermaidFlowEdge[];
} | null {
  const cleanedSource = source
    .split(/\r?\n/)
    .map((line) => line.split("%%", 1)[0])
    .join("\n");
  const header = cleanedSource.match(
    /^\s*(?:flowchart|graph)\b(?:\s+(TD|TB|BT|LR|RL))?/i,
  );
  if (!header) return null;
  const rawDirection = (header[1] || "TD").toUpperCase();
  const direction: MermaidFlowDirection = rawDirection === "TB"
    ? "TD"
    : rawDirection as MermaidFlowDirection;

  const nodes = new Map<string, MermaidFlowNode>();
  const nodeOrder: string[] = [];
  const edges: MermaidFlowEdge[] = [];
  const edgeKeys = new Set<string>();
  const ensureElementBudget = (additionalElements = 0) => {
    if (nodes.size + edges.length + additionalElements + 1 > MAX_DIAGRAM_ELEMENTS) {
      throw new Error("Diagram has too many elements to preview");
    }
  };
  const rememberNode = (
    id: string,
    label?: string,
    shape: DiagramShapeKind = "rect",
  ) => {
    const cleanId = id.trim();
    if (!cleanId) return;
    const cleanLabel = decodeMermaidLabel(label || cleanId) || cleanId;
    const existing = nodes.get(cleanId);
    if (existing) {
      if (label) {
        existing.label = cleanLabel;
        existing.shape = shape;
      }
      return;
    }
    ensureElementBudget(1);
    nodes.set(cleanId, { id: cleanId, label: cleanLabel, shape });
    nodeOrder.push(cleanId);
  };

  const nodePatterns: Array<{ pattern: RegExp; shape: DiagramShapeKind }> = [
    {
      pattern: /\b([A-Za-z][A-Za-z0-9_-]*)\s*\[\s*\[\s*(?:"([^"\n]+)"|'([^'\n]+)'|([^\]\n]+?))\s*\]\s*\]/g,
      shape: "rect",
    },
    {
      pattern: /\b([A-Za-z][A-Za-z0-9_-]*)\s*\[\s*(?:"([^"\n]+)"|'([^'\n]+)'|([^\]\n]+?))\s*\]/g,
      shape: "rect",
    },
    {
      pattern: /\b([A-Za-z][A-Za-z0-9_-]*)\s*\{\s*(?:"([^"\n]+)"|'([^'\n]+)'|([^}\n]+))\s*\}/g,
      shape: "diamond",
    },
    {
      pattern: /\b([A-Za-z][A-Za-z0-9_-]*)\s*\(\s*\(\s*(?:"([^"\n]+)"|'([^'\n]+)'|([^)\n]+?))\s*\)\s*\)/g,
      shape: "ellipse",
    },
    {
      pattern: /\b([A-Za-z][A-Za-z0-9_-]*)\s*\(\s*(?:"([^"\n]+)"|'([^'\n]+)'|([^)\n]+?))\s*\)/g,
      shape: "roundRect",
    },
  ];
  const arrowPattern = /--\s+(.+?)\s+-->|-\.\s+(.+?)\s+\.->|==\s+(.+?)\s+==>|<-->|<==>|<-\.->|o--o|x--x|o-->|x-->|<--o|<--x|--o|--x|<--|-->|---|==>|-\.->/g;
  const lines = splitMermaidStatements(cleanedSource.slice(header[0].length));

  lines.forEach((line) => {
    const subgraph = line.match(/^subgraph\s+([A-Za-z][A-Za-z0-9_-]*)(?:\s*\[\s*(.+?)\s*\])?\s*$/i);
    if (subgraph) {
      rememberNode(subgraph[1], subgraph[2], "rect");
      return;
    }
    if (/^end\b/i.test(line)) return;
    if (/^(?:style|classDef|class|linkStyle|direction)\b/i.test(line)) return;

    let simplified = line;
    nodePatterns.forEach(({ pattern, shape }) => {
      simplified = simplified.replace(
        pattern,
        (_match, id: string, doubleQuoted: string, singleQuoted: string, plain: string) => {
          rememberNode(id, doubleQuoted || singleQuoted || plain, shape);
          return id;
        },
      );
    });
    const identifiers = (value: string) => Array.from(
      value.matchAll(/\b[A-Za-z][A-Za-z0-9_-]*\b/g),
      (match) => match[0],
    );
    const endpointIds = (value: string) => value
      .split("&")
      .map((part) => identifiers(part)[0])
      .filter((id): id is string => Boolean(id));
    const arrows = Array.from(simplified.matchAll(arrowPattern));
    if (!arrows.length) {
      const standaloneNodes = simplified.trim();
      if (/^[A-Za-z][A-Za-z0-9_-]*(?:\s*&\s*[A-Za-z][A-Za-z0-9_-]*)*$/.test(standaloneNodes)) {
        endpointIds(standaloneNodes).forEach((id) => rememberNode(id));
      }
      return;
    }

    let fromIds = endpointIds(simplified.slice(0, arrows[0].index));
    arrows.forEach((arrow, index) => {
      const start = (arrow.index || 0) + arrow[0].length;
      const end = arrows[index + 1]?.index ?? simplified.length;
      const segment = simplified.slice(start, end);
      const labelMatch = segment.match(/^\s*\|\s*"?([^|"]+)"?\s*\|/);
      const endpointText = labelMatch ? segment.slice(labelMatch[0].length) : segment;
      const toIds = endpointIds(endpointText);
      const inlineLabel = arrow.slice(1).find((value) => value !== undefined);
      const markers = mermaidEdgeMarkers(arrow[0]);
      fromIds.forEach((from) => {
        toIds.forEach((to) => {
          rememberNode(from);
          rememberNode(to);
          const edge: MermaidFlowEdge = {
            from,
            to,
            label: labelMatch
              ? decodeMermaidLabel(labelMatch[1])
              : inlineLabel
                ? decodeMermaidLabel(inlineLabel)
                : undefined,
            ...markers,
          };
          const edgeKey = JSON.stringify([
            edge.from,
            edge.to,
            edge.label ?? null,
            edge.markerStart ?? null,
            edge.markerEnd ?? null,
          ]);
          if (!edgeKeys.has(edgeKey)) {
            ensureElementBudget(1);
            edgeKeys.add(edgeKey);
            edges.push(edge);
          }
        });
      });
      fromIds = toIds;
    });
  });

  const renderedNodes = nodeOrder
    .map((id) => nodes.get(id))
    .filter((node): node is MermaidFlowNode => Boolean(node));
  return renderedNodes.length
    ? { direction, nodes: renderedNodes, edges }
    : null;
}

export function assertMermaidSourceWithinElementBudget(source: string): void {
  if (parseMermaidFlow(source)) return;
  if (splitMermaidStatements(source).length + 1 > MAX_DIAGRAM_ELEMENTS) {
    throw new Error("Diagram has too many elements to preview");
  }
}

function diagramLabelWidth(value: string): number {
  return Array.from(value).reduce(
    (width, character) => width + (character.codePointAt(0)! > 0xff ? 2 : 1),
    0,
  );
}

function wrapDiagramLabel(label: string, limit = 24): string {
  const lines: string[] = [];
  let line = "";
  const pushLongToken = (token: string) => {
    Array.from(token).forEach((character) => {
      if (line && diagramLabelWidth(`${line}${character}`) > limit) {
        lines.push(line);
        line = character;
      } else {
        line += character;
      }
    });
  };

  label.trim().split(/\s+/).filter(Boolean).forEach((word) => {
    const candidate = line ? `${line} ${word}` : word;
    if (diagramLabelWidth(candidate) <= limit) {
      line = candidate;
      return;
    }
    if (line) {
      lines.push(line);
      line = "";
    }
    pushLongToken(word);
  });
  if (line) lines.push(line);
  return lines.join("\n");
}

export function createDiagramDocumentFromMermaidSource(
  source: string,
  title = "Diagram",
): EditableDiagramDocument | null {
  const graph = parseMermaidFlow(source);
  if (!graph) return null;
  if (graph.nodes.length + graph.edges.length + 1 > MAX_DIAGRAM_ELEMENTS) {
    throw new Error("Diagram has too many elements to preview");
  }

  const layoutTraversal = new Map<string, string[]>();
  const backEdgeIndexes = new Set<number>();
  const hasLayoutPath = (start: string, target: string) => {
    const pending = [start];
    const seen = new Set<string>();
    while (pending.length) {
      const current = pending.pop()!;
      if (current === target) return true;
      if (seen.has(current)) continue;
      seen.add(current);
      (layoutTraversal.get(current) || []).forEach((next) => {
        if (!seen.has(next)) pending.push(next);
      });
    }
    return false;
  };
  graph.edges.forEach((edge, index) => {
    if (hasLayoutPath(edge.to, edge.from)) {
      backEdgeIndexes.add(index);
    } else {
      layoutTraversal.set(edge.from, [
        ...(layoutTraversal.get(edge.from) || []),
        edge.to,
      ]);
    }
  });

  const outgoing = new Map<string, string[]>();
  const indegree = new Map(graph.nodes.map((node) => [node.id, 0]));
  graph.edges.forEach((edge, index) => {
    if (backEdgeIndexes.has(index)) return;
    outgoing.set(edge.from, [...(outgoing.get(edge.from) || []), edge.to]);
    indegree.set(edge.to, (indegree.get(edge.to) || 0) + 1);
  });
  const remainingIndegree = new Map(indegree);
  const levels = new Map<string, number>();
  const queue = graph.nodes.filter((node) => !remainingIndegree.get(node.id)).map((node) => node.id);
  queue.forEach((id) => levels.set(id, 0));
  for (let index = 0; index < queue.length; index += 1) {
    const current = queue[index];
    const nextLevel = (levels.get(current) || 0) + 1;
    (outgoing.get(current) || []).forEach((next) => {
      levels.set(next, Math.max(levels.get(next) ?? 0, nextLevel));
      const nextIndegree = (remainingIndegree.get(next) || 0) - 1;
      remainingIndegree.set(next, nextIndegree);
      if (nextIndegree === 0) queue.push(next);
    });
  }
  graph.nodes.forEach((node) => {
    if (!levels.has(node.id)) levels.set(node.id, 0);
  });

  const rows = new Map<number, MermaidFlowNode[]>();
  graph.nodes.forEach((node) => {
    const level = levels.get(node.id) || 0;
    rows.set(level, [...(rows.get(level) || []), node]);
  });
  const nodeWidth = 260;
  const nodeHeight = 112;
  const crossGap = 56;
  const levelGap = 112;
  const stageStartX = 80;
  const stageStartY = 150;
  const widestRow = Math.max(1, ...Array.from(rows.values(), (row) => row.length));
  const maxLevel = Math.max(0, ...levels.values());
  const isVertical = graph.direction === "TD" || graph.direction === "BT";
  const isReverse = graph.direction === "BT" || graph.direction === "RL";
  const maxCrossSize = isVertical
    ? widestRow * nodeWidth + (widestRow - 1) * crossGap
    : widestRow * nodeHeight + (widestRow - 1) * crossGap;
  const canvasWidth = isVertical
    ? Math.max(800, 160 + maxCrossSize)
    : Math.max(800, 160 + (maxLevel + 1) * nodeWidth + maxLevel * levelGap);
  const canvasHeight = isVertical
    ? Math.max(600, stageStartY + (maxLevel + 1) * nodeHeight + maxLevel * levelGap + 80)
    : Math.max(600, stageStartY + maxCrossSize + 80);
  const elementIds = new Map<string, string>();
  const titleWidth = Math.min(1040, canvasWidth - 160);
  const elements: DiagramElement[] = [
    {
      id: "mermaid_preview_title",
      kind: "text",
      x: (canvasWidth - titleWidth) / 2,
      y: 48,
      w: titleWidth,
      h: 48,
      text: title,
      textStyle: {
        fontFamily: DEFAULT_THEME.fontFamily,
        fontSize: 28,
        fontWeight: 700,
        color: DEFAULT_THEME.palette.text,
        align: "center",
      },
    },
  ];

  Array.from(rows.entries()).sort(([left], [right]) => left - right).forEach(([level, row]) => {
    const crossSize = isVertical
      ? row.length * nodeWidth + (row.length - 1) * crossGap
      : row.length * nodeHeight + (row.length - 1) * crossGap;
    const visualLevel = isReverse ? maxLevel - level : level;
    row.forEach((node, index) => {
      const elementId = `mermaid_node_${node.id}`;
      elementIds.set(node.id, elementId);
      const x = isVertical
        ? (canvasWidth - crossSize) / 2 + index * (nodeWidth + crossGap)
        : stageStartX + visualLevel * (nodeWidth + levelGap);
      const y = isVertical
        ? stageStartY + visualLevel * (nodeHeight + levelGap)
        : stageStartY + (maxCrossSize - crossSize) / 2 + index * (nodeHeight + crossGap);
      elements.push({
        id: elementId,
        kind: "shape",
        shape: node.shape,
        x,
        y,
        w: nodeWidth,
        h: nodeHeight,
        fill: level % 2 === 0 ? DEFAULT_THEME.palette.blueFill : DEFAULT_THEME.palette.cream,
        stroke: DEFAULT_THEME.palette.line,
        strokeWidth: 2,
        radius: 16,
        text: wrapDiagramLabel(node.label),
        textStyle: {
          fontFamily: DEFAULT_THEME.fontFamily,
          fontSize: 18,
          fontWeight: 700,
          color: DEFAULT_THEME.palette.text,
          align: "center",
        },
      });
    });
  });
  const flowAnchors: Record<MermaidFlowDirection, { from: DiagramAnchor; to: DiagramAnchor }> = {
    TD: { from: "bottom", to: "top" },
    BT: { from: "top", to: "bottom" },
    LR: { from: "right", to: "left" },
    RL: { from: "left", to: "right" },
  };
  const anchors = flowAnchors[graph.direction];
  graph.edges.forEach((edge, index) => {
    const fromId = elementIds.get(edge.from);
    const toId = elementIds.get(edge.to);
    if (!fromId || !toId) return;
    elements.push({
      id: `mermaid_connector_${index + 1}`,
      kind: "connector",
      from: { bind: { elementId: fromId, anchor: anchors.from } },
      to: { bind: { elementId: toId, anchor: anchors.to } },
      routing: (levels.get(edge.to) || 0) > (levels.get(edge.from) || 0) ? "straight" : "curve",
      stroke: DEFAULT_THEME.palette.accent,
      strokeWidth: 4,
      // Keep v1 readers usable during rolling deploys: they only understand
      // the legacy booleans, while current readers prefer the marker fields.
      arrowStart: Boolean(edge.markerStart),
      arrowEnd: Boolean(edge.markerEnd),
      markerStart: edge.markerStart,
      markerEnd: edge.markerEnd,
      label: edge.label ? wrapDiagramLabel(edge.label, 22) : undefined,
      textStyle: edge.label ? {
        fontFamily: DEFAULT_THEME.fontFamily,
        fontSize: 14,
        fontWeight: 600,
        color: DEFAULT_THEME.palette.text,
        align: "center",
      } : undefined,
    });
  });

  return {
    version: DIAGRAM_VERSION,
    id: "mermaid_preview",
    title,
    prompt: source,
    canvas: {
      width: canvasWidth,
      height: canvasHeight,
      unit: "px",
      originX: 0,
      originY: 0,
    },
    theme: DEFAULT_THEME,
    elements,
    groups: [{ id: "mermaid_preview_group", label: "Mermaid flow", elementIds: Array.from(elementIds.values()) }],
  };
}

type DiagramJsonRecord = Record<string, unknown>;

const DIAGRAM_SHAPE_KINDS = new Set<string>([
  "rect", "roundRect", "ellipse", "diamond", "triangle", "hexagon",
  "parallelogram", "trapezoid", "cylinder", "document", "rightArrow", "downArrow",
]);
const DIAGRAM_ANCHORS = new Set<string>(["top", "right", "bottom", "left", "center"]);
const DIAGRAM_ROUTINGS = new Set<string>(["straight", "elbow", "orthogonal", "curve"]);
const DIAGRAM_MARKERS = new Set<string>(["arrow", "circle", "cross"]);
const DIAGRAM_DASHES = new Set<string>(["none", "dash", "dot"]);
const DIAGRAM_FONT_STYLES = new Set<string>(["normal", "italic"]);
const DIAGRAM_TEXT_ALIGNMENTS = new Set<string>(["left", "center", "right"]);
const DIAGRAM_UNITS = new Set<string>(["px", "in"]);
const DIAGRAM_CONSTRAINT_TYPES = new Set<string>([
  "alignX", "alignY", "distributeX", "distributeY",
]);

function isDiagramJsonRecord(value: unknown): value is DiagramJsonRecord {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function isOptionalString(value: unknown): boolean {
  return value === undefined || typeof value === "string";
}

function isOptionalBoolean(value: unknown): boolean {
  return value === undefined || typeof value === "boolean";
}

function isOptionalFiniteNumber(value: unknown): boolean {
  return value === undefined || (typeof value === "number" && Number.isFinite(value));
}

function isOptionalDiagramCanvasDimension(value: unknown): boolean {
  return value === undefined || (
    typeof value === "number"
    && Number.isFinite(value)
    && value > 0
    && value <= MAX_DIAGRAM_CANVAS_DIMENSION
  );
}

function isOptionalEnum(value: unknown, values: ReadonlySet<string>): boolean {
  return value === undefined || (typeof value === "string" && values.has(value));
}

function isDiagramTextStyle(value: unknown): boolean {
  if (value === undefined) return true;
  if (!isDiagramJsonRecord(value)) return false;
  return isOptionalString(value.fontFamily)
    && isOptionalFiniteNumber(value.fontSize)
    && isOptionalFiniteNumber(value.fontWeight)
    && isOptionalEnum(value.fontStyle, DIAGRAM_FONT_STYLES)
    && isOptionalString(value.color)
    && isOptionalEnum(value.align, DIAGRAM_TEXT_ALIGNMENTS);
}

function isDiagramElementBounds(value: DiagramJsonRecord): boolean {
  return isOptionalFiniteNumber(value.x)
    && isOptionalFiniteNumber(value.y)
    && isOptionalFiniteNumber(value.w)
    && isOptionalFiniteNumber(value.h);
}

function isDiagramEndpoint(value: unknown): boolean {
  if (!isDiagramJsonRecord(value)) return false;
  if (!isOptionalFiniteNumber(value.x) || !isOptionalFiniteNumber(value.y)) return false;
  if (value.bind === undefined) return true;
  return isDiagramJsonRecord(value.bind)
    && typeof value.bind.elementId === "string"
    && DIAGRAM_ANCHORS.has(String(value.bind.anchor));
}

function isDiagramElement(value: unknown): boolean {
  if (!isDiagramJsonRecord(value)) return false;
  if (!isOptionalString(value.id)
    || !isOptionalString(value.name)
    || !isOptionalBoolean(value.locked)) return false;

  if (value.kind === "connector") {
    return isDiagramEndpoint(value.from)
      && isDiagramEndpoint(value.to)
      && isOptionalEnum(value.routing, DIAGRAM_ROUTINGS)
      && isOptionalString(value.stroke)
      && isOptionalFiniteNumber(value.strokeWidth)
      && isOptionalEnum(value.strokeDash, DIAGRAM_DASHES)
      && isOptionalBoolean(value.arrowStart)
      && isOptionalBoolean(value.arrowEnd)
      && isOptionalEnum(value.markerStart, DIAGRAM_MARKERS)
      && isOptionalEnum(value.markerEnd, DIAGRAM_MARKERS)
      && isOptionalString(value.label)
      && isDiagramTextStyle(value.textStyle)
      && (value.controlPoint === undefined
        || (isDiagramJsonRecord(value.controlPoint)
          && isOptionalFiniteNumber(value.controlPoint.x)
          && isOptionalFiniteNumber(value.controlPoint.y)));
  }

  if (value.kind === "text") {
    return isDiagramElementBounds(value)
      && isOptionalString(value.text)
      && isDiagramTextStyle(value.textStyle);
  }

  if (value.kind !== "shape") return false;
  return isDiagramElementBounds(value)
    && isOptionalEnum(value.shape, DIAGRAM_SHAPE_KINDS)
    && isOptionalString(value.fill)
    && isOptionalString(value.stroke)
    && isOptionalFiniteNumber(value.strokeWidth)
    && isOptionalEnum(value.strokeDash, DIAGRAM_DASHES)
    && isOptionalFiniteNumber(value.opacity)
    && isOptionalFiniteNumber(value.radius)
    && isOptionalString(value.text)
    && isDiagramTextStyle(value.textStyle);
}

function isDiagramCanvas(value: unknown): boolean {
  return isDiagramJsonRecord(value)
    && isOptionalDiagramCanvasDimension(value.width)
    && isOptionalDiagramCanvasDimension(value.height)
    && isOptionalEnum(value.unit, DIAGRAM_UNITS)
    && isOptionalFiniteNumber(value.originX)
    && isOptionalFiniteNumber(value.originY);
}

function isDiagramTheme(value: unknown): boolean {
  if (value === undefined) return true;
  if (!isDiagramJsonRecord(value)
    || !isOptionalString(value.fontFamily)
    || !isOptionalString(value.labelFontFamily)) return false;
  return value.palette === undefined
    || (isDiagramJsonRecord(value.palette)
      && Object.values(value.palette).every((color) => typeof color === "string"));
}

function isDiagramGroups(value: unknown): boolean {
  return value === undefined || (Array.isArray(value) && value.every((group) => (
    isDiagramJsonRecord(group)
    && typeof group.id === "string"
    && isOptionalString(group.label)
    && Array.isArray(group.elementIds)
    && group.elementIds.every((elementId) => typeof elementId === "string")
  )));
}

function isDiagramConstraints(value: unknown): boolean {
  return value === undefined || (Array.isArray(value) && value.every((constraint) => (
    isDiagramJsonRecord(constraint)
    && typeof constraint.type === "string"
    && DIAGRAM_CONSTRAINT_TYPES.has(constraint.type)
    && Array.isArray(constraint.elementIds)
    && constraint.elementIds.every((elementId) => typeof elementId === "string")
  )));
}

function hasValidDiagramElementRelationships(elements: DiagramJsonRecord[]): boolean {
  const elementIds = new Set<string>();
  const endpointTargetIds = new Set<string>();
  for (const element of elements) {
    if (element.id === undefined) continue;
    const elementId = String(element.id);
    if (!elementId.trim() || elementIds.has(elementId)) return false;
    elementIds.add(elementId);
    if (element.kind !== "connector") endpointTargetIds.add(elementId);
  }

  return elements.every((element) => {
    if (element.kind !== "connector") return true;
    return [element.from, element.to].every((endpoint) => {
      if (!isDiagramJsonRecord(endpoint) || endpoint.bind === undefined) return true;
      return isDiagramJsonRecord(endpoint.bind)
        && typeof endpoint.bind.elementId === "string"
        && endpointTargetIds.has(endpoint.bind.elementId);
    });
  });
}

export function isDiagramDocument(value: unknown): boolean {
  if (!isDiagramJsonRecord(value)) return false;
  return value.version === DIAGRAM_VERSION
    && isOptionalString(value.id)
    && isOptionalString(value.title)
    && isOptionalString(value.prompt)
    && isDiagramCanvas(value.canvas)
    && isDiagramTheme(value.theme)
    && Array.isArray(value.elements)
    && value.elements.every(isDiagramElement)
    && hasValidDiagramElementRelationships(value.elements)
    && isDiagramGroups(value.groups)
    && isDiagramConstraints(value.constraints);
}

export function parseDiagramDocument(content: string, fallbackTitle = "Untitled diagram"): EditableDiagramDocument {
  if (!content.trim()) return createDefaultDiagramDocument(fallbackTitle);
  let parsed: unknown;
  try {
    parsed = JSON.parse(content) as unknown;
  } catch {
    throw new Error("Diagram JSON is invalid");
  }
  if (!isDiagramDocument(parsed)) {
    throw new Error("This file is not an editable diagram document");
  }
  return normalizeDiagramDocument(parsed as EditableDiagramDocument, fallbackTitle);
}

export function serializeDiagramDocument(document: EditableDiagramDocument): string {
  return `${JSON.stringify(normalizeDiagramDocument(document), null, 2)}\n`;
}

export function normalizeDiagramDocument(
  document: EditableDiagramDocument,
  fallbackTitle = "Untitled diagram",
): EditableDiagramDocument {
  if ((document.elements || []).length > MAX_DIAGRAM_ELEMENTS) {
    throw new Error("Diagram has too many elements to preview");
  }
  const canvas = {
    width: clampNumber(document.canvas?.width, 320, MAX_DIAGRAM_CANVAS_DIMENSION, 1200),
    height: clampNumber(document.canvas?.height, 180, MAX_DIAGRAM_CANVAS_DIMENSION, 675),
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
  const normalized: EditableDiagramDocument = {
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
  return normalized;
}

function normalizeElement(element: DiagramElement, canvas: DiagramCanvasSpec): DiagramElement {
  if (element.kind === "connector") {
    const normalizeMarker = (
      marker: DiagramConnectorMarker | undefined,
      legacyArrow: boolean | undefined,
    ): DiagramConnectorMarker | undefined => (
      marker === "arrow" || marker === "circle" || marker === "cross"
        ? marker
        : legacyArrow
          ? "arrow"
          : undefined
    );
    const markerStart = normalizeMarker(element.markerStart, element.arrowStart);
    const markerEnd = normalizeMarker(element.markerEnd, element.arrowEnd);
    return {
      ...element,
      id: element.id || createDiagramId("conn"),
      stroke: element.stroke || DEFAULT_THEME.palette.line,
      strokeWidth: clampNumber(element.strokeWidth, 1, 24, 2),
      routing: element.routing || "straight",
      arrowStart: Boolean(markerStart),
      arrowEnd: Boolean(markerEnd),
      markerStart,
      markerEnd,
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
    opacity: element.opacity === undefined
      ? undefined
      : clampNumber(element.opacity, 0, 1, 1),
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
  boundsById?: ReadonlyMap<string, DiagramBounds>,
): { x: number; y: number } {
  if (endpoint.bind) {
    const target = boundsById
      ? undefined
      : elements.find((element) => element.id === endpoint.bind?.elementId);
    const bounds = boundsById
      ? boundsById.get(endpoint.bind.elementId) || null
      : target
        ? getElementBounds(target)
        : null;
    if (bounds) return getAnchorPoint(bounds, endpoint.bind.anchor);
  }
  return { x: endpoint.x ?? 0, y: endpoint.y ?? 0 };
}

export function cloneDiagramDocument(document: EditableDiagramDocument): EditableDiagramDocument {
  return JSON.parse(JSON.stringify(document)) as EditableDiagramDocument;
}
