import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties, type PointerEvent, type ReactNode } from "react";
import "./ResizablePaneGroup.css";

export interface ResizablePaneDefinition {
  id: string;
  label: string;
  children: ReactNode;
  initialSize: number;
  minSize?: number;
  maxSize?: number;
  className?: string;
}

interface ResizablePaneGroupProps {
  panes: ResizablePaneDefinition[];
  storageKey: string;
  className?: string;
}

interface ActiveDrag {
  handleIndex: number;
  startX: number;
  startSizes: number[];
  containerWidth: number;
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

export default function ResizablePaneGroup({ panes, storageKey, className = "" }: ResizablePaneGroupProps) {
  const paneKey = useMemo(() => panes.map((pane) => pane.id).join("\0"), [panes]);
  const initialSizes = useMemo(() => normalizedSizes(panes), [paneKey]);
  const [sizes, setSizes] = useState<number[]>(initialSizes);
  const [draggingHandle, setDraggingHandle] = useState<number | null>(null);
  const rootRef = useRef<HTMLDivElement>(null);
  const dragRef = useRef<ActiveDrag | null>(null);

  useEffect(() => {
    setSizes(readStoredSizes(storageKey, panes) || initialSizes);
  }, [initialSizes, paneKey, storageKey]);

  const persistSizes = useCallback((nextSizes: number[]) => {
    try {
      window.localStorage.setItem(storageKey, JSON.stringify(nextSizes));
    } catch {
      // Storage can be disabled without affecting the splitter itself.
    }
  }, [storageKey]);

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
    if (!root) return;
    event.preventDefault();
    event.currentTarget.setPointerCapture(event.pointerId);
    dragRef.current = {
      handleIndex,
      startX: event.clientX,
      startSizes: sizes,
      containerWidth: root.getBoundingClientRect().width,
    };
    setDraggingHandle(handleIndex);
  }, [sizes]);

  const moveDrag = useCallback((event: PointerEvent<HTMLDivElement>) => {
    const drag = dragRef.current;
    if (!drag) return;
    const delta = ((event.clientX - drag.startX) / drag.containerWidth) * 100;
    setSizes(resizePair(
      drag.handleIndex,
      drag.startSizes[drag.handleIndex] + delta,
      drag.startSizes,
      drag.containerWidth,
    ));
  }, [resizePair]);

  const endDrag = useCallback(() => {
    if (!dragRef.current) return;
    dragRef.current = null;
    setDraggingHandle(null);
    setSizes((current) => {
      persistSizes(current);
      return current;
    });
  }, [persistSizes]);

  const handleKeyboardResize = useCallback((event: React.KeyboardEvent<HTMLDivElement>, handleIndex: number) => {
    if (!rootRef.current) return;
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
    persistSizes(next);
  }, [persistSizes, resizePair, sizes]);

  const resetSizes = useCallback(() => {
    setSizes(initialSizes);
    persistSizes(initialSizes);
  }, [initialSizes, persistSizes]);

  return (
    <div
      ref={rootRef}
      className={`resizable-pane-group${draggingHandle !== null ? " is-resizing" : ""}${className ? ` ${className}` : ""}`}
    >
      {panes.map((pane, index) => (
        <div key={pane.id} className="resizable-pane-group__item">
          <div
            className={`resizable-pane-group__pane${pane.className ? ` ${pane.className}` : ""}`}
            data-pane-id={pane.id}
            style={{
              "--pane-size": sizes[index],
              minWidth: pane.minSize,
              maxWidth: pane.maxSize,
            } as CSSProperties}
          >
            {pane.children}
          </div>
          {index < panes.length - 1 && (
            <div
              role="separator"
              tabIndex={0}
              aria-label={`Resize ${pane.label} and ${panes[index + 1].label}`}
              aria-orientation="vertical"
              aria-valuemin={Math.round(((pane.minSize || 0) / Math.max(1, rootRef.current?.clientWidth || 1)) * 100)}
              aria-valuemax={100}
              aria-valuenow={Math.round(sizes[index])}
              className={`resizable-pane-group__handle${draggingHandle === index ? " is-active" : ""}`}
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
      ))}
    </div>
  );
}
