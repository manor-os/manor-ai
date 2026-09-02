import {
  DIAGRAM_VERSION,
  type DiagramAnchor,
  type DiagramConnectorMarker,
  type DiagramElement,
  type DiagramShapeKind,
  type EditableDiagramDocument,
} from "./schema";

type DrawioCell = {
  id: string;
  parentId: string;
  isGroup: boolean;
  value: string;
  style: Record<string, string>;
  x: number;
  y: number;
  w: number;
  h: number;
  shape: DiagramShapeKind;
  isTextOnly: boolean;
};

const MAX_DRAWIO_SOURCE_CHARS = 40 * 1024 * 1024;
const MAX_DRAWIO_COMPRESSED_BYTES = 10 * 1024 * 1024;
const MAX_DRAWIO_INFLATED_BYTES = 40 * 1024 * 1024;
const MAX_DRAWIO_PAGES = 100;
const MAX_DRAWIO_CELLS = 10_000;

function parseXml(value: string): Document {
  const document = new DOMParser().parseFromString(value, "application/xml");
  if (document.querySelector("parsererror")) throw new Error("Invalid Draw.io XML");
  return document;
}

function decodeDrawioLabel(value: string): string {
  const withLines = value.replace(/<br\s*\/?\s*>/gi, "\n");
  const document = new DOMParser().parseFromString(`<body>${withLines}</body>`, "text/html");
  return (document.body.textContent || "")
    .split("\n")
    .map((line) => line.replace(/\s+/g, " ").trim())
    .filter(Boolean)
    .join("\n");
}

function parseStyle(value: string): Record<string, string> {
  return Object.fromEntries(
    value.split(";").map((entry) => entry.trim()).filter(Boolean).map((entry) => {
      const separator = entry.indexOf("=");
      return separator < 0
        ? [entry, "1"]
        : [entry.slice(0, separator), entry.slice(separator + 1)];
    }),
  );
}

function safeColor(value: string | undefined, fallback: string): string {
  const color = String(value || "").trim();
  return /^(?:#[0-9a-f]{3,8}|rgba?\([\d\s.,%]+\)|[a-z]+)$/i.test(color)
    ? color
    : fallback;
}

function drawioNumber(value: string | null | undefined, fallback: number): number {
  if (value == null || value.trim() === "") return fallback;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function drawioShape(style: Record<string, string>): DiagramShapeKind | null {
  const shape = (style.shape || "").toLowerCase();
  if (style.ellipse === "1" || shape === "ellipse") return "ellipse";
  if (style.rhombus === "1" || shape === "rhombus") return "diamond";
  if (shape === "cylinder" || shape === "cylinder3") return "cylinder";
  if (shape === "hexagon") return "hexagon";
  if (shape === "parallelogram") return "parallelogram";
  if (shape === "document") return "document";
  if (!shape || shape === "rect" || shape === "rectangle") {
    return style.rounded === "1" ? "roundRect" : "rect";
  }
  return null;
}

export function drawioUnsupportedVertexStyle(
  styleValue: string,
  labelValue = "",
): string | null {
  const style = parseStyle(styleValue);
  const shape = (style.shape || "").toLowerCase();
  if (style.image || shape === "image" || /<(?:img|svg)\b/i.test(labelValue)) {
    return "embedded images";
  }
  if (style.swimlane === "1" || shape.includes("swimlane")) return "swimlanes";
  if (
    (style.rotation && Number(style.rotation) !== 0)
    || style.flipH === "1"
    || style.flipV === "1"
  ) {
    return "rotated or flipped shapes";
  }
  if (style.gradientColor && style.gradientColor.toLowerCase() !== "none") {
    return "gradient fills";
  }
  if ((drawioNumber(style.fontStyle, 0) & 4) !== 0) return "underlined text";
  if (style.align && !["left", "center", "right"].includes(style.align)) {
    return `text alignment \"${style.align}\"`;
  }
  if (!drawioShape(style)) return `shape \"${style.shape || "unknown"}\"`;
  return null;
}

function drawioMarker(
  value: string | undefined,
  fallback?: DiagramConnectorMarker,
): DiagramConnectorMarker | undefined {
  const marker = String(value || "").trim().toLowerCase();
  if (!marker) return fallback;
  if (marker === "none") return undefined;
  if (["classic", "classicthin", "block", "blockthin", "open", "openthin"].includes(marker)) {
    return "arrow";
  }
  if (marker === "oval") return "circle";
  if (marker === "cross") return "cross";
  throw new Error(`Draw.io preview does not support connector marker \"${value}\"`);
}

function drawioAnchor(
  style: Record<string, string>,
  prefix: "entry" | "exit",
  fallback: DiagramAnchor,
): DiagramAnchor {
  const xValue = style[`${prefix}X`];
  const yValue = style[`${prefix}Y`];
  if (xValue == null && yValue == null) return fallback;
  const x = Number(xValue);
  const y = Number(yValue);
  if (!Number.isFinite(x) || !Number.isFinite(y)) {
    throw new Error("Draw.io preview does not support incomplete connector anchors");
  }
  const closeTo = (value: number, expected: number) => Math.abs(value - expected) < 0.02;
  if (closeTo(x, 0) && closeTo(y, 0.5)) return "left";
  if (closeTo(x, 1) && closeTo(y, 0.5)) return "right";
  if (closeTo(x, 0.5) && closeTo(y, 0)) return "top";
  if (closeTo(x, 0.5) && closeTo(y, 1)) return "bottom";
  if (closeTo(x, 0.5) && closeTo(y, 0.5)) return "center";
  throw new Error("Draw.io preview does not support custom connector anchors");
}

function drawioRouting(style: Record<string, string>, geometry?: Element): "straight" | "orthogonal" {
  if (geometry?.querySelector("mxPoint")) {
    throw new Error("Draw.io preview does not support connector waypoints");
  }
  if (style.curved === "1") {
    throw new Error("Draw.io preview does not support curved connectors");
  }
  const edgeStyle = String(style.edgeStyle || "").toLowerCase();
  if (!edgeStyle) return "straight";
  if (edgeStyle.includes("orthogonal") || edgeStyle.includes("elbow")) return "orthogonal";
  throw new Error(`Draw.io preview does not support connector style \"${style.edgeStyle}\"`);
}

type DrawioPageSource = { title: string; modelXml: string };
type DrawioInflateBudget = { inflatedBytes: number };
type DrawioRenderBudget = { cellCount: number };

async function decodeDrawioDiagram(
  diagram: Element,
  budget: DrawioInflateBudget,
): Promise<string> {
  const nestedModel = Array.from(diagram.children).find((child) => child.tagName === "mxGraphModel");
  if (nestedModel) return nestedModel.outerHTML;

  let payload = (diagram.textContent || "").trim();
  if (!payload) throw new Error("Draw.io diagram data is empty");
  if (payload.startsWith("%3C")) payload = decodeURIComponent(payload);
  if (payload.startsWith("<")) return payload;

  const normalized = payload.replace(/-/g, "+").replace(/_/g, "/").replace(/\s+/g, "");
  if (normalized.length > Math.ceil(MAX_DRAWIO_COMPRESSED_BYTES * 4 / 3) + 4) {
    throw new Error("Compressed Draw.io data is too large to preview");
  }
  const binary = atob(normalized);
  const bytes = Uint8Array.from(binary, (character) => character.charCodeAt(0));
  if (bytes.byteLength > MAX_DRAWIO_COMPRESSED_BYTES) {
    throw new Error("Compressed Draw.io data is too large to preview");
  }

  const { Inflate } = await import("pako");
  const chunks: Uint8Array[] = [];
  let pageInflatedBytes = 0;
  const inflator = new Inflate({ raw: true, chunkSize: 64 * 1024 });
  inflator.onData = (chunk) => {
    pageInflatedBytes += chunk.byteLength;
    budget.inflatedBytes += chunk.byteLength;
    if (budget.inflatedBytes > MAX_DRAWIO_INFLATED_BYTES) {
      throw new Error("Inflated Draw.io data is too large to preview");
    }
    chunks.push(chunk);
  };
  inflator.push(bytes, true);
  if (inflator.err) throw new Error(inflator.msg || "Invalid compressed Draw.io data");

  const inflatedData = new Uint8Array(pageInflatedBytes);
  let offset = 0;
  chunks.forEach((chunk) => {
    inflatedData.set(chunk, offset);
    offset += chunk.byteLength;
  });
  const inflated = new TextDecoder().decode(inflatedData);
  try {
    return decodeURIComponent(inflated);
  } catch {
    return inflated;
  }
}

async function drawioModelXmlPages(content: string, title: string): Promise<DrawioPageSource[]> {
  if (content.length > MAX_DRAWIO_SOURCE_CHARS) {
    throw new Error("Draw.io source is too large to preview");
  }
  const sourceDocument = parseXml(content);
  const root = sourceDocument.documentElement;
  if (root?.tagName === "mxGraphModel") {
    return [{ title, modelXml: root.outerHTML }];
  }

  const diagrams = Array.from(sourceDocument.querySelectorAll("diagram"));
  if (!diagrams.length) throw new Error("Draw.io diagram data is missing");
  if (diagrams.length > MAX_DRAWIO_PAGES) {
    throw new Error("Draw.io diagram has too many pages to preview");
  }
  const budget: DrawioInflateBudget = { inflatedBytes: 0 };
  return Promise.all(diagrams.map(async (diagram, index) => ({
    title: diagram.getAttribute("name")?.trim() || `Page ${index + 1}`,
    modelXml: await decodeDrawioDiagram(diagram, budget),
  })));
}

function connectorAnchors(from: DrawioCell, to: DrawioCell): { from: DiagramAnchor; to: DiagramAnchor } {
  const dx = (to.x + to.w / 2) - (from.x + from.w / 2);
  const dy = (to.y + to.h / 2) - (from.y + from.h / 2);
  if (Math.abs(dx) >= Math.abs(dy)) {
    return dx >= 0
      ? { from: "right", to: "left" }
      : { from: "left", to: "right" };
  }
  return dy >= 0
    ? { from: "bottom", to: "top" }
    : { from: "top", to: "bottom" };
}

function drawioCellIsVisible(
  cell: Element,
  cellElementsById: Map<string, Element>,
  visibilityByCellId: Map<string, boolean>,
): boolean {
  const visited = new Set<string>();
  const path: string[] = [];
  let current: Element | undefined = cell;
  let visible = true;
  while (current) {
    if (
      current.getAttribute("visible") === "0"
      || drawioCellWrapper(current)?.getAttribute("visible") === "0"
    ) {
      visible = false;
      break;
    }
    const currentId = drawioCellId(current);
    if (currentId) {
      const cached = visibilityByCellId.get(currentId);
      if (cached !== undefined) {
        visible = cached;
        break;
      }
      if (visited.has(currentId)) break;
      visited.add(currentId);
      path.push(currentId);
    }
    const parentId: string = drawioCellAttribute(current, "parent") || "";
    current = parentId ? cellElementsById.get(parentId) : undefined;
  }
  path.forEach((id) => visibilityByCellId.set(id, visible));
  return visible;
}

function drawioCellWrapper(cell: Element): Element | null {
  const wrapper = cell.parentElement;
  if (!wrapper) return null;
  const tagName = wrapper.tagName.toLowerCase();
  return tagName === "object" || tagName === "userobject" ? wrapper : null;
}

function drawioCellId(cell: Element): string {
  return drawioCellWrapper(cell)?.getAttribute("id") || cell.getAttribute("id") || "";
}

function drawioCellAttribute(cell: Element, name: string): string | null {
  const directValue = cell.getAttribute(name);
  if (directValue) return directValue;
  const wrapper = drawioCellWrapper(cell);
  if (!wrapper) return directValue;
  if (name === "value") {
    return wrapper.getAttribute("label") || wrapper.getAttribute("value") || directValue;
  }
  return wrapper.getAttribute(name) || directValue;
}

function createDiagramDocumentFromDrawioModel(
  modelXml: string,
  title: string,
  pageIndex: number,
  budget: DrawioRenderBudget,
): EditableDiagramDocument {
  const modelDocument = parseXml(modelXml);
  const cellElements = Array.from(modelDocument.querySelectorAll("mxCell"));
  budget.cellCount += cellElements.length;
  if (budget.cellCount > MAX_DRAWIO_CELLS) {
    throw new Error("Draw.io diagram has too many elements to preview");
  }
  const cellElementsById = new Map(
    cellElements
      .map((cell) => [drawioCellId(cell), cell] as const)
      .filter(([id]) => Boolean(id)),
  );
  const visibilityByCellId = new Map<string, boolean>();
  const cellsById = new Map<string, DrawioCell>();

  cellElements.filter((cell) => (
    drawioCellAttribute(cell, "vertex") === "1"
    && drawioCellIsVisible(cell, cellElementsById, visibilityByCellId)
  )).forEach((cell) => {
    const geometry = Array.from(cell.children).find((child) => child.tagName === "mxGeometry");
    if (!geometry) throw new Error("Draw.io preview does not support vertices without geometry");
    const id = drawioCellId(cell) || `cell_${cellsById.size + 1}`;
    const styleValue = drawioCellAttribute(cell, "style") || "";
    const style = parseStyle(styleValue);
    const isGroup = style.group === "1" || style.shape?.toLowerCase() === "group";
    const value = drawioCellAttribute(cell, "value") || "";
    if (!isGroup) {
      const unsupportedFeature = drawioUnsupportedVertexStyle(styleValue, value);
      if (unsupportedFeature) {
        throw new Error(`Draw.io preview does not support ${unsupportedFeature}`);
      }
    }
    cellsById.set(id, {
      id,
      parentId: drawioCellAttribute(cell, "parent") || "",
      isGroup,
      value: decodeDrawioLabel(value),
      style,
      x: drawioNumber(geometry.getAttribute("x"), 0),
      y: drawioNumber(geometry.getAttribute("y"), 0),
      w: Math.max(24, drawioNumber(geometry.getAttribute("width"), 120)),
      h: Math.max(24, drawioNumber(geometry.getAttribute("height"), 60)),
      shape: drawioShape(style) || "rect",
      isTextOnly: style.fillColor === "none" && style.strokeColor === "none",
    });
  });

  const positionCache = new Map<string, { x: number; y: number }>();
  const absolutePosition = (cell: DrawioCell, visiting = new Set<string>()): { x: number; y: number } => {
    const cached = positionCache.get(cell.id);
    if (cached) return cached;
    if (visiting.has(cell.id)) return { x: cell.x, y: cell.y };
    visiting.add(cell.id);
    const parent = cellsById.get(cell.parentId);
    const parentPosition = parent ? absolutePosition(parent, visiting) : { x: 0, y: 0 };
    const position = { x: parentPosition.x + cell.x, y: parentPosition.y + cell.y };
    positionCache.set(cell.id, position);
    return position;
  };
  cellsById.forEach((cell) => Object.assign(cell, absolutePosition(cell)));

  const elementIdByCellId = new Map<string, string>();
  const elements: DiagramElement[] = [];
  Array.from(cellsById.values()).filter((cell) => !cell.isGroup).forEach((cell, index) => {
    const elementId = `drawio_node_${index + 1}`;
    elementIdByCellId.set(cell.id, elementId);
    if (cell.isTextOnly) {
      elements.push({
        id: elementId,
        kind: "text",
        x: cell.x,
        y: cell.y,
        w: cell.w,
        h: cell.h,
        text: cell.value,
        textStyle: {
          fontFamily: "Inter, ui-sans-serif, system-ui, sans-serif",
          fontSize: Math.max(8, Math.min(48, drawioNumber(cell.style.fontSize, 14))),
          fontWeight: (drawioNumber(cell.style.fontStyle, 0) & 1) !== 0 ? 700 : 500,
          fontStyle: (drawioNumber(cell.style.fontStyle, 0) & 2) !== 0 ? "italic" : "normal",
          color: safeColor(cell.style.fontColor, "#292524"),
          align: cell.style.align === "left" || cell.style.align === "right"
            ? cell.style.align
            : "center",
        },
      });
      return;
    }
    elements.push({
      id: elementId,
      kind: "shape",
      shape: cell.shape,
      x: cell.x,
      y: cell.y,
      w: cell.w,
      h: cell.h,
      fill: safeColor(cell.style.fillColor, "#ffffff"),
      stroke: safeColor(cell.style.strokeColor, "#57534e"),
      strokeWidth: Math.max(1, Math.min(12, drawioNumber(cell.style.strokeWidth, 2))),
      strokeDash: cell.style.dashed === "1" ? "dash" : "none",
      radius: cell.style.rounded === "1" ? 12 : 0,
      text: cell.value,
      textStyle: {
        fontFamily: "Inter, ui-sans-serif, system-ui, sans-serif",
        fontSize: Math.max(8, Math.min(48, drawioNumber(cell.style.fontSize, 14))),
        fontWeight: (drawioNumber(cell.style.fontStyle, 0) & 1) !== 0 ? 700 : 500,
        fontStyle: (drawioNumber(cell.style.fontStyle, 0) & 2) !== 0 ? "italic" : "normal",
        color: safeColor(cell.style.fontColor, "#292524"),
        align: cell.style.align === "left" || cell.style.align === "right"
          ? cell.style.align
          : "center",
      },
    });
  });

  cellElements.filter((cell) => {
    if (
      drawioCellAttribute(cell, "edge") !== "1"
      || !drawioCellIsVisible(cell, cellElementsById, visibilityByCellId)
    ) {
      return false;
    }
    return [drawioCellAttribute(cell, "source"), drawioCellAttribute(cell, "target")].every((terminalId) => {
      const terminal = terminalId ? cellElementsById.get(terminalId) : undefined;
      return !terminal || drawioCellIsVisible(terminal, cellElementsById, visibilityByCellId);
    });
  }).forEach((edge, index) => {
    const sourceCell = cellsById.get(drawioCellAttribute(edge, "source") || "");
    const targetCell = cellsById.get(drawioCellAttribute(edge, "target") || "");
    if (!sourceCell || !targetCell) {
      throw new Error("Draw.io preview does not support unconnected connectors");
    }
    const fromId = elementIdByCellId.get(sourceCell.id);
    const toId = elementIdByCellId.get(targetCell.id);
    if (!fromId || !toId) {
      throw new Error("Draw.io preview does not support connectors bound to groups");
    }
    const style = parseStyle(drawioCellAttribute(edge, "style") || "");
    const geometry = Array.from(edge.children).find((child) => child.tagName === "mxGeometry");
    const anchors = connectorAnchors(sourceCell, targetCell);
    const markerStart = drawioMarker(style.startArrow);
    const markerEnd = drawioMarker(style.endArrow, "arrow");
    const label = decodeDrawioLabel(drawioCellAttribute(edge, "value") || "");
    elements.push({
      id: `drawio_connector_${index + 1}`,
      kind: "connector",
      from: { bind: { elementId: fromId, anchor: drawioAnchor(style, "exit", anchors.from) } },
      to: { bind: { elementId: toId, anchor: drawioAnchor(style, "entry", anchors.to) } },
      routing: drawioRouting(style, geometry),
      stroke: safeColor(style.strokeColor, "#57534e"),
      strokeWidth: Math.max(1, Math.min(12, drawioNumber(style.strokeWidth, 2))),
      strokeDash: style.dashed === "1" ? "dash" : "none",
      arrowStart: Boolean(markerStart),
      arrowEnd: Boolean(markerEnd),
      markerStart,
      markerEnd,
      label: label || undefined,
      textStyle: label ? {
        fontFamily: "Inter, ui-sans-serif, system-ui, sans-serif",
        fontSize: 13,
        fontWeight: 600,
        color: "#292524",
        align: "center",
      } : undefined,
    });
  });

  const visibleElements = elements.filter((element) => element.kind !== "connector");
  const minX = visibleElements.length
    ? Math.min(...visibleElements.map((element) => element.x))
    : 0;
  const minY = visibleElements.length
    ? Math.min(...visibleElements.map((element) => element.y))
    : 0;
  const maxX = visibleElements.length
    ? Math.max(...visibleElements.map((element) => element.x + element.w))
    : Math.max(320, drawioNumber(modelDocument.documentElement.getAttribute("pageWidth"), 800));
  const maxY = visibleElements.length
    ? Math.max(...visibleElements.map((element) => element.y + element.h))
    : Math.max(180, drawioNumber(modelDocument.documentElement.getAttribute("pageHeight"), 600));

  return {
    version: DIAGRAM_VERSION,
    id: `drawio_preview_${pageIndex + 1}`,
    title,
    prompt: modelXml,
    canvas: {
      width: Math.max(320, maxX - minX + 160),
      height: Math.max(180, maxY - minY + 160),
      unit: "px",
      originX: minX - 80,
      originY: minY - 80,
    },
    theme: {
      fontFamily: "Inter, ui-sans-serif, system-ui, sans-serif",
      labelFontFamily: "Inter, ui-sans-serif, system-ui, sans-serif",
      palette: {},
    },
    elements,
    groups: elementIdByCellId.size ? [{
      id: "drawio_preview_group",
      label: "Draw.io diagram",
      elementIds: Array.from(elementIdByCellId.values()),
    }] : [],
  };
}

export async function createDiagramDocumentsFromDrawioSource(
  content: string,
  title: string,
): Promise<EditableDiagramDocument[]> {
  const pages = await drawioModelXmlPages(content, title);
  const budget: DrawioRenderBudget = { cellCount: 0 };
  return pages.map((page, index) => createDiagramDocumentFromDrawioModel(
    page.modelXml,
    page.title || title,
    index,
    budget,
  ));
}

export async function createDiagramDocumentFromDrawioSource(
  content: string,
  title: string,
): Promise<EditableDiagramDocument> {
  const documents = await createDiagramDocumentsFromDrawioSource(content, title);
  return documents[0];
}
