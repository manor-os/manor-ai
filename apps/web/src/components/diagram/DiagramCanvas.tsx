import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  cloneDiagramDocument,
  createDiagramId,
  getAnchorPoint,
  getElementBounds,
  resolveEndpoint,
  type DiagramAnchor,
  type DiagramConnectorElement,
  type DiagramEndpointBinding,
  type DiagramConnectorRouting,
  type DiagramElement,
  type DiagramShapeElement,
  type DiagramTextElement,
  type EditableDiagramDocument,
} from "../../lib/diagram/schema";
import {
  IconCopy,
  IconDragHandle,
  IconLock,
  IconRedo,
  IconText,
  IconTrash,
  IconUndo,
} from "../icons";

type DiagramTool = "select" | "pan" | "connector";
type ResizeHandle = "nw" | "ne" | "sw" | "se";

interface DiagramCanvasProps {
  document: EditableDiagramDocument;
  onChange: (document: EditableDiagramDocument) => void;
}

interface DragState {
  type: "move" | "resize" | "connector-end" | "connector-control";
  elementId: string;
  pointerId: number;
  start: { x: number; y: number };
  viewBox: { originX: number; originY: number; width: number; height: number };
  base: EditableDiagramDocument;
  handle?: ResizeHandle;
  endpoint?: "from" | "to";
}

interface PanState {
  pointerId: number;
  startClientX: number;
  startClientY: number;
  scrollLeft: number;
  scrollTop: number;
}

interface ConnectorDragState {
  pointerId: number;
  source: DiagramEndpointBinding;
  from: { x: number; y: number };
  to: { x: number; y: number };
  viewBox: DragState["viewBox"];
  startClientX: number;
  startClientY: number;
  moved: boolean;
  target?: DiagramEndpointBinding | null;
}

interface ConnectorDraftState {
  source: DiagramEndpointBinding;
  from: { x: number; y: number };
  to: { x: number; y: number };
  target?: DiagramEndpointBinding | null;
}

interface ConnectorEndpointPreviewState {
  point: { x: number; y: number };
  target?: DiagramEndpointBinding | null;
}

const HANDLE_SIZE = 10;
const ANCHOR_DOT_RADIUS = 5;
const CONNECTOR_ANCHORS: DiagramAnchor[] = ["top", "right", "bottom", "left"];
const CONNECTOR_SNAP_PX = 34;
const MIN_SIZE = 24;
const MIN_ZOOM = 0.35;
const MAX_ZOOM = 2.5;
const EDITOR_VIEW_MIN_WIDTH = 2400;
const EDITOR_VIEW_MIN_HEIGHT = 1600;
const EDITOR_VIEW_PAD_X = 160;
const EDITOR_VIEW_PAD_Y = 120;
const DIAGRAM_SHAPE_TOOLS: Array<{ shape: DiagramShapeElement["shape"]; label: string }> = [
  { shape: "rect", label: "Rectangle" },
  { shape: "roundRect", label: "Round rect" },
  { shape: "ellipse", label: "Ellipse" },
  { shape: "diamond", label: "Diamond" },
  { shape: "triangle", label: "Triangle" },
  { shape: "hexagon", label: "Hexagon" },
  { shape: "parallelogram", label: "Parallelogram" },
  { shape: "trapezoid", label: "Trapezoid" },
  { shape: "cylinder", label: "Cylinder" },
  { shape: "document", label: "Document" },
  { shape: "rightArrow", label: "Right arrow" },
  { shape: "downArrow", label: "Down arrow" },
];

function clampZoom(value: number) {
  return Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, value));
}

const computeEditorViewBox = (canvas: EditableDiagramDocument["canvas"]) => {
  const persistedOriginX = canvas.originX ?? 0;
  const persistedOriginY = canvas.originY ?? 0;
  const originX = Math.min(persistedOriginX, -EDITOR_VIEW_PAD_X);
  const originY = Math.min(persistedOriginY, -EDITOR_VIEW_PAD_Y);
  const right = Math.max(persistedOriginX + canvas.width, EDITOR_VIEW_PAD_X * 2);
  const bottom = Math.max(persistedOriginY + canvas.height, EDITOR_VIEW_PAD_Y * 2);
  return {
    originX,
    originY,
    width: Math.max(EDITOR_VIEW_MIN_WIDTH, right - originX + EDITOR_VIEW_PAD_X),
    height: Math.max(EDITOR_VIEW_MIN_HEIGHT, bottom - originY + EDITOR_VIEW_PAD_Y),
  };
};

export default function DiagramCanvas({ document, onChange }: DiagramCanvasProps) {
  const [diagram, setDiagram] = useState<EditableDiagramDocument>(document);
  const [selectedId, setSelectedId] = useState<string | null>(document.elements[0]?.id || null);
  const [tool, setTool] = useState<DiagramTool>("select");
  const [connectorSource, setConnectorSource] = useState<DiagramEndpointBinding | null>(null);
  const [undoStack, setUndoStack] = useState<EditableDiagramDocument[]>([]);
  const [redoStack, setRedoStack] = useState<EditableDiagramDocument[]>([]);
  const [zoom, setZoom] = useState(1);
  const [showGrid, setShowGrid] = useState(true);
  const [toolbarMenu, setToolbarMenu] = useState<"insert" | "edit" | "view" | null>(null);
  const viewportRef = useRef<HTMLDivElement | null>(null);
  const canvasSurfaceRef = useRef<HTMLDivElement | null>(null);
  const svgRef = useRef<SVGSVGElement | null>(null);
  const toolbarRef = useRef<HTMLDivElement | null>(null);
  const dragRef = useRef<DragState | null>(null);
  const panRef = useRef<PanState | null>(null);
  const connectorDragRef = useRef<ConnectorDragState | null>(null);
  const suppressAnchorClickRef = useRef(false);
  const diagramRef = useRef(diagram);
  const zoomRef = useRef(zoom);
  const lastCommittedDocumentRef = useRef<EditableDiagramDocument | null>(null);
  const [isPanning, setIsPanning] = useState(false);
  const [connectorDraft, setConnectorDraft] = useState<ConnectorDraftState | null>(null);
  const [connectorEndpointPreview, setConnectorEndpointPreview] = useState<ConnectorEndpointPreviewState | null>(null);
  const editorViewBox = useMemo(() => computeEditorViewBox(diagram.canvas), [diagram.canvas]);
  const editorViewBoxRef = useRef(editorViewBox);

  useEffect(() => {
    editorViewBoxRef.current = editorViewBox;
  }, [editorViewBox]);

  useEffect(() => {
    const cameFromInternalEdit = lastCommittedDocumentRef.current === document;
    lastCommittedDocumentRef.current = null;
    setDiagram(document);
    diagramRef.current = document;
    setSelectedId((current) => {
      if (cameFromInternalEdit && current && document.elements.some((element) => element.id === current)) return current;
      return document.elements.find((element) => element.kind !== "connector")?.id || document.elements[0]?.id || null;
    });
    if (!cameFromInternalEdit) {
      setTool("select");
      setConnectorSource(null);
      setConnectorDraft(null);
      setConnectorEndpointPreview(null);
      setUndoStack([]);
      setRedoStack([]);
      const viewBox = computeEditorViewBox(document.canvas);
      const bounds = contentBounds(document.elements);
      requestAnimationFrame(() => {
        const viewport = viewportRef.current;
        if (!viewport) return;
        if (!bounds) {
          viewport.scrollLeft = 0;
          viewport.scrollTop = 0;
          return;
        }
        viewport.scrollLeft = Math.max(0, (bounds.x - viewBox.originX - 180) * zoomRef.current);
        viewport.scrollTop = Math.max(0, (bounds.y - viewBox.originY - 140) * zoomRef.current);
      });
    }
  }, [document]);

  useEffect(() => {
    diagramRef.current = diagram;
  }, [diagram]);

  useEffect(() => {
    zoomRef.current = zoom;
  }, [zoom]);

  useEffect(() => {
    const handlePointerDown = (event: PointerEvent) => {
      if (!toolbarMenu) return;
      const target = event.target as Node | null;
      if (target && toolbarRef.current?.contains(target)) return;
      setToolbarMenu(null);
    };
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") setToolbarMenu(null);
    };
    window.addEventListener("pointerdown", handlePointerDown);
    window.addEventListener("keydown", handleKeyDown);
    return () => {
      window.removeEventListener("pointerdown", handlePointerDown);
      window.removeEventListener("keydown", handleKeyDown);
    };
  }, [toolbarMenu]);

  const setZoomAtPoint = useCallback((nextZoomValue: number, clientX?: number, clientY?: number) => {
    const nextZoom = clampZoom(nextZoomValue);
    const previousZoom = zoomRef.current;
    if (Math.abs(nextZoom - previousZoom) < 0.001) return;

    const viewport = viewportRef.current;
    const surface = canvasSurfaceRef.current;
    let localX: number | null = null;
    let localY: number | null = null;

    if (viewport && surface && clientX != null && clientY != null) {
      const rect = surface.getBoundingClientRect();
      localX = (clientX - rect.left) / previousZoom;
      localY = (clientY - rect.top) / previousZoom;
    }

    zoomRef.current = nextZoom;
    setZoom(nextZoom);

    if (viewport && localX != null && localY != null) {
      requestAnimationFrame(() => {
        viewport.scrollLeft += localX * (nextZoom - previousZoom);
        viewport.scrollTop += localY * (nextZoom - previousZoom);
      });
    }
  }, []);

  useEffect(() => {
    const viewport = viewportRef.current;
    if (!viewport) return;

    const handleWheel = (event: WheelEvent) => {
      if (!event.ctrlKey && !event.metaKey) return;
      event.preventDefault();
      const factor = Math.exp(-event.deltaY * 0.002);
      setZoomAtPoint(zoomRef.current * factor, event.clientX, event.clientY);
    };

    viewport.addEventListener("wheel", handleWheel, { passive: false });
    return () => viewport.removeEventListener("wheel", handleWheel);
  }, [setZoomAtPoint]);

  const elementsById = useMemo(() => new Map(diagram.elements.map((element) => [element.id, element])), [diagram.elements]);
  const selectedElement = selectedId ? elementsById.get(selectedId) || null : null;
  const shapeOptions = useMemo(
    () => diagram.elements.filter((element): element is DiagramShapeElement | DiagramTextElement => element.kind !== "connector"),
    [diagram.elements],
  );

  const commit = useCallback((next: EditableDiagramDocument, pushHistory = true) => {
    if (pushHistory) {
      setUndoStack((stack) => [...stack.slice(-39), cloneDiagramDocument(diagramRef.current)]);
      setRedoStack([]);
    }
    diagramRef.current = next;
    lastCommittedDocumentRef.current = next;
    setDiagram(next);
    onChange(next);
  }, [onChange]);

  const updateElement = useCallback((elementId: string, updater: (element: DiagramElement) => DiagramElement) => {
    const next = {
      ...diagramRef.current,
      elements: diagramRef.current.elements.map((element) => element.id === elementId ? updater(element) : element),
    };
    commit(next);
  }, [commit]);

  const undo = useCallback(() => {
    setUndoStack((stack) => {
      if (!stack.length) return stack;
      const previous = stack[stack.length - 1];
      setRedoStack((redo) => [...redo, cloneDiagramDocument(diagramRef.current)]);
      diagramRef.current = previous;
      lastCommittedDocumentRef.current = previous;
      setDiagram(previous);
      onChange(previous);
      return stack.slice(0, -1);
    });
  }, [onChange]);

  const redo = useCallback(() => {
    setRedoStack((stack) => {
      if (!stack.length) return stack;
      const next = stack[stack.length - 1];
      setUndoStack((undoStackValue) => [...undoStackValue, cloneDiagramDocument(diagramRef.current)]);
      diagramRef.current = next;
      lastCommittedDocumentRef.current = next;
      setDiagram(next);
      onChange(next);
      return stack.slice(0, -1);
    });
  }, [onChange]);

  const pointFromEvent = useCallback((event: MouseEvent | React.PointerEvent, viewBox?: DragState["viewBox"]): { x: number; y: number } => {
    const svg = svgRef.current;
    if (!svg) return { x: 0, y: 0 };
    const rect = svg.getBoundingClientRect();
    const canvas = viewBox || editorViewBoxRef.current;
    return {
      x: canvas.originX + ((event.clientX - rect.left) / rect.width) * canvas.width,
      y: canvas.originY + ((event.clientY - rect.top) / rect.height) * canvas.height,
    };
  }, []);

  const startDrag = useCallback((event: React.PointerEvent, elementId: string, type: DragState["type"], handle?: ResizeHandle, endpoint?: "from" | "to") => {
    const element = elementsById.get(elementId);
    if (!element) return;
    if (element.locked) {
      event.preventDefault();
      event.stopPropagation();
      setSelectedId(elementId);
      return;
    }
    event.preventDefault();
    event.stopPropagation();
    try {
      event.currentTarget.setPointerCapture(event.pointerId);
    } catch {
      // Best-effort only; window-level pointer listeners below keep drag usable.
    }
    setSelectedId(elementId);
    setConnectorEndpointPreview(null);
    const viewBox = {
      ...editorViewBoxRef.current,
    };
    dragRef.current = {
      type,
      elementId,
      pointerId: event.pointerId,
      start: pointFromEvent(event, viewBox),
      viewBox,
      base: cloneDiagramDocument(diagramRef.current),
      handle,
      endpoint,
    };
  }, [elementsById, pointFromEvent]);

  const startPan = useCallback((event: React.PointerEvent) => {
    if (tool !== "pan") return;
    const viewport = viewportRef.current;
    if (!viewport) return;
    event.preventDefault();
    event.stopPropagation();
    try {
      event.currentTarget.setPointerCapture(event.pointerId);
    } catch {
      // Best-effort only; window-level pointer listeners below keep pan usable.
    }
    setSelectedId(null);
    setConnectorSource(null);
    panRef.current = {
      pointerId: event.pointerId,
      startClientX: event.clientX,
      startClientY: event.clientY,
      scrollLeft: viewport.scrollLeft,
      scrollTop: viewport.scrollTop,
    };
    setIsPanning(true);
  }, [tool]);

  const startConnectorDrag = useCallback((event: React.PointerEvent, source: DiagramEndpointBinding) => {
    if (connectorSource && (connectorSource.elementId !== source.elementId || connectorSource.anchor !== source.anchor)) {
      event.preventDefault();
      event.stopPropagation();
      return;
    }
    const sourceElement = diagramRef.current.elements.find((element) => element.id === source.elementId);
    const sourceBounds = sourceElement ? getElementBounds(sourceElement) : null;
    if (!sourceBounds) return;
    event.preventDefault();
    event.stopPropagation();
    try {
      event.currentTarget.setPointerCapture(event.pointerId);
    } catch {
      // Best-effort only; window-level pointer listeners below keep drag usable.
    }
    suppressAnchorClickRef.current = false;
    const viewBox = { ...editorViewBoxRef.current };
    const from = getAnchorPoint(sourceBounds, source.anchor);
    const to = pointFromEvent(event, viewBox);
    connectorDragRef.current = {
      pointerId: event.pointerId,
      source,
      from,
      to,
      viewBox,
      startClientX: event.clientX,
      startClientY: event.clientY,
      moved: false,
      target: null,
    };
    setTool("connector");
    setSelectedId(source.elementId);
    setConnectorSource(source);
    setConnectorDraft({ source, from, to });
  }, [connectorSource, pointFromEvent]);

  useEffect(() => {
    const handleMove = (event: PointerEvent) => {
      const pan = panRef.current;
      const viewport = viewportRef.current;
      if (!pan || !viewport) return;
      if (event.pointerId !== pan.pointerId) return;
      event.preventDefault();
      viewport.scrollLeft = pan.scrollLeft - (event.clientX - pan.startClientX);
      viewport.scrollTop = pan.scrollTop - (event.clientY - pan.startClientY);
    };
    const handleUp = (event: PointerEvent) => {
      const pan = panRef.current;
      if (!pan) return;
      if (event.pointerId !== pan.pointerId) return;
      panRef.current = null;
      setIsPanning(false);
    };
    window.addEventListener("pointermove", handleMove);
    window.addEventListener("pointerup", handleUp);
    window.addEventListener("pointercancel", handleUp);
    return () => {
      window.removeEventListener("pointermove", handleMove);
      window.removeEventListener("pointerup", handleUp);
      window.removeEventListener("pointercancel", handleUp);
    };
  }, []);

  useEffect(() => {
    const handleMove = (event: PointerEvent) => {
      const drag = dragRef.current;
      if (!drag) return;
      if (event.pointerId !== drag.pointerId) return;
      const point = pointFromEvent(event, drag.viewBox);
      const dx = point.x - drag.start.x;
      const dy = point.y - drag.start.y;
      let next = ensureCanvasFitsDocument(applyDrag(drag, dx, dy));
      if (drag.type === "connector-end" && drag.endpoint) {
        const opposite = connectorOppositeBinding(drag.base.elements, drag.elementId, drag.endpoint);
        const snap = findNearestAnchor(point, next.elements, opposite, CONNECTOR_SNAP_PX / zoom);
        if (snap) {
          next = {
            ...next,
            elements: next.elements.map((element) => {
              if (element.id !== drag.elementId || element.kind !== "connector") return element;
              return {
                ...element,
                [drag.endpoint!]: { bind: snap.binding },
              };
            }),
          };
        }
        setConnectorEndpointPreview({
          point: snap?.point || point,
          target: snap?.binding || null,
        });
      }
      diagramRef.current = next;
      setDiagram(next);
    };
    const handleUp = (event: PointerEvent) => {
      const drag = dragRef.current;
      if (!drag) return;
      if (event.pointerId !== drag.pointerId) return;
      let current = diagramRef.current;
      if (drag.type === "connector-end" && drag.endpoint) {
        const point = pointFromEvent(event, drag.viewBox);
        const opposite = connectorOppositeBinding(drag.base.elements, drag.elementId, drag.endpoint);
        const snap = findNearestAnchor(point, current.elements, opposite, CONNECTOR_SNAP_PX / zoom);
        if (snap) {
          current = {
            ...current,
            elements: current.elements.map((element) => {
              if (element.id !== drag.elementId || element.kind !== "connector") return element;
              return {
                ...element,
                [drag.endpoint!]: { bind: snap.binding },
              };
            }),
          };
          diagramRef.current = current;
        }
      }
      dragRef.current = null;
      setConnectorEndpointPreview(null);
      setUndoStack((stack) => [...stack.slice(-39), drag.base]);
      setRedoStack([]);
      setDiagram(current);
      lastCommittedDocumentRef.current = current;
      onChange(current);
    };
    window.addEventListener("pointermove", handleMove);
    window.addEventListener("pointerup", handleUp);
    window.addEventListener("pointercancel", handleUp);
    return () => {
      window.removeEventListener("pointermove", handleMove);
      window.removeEventListener("pointerup", handleUp);
      window.removeEventListener("pointercancel", handleUp);
    };
  }, [onChange, pointFromEvent, zoom]);

  useEffect(() => {
    const handleMove = (event: PointerEvent) => {
      const drag = connectorDragRef.current;
      if (!drag) return;
      if (event.pointerId !== drag.pointerId) return;
      event.preventDefault();
      const screenDx = event.clientX - drag.startClientX;
      const screenDy = event.clientY - drag.startClientY;
      if (!drag.moved && Math.hypot(screenDx, screenDy) > 4) {
        drag.moved = true;
      }
      const point = pointFromEvent(event, drag.viewBox);
      const snap = findNearestAnchor(point, diagramRef.current.elements, drag.source, CONNECTOR_SNAP_PX / zoom);
      const to = snap?.point || point;
      drag.to = to;
      drag.target = snap?.binding || null;
      connectorDragRef.current = drag;
      setConnectorDraft({
        source: drag.source,
        from: drag.from,
        to,
        target: drag.target,
      });
    };
    const handleUp = (event: PointerEvent) => {
      const drag = connectorDragRef.current;
      if (!drag) return;
      if (event.pointerId !== drag.pointerId) return;
      event.preventDefault();
      const point = pointFromEvent(event, drag.viewBox);
      const snap = findNearestAnchor(point, diagramRef.current.elements, drag.source, CONNECTOR_SNAP_PX / zoom);
      connectorDragRef.current = null;
      setConnectorDraft(null);
      if (drag.moved || snap) {
        suppressAnchorClickRef.current = true;
      }
      if (!snap) {
        setConnectorSource(drag.source);
        setSelectedId(drag.source.elementId);
        setTool("connector");
        return;
      }
      const nextConnector: DiagramConnectorElement = {
        id: createDiagramId("conn"),
        kind: "connector",
        from: { bind: drag.source },
        to: { bind: snap.binding },
        routing: "straight",
        stroke: diagramRef.current.theme.palette.accent || "#4f7d75",
        strokeWidth: 3,
        arrowEnd: true,
      };
      commit(ensureCanvasFitsDocument({
        ...diagramRef.current,
        elements: [...diagramRef.current.elements, nextConnector],
      }));
      setSelectedId(nextConnector.id);
      setConnectorSource(null);
      setTool("select");
    };
    window.addEventListener("pointermove", handleMove);
    window.addEventListener("pointerup", handleUp);
    window.addEventListener("pointercancel", handleUp);
    return () => {
      window.removeEventListener("pointermove", handleMove);
      window.removeEventListener("pointerup", handleUp);
      window.removeEventListener("pointercancel", handleUp);
    };
  }, [commit, pointFromEvent, zoom]);

  const applyDrag = useCallback((drag: DragState, dx: number, dy: number): EditableDiagramDocument => {
    return {
      ...drag.base,
      elements: drag.base.elements.map((element) => {
        if (element.id !== drag.elementId) return element;
        if (drag.type === "connector-end" && element.kind === "connector" && drag.endpoint) {
          const endpoint = element[drag.endpoint];
          return {
            ...element,
            [drag.endpoint]: {
              ...endpoint,
              bind: undefined,
              x: (endpoint.x ?? resolveEndpoint(endpoint, drag.base.elements).x) + dx,
              y: (endpoint.y ?? resolveEndpoint(endpoint, drag.base.elements).y) + dy,
            },
          };
        }
        if (drag.type === "connector-control" && element.kind === "connector") {
          const from = resolveEndpoint(element.from, drag.base.elements);
          const to = resolveEndpoint(element.to, drag.base.elements);
          const controlPoint = element.controlPoint || defaultConnectorControlPoint(element.routing || "straight", from, to);
          return {
            ...element,
            controlPoint: {
              x: controlPoint.x + dx,
              y: controlPoint.y + dy,
            },
          };
        }
        if (element.kind === "connector") return element;
        if (drag.type === "move") return { ...element, x: element.x + dx, y: element.y + dy };
        if (drag.type === "resize" && drag.handle) {
          let { x, y, w, h } = element;
          if (drag.handle.includes("e")) w += dx;
          if (drag.handle.includes("s")) h += dy;
          if (drag.handle.includes("w")) {
            x += dx;
            w -= dx;
          }
          if (drag.handle.includes("n")) {
            y += dy;
            h -= dy;
          }
          if (w < MIN_SIZE) {
            if (drag.handle.includes("w")) x -= MIN_SIZE - w;
            w = MIN_SIZE;
          }
          if (h < MIN_SIZE) {
            if (drag.handle.includes("n")) y -= MIN_SIZE - h;
            h = MIN_SIZE;
          }
          return { ...element, x, y, w, h };
        }
        return element;
      }),
    };
  }, []);

  const addShape = useCallback((shape: DiagramShapeElement["shape"]) => {
    const nextElement: DiagramShapeElement = {
      id: createDiagramId("shape"),
      kind: "shape",
      shape,
      x: 160 + diagram.elements.length * 6,
      y: 140 + diagram.elements.length * 4,
      w: shape === "rightArrow" || shape === "downArrow" ? 190 : 170,
      h: shape === "rightArrow" || shape === "downArrow" ? 76 : 82,
      fill: shape === "roundRect" || shape === "rightArrow" || shape === "downArrow" ? "#e8eff4" : "#ffffff",
      stroke: "#1c1917",
      strokeWidth: 2,
      radius: shape === "roundRect" ? 16 : 0,
      text: "Node",
      textStyle: { fontSize: 18, fontWeight: 700, color: "#1c1917", align: "center" },
    };
    commit(ensureCanvasFitsDocument({ ...diagram, elements: [...diagram.elements, nextElement] }));
    setSelectedId(nextElement.id);
    setTool("select");
    setConnectorSource(null);
  }, [commit, diagram]);

  const addText = useCallback(() => {
    const nextElement: DiagramTextElement = {
      id: createDiagramId("text"),
      kind: "text",
      x: 180,
      y: 90,
      w: 260,
      h: 42,
      text: "Text label",
      textStyle: { fontSize: 24, fontWeight: 700, color: "#1c1917", align: "center", fontFamily: "Times New Roman, serif" },
    };
    commit(ensureCanvasFitsDocument({ ...diagram, elements: [...diagram.elements, nextElement] }));
    setSelectedId(nextElement.id);
    setTool("select");
    setConnectorSource(null);
  }, [commit, diagram]);

  const duplicateSelected = useCallback(() => {
    if (!selectedElement) return;
    const copy = cloneElement(selectedElement);
    commit(ensureCanvasFitsDocument({ ...diagram, elements: [...diagram.elements, copy] }));
    setSelectedId(copy.id);
  }, [commit, diagram, selectedElement]);

  const toggleSelectedLock = useCallback(() => {
    if (!selectedElement) return;
    updateElement(selectedElement.id, (element) => ({ ...element, locked: !element.locked }));
  }, [selectedElement, updateElement]);

  const deleteSelected = useCallback(() => {
    if (!selectedId) return;
    const selected = diagramRef.current.elements.find((element) => element.id === selectedId);
    if (selected?.locked) return;
    const next = {
      ...diagramRef.current,
      elements: diagramRef.current.elements
        .filter((element) => element.id !== selectedId)
        .map((element) => {
          if (element.kind !== "connector") return element;
          const fromMatches = element.from.bind?.elementId === selectedId;
          const toMatches = element.to.bind?.elementId === selectedId;
          if (!fromMatches && !toMatches) return element;
          return {
            ...element,
            from: fromMatches ? { x: resolveEndpoint(element.from, diagramRef.current.elements).x, y: resolveEndpoint(element.from, diagramRef.current.elements).y } : element.from,
            to: toMatches ? { x: resolveEndpoint(element.to, diagramRef.current.elements).x, y: resolveEndpoint(element.to, diagramRef.current.elements).y } : element.to,
          };
        }),
    };
    commit(next);
    setSelectedId(next.elements[0]?.id || null);
  }, [commit, selectedId]);

  useEffect(() => {
    const handler = (event: KeyboardEvent) => {
      const target = event.target instanceof HTMLElement ? event.target : null;
      if (target?.closest("input, textarea, select, [contenteditable='true']")) return;
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "z" && !event.shiftKey) {
        event.preventDefault();
        undo();
        return;
      }
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "z" && event.shiftKey) {
        event.preventDefault();
        redo();
        return;
      }
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "d") {
        event.preventDefault();
        duplicateSelected();
        return;
      }
      if (!selectedId) return;
      const selected = diagramRef.current.elements.find((element) => element.id === selectedId);
      if (selected?.locked && (event.key === "Delete" || event.key === "Backspace" || event.key.startsWith("Arrow"))) {
        event.preventDefault();
        return;
      }
      if (event.key === "Delete" || event.key === "Backspace") {
        event.preventDefault();
        deleteSelected();
        return;
      }
      const delta = event.shiftKey ? 10 : 2;
      const move = {
        ArrowLeft: { x: -delta, y: 0 },
        ArrowRight: { x: delta, y: 0 },
        ArrowUp: { x: 0, y: -delta },
        ArrowDown: { x: 0, y: delta },
      }[event.key];
      if (!move) return;
      event.preventDefault();
      updateElement(selectedId, (element) => {
        if (element.kind === "connector") return element;
        return { ...element, x: element.x + move.x, y: element.y + move.y };
      });
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [deleteSelected, duplicateSelected, redo, selectedId, undo, updateElement]);

  const finishOrStartConnector = useCallback((binding: DiagramEndpointBinding) => {
    if (!connectorSource || connectorSource.elementId === binding.elementId) {
      setConnectorSource(binding);
      setSelectedId(binding.elementId);
      setTool("connector");
      return;
    }
    const nextConnector: DiagramConnectorElement = {
      id: createDiagramId("conn"),
      kind: "connector",
      from: { bind: connectorSource },
      to: { bind: binding },
      routing: "straight",
      stroke: diagramRef.current.theme.palette.accent || "#4f7d75",
      strokeWidth: 3,
      arrowEnd: true,
    };
    commit(ensureCanvasFitsDocument({
      ...diagramRef.current,
      elements: [...diagramRef.current.elements, nextConnector],
    }));
    setSelectedId(nextConnector.id);
    setConnectorSource(null);
    setTool("select");
  }, [commit, connectorSource]);

  const handleAnchorClick = useCallback((event: React.SyntheticEvent, binding: DiagramEndpointBinding) => {
    event.preventDefault();
    event.stopPropagation();
    if (suppressAnchorClickRef.current) {
      suppressAnchorClickRef.current = false;
      return;
    }
    finishOrStartConnector(binding);
  }, [finishOrStartConnector]);

  const handleElementClick = useCallback((event: React.SyntheticEvent, element: DiagramElement) => {
    event.stopPropagation();
    if (tool === "pan") return;
    if (tool === "connector" && element.kind !== "connector") {
      finishOrStartConnector({
        elementId: element.id,
        anchor: connectorSource ? "left" : "right",
      });
      return;
    }
    setSelectedId(element.id);
  }, [connectorSource, finishOrStartConnector, tool]);

  const exportSvg = useCallback(() => {
    const blob = new Blob([buildExportSvg(diagram)], { type: "image/svg+xml;charset=utf-8" });
    downloadBlob(blob, `${diagram.title || "diagram"}.svg`);
  }, [diagram]);

  const fitToContent = useCallback(() => {
    commit(fitCanvasToContent(diagramRef.current, 120));
  }, [commit]);

  const expandCanvas = useCallback(() => {
    const canvas = diagramRef.current.canvas;
    commit({
      ...diagramRef.current,
      canvas: {
        ...canvas,
        originX: (canvas.originX ?? 0) - 400,
        originY: (canvas.originY ?? 0) - 260,
        width: Math.min(10000, canvas.width + 800),
        height: Math.min(10000, canvas.height + 520),
      },
    });
  }, [commit]);

  const connectorPreviewPoint = connectorDraft?.to || connectorEndpointPreview?.point || null;
  const connectorPreviewTarget = connectorDraft?.target || connectorEndpointPreview?.target || null;
  const isConnectorDragging = Boolean(connectorDraft || connectorEndpointPreview);
  const runToolbarAction = (action: () => void) => {
    action();
    setToolbarMenu(null);
  };

  return (
    <div className="manor-editor-canvas-layout">
      <div style={{ minWidth: 0, display: "flex", flexDirection: "column", overflow: "hidden" }}>
        <div ref={toolbarRef} className="manor-editor-toolbar diagram-editor-toolbar">
          <IconButton title="Undo" disabled={!undoStack.length} onClick={undo}><IconUndo size={16} /></IconButton>
          <IconButton title="Redo" disabled={!redoStack.length} onClick={redo}><IconRedo size={16} /></IconButton>
          <Divider />
          <IconButton title="Select" active={tool === "select"} onClick={() => { setTool("select"); setConnectorSource(null); }}>
            <span style={{ fontSize: 13, fontWeight: 700 }}>↖</span>
          </IconButton>
          <IconButton title="Move canvas" active={tool === "pan"} onClick={() => { setTool("pan"); setSelectedId(null); setConnectorSource(null); }}>
            <IconDragHandle size={16} />
          </IconButton>
          <Divider />
          <div className="diagram-toolbar-menu">
            <button
              type="button"
              className={`manor-editor-tool-button ${toolbarMenu === "insert" || tool === "connector" ? "manor-editor-tool-button--active" : ""}`}
              onClick={() => setToolbarMenu((current) => current === "insert" ? null : "insert")}
            >
              Insert <span className="diagram-toolbar-caret">▾</span>
            </button>
            {toolbarMenu === "insert" && (
              <div className="diagram-toolbar-dropdown">
                <div className="diagram-toolbar-menu-title">Add</div>
                <button type="button" className="diagram-toolbar-menu-item" onClick={() => runToolbarAction(addText)}>
                  <IconText size={15} /> Text
                </button>
                <button
                  type="button"
                  className={`diagram-toolbar-menu-item ${tool === "connector" ? "diagram-toolbar-menu-item--active" : ""}`}
                  onClick={() => runToolbarAction(() => { setTool("connector"); setConnectorSource(null); })}
                >
                  <span className="diagram-toolbar-menu-symbol">→</span> Connector
                </button>
                <div className="diagram-toolbar-menu-divider" />
                <div className="diagram-toolbar-menu-title">Shapes</div>
                <div className="diagram-toolbar-shape-grid">
                  {DIAGRAM_SHAPE_TOOLS.map(({ shape, label }) => (
                    <button
                      key={shape}
                      type="button"
                      className="diagram-toolbar-shape-option"
                      title={label}
                      onClick={() => runToolbarAction(() => addShape(shape))}
                    >
                      <ShapeIcon kind={shape} />
                      <span>{label}</span>
                    </button>
                  ))}
                </div>
              </div>
            )}
          </div>
          <div className="diagram-toolbar-menu">
            <button
              type="button"
              className={`manor-editor-tool-button ${toolbarMenu === "edit" ? "manor-editor-tool-button--active" : ""}`}
              onClick={() => setToolbarMenu((current) => current === "edit" ? null : "edit")}
            >
              Edit <span className="diagram-toolbar-caret">▾</span>
            </button>
            {toolbarMenu === "edit" && (
              <div className="diagram-toolbar-dropdown">
                <div className="diagram-toolbar-menu-title">{selectedElement ? "Selected object" : "No object selected"}</div>
                <button type="button" className="diagram-toolbar-menu-item" disabled={!selectedElement} onClick={() => runToolbarAction(duplicateSelected)}>
                  <IconCopy size={15} /> Duplicate
                </button>
                <button type="button" className="diagram-toolbar-menu-item" disabled={!selectedElement} onClick={() => runToolbarAction(toggleSelectedLock)}>
                  <IconLock size={15} /> {selectedElement?.locked ? "Unlock" : "Lock"}
                </button>
                <button
                  type="button"
                  className="diagram-toolbar-menu-item diagram-toolbar-menu-item--danger"
                  disabled={!selectedElement || Boolean(selectedElement?.locked)}
                  onClick={() => runToolbarAction(deleteSelected)}
                >
                  <IconTrash size={15} /> Delete
                </button>
              </div>
            )}
          </div>
          <Divider />
          <label style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 12, color: "#57534e" }}>
            Zoom
            <input
              type="range"
              className="manor-editor-range diagram-zoom-slider"
              min={MIN_ZOOM}
              max={MAX_ZOOM}
              step={0.05}
              value={zoom}
              onChange={(event) => setZoomAtPoint(Number(event.target.value))}
            />
          </label>
          <button onClick={() => setZoomAtPoint(1)} className="btn-manor-ghost" style={{ fontSize: 12, padding: "5px 9px" }}>
            100%
          </button>
          <div className="diagram-toolbar-menu">
            <button
              type="button"
              className={`manor-editor-tool-button ${toolbarMenu === "view" ? "manor-editor-tool-button--active" : ""}`}
              onClick={() => setToolbarMenu((current) => current === "view" ? null : "view")}
            >
              View <span className="diagram-toolbar-caret">▾</span>
            </button>
            {toolbarMenu === "view" && (
              <div className="diagram-toolbar-dropdown diagram-toolbar-dropdown--right">
                <button type="button" className={`diagram-toolbar-menu-item ${showGrid ? "diagram-toolbar-menu-item--active" : ""}`} onClick={() => runToolbarAction(() => setShowGrid((value) => !value))}>
                  <span className="diagram-toolbar-menu-check">{showGrid ? "✓" : ""}</span> Grid
                </button>
                <button type="button" className="diagram-toolbar-menu-item" onClick={() => runToolbarAction(fitToContent)}>
                  Fit to content
                </button>
                <button type="button" className="diagram-toolbar-menu-item" onClick={() => runToolbarAction(expandCanvas)}>
                  Expand canvas
                </button>
                <div className="diagram-toolbar-menu-divider" />
                <button type="button" className="diagram-toolbar-menu-item" onClick={() => runToolbarAction(exportSvg)}>
                  Export SVG
                </button>
              </div>
            )}
          </div>
          <span style={{ color: "#78716c", fontSize: 12 }}>
            {diagram.elements.length} objects
          </span>
        </div>

        {tool === "connector" && (
          <div className="manor-editor-hint manor-editor-hint-warning">
            {connectorSource ? "Select the target anchor for the connector." : "Select the first anchor for the connector."}
          </div>
        )}

        <div
          ref={viewportRef}
          className="manor-editor-canvas-viewport"
          onPointerDown={tool === "pan" ? startPan : undefined}
          style={{
            backgroundImage: showGrid
              ? `linear-gradient(#e7e5e4 1px, transparent 1px), linear-gradient(90deg, #e7e5e4 1px, transparent 1px)`
              : undefined,
            backgroundSize: showGrid ? `${24 * zoom}px ${24 * zoom}px` : undefined,
            cursor: isPanning ? "grabbing" : tool === "pan" ? "grab" : "default",
            userSelect: isPanning ? "none" : undefined,
          }}
        >
          <div ref={canvasSurfaceRef} style={{
            width: `${editorViewBox.width * zoom}px`,
            height: `${editorViewBox.height * zoom}px`,
            maxWidth: "none",
            background: "transparent",
            overflow: "visible",
            flexShrink: 0,
          }}>
            <svg
              ref={svgRef}
              viewBox={`${editorViewBox.originX} ${editorViewBox.originY} ${editorViewBox.width} ${editorViewBox.height}`}
              width="100%"
              height="100%"
              style={{
                display: "block",
                background: "transparent",
                touchAction: "none",
                overflow: "visible",
                cursor: isPanning ? "grabbing" : tool === "pan" ? "grab" : "default",
              }}
              onPointerDown={(event) => {
                if (tool === "pan") {
                  startPan(event);
                  return;
                }
                setSelectedId(null);
                setConnectorSource(null);
              }}
            >
              <defs>
                <marker id="diagram-arrow-end" markerWidth="14" markerHeight="14" refX="12" refY="7" orient="auto" markerUnits="userSpaceOnUse">
                  <path d="M1,1 L13,7 L1,13 Z" fill="context-stroke" />
                </marker>
                <marker id="diagram-arrow-start" markerWidth="14" markerHeight="14" refX="2" refY="7" orient="auto-start-reverse" markerUnits="userSpaceOnUse">
                  <path d="M13,1 L1,7 L13,13 Z" fill="context-stroke" />
                </marker>
              </defs>
              {diagram.elements.filter((element) => element.kind === "connector").map((element) => (
                <Connector
                  key={element.id}
                  connector={element as DiagramConnectorElement}
                  elements={diagram.elements}
                  selected={false}
                  onSelect={(event) => handleElementClick(event, element)}
                  onEndpointDrag={startDrag}
                  onPanStart={startPan}
                  panMode={tool === "pan"}
                />
              ))}
              {diagram.elements.filter((element) => element.kind !== "connector").map((element) => (
                <DiagramObject
                  key={element.id}
                  element={element as DiagramShapeElement | DiagramTextElement}
                  selected={selectedId === element.id}
                  onPointerDown={(event) => tool === "pan" ? startPan(event) : startDrag(event, element.id, "move")}
                  onSelect={(event) => handleElementClick(event, element)}
                  onResizeStart={startDrag}
                  panMode={tool === "pan"}
                  showAnchors={
                    (tool === "connector" && !isConnectorDragging)
                    || selectedId === element.id
                    || connectorSource?.elementId === element.id
                    || connectorPreviewTarget?.elementId === element.id
                    || Boolean(connectorPreviewPoint && pointIsNearElement(connectorPreviewPoint, element, (CONNECTOR_SNAP_PX * 1.75) / zoom))
                  }
                  connectorSource={connectorSource}
                  connectorTarget={connectorPreviewTarget}
                  onAnchorPointerDown={startConnectorDrag}
                  onAnchorClick={handleAnchorClick}
                />
              ))}
              {diagram.elements.filter((element) => element.kind === "connector" && selectedId === element.id).map((element) => (
                <Connector
                  key={`${element.id}-controls`}
                  connector={element as DiagramConnectorElement}
                  elements={diagram.elements}
                  selected
                  controlsOnly
                  onSelect={(event) => handleElementClick(event, element)}
                  onEndpointDrag={startDrag}
                  onPanStart={startPan}
                  panMode={tool === "pan"}
                />
              ))}
              {connectorDraft && (
                <path
                  d={connectorPath("straight", connectorDraft.from, connectorDraft.to)}
                  fill="none"
                  stroke={diagram.theme.palette.accent || "#4f7d75"}
                  strokeWidth={3}
                  strokeDasharray="8 5"
                  opacity={0.9}
                  markerEnd="url(#diagram-arrow-end)"
                  pointerEvents="none"
                />
              )}
            </svg>
          </div>
        </div>
      </div>

      <aside className="manor-editor-sidebar manor-editor-inspector">
        <div style={{ flex: 1, minHeight: 0, overflowY: "auto", padding: 14 }}>
          {selectedElement ? (
            <Inspector
              element={selectedElement}
              shapes={shapeOptions}
              onChange={(updater) => updateElement(selectedElement.id, updater)}
            />
          ) : (
            <CanvasInspector
              document={diagram}
              onChange={(patch) => commit({ ...diagram, ...patch })}
            />
          )}
        </div>
      </aside>
    </div>
  );
}

function DiagramObject({
  element,
  selected,
  onPointerDown,
  onSelect,
  onResizeStart,
  panMode,
  showAnchors,
  connectorSource,
  connectorTarget,
  onAnchorPointerDown,
  onAnchorClick,
}: {
  element: DiagramShapeElement | DiagramTextElement;
  selected: boolean;
  onPointerDown: (event: React.PointerEvent) => void;
  onSelect: (event: React.SyntheticEvent) => void;
  onResizeStart: (event: React.PointerEvent, elementId: string, type: DragState["type"], handle?: ResizeHandle) => void;
  panMode?: boolean;
  showAnchors?: boolean;
  connectorSource?: DiagramEndpointBinding | null;
  connectorTarget?: DiagramEndpointBinding | null;
  onAnchorPointerDown: (event: React.PointerEvent, binding: DiagramEndpointBinding) => void;
  onAnchorClick: (event: React.SyntheticEvent, binding: DiagramEndpointBinding) => void;
}) {
  const bounds = getElementBounds(element)!;
  const text = element.kind === "shape" ? element.text : element.text;
  const textStyle = element.kind === "shape" ? element.textStyle : element.textStyle;
  const fill = element.kind === "shape" ? element.fill : "transparent";
  const stroke = element.kind === "shape" ? element.stroke : "transparent";
  const strokeWidth = element.kind === "shape" ? element.strokeWidth || 0 : 0;
  const dash = element.kind === "shape" ? dashArray(element.strokeDash) : undefined;

  return (
    <g onPointerDown={onPointerDown} onClick={onSelect} style={{ cursor: panMode ? "grab" : element.locked ? "default" : "move" }}>
      <rect
        x={bounds.x}
        y={bounds.y}
        width={bounds.w}
        height={bounds.h}
        fill="transparent"
        stroke="none"
        pointerEvents="all"
      />
      {element.kind === "shape" && renderShape(element, fill, stroke, strokeWidth, dash)}
      <foreignObject x={bounds.x} y={bounds.y} width={bounds.w} height={bounds.h} pointerEvents="none">
        <div style={{
          width: "100%", height: "100%", display: "flex", alignItems: "center",
          justifyContent: textStyle?.align === "left" ? "flex-start" : textStyle?.align === "right" ? "flex-end" : "center",
          textAlign: textStyle?.align || "center", padding: element.kind === "shape" ? "6px 10px" : 0,
          color: textStyle?.color || "#1c1917", fontFamily: textStyle?.fontFamily,
          fontSize: textStyle?.fontSize || 18, fontWeight: textStyle?.fontWeight || 400,
          fontStyle: textStyle?.fontStyle || "normal", lineHeight: 1.08, whiteSpace: "pre-wrap",
          wordBreak: "break-word", overflow: "hidden", boxSizing: "border-box",
        }}>
          {text}
        </div>
      </foreignObject>
      {selected && !panMode && (
        <g data-editor-ui="true">
          <rect x={bounds.x} y={bounds.y} width={bounds.w} height={bounds.h} fill="none" stroke="#4f7d75" strokeWidth={2} strokeDasharray="5 4" pointerEvents="none" />
          {(["nw", "ne", "sw", "se"] as ResizeHandle[]).map((handle) => {
            const point = handlePoint(bounds, handle);
            return (
              <rect
                key={handle}
                x={point.x - HANDLE_SIZE / 2}
                y={point.y - HANDLE_SIZE / 2}
                width={HANDLE_SIZE}
                height={HANDLE_SIZE}
                rx={2}
                fill="#ffffff"
                stroke="#4f7d75"
                strokeWidth={2}
                cursor={`${handle}-resize`}
                onPointerDown={(event) => onResizeStart(event, element.id, "resize", handle)}
              />
            );
          })}
        </g>
      )}
      {showAnchors && !panMode && (
        <AnchorDots
          bounds={bounds}
          elementId={element.id}
          activeSource={connectorSource}
          activeTarget={connectorTarget}
          onAnchorPointerDown={onAnchorPointerDown}
          onAnchorClick={onAnchorClick}
        />
      )}
    </g>
  );
}

function AnchorDots({
  bounds,
  elementId,
  activeSource,
  activeTarget,
  onAnchorPointerDown,
  onAnchorClick,
}: {
  bounds: { x: number; y: number; w: number; h: number };
  elementId: string;
  activeSource?: DiagramEndpointBinding | null;
  activeTarget?: DiagramEndpointBinding | null;
  onAnchorPointerDown: (event: React.PointerEvent, binding: DiagramEndpointBinding) => void;
  onAnchorClick: (event: React.SyntheticEvent, binding: DiagramEndpointBinding) => void;
}) {
  return (
    <g data-editor-ui="true">
      {CONNECTOR_ANCHORS.map((anchor) => {
        const point = getAnchorPoint(bounds, anchor);
        const binding = { elementId, anchor };
        const active = activeSource?.elementId === elementId && activeSource.anchor === anchor;
        const target = activeTarget?.elementId === elementId && activeTarget.anchor === anchor;
        return (
          <g key={anchor}>
            <circle
              cx={point.x}
              cy={point.y}
              r={ANCHOR_DOT_RADIUS + 7}
              fill="transparent"
              cursor="crosshair"
              pointerEvents="all"
              onPointerDown={(event) => onAnchorPointerDown(event, binding)}
              onClick={(event) => onAnchorClick(event, binding)}
            />
            <circle
              cx={point.x}
              cy={point.y}
              r={active || target ? ANCHOR_DOT_RADIUS + 1.5 : ANCHOR_DOT_RADIUS}
              fill={active ? "#4f7d75" : target ? "#38bdf8" : "#ffffff"}
              stroke={active ? "#436b65" : target ? "#4a7d96" : "#4f7d75"}
              strokeWidth={2}
              pointerEvents="none"
            />
          </g>
        );
      })}
    </g>
  );
}

function Connector({
  connector,
  elements,
  selected,
  onSelect,
  onEndpointDrag,
  onPanStart,
  panMode,
  controlsOnly,
}: {
  connector: DiagramConnectorElement;
  elements: DiagramElement[];
  selected: boolean;
  onSelect: (event: React.SyntheticEvent) => void;
  onEndpointDrag: (event: React.PointerEvent, elementId: string, type: DragState["type"], handle?: ResizeHandle, endpoint?: "from" | "to") => void;
  onPanStart?: (event: React.PointerEvent) => void;
  panMode?: boolean;
  controlsOnly?: boolean;
}) {
  const from = resolveEndpoint(connector.from, elements);
  const to = resolveEndpoint(connector.to, elements);
  const routing = connector.routing || "straight";
  const d = connectorPath(routing, from, to, connector.controlPoint);
  const stroke = connector.stroke || "#1c1917";
  const labelPoint = { x: (from.x + to.x) / 2, y: (from.y + to.y) / 2 };
  const controlPoint = connectorSupportsControlPoint(routing)
    ? connector.controlPoint || defaultConnectorControlPoint(routing, from, to)
    : null;

  return (
    <g
      onPointerDown={(event) => {
        if (panMode) {
          onPanStart?.(event);
          return;
        }
        event.stopPropagation();
      }}
      onClick={(event) => {
        if (panMode) {
          event.stopPropagation();
          return;
        }
        onSelect(event);
      }}
      style={{ cursor: panMode ? "grab" : "pointer" }}
    >
      {!controlsOnly && (
        <>
          <path
            d={d}
            fill="none"
            stroke={stroke}
            strokeWidth={connector.strokeWidth || 2}
            strokeDasharray={dashArray(connector.strokeDash)}
            markerEnd={connector.arrowEnd ? "url(#diagram-arrow-end)" : undefined}
            markerStart={connector.arrowStart ? "url(#diagram-arrow-start)" : undefined}
          />
          <path d={d} fill="none" stroke="transparent" strokeWidth={18} />
          {connector.label && (
            <text x={labelPoint.x} y={labelPoint.y - 8} textAnchor="middle" fontSize={connector.textStyle?.fontSize || 14} fill={connector.textStyle?.color || "#1c1917"} fontFamily={connector.textStyle?.fontFamily}>
              {connector.label}
            </text>
          )}
        </>
      )}
      {selected && !panMode && (
        <g data-editor-ui="true">
          <path d={d} fill="none" stroke="#4f7d75" strokeWidth={(connector.strokeWidth || 2) + 4} opacity={0.2} />
          {controlPoint && (
            <>
              {routing === "curve" && (
                <path
                  d={`M ${from.x} ${from.y} L ${controlPoint.x} ${controlPoint.y} L ${to.x} ${to.y}`}
                  fill="none"
                  stroke="#4f7d75"
                  strokeWidth={1.5}
                  strokeDasharray="4 4"
                  opacity={0.55}
                  pointerEvents="none"
                />
              )}
              <circle
                cx={controlPoint.x}
                cy={controlPoint.y}
                r={8}
                fill="#f3ecd6"
                stroke="#4f7d75"
                strokeWidth={2}
                cursor="grab"
                onPointerDown={(event) => onEndpointDrag(event, connector.id, "connector-control")}
              >
                <title>{routing === "curve" ? "Drag curve control point" : "Drag elbow bend point"}</title>
              </circle>
            </>
          )}
          {(["from", "to"] as const).map((endpoint) => {
            const point = endpoint === "from" ? from : to;
            return (
              <circle
                key={endpoint}
                cx={point.x}
                cy={point.y}
                r={7}
                fill="#ffffff"
                stroke="#4f7d75"
                strokeWidth={2}
                cursor="crosshair"
                onPointerDown={(event) => onEndpointDrag(event, connector.id, "connector-end", undefined, endpoint)}
              />
            );
          })}
        </g>
      )}
    </g>
  );
}

function Inspector({
  element,
  shapes,
  onChange,
}: {
  element: DiagramElement;
  shapes: Array<DiagramShapeElement | DiagramTextElement>;
  onChange: (updater: (element: DiagramElement) => DiagramElement) => void;
}) {
  if (element.kind === "connector") {
    return (
      <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
        <InspectorTitle label="Connector" id={element.id} />
        <label style={checkStyle}>
          <input type="checkbox" checked={Boolean(element.locked)} onChange={(event) => onChange((current) => ({ ...current, locked: event.target.checked }))} />
          Locked
        </label>
        <Field label="Stroke"><ColorInput value={element.stroke || "#1c1917"} onChange={(stroke) => onChange((current) => ({ ...(current as DiagramConnectorElement), stroke }))} /></Field>
        <NumberField label="Width" value={element.strokeWidth || 2} min={1} max={24} onChange={(strokeWidth) => onChange((current) => ({ ...(current as DiagramConnectorElement), strokeWidth }))} />
        <Field label="Routing">
          <select value={element.routing || "straight"} onChange={(event) => onChange((current) => ({ ...(current as DiagramConnectorElement), routing: event.target.value as DiagramConnectorRouting }))} style={selectStyle}>
            <option value="straight">Straight</option>
            <option value="elbow">Elbow</option>
            <option value="orthogonal">Orthogonal</option>
            <option value="curve">Curve</option>
          </select>
        </Field>
        <Field label="Line">
          <select value={element.strokeDash || "none"} onChange={(event) => onChange((current) => ({ ...(current as DiagramConnectorElement), strokeDash: event.target.value as DiagramConnectorElement["strokeDash"] }))} style={selectStyle}>
            <option value="none">Solid</option>
            <option value="dash">Dashed</option>
            <option value="dot">Dotted</option>
          </select>
        </Field>
        <Field label="Label">
          <input value={element.label || ""} onChange={(event) => onChange((current) => ({ ...(current as DiagramConnectorElement), label: event.target.value }))} style={inputStyle} />
        </Field>
        <Field label="From">
          <EndpointSelect endpoint={element.from.bind} shapes={shapes} onChange={(bind) => onChange((current) => ({ ...(current as DiagramConnectorElement), from: { bind } }))} />
        </Field>
        <Field label="To">
          <EndpointSelect endpoint={element.to.bind} shapes={shapes} onChange={(bind) => onChange((current) => ({ ...(current as DiagramConnectorElement), to: { bind } }))} />
        </Field>
        <label style={checkStyle}>
          <input type="checkbox" checked={Boolean(element.arrowStart)} onChange={(event) => onChange((current) => ({ ...(current as DiagramConnectorElement), arrowStart: event.target.checked }))} />
          Arrow start
        </label>
        <label style={checkStyle}>
          <input type="checkbox" checked={Boolean(element.arrowEnd)} onChange={(event) => onChange((current) => ({ ...(current as DiagramConnectorElement), arrowEnd: event.target.checked }))} />
          Arrow end
        </label>
      </div>
    );
  }

  const text = element.kind === "shape" ? element.text || "" : element.text;
  const style = element.textStyle || {};
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      <InspectorTitle label={element.kind === "shape" ? "Shape" : "Text"} id={element.id} />
      <label style={checkStyle}>
        <input type="checkbox" checked={Boolean(element.locked)} onChange={(event) => onChange((current) => ({ ...current, locked: event.target.checked }))} />
        Locked
      </label>
      {element.kind === "shape" && (
        <>
          <Field label="Shape">
            <select value={element.shape} onChange={(event) => onChange((current) => ({ ...(current as DiagramShapeElement), shape: event.target.value as DiagramShapeElement["shape"] }))} style={selectStyle}>
              <option value="rect">Rectangle</option>
              <option value="roundRect">Round rect</option>
              <option value="ellipse">Ellipse</option>
              <option value="diamond">Diamond</option>
              <option value="triangle">Triangle</option>
              <option value="hexagon">Hexagon</option>
              <option value="parallelogram">Parallelogram</option>
              <option value="trapezoid">Trapezoid</option>
              <option value="cylinder">Cylinder</option>
              <option value="document">Document</option>
              <option value="rightArrow">Right arrow</option>
              <option value="downArrow">Down arrow</option>
            </select>
          </Field>
          <Field label="Fill"><ColorInput value={element.fill || "#ffffff"} allowTransparent onChange={(fill) => onChange((current) => ({ ...(current as DiagramShapeElement), fill }))} /></Field>
          <Field label="Stroke"><ColorInput value={element.stroke || "#1c1917"} onChange={(stroke) => onChange((current) => ({ ...(current as DiagramShapeElement), stroke }))} /></Field>
          <NumberField label="Stroke width" value={element.strokeWidth || 0} min={0} max={24} onChange={(strokeWidth) => onChange((current) => ({ ...(current as DiagramShapeElement), strokeWidth }))} />
        </>
      )}
      <Field label="Text">
        <textarea value={text} onChange={(event) => onChange((current) => current.kind === "shape" ? { ...current, text: event.target.value } : { ...current, text: event.target.value })} style={{ ...inputStyle, height: 78, resize: "vertical" }} />
      </Field>
      <Field label="Text color"><ColorInput value={style.color || "#1c1917"} onChange={(color) => onChange((current) => ({ ...current, textStyle: { ...("textStyle" in current ? current.textStyle : {}), color } } as DiagramElement))} /></Field>
      <NumberField label="Font size" value={style.fontSize || 18} min={6} max={96} onChange={(fontSize) => onChange((current) => ({ ...current, textStyle: { ...("textStyle" in current ? current.textStyle : {}), fontSize } } as DiagramElement))} />
      <NumberField label="X" value={element.x} min={-2000} max={4000} onChange={(x) => onChange((current) => current.kind === "connector" ? current : { ...current, x })} />
      <NumberField label="Y" value={element.y} min={-2000} max={4000} onChange={(y) => onChange((current) => current.kind === "connector" ? current : { ...current, y })} />
      <NumberField label="W" value={element.w} min={12} max={4000} onChange={(w) => onChange((current) => current.kind === "connector" ? current : { ...current, w })} />
      <NumberField label="H" value={element.h} min={12} max={4000} onChange={(h) => onChange((current) => current.kind === "connector" ? current : { ...current, h })} />
    </div>
  );
}

function CanvasInspector({
  document,
  onChange,
}: {
  document: EditableDiagramDocument;
  onChange: (patch: Partial<EditableDiagramDocument>) => void;
}) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      <InspectorTitle label="Canvas" id={document.id} />
      <Field label="Title">
        <input value={document.title} onChange={(event) => onChange({ title: event.target.value })} style={inputStyle} />
      </Field>
      <NumberField label="Width" value={document.canvas.width} min={320} max={10000} onChange={(width) => onChange({ canvas: { ...document.canvas, width } })} />
      <NumberField label="Height" value={document.canvas.height} min={180} max={10000} onChange={(height) => onChange({ canvas: { ...document.canvas, height } })} />
      <div style={{ padding: 10, background: "#fafaf9", border: "1px solid rgba(28,25,23,0.06)", borderRadius: 8, color: "#78716c", fontSize: 12, lineHeight: 1.45 }}>
        {document.elements.length} objects · {document.elements.filter((element) => element.kind === "connector").length} connectors
      </div>
    </div>
  );
}

function EndpointSelect({
  endpoint,
  shapes,
  onChange,
}: {
  endpoint?: { elementId: string; anchor: DiagramAnchor };
  shapes: Array<DiagramShapeElement | DiagramTextElement>;
  onChange: (bind: { elementId: string; anchor: DiagramAnchor }) => void;
}) {
  return (
    <div style={{ display: "grid", gridTemplateColumns: "1fr 84px", gap: 6 }}>
      <select value={endpoint?.elementId || ""} onChange={(event) => onChange({ elementId: event.target.value, anchor: endpoint?.anchor || "center" })} style={selectStyle}>
        {shapes.map((shape) => (
          <option key={shape.id} value={shape.id}>{shape.kind === "shape" ? shape.text || shape.name || shape.id : shape.text || shape.id}</option>
        ))}
      </select>
      <select value={endpoint?.anchor || "center"} onChange={(event) => endpoint && onChange({ ...endpoint, anchor: event.target.value as DiagramAnchor })} style={selectStyle}>
        <option value="top">Top</option>
        <option value="right">Right</option>
        <option value="bottom">Bottom</option>
        <option value="left">Left</option>
        <option value="center">Center</option>
      </select>
    </div>
  );
}

function InspectorTitle({ label, id }: { label: string; id: string }) {
  return (
    <div>
      <h3 style={{ margin: 0, fontSize: 14, color: "#1c1917" }}>{label}</h3>
      <p style={{ margin: "4px 0 0", fontSize: 11, color: "#a8a29e", overflow: "hidden", textOverflow: "ellipsis" }}>{id}</p>
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label style={{ display: "flex", flexDirection: "column", gap: 5, fontSize: 11, fontWeight: 700, color: "#78716c", textTransform: "uppercase", letterSpacing: 0 }}>
      {label}
      {children}
    </label>
  );
}

function NumberField({ label, value, min, max, onChange }: { label: string; value: number; min: number; max: number; onChange: (value: number) => void }) {
  return (
    <Field label={label}>
      <input type="number" min={min} max={max} value={Math.round(value * 100) / 100} onChange={(event) => onChange(Number(event.target.value) || 0)} style={inputStyle} />
    </Field>
  );
}

function ColorInput({ value, allowTransparent, onChange }: { value: string; allowTransparent?: boolean; onChange: (value: string) => void }) {
  const isTransparent = value === "transparent";
  return (
    <div style={{ display: "flex", gap: 6 }}>
      <input
        type="color"
        value={isTransparent ? "#ffffff" : value}
        onChange={(event) => onChange(event.target.value)}
        style={{ width: 36, height: 32, border: "1px solid rgba(28,25,23,0.06)", borderRadius: 6, padding: 0, background: "#fff" }}
      />
      <input value={value} onChange={(event) => onChange(event.target.value)} style={{ ...inputStyle, flex: 1 }} />
      {allowTransparent && (
        <button type="button" onClick={() => onChange(isTransparent ? "#ffffff" : "transparent")} className="btn-manor-ghost" style={{ padding: "0 8px", fontSize: 11 }}>
          Clear
        </button>
      )}
    </div>
  );
}

function IconButton({ title, children, onClick, disabled, active }: { title: string; children: React.ReactNode; onClick: () => void; disabled?: boolean; active?: boolean }) {
  return (
    <button
      title={title}
      onClick={onClick}
      disabled={disabled}
      style={{
        width: 32, height: 32, display: "inline-flex", alignItems: "center", justifyContent: "center",
        borderRadius: 7, border: active ? "1px solid #4f7d75" : "1px solid #e2dfdc",
        background: active ? "#e5eeeb" : "#ffffff", color: active ? "#436b65" : "#44403c",
        opacity: disabled ? 0.38 : 1, cursor: disabled ? "default" : "pointer",
      }}
    >
      {children}
    </button>
  );
}

function Divider() {
  return <div style={{ width: 1, height: 22, background: "#e2dfdc", margin: "0 2px" }} />;
}

function ShapeIcon({ kind }: { kind: DiagramShapeElement["shape"] }) {
  if (kind === "ellipse") return <svg width="16" height="16"><ellipse cx="8" cy="8" rx="6" ry="5" fill="none" stroke="currentColor" strokeWidth="1.8" /></svg>;
  if (kind === "diamond") return <svg width="16" height="16"><path d="M8 1.5 14.5 8 8 14.5 1.5 8Z" fill="none" stroke="currentColor" strokeWidth="1.8" /></svg>;
  if (kind === "triangle") return <svg width="16" height="16"><path d="M8 2 14 14H2Z" fill="none" stroke="currentColor" strokeWidth="1.8" /></svg>;
  if (kind === "hexagon") return <svg width="16" height="16"><path d="M5 2h6l3 6-3 6H5L2 8Z" fill="none" stroke="currentColor" strokeWidth="1.8" /></svg>;
  if (kind === "parallelogram") return <svg width="16" height="16"><path d="M5 3h9l-3 10H2Z" fill="none" stroke="currentColor" strokeWidth="1.8" /></svg>;
  if (kind === "trapezoid") return <svg width="16" height="16"><path d="M5 3h6l4 10H1Z" fill="none" stroke="currentColor" strokeWidth="1.8" /></svg>;
  if (kind === "cylinder") return <svg width="16" height="16"><path d="M3 4c0-1.3 10-1.3 10 0v8c0 1.3-10 1.3-10 0Z" fill="none" stroke="currentColor" strokeWidth="1.6" /><path d="M3 4c0 1.3 10 1.3 10 0" fill="none" stroke="currentColor" strokeWidth="1.6" /></svg>;
  if (kind === "document") return <svg width="16" height="16"><path d="M3 2h10v11c-2-1.2-4 1.2-6 0s-2 1.2-4 0Z" fill="none" stroke="currentColor" strokeWidth="1.6" /></svg>;
  if (kind === "rightArrow") return <svg width="16" height="16"><path d="M2 5h7V2l5 6-5 6v-3H2Z" fill="none" stroke="currentColor" strokeWidth="1.5" /></svg>;
  if (kind === "downArrow") return <svg width="16" height="16"><path d="M5 2h6v7h3l-6 5-6-5h3Z" fill="none" stroke="currentColor" strokeWidth="1.5" /></svg>;
  return <svg width="16" height="16"><rect x="2.5" y="3" width="11" height="10" rx={kind === "roundRect" ? 3 : 0} fill="none" stroke="currentColor" strokeWidth="1.8" /></svg>;
}

function renderShape(element: DiagramShapeElement, fill?: string, stroke?: string, strokeWidth?: number, dash?: string) {
  const common = { fill: fill || "transparent", stroke: stroke || "transparent", strokeWidth, strokeDasharray: dash, opacity: element.opacity };
  if (element.shape === "ellipse") {
    return <ellipse cx={element.x + element.w / 2} cy={element.y + element.h / 2} rx={element.w / 2} ry={element.h / 2} {...common} />;
  }
  if (element.shape === "diamond") {
    return <polygon points={`${element.x + element.w / 2},${element.y} ${element.x + element.w},${element.y + element.h / 2} ${element.x + element.w / 2},${element.y + element.h} ${element.x},${element.y + element.h / 2}`} {...common} />;
  }
  if (element.shape === "triangle") {
    return <polygon points={`${element.x + element.w / 2},${element.y} ${element.x + element.w},${element.y + element.h} ${element.x},${element.y + element.h}`} {...common} />;
  }
  if (element.shape === "hexagon") {
    const q = element.w * 0.18;
    return <polygon points={`${element.x + q},${element.y} ${element.x + element.w - q},${element.y} ${element.x + element.w},${element.y + element.h / 2} ${element.x + element.w - q},${element.y + element.h} ${element.x + q},${element.y + element.h} ${element.x},${element.y + element.h / 2}`} {...common} />;
  }
  if (element.shape === "parallelogram") {
    const q = element.w * 0.18;
    return <polygon points={`${element.x + q},${element.y} ${element.x + element.w},${element.y} ${element.x + element.w - q},${element.y + element.h} ${element.x},${element.y + element.h}`} {...common} />;
  }
  if (element.shape === "trapezoid") {
    const q = element.w * 0.18;
    return <polygon points={`${element.x + q},${element.y} ${element.x + element.w - q},${element.y} ${element.x + element.w},${element.y + element.h} ${element.x},${element.y + element.h}`} {...common} />;
  }
  if (element.shape === "cylinder") {
    const ry = Math.min(18, element.h * 0.18);
    return (
      <g>
        <path d={`M ${element.x} ${element.y + ry} C ${element.x} ${element.y - ry / 2} ${element.x + element.w} ${element.y - ry / 2} ${element.x + element.w} ${element.y + ry} L ${element.x + element.w} ${element.y + element.h - ry} C ${element.x + element.w} ${element.y + element.h + ry / 2} ${element.x} ${element.y + element.h + ry / 2} ${element.x} ${element.y + element.h - ry} Z`} {...common} />
        <ellipse cx={element.x + element.w / 2} cy={element.y + ry} rx={element.w / 2} ry={ry} fill="none" stroke={stroke || "transparent"} strokeWidth={strokeWidth} strokeDasharray={dash} />
      </g>
    );
  }
  if (element.shape === "document") {
    const wave = Math.min(18, element.h * 0.18);
    return <path d={`M ${element.x} ${element.y} H ${element.x + element.w} V ${element.y + element.h - wave} C ${element.x + element.w * 0.72} ${element.y + element.h - wave * 2} ${element.x + element.w * 0.52} ${element.y + element.h + wave * 0.2} ${element.x + element.w * 0.26} ${element.y + element.h - wave * 0.8} C ${element.x + element.w * 0.16} ${element.y + element.h - wave * 1.2} ${element.x + element.w * 0.08} ${element.y + element.h - wave * 0.6} ${element.x} ${element.y + element.h - wave} Z`} {...common} />;
  }
  if (element.shape === "rightArrow") {
    const head = Math.min(element.w * 0.32, element.h * 0.9);
    return <polygon points={`${element.x},${element.y + element.h * 0.24} ${element.x + element.w - head},${element.y + element.h * 0.24} ${element.x + element.w - head},${element.y} ${element.x + element.w},${element.y + element.h / 2} ${element.x + element.w - head},${element.y + element.h} ${element.x + element.w - head},${element.y + element.h * 0.76} ${element.x},${element.y + element.h * 0.76}`} {...common} />;
  }
  if (element.shape === "downArrow") {
    const head = Math.min(element.h * 0.34, element.w * 0.45);
    return <polygon points={`${element.x + element.w * 0.24},${element.y} ${element.x + element.w * 0.76},${element.y} ${element.x + element.w * 0.76},${element.y + element.h - head} ${element.x + element.w},${element.y + element.h - head} ${element.x + element.w / 2},${element.y + element.h} ${element.x},${element.y + element.h - head} ${element.x + element.w * 0.24},${element.y + element.h - head}`} {...common} />;
  }
  return <rect x={element.x} y={element.y} width={element.w} height={element.h} rx={element.shape === "roundRect" ? element.radius || 16 : 0} {...common} />;
}

function dashArray(dash?: "none" | "dash" | "dot"): string | undefined {
  if (dash === "dash") return "8 5";
  if (dash === "dot") return "2 5";
  return undefined;
}

function handlePoint(bounds: { x: number; y: number; w: number; h: number }, handle: ResizeHandle) {
  return {
    x: handle.includes("w") ? bounds.x : bounds.x + bounds.w,
    y: handle.includes("n") ? bounds.y : bounds.y + bounds.h,
  };
}

function connectorOppositeBinding(elements: DiagramElement[], connectorId: string, endpoint: "from" | "to"): DiagramEndpointBinding | undefined {
  const connector = elements.find((element): element is DiagramConnectorElement => element.id === connectorId && element.kind === "connector");
  if (!connector) return undefined;
  return endpoint === "from" ? connector.to.bind : connector.from.bind;
}

function pointIsNearElement(point: { x: number; y: number }, element: DiagramElement, threshold: number): boolean {
  const bounds = getElementBounds(element);
  if (!bounds) return false;
  return (
    point.x >= bounds.x - threshold
    && point.x <= bounds.x + bounds.w + threshold
    && point.y >= bounds.y - threshold
    && point.y <= bounds.y + bounds.h + threshold
  );
}

function findNearestAnchor(
  point: { x: number; y: number },
  elements: DiagramElement[],
  source?: DiagramEndpointBinding,
  threshold = CONNECTOR_SNAP_PX,
): { binding: DiagramEndpointBinding; point: { x: number; y: number }; distance: number } | null {
  let best: { binding: DiagramEndpointBinding; point: { x: number; y: number }; distance: number; containsPoint: boolean } | null = null;
  for (const element of elements) {
    if (element.kind === "connector") continue;
    if (source?.elementId === element.id) continue;
    const bounds = getElementBounds(element);
    if (!bounds) continue;
    const containsPoint = point.x >= bounds.x && point.x <= bounds.x + bounds.w && point.y >= bounds.y && point.y <= bounds.y + bounds.h;
    for (const anchor of CONNECTOR_ANCHORS) {
      const anchorPoint = getAnchorPoint(bounds, anchor);
      const distance = Math.hypot(point.x - anchorPoint.x, point.y - anchorPoint.y);
      if (!containsPoint && distance > threshold) continue;
      if (!best || (containsPoint && !best.containsPoint) || distance < best.distance) {
        best = {
          binding: { elementId: element.id, anchor },
          point: anchorPoint,
          distance,
          containsPoint,
        };
      }
    }
  }
  if (!best) return null;
  return {
    binding: best.binding,
    point: best.point,
    distance: best.distance,
  };
}

function connectorSupportsControlPoint(routing: DiagramConnectorRouting): boolean {
  return routing === "curve" || routing === "elbow" || routing === "orthogonal";
}

function defaultConnectorControlPoint(
  routing: DiagramConnectorRouting,
  from: { x: number; y: number },
  to: { x: number; y: number },
): { x: number; y: number } {
  const midX = from.x + (to.x - from.x) / 2;
  const midY = from.y + (to.y - from.y) / 2;
  if (routing === "curve") {
    const dx = to.x - from.x;
    const dy = to.y - from.y;
    const distance = Math.hypot(dx, dy) || 1;
    const offset = Math.min(140, Math.max(48, distance * 0.2));
    return {
      x: midX - (dy / distance) * offset,
      y: midY + (dx / distance) * offset,
    };
  }
  if (routing === "orthogonal") {
    return { x: midX, y: to.y };
  }
  if (routing === "elbow") {
    return { x: to.x, y: midY };
  }
  return { x: midX, y: midY };
}

function connectorPath(
  routing: DiagramConnectorRouting,
  from: { x: number; y: number },
  to: { x: number; y: number },
  controlPoint?: { x: number; y: number },
): string {
  const control = controlPoint || defaultConnectorControlPoint(routing, from, to);
  if (routing === "curve") {
    return `M ${from.x} ${from.y} Q ${control.x} ${control.y} ${to.x} ${to.y}`;
  }
  if (routing === "orthogonal") {
    return `M ${from.x} ${from.y} L ${control.x} ${from.y} L ${control.x} ${control.y} L ${to.x} ${control.y} L ${to.x} ${to.y}`;
  }
  if (routing === "elbow") {
    return `M ${from.x} ${from.y} L ${from.x} ${control.y} L ${control.x} ${control.y} L ${control.x} ${to.y} L ${to.x} ${to.y}`;
  }
  return `M ${from.x} ${from.y} L ${to.x} ${to.y}`;
}

function ensureCanvasFitsDocument(document: EditableDiagramDocument, margin = 160): EditableDiagramDocument {
  const fitted = fitCanvasToContent(document, margin, true);
  return fitted;
}

function fitCanvasToContent(document: EditableDiagramDocument, margin = 120, growOnly = false): EditableDiagramDocument {
  const bounds = contentBounds(document.elements);
  if (!bounds) return document;
  const currentOriginX = document.canvas.originX ?? 0;
  const currentOriginY = document.canvas.originY ?? 0;
  const currentRight = currentOriginX + document.canvas.width;
  const currentBottom = currentOriginY + document.canvas.height;
  const contentOriginX = Math.floor(bounds.x - margin);
  const contentOriginY = Math.floor(bounds.y - margin);
  const contentRight = Math.ceil(bounds.x + bounds.w + margin);
  const contentBottom = Math.ceil(bounds.y + bounds.h + margin);
  const originX = growOnly ? Math.min(currentOriginX, contentOriginX) : contentOriginX;
  const originY = growOnly ? Math.min(currentOriginY, contentOriginY) : contentOriginY;
  const right = growOnly ? Math.max(currentRight, contentRight) : contentRight;
  const bottom = growOnly ? Math.max(currentBottom, contentBottom) : contentBottom;
  const width = Math.min(10000, Math.max(320, right - originX));
  const height = Math.min(10000, Math.max(180, bottom - originY));
  if (
    originX === currentOriginX
    && originY === currentOriginY
    && width === document.canvas.width
    && height === document.canvas.height
  ) {
    return document;
  }
  return {
    ...document,
    canvas: { ...document.canvas, originX, originY, width, height },
  };
}

function contentBounds(elements: DiagramElement[]) {
  const boxes: Array<{ x: number; y: number; w: number; h: number }> = [];
  elements.forEach((element) => {
    if (element.kind === "connector") {
      const from = resolveEndpoint(element.from, elements);
      const to = resolveEndpoint(element.to, elements);
      const controlPoint = connectorSupportsControlPoint(element.routing || "straight")
        ? element.controlPoint || defaultConnectorControlPoint(element.routing || "straight", from, to)
        : null;
      const points = controlPoint ? [from, to, controlPoint] : [from, to];
      const minX = Math.min(...points.map((point) => point.x));
      const minY = Math.min(...points.map((point) => point.y));
      const maxX = Math.max(...points.map((point) => point.x));
      const maxY = Math.max(...points.map((point) => point.y));
      boxes.push({
        x: minX,
        y: minY,
        w: Math.max(1, maxX - minX),
        h: Math.max(1, maxY - minY),
      });
      return;
    }
    boxes.push({ x: element.x, y: element.y, w: element.w, h: element.h });
  });
  if (!boxes.length) return null;
  const minX = Math.min(...boxes.map((box) => box.x));
  const minY = Math.min(...boxes.map((box) => box.y));
  const maxX = Math.max(...boxes.map((box) => box.x + box.w));
  const maxY = Math.max(...boxes.map((box) => box.y + box.h));
  return { x: minX, y: minY, w: maxX - minX, h: maxY - minY };
}

function buildExportSvg(document: EditableDiagramDocument): string {
  const exportBounds = fitCanvasToContent(document, 96, false).canvas;
  const originX = exportBounds.originX ?? 0;
  const originY = exportBounds.originY ?? 0;
  const body = [
    `<rect x="${originX}" y="${originY}" width="${exportBounds.width}" height="${exportBounds.height}" fill="#ffffff"/>`,
    ...document.elements.filter((element) => element.kind === "connector").map((element) => connectorToSvg(element as DiagramConnectorElement, document.elements)),
    ...document.elements.filter((element) => element.kind !== "connector").map((element) => objectToSvg(element as DiagramShapeElement | DiagramTextElement)),
  ].join("\n");
  return [
    `<svg xmlns="http://www.w3.org/2000/svg" width="${exportBounds.width}" height="${exportBounds.height}" viewBox="${originX} ${originY} ${exportBounds.width} ${exportBounds.height}">`,
    `<defs>`,
    `<marker id="diagram-arrow-end" markerWidth="14" markerHeight="14" refX="12" refY="7" orient="auto" markerUnits="userSpaceOnUse"><path d="M1,1 L13,7 L1,13 Z" fill="context-stroke"/></marker>`,
    `<marker id="diagram-arrow-start" markerWidth="14" markerHeight="14" refX="2" refY="7" orient="auto-start-reverse" markerUnits="userSpaceOnUse"><path d="M13,1 L1,7 L13,13 Z" fill="context-stroke"/></marker>`,
    `</defs>`,
    body,
    `</svg>`,
  ].join("\n");
}

function objectToSvg(element: DiagramShapeElement | DiagramTextElement): string {
  const parts: string[] = [];
  if (element.kind === "shape") parts.push(shapeToSvg(element));
  const text = element.kind === "shape" ? element.text : element.text;
  if (text) parts.push(textToSvg(text, element.x, element.y, element.w, element.h, element.textStyle, element.kind === "shape" ? 10 : 0));
  return parts.join("\n");
}

function shapeToSvg(element: DiagramShapeElement): string {
  const common = svgCommon(element.fill || "transparent", element.stroke || "transparent", element.strokeWidth || 0, element.strokeDash, element.opacity);
  if (element.shape === "ellipse") return `<ellipse cx="${element.x + element.w / 2}" cy="${element.y + element.h / 2}" rx="${element.w / 2}" ry="${element.h / 2}" ${common}/>`;
  if (element.shape === "diamond") return `<polygon points="${element.x + element.w / 2},${element.y} ${element.x + element.w},${element.y + element.h / 2} ${element.x + element.w / 2},${element.y + element.h} ${element.x},${element.y + element.h / 2}" ${common}/>`;
  if (element.shape === "triangle") return `<polygon points="${element.x + element.w / 2},${element.y} ${element.x + element.w},${element.y + element.h} ${element.x},${element.y + element.h}" ${common}/>`;
  if (element.shape === "hexagon") {
    const q = element.w * 0.18;
    return `<polygon points="${element.x + q},${element.y} ${element.x + element.w - q},${element.y} ${element.x + element.w},${element.y + element.h / 2} ${element.x + element.w - q},${element.y + element.h} ${element.x + q},${element.y + element.h} ${element.x},${element.y + element.h / 2}" ${common}/>`;
  }
  if (element.shape === "parallelogram") {
    const q = element.w * 0.18;
    return `<polygon points="${element.x + q},${element.y} ${element.x + element.w},${element.y} ${element.x + element.w - q},${element.y + element.h} ${element.x},${element.y + element.h}" ${common}/>`;
  }
  if (element.shape === "trapezoid") {
    const q = element.w * 0.18;
    return `<polygon points="${element.x + q},${element.y} ${element.x + element.w - q},${element.y} ${element.x + element.w},${element.y + element.h} ${element.x},${element.y + element.h}" ${common}/>`;
  }
  if (element.shape === "cylinder") {
    const ry = Math.min(18, element.h * 0.18);
    const d = `M ${element.x} ${element.y + ry} C ${element.x} ${element.y - ry / 2} ${element.x + element.w} ${element.y - ry / 2} ${element.x + element.w} ${element.y + ry} L ${element.x + element.w} ${element.y + element.h - ry} C ${element.x + element.w} ${element.y + element.h + ry / 2} ${element.x} ${element.y + element.h + ry / 2} ${element.x} ${element.y + element.h - ry} Z`;
    return `<path d="${d}" ${common}/><ellipse cx="${element.x + element.w / 2}" cy="${element.y + ry}" rx="${element.w / 2}" ry="${ry}" fill="none" stroke="${xmlAttr(element.stroke || "transparent")}" stroke-width="${element.strokeWidth || 0}"${dashSvgAttr(element.strokeDash)}/>`;
  }
  if (element.shape === "document") {
    const wave = Math.min(18, element.h * 0.18);
    return `<path d="M ${element.x} ${element.y} H ${element.x + element.w} V ${element.y + element.h - wave} C ${element.x + element.w * 0.72} ${element.y + element.h - wave * 2} ${element.x + element.w * 0.52} ${element.y + element.h + wave * 0.2} ${element.x + element.w * 0.26} ${element.y + element.h - wave * 0.8} C ${element.x + element.w * 0.16} ${element.y + element.h - wave * 1.2} ${element.x + element.w * 0.08} ${element.y + element.h - wave * 0.6} ${element.x} ${element.y + element.h - wave} Z" ${common}/>`;
  }
  if (element.shape === "rightArrow") {
    const head = Math.min(element.w * 0.32, element.h * 0.9);
    return `<polygon points="${element.x},${element.y + element.h * 0.24} ${element.x + element.w - head},${element.y + element.h * 0.24} ${element.x + element.w - head},${element.y} ${element.x + element.w},${element.y + element.h / 2} ${element.x + element.w - head},${element.y + element.h} ${element.x + element.w - head},${element.y + element.h * 0.76} ${element.x},${element.y + element.h * 0.76}" ${common}/>`;
  }
  if (element.shape === "downArrow") {
    const head = Math.min(element.h * 0.34, element.w * 0.45);
    return `<polygon points="${element.x + element.w * 0.24},${element.y} ${element.x + element.w * 0.76},${element.y} ${element.x + element.w * 0.76},${element.y + element.h - head} ${element.x + element.w},${element.y + element.h - head} ${element.x + element.w / 2},${element.y + element.h} ${element.x},${element.y + element.h - head} ${element.x + element.w * 0.24},${element.y + element.h - head}" ${common}/>`;
  }
  return `<rect x="${element.x}" y="${element.y}" width="${element.w}" height="${element.h}" rx="${element.shape === "roundRect" ? element.radius || 16 : 0}" ${common}/>`;
}

function connectorToSvg(connector: DiagramConnectorElement, elements: DiagramElement[]): string {
  const from = resolveEndpoint(connector.from, elements);
  const to = resolveEndpoint(connector.to, elements);
  const d = connectorPath(connector.routing || "straight", from, to, connector.controlPoint);
  const line = `<path d="${d}" fill="none" stroke="${xmlAttr(connector.stroke || "#1c1917")}" stroke-width="${connector.strokeWidth || 2}"${dashSvgAttr(connector.strokeDash)}${connector.arrowStart ? ` marker-start="url(#diagram-arrow-start)"` : ""}${connector.arrowEnd ? ` marker-end="url(#diagram-arrow-end)"` : ""}/>`;
  if (!connector.label) return line;
  const labelPoint = { x: (from.x + to.x) / 2, y: (from.y + to.y) / 2 - 8 };
  return `${line}\n${textToSvg(connector.label, labelPoint.x - 80, labelPoint.y - 14, 160, 28, connector.textStyle, 0)}`;
}

function textToSvg(text: string, x: number, y: number, w: number, h: number, style?: { fontFamily?: string; fontSize?: number; fontWeight?: number; fontStyle?: "normal" | "italic"; color?: string; align?: "left" | "center" | "right" }, padding = 0): string {
  const lines = text.split(/\r?\n/);
  const fontSize = style?.fontSize || 18;
  const lineHeight = fontSize * 1.08;
  const anchor = style?.align === "left" ? "start" : style?.align === "right" ? "end" : "middle";
  const textX = style?.align === "left" ? x + padding : style?.align === "right" ? x + w - padding : x + w / 2;
  const firstY = y + h / 2 - ((lines.length - 1) * lineHeight) / 2 + fontSize * 0.34;
  const tspans = lines.map((line, index) => `<tspan x="${textX}" y="${firstY + index * lineHeight}">${xmlText(line)}</tspan>`).join("");
  return `<text text-anchor="${anchor}" font-family="${xmlAttr(style?.fontFamily || "Times New Roman, serif")}" font-size="${fontSize}" font-weight="${style?.fontWeight || 400}" font-style="${style?.fontStyle || "normal"}" fill="${xmlAttr(style?.color || "#1c1917")}">${tspans}</text>`;
}

function svgCommon(fill: string, stroke: string, strokeWidth: number, dash?: "none" | "dash" | "dot", opacity?: number): string {
  return `fill="${xmlAttr(fill)}" stroke="${xmlAttr(stroke)}" stroke-width="${strokeWidth}"${dashSvgAttr(dash)}${opacity === undefined ? "" : ` opacity="${opacity}"`}`;
}

function dashSvgAttr(dash?: "none" | "dash" | "dot"): string {
  const value = dashArray(dash);
  return value ? ` stroke-dasharray="${value}"` : "";
}

function xmlAttr(value: string): string {
  return value.replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function xmlText(value: string): string {
  return value.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function cloneElement(element: DiagramElement): DiagramElement {
  const next = JSON.parse(JSON.stringify(element)) as DiagramElement;
  next.id = createDiagramId(element.kind);
  if (next.kind !== "connector") {
    next.x += 24;
    next.y += 24;
  }
  return next;
}

function downloadBlob(blob: Blob, name: string) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = name.replace(/[^\w\u4e00-\u9fa5.-]+/g, "-");
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

const inputStyle: React.CSSProperties = {
  width: "100%",
  border: "1px solid rgba(28,25,23,0.06)",
  borderRadius: 7,
  padding: "7px 8px",
  fontSize: 12,
  color: "#1c1917",
  background: "#ffffff",
  boxSizing: "border-box",
};

const selectStyle: React.CSSProperties = {
  ...inputStyle,
  padding: "6px 7px",
};

const checkStyle: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: 7,
  fontSize: 12,
  color: "#44403c",
};
