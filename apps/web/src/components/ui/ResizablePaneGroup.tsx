import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type CSSProperties, type PointerEvent, type ReactNode } from "react";
import "./ResizablePaneGroup.css";

export interface ResizablePaneDefinition {
  id: string;
  label: string;
  children: ReactNode;
  initialSize: number;
  minSize?: number;
  maxSize?: number;
  className?: string;
  collapsed?: boolean;
}

interface ResizablePaneGroupProps {
  panes: ResizablePaneDefinition[];
  storageKey: string;
  className?: string;
  onPaneCollapse?: (paneId: string) => void;
  collapseThreshold?: number;
}

interface ActiveDrag {
  handleIndex: number;
  startX: number;
  startSizes: number[];
  containerWidth: number;
  requestedLeftSize: number;
}

interface PaneLayoutState {
  paneKey: string;
  sizes: number[];
}

function normalizedSizes(panes: ResizablePaneDefinition[]): number[] {
  const total = panes.reduce((sum, pane) => sum + Math.max(0, pane.initialSize), 0) || 1;
  return panes.map((pane) => (Math.max(0, pane.initialSize) / total) * 100);
}

function readStoredSizes(storageKey: string, panes: ResizablePaneDefinition[]): number[] | null {
  try {
    const value = JSON.parse(window.localStorage.getItem(storageKey) || "null");
    if (!Array.isArray(value) || value.length !== panes.length) return null;
    const sizes = value.map(Number);
    if (sizes.some((size) => !Number.isFinite(size) || size <= 0)) return null;
    const total = sizes.reduce((sum, size) => sum + size, 0);
    if (total <= 0) return null;
    return sizes.map((size) => (size / total) * 100);
  } catch {
    return null;
  }
}

function clamp(value: number, minimum: number, maximum: number): number {
  return Math.min(maximum, Math.max(minimum, value));
}

export const RESIZABLE_PANE_MOTION_MS = 480;

export default function ResizablePaneGroup({ panes, storageKey, className = "", onPaneCollapse, collapseThreshold = 80 }: ResizablePaneGroupProps) {
  const paneKey = useMemo(() => panes.map((pane) => pane.id).join("\0"), [panes]);
  const initialSizes = useMemo(() => normalizedSizes(panes), [paneKey]);
  const [layout, setLayout] = useState<PaneLayoutState>(() => ({ paneKey, sizes: initialSizes }));
  const sizes = layout.paneKey === paneKey ? layout.sizes : initialSizes;
  const [draggingHandle, setDraggingHandle] = useState<number | null>(null);
  const [ready, setReady] = useState(false);
  const [animating, setAnimating] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);
  const dragRef = useRef<ActiveDrag | null>(null);
  const collapsedSignature = panes.map((pane) => `${pane.id}:${pane.collapsed ? 1 : 0}`).join("|");
  const collapsedSignatureRef = useRef(collapsedSignature);
  const paneKeyRef = useRef(paneKey);
  const displayedSizes = useMemo(
    () => panes.map((pane, index) => (pane.collapsed ? 0 : sizes[index] ?? 0)),
    [panes, sizes],
  );

  const setSizes = useCallback((next: number[] | ((current: number[]) => number[])) => {
    setLayout((current) => {
      const currentSizes = current.paneKey === paneKey ? current.sizes : initialSizes;
      return {
        paneKey,
        sizes: typeof next === "function" ? next(currentSizes) : next,
      };
    });
  }, [initialSizes, paneKey]);

  useLayoutEffect(() => {
    setReady(false);
    setSizes(readStoredSizes(storageKey, panes) || initialSizes);
  }, [initialSizes, paneKey, setSizes, storageKey]);

  useEffect(() => {
    const frame = window.requestAnimationFrame(() => setReady(true));
    return () => window.cancelAnimationFrame(frame);
  }, [paneKey, storageKey]);

  useLayoutEffect(() => {
    if (paneKeyRef.current !== paneKey) {
      paneKeyRef.current = paneKey;
      collapsedSignatureRef.current = collapsedSignature;
      setAnimating(false);
      return;
    }
    if (collapsedSignatureRef.current === collapsedSignature) return;
    collapsedSignatureRef.current = collapsedSignature;
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
      setAnimating(false);
      return;
    }
    setAnimating(true);
    const timer = window.setTimeout(() => setAnimating(false), RESIZABLE_PANE_MOTION_MS);
    return () => window.clearTimeout(timer);
  }, [collapsedSignature, paneKey]);

  const persistSizes = useCallback((nextSizes: number[]) => {
    if (panes.some((pane) => pane.collapsed)) return;
    try {
      window.localStorage.setItem(storageKey, JSON.stringify(nextSizes));
    } catch {
      // Storage can be disabled without affecting the splitter itself.
    }
  }, [panes, storageKey]);

  const finishResize = useCallback((next: number[], handleIndex: number, containerWidth: number, requestedLeftSize: number) => {
    // Detect edge gestures before minimum widths clamp the visible layout.
    // Otherwise a pane with a minSize larger than collapseThreshold can never collapse.
    const pairTotal = next[handleIndex] + next[handleIndex + 1];
    const requestedSizes = { [handleIndex]: requestedLeftSize, [handleIndex + 1]: pairTotal - requestedLeftSize };
    const collapsedIndex = [handleIndex, handleIndex + 1].find(
      (index) => requestedSizes[index] / 100 * containerWidth < collapseThreshold,
    );
    if (onPaneCollapse && collapsedIndex !== undefined) {
      onPaneCollapse(panes[collapsedIndex].id);
      return;
    }
    persistSizes(next);
  }, [collapseThreshold, onPaneCollapse, panes, persistSizes]);

  const resizePair = useCallback((handleIndex: number, requestedLeftSize: number, baseSizes: number[], containerWidth: number) => {
    const leftPane = panes[handleIndex];
    const rightPane = panes[handleIndex + 1];
    if (!leftPane || !rightPane || containerWidth <= 0) return baseSizes;

    const pairTotal = baseSizes[handleIndex] + baseSizes[handleIndex + 1];
    const leftMinimum = ((leftPane.minSize || 0) / containerWidth) * 100;
    const rightMinimum = ((rightPane.minSize || 0) / containerWidth) * 100;
    const leftMaximum = Math.min(
      leftPane.maxSize ? (leftPane.maxSize / containerWidth) * 100 : pairTotal,
      pairTotal - rightMinimum,
    );
    const rightMaximum = rightPane.maxSize ? (rightPane.maxSize / containerWidth) * 100 : pairTotal;
    const minimumFromRightMaximum = Math.max(0, pairTotal - rightMaximum);
    const nextLeft = clamp(requestedLeftSize, Math.max(leftMinimum, minimumFromRightMaximum), leftMaximum);
    const next = [...baseSizes];
    next[handleIndex] = nextLeft;
    next[handleIndex + 1] = pairTotal - nextLeft;
    return next;
  }, [panes]);

  const startDrag = useCallback((event: PointerEvent<HTMLDivElement>, handleIndex: number) => {
    const root = rootRef.current;
    if (!root || panes[handleIndex]?.collapsed || panes[handleIndex + 1]?.collapsed) return;
    event.preventDefault();
    event.currentTarget.setPointerCapture(event.pointerId);
    dragRef.current = {
      handleIndex,
      startX: event.clientX,
      startSizes: sizes,
      containerWidth: root.getBoundingClientRect().width,
      requestedLeftSize: sizes[handleIndex],
    };
    setDraggingHandle(handleIndex);
  }, [panes, sizes]);

  const moveDrag = useCallback((event: PointerEvent<HTMLDivElement>) => {
    const drag = dragRef.current;
    if (!drag) return;
    const delta = ((event.clientX - drag.startX) / drag.containerWidth) * 100;
    drag.requestedLeftSize = drag.startSizes[drag.handleIndex] + delta;
    setSizes(resizePair(
      drag.handleIndex,
      drag.requestedLeftSize,
      drag.startSizes,
      drag.containerWidth,
    ));
  }, [resizePair, setSizes]);

  const endDrag = useCallback(() => {
    const drag = dragRef.current;
    if (!drag) return;
    dragRef.current = null;
    setDraggingHandle(null);
    finishResize(sizes, drag.handleIndex, drag.containerWidth, drag.requestedLeftSize);
  }, [finishResize, sizes]);

  const handleKeyboardResize = useCallback((event: React.KeyboardEvent<HTMLDivElement>, handleIndex: number) => {
    if (!rootRef.current || panes[handleIndex]?.collapsed || panes[handleIndex + 1]?.collapsed) return;
    const increment = event.shiftKey ? 8 : 2;
    let requested: number | null = null;
    if (event.key === "ArrowLeft") requested = sizes[handleIndex] - increment;
    if (event.key === "ArrowRight") requested = sizes[handleIndex] + increment;
    if (event.key === "Home") requested = 0;
    if (event.key === "End") requested = 100;
    if (requested === null) return;
    event.preventDefault();
    const next = resizePair(handleIndex, requested, sizes, rootRef.current.getBoundingClientRect().width);
    setSizes(next);
    finishResize(next, handleIndex, rootRef.current.getBoundingClientRect().width, requested);
  }, [finishResize, panes, resizePair, setSizes, sizes]);

  const resetSizes = useCallback(() => {
    if (panes.some((pane) => pane.collapsed)) return;
    setSizes(initialSizes);
    persistSizes(initialSizes);
  }, [initialSizes, panes, persistSizes, setSizes]);

  return (
    <div
      ref={rootRef}
      className={`resizable-pane-group${ready ? " is-ready" : ""}${animating ? " is-animating" : ""}${draggingHandle !== null ? " is-resizing" : ""}${className ? ` ${className}` : ""}`}
    >
      {panes.map((pane, index) => {
        const handleCollapsed = Boolean(pane.collapsed || panes[index + 1]?.collapsed);
        return (
          <div key={pane.id} className="resizable-pane-group__item">
            <div
              className={`resizable-pane-group__pane${pane.className ? ` ${pane.className}` : ""}${pane.collapsed ? " is-collapsed" : ""}`}
              data-pane-id={pane.id}
              aria-hidden={pane.collapsed || undefined}
              style={{
                "--pane-size": displayedSizes[index],
                "--pane-content-min": pane.minSize ? `${pane.minSize}px` : "0px",
                flexGrow: displayedSizes[index],
                minWidth: pane.collapsed || animating ? 0 : pane.minSize,
                maxWidth: pane.collapsed || animating ? undefined : pane.maxSize,
              } as CSSProperties}
            >
              {pane.children}
            </div>
            {index < panes.length - 1 && (
              <div
                role="separator"
                tabIndex={handleCollapsed ? -1 : 0}
                aria-hidden={handleCollapsed || undefined}
                aria-label={`Resize ${pane.label} and ${panes[index + 1].label}`}
                aria-orientation="vertical"
                aria-valuemin={Math.round(((pane.minSize || 0) / Math.max(1, rootRef.current?.clientWidth || 1)) * 100)}
                aria-valuemax={100}
                aria-valuenow={Math.round(displayedSizes[index])}
                className={`resizable-pane-group__handle${draggingHandle === index ? " is-active" : ""}${handleCollapsed ? " is-collapsed" : ""}`}
                onPointerDown={(event) => startDrag(event, index)}
                onPointerMove={moveDrag}
                onPointerUp={endDrag}
                onLostPointerCapture={endDrag}
                onKeyDown={(event) => handleKeyboardResize(event, index)}
                onDoubleClick={resetSizes}
                title="Drag to resize · Double-click to reset"
              />
            )}
          </div>
        );
      })}
    </div>
  );
}
