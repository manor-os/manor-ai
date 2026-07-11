import { useMemo, useState, useRef, useCallback, useEffect } from "react";
import { t } from "../../lib/i18n";

/* ── types ─────────────────────────────────────────────── */

export interface StepNode {
  id: string;
  name: string;
  status: string;
  type?: string;
  depends_on?: string[];
  outputs?: Record<string, unknown>;
  order_index?: number;
}

export interface StepGraphProps {
  steps: StepNode[];
  onStepClick?: (step: StepNode) => void;
  activeStepId?: string;
  /** Height of the canvas container (default 420) */
  height?: number;
  /** Optional visual treatment for dense workspace goal canvases. */
  variant?: "default" | "goal";
}

/* ── constants ──────────────────────────────────────────── */

const COMPLETED_STYLE = { border: "#34d399", bg: "#f1f6f3", dot: "#44895f", arrow: "#34d399" };
const RUNNING_STYLE = { border: "#8aa9d1", bg: "#f3f6fa", dot: "#5f84bd", arrow: "#8aa9d1" };
const PENDING_STYLE = { border: "#e7e5e4", bg: "#fafaf9", dot: "#a8a29e", arrow: "#e7e5e4" };
const FAILED_STYLE = { border: "#d18b86", bg: "#f8f0ef", dot: "#d65f59", arrow: "#d18b86" };
const WAITING_STYLE = { border: "#ddbb63", bg: "#faf7ef", dot: "#cf9b44", arrow: "#ddbb63" };

const STATUS_STYLES: Record<string, { border: string; bg: string; dot: string; arrow: string }> = {
  completed: COMPLETED_STYLE,
  done: COMPLETED_STYLE,
  running: RUNNING_STYLE,
  in_progress: RUNNING_STYLE,
  pending: PENDING_STYLE,
  draft: PENDING_STYLE,
  skipped: PENDING_STYLE,
  cancelled: PENDING_STYLE,
  failed: FAILED_STYLE,
  error: FAILED_STYLE,
  waiting: WAITING_STYLE,
  waiting_human: WAITING_STYLE,
  waiting_on_customer: WAITING_STYLE,
  pending_approval: WAITING_STYLE,
  blocked: WAITING_STYLE,
};

const GOAL_STYLE = { border: "#9079c2", bg: "#f5f3ff", dot: "#6f4ba8", arrow: "#a78bfa" };

const STEP_ICONS: Record<string, string> = {
  llm:      "M9.813 15.904L9 18.75l-.813-2.846a4.5 4.5 0 00-3.09-3.09L2.25 12l2.846-.813a4.5 4.5 0 003.09-3.09L9 5.25l.813 2.846a4.5 4.5 0 003.09 3.09L15.75 12l-2.846.813a4.5 4.5 0 00-3.09 3.09z",
  tool:     "M11.42 15.17l-5.47-5.47a2.25 2.25 0 010-3.18l.97-.97a2.25 2.25 0 013.18 0l5.47 5.47",
  action:   "M11.42 15.17l-5.47-5.47a2.25 2.25 0 010-3.18l.97-.97a2.25 2.25 0 013.18 0l5.47 5.47",
  human:    "M15.75 6a3.75 3.75 0 11-7.5 0 3.75 3.75 0 017.5 0zM4.501 20.118a7.5 7.5 0 0114.998 0",
  goal:     "M11.48 3.499a.562.562 0 011.04 0l2.125 5.111a.563.563 0 00.475.345l5.518.442c.499.04.701.663.321.988l-4.204 3.602a.563.563 0 00-.182.557l1.285 5.385a.562.562 0 01-.84.61l-4.725-2.885a.563.563 0 00-.586 0L6.982 20.54a.562.562 0 01-.84-.61l1.285-5.386a.562.562 0 00-.182-.557l-4.204-3.602a.563.563 0 01.321-.988l5.518-.442a.563.563 0 00.475-.345L11.48 3.5z",
  subagent: "M18 18.72a9.094 9.094 0 003.741-.479 3 3 0 00-4.682-2.72m.94 3.198l.001.031c0 .225-.012.447-.037.666A11.944 11.944 0 0112 21c-2.17 0-4.207-.576-5.963-1.584A6.062 6.062 0 016 18.719m12 0a5.971 5.971 0 00-.941-3.197m0 0A5.995 5.995 0 0012 12.75a5.995 5.995 0 00-5.058 2.772m0 0a3 3 0 00-4.681 2.72 8.986 8.986 0 003.74.477m.94-3.197a5.971 5.971 0 00-.94 3.197",
  default:  "M4.5 12a7.5 7.5 0 0015 0m-15 0a7.5 7.5 0 0115 0m-15 0H3m16.5 0H21",
};

interface GraphLayout {
  nodeW: number;
  nodeH: number;
  gapX: number;
  gapY: number;
}

const DEFAULT_LAYOUT: GraphLayout = { nodeW: 180, nodeH: 70, gapX: 56, gapY: 18 };
const GOAL_LAYOUT: GraphLayout = { nodeW: 196, nodeH: 72, gapX: 76, gapY: 24 };
const MIN_ZOOM = 0.22;
const MAX_ZOOM = 3;
const MAX_FIT_ZOOM = 1.2;
const NODE_STAGGER_SECONDS = 0.03;
const MAX_NODE_STAGGER_SECONDS = 0.6;

function displayStatus(status?: string): string {
  if (!status) return "pending";
  if (status === "done") return "completed";
  if (status === "in_progress") return "running";
  return status.replace(/_/g, " ");
}

function isCompletedStatus(status?: string): boolean {
  return status === "completed" || status === "done";
}

/* ── layout logic ──────────────────────────────────────── */

interface LayoutNode {
  step: StepNode;
  col: number;
  row: number;
  x: number;
  y: number;
}

function layoutSteps(steps: StepNode[], layout: GraphLayout): { nodes: LayoutNode[]; width: number; height: number } {
  if (steps.length === 0) return { nodes: [], width: 0, height: 0 };

  const depMap = new Map<string, string[]>();
  for (const s of steps) {
    depMap.set(s.id, s.depends_on ?? []);
  }

  const colMap = new Map<string, number>();
  const visited = new Set<string>();

  function getCol(id: string): number {
    if (colMap.has(id)) return colMap.get(id)!;
    if (visited.has(id)) return 0;
    visited.add(id);
    const deps = depMap.get(id) ?? [];
    const maxDep = deps.length > 0 ? Math.max(...deps.map(getCol)) + 1 : 0;
    colMap.set(id, maxDep);
    return maxDep;
  }

  for (const s of steps) getCol(s.id);

  const allZero = [...colMap.values()].every((c) => c === 0);
  if (allZero && steps.length > 1) {
    const sorted = [...steps].sort((a, b) => (a.order_index ?? 0) - (b.order_index ?? 0));
    sorted.forEach((s, i) => colMap.set(s.id, i));
  }

  const columns = new Map<number, StepNode[]>();
  for (const s of steps) {
    const col = colMap.get(s.id) ?? 0;
    if (!columns.has(col)) columns.set(col, []);
    columns.get(col)!.push(s);
  }

  const maxCol = Math.max(...columns.keys());
  const nodes: LayoutNode[] = [];
  let maxRow = 0;

  for (let c = 0; c <= maxCol; c++) {
    const colSteps = columns.get(c) ?? [];
    colSteps.forEach((step, r) => {
      nodes.push({
        step,
        col: c,
        row: r,
        x: c * (layout.nodeW + layout.gapX),
        y: r * (layout.nodeH + layout.gapY),
      });
      if (r > maxRow) maxRow = r;
    });
  }

  const width = (maxCol + 1) * (layout.nodeW + layout.gapX) - layout.gapX;
  const height = (maxRow + 1) * (layout.nodeH + layout.gapY) - layout.gapY;

  return { nodes, width, height };
}

/* ── canvas component ──────────────────────────────────── */

export default function StepGraph({ steps, onStepClick, activeStepId, height: containerHeight = 420, variant = "default" }: StepGraphProps) {
  const isGoalVariant = variant === "goal";
  const layout = isGoalVariant ? GOAL_LAYOUT : DEFAULT_LAYOUT;
  const { nodeW, nodeH } = layout;
  const canvasPadding = isGoalVariant ? 96 : 80;
  const [hovered, setHovered] = useState<string | null>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const svgRef = useRef<SVGSVGElement>(null);

  // Pan & zoom state
  const [zoom, setZoom] = useState(1);
  const [pan, setPan] = useState({ x: 0, y: 0 });
  const [isPanning, setIsPanning] = useState(false);
  const panStart = useRef({ x: 0, y: 0, panX: 0, panY: 0 });

  const { nodes, width, height } = useMemo(() => layoutSteps(steps, layout), [steps, layout]);

  // Auto-fit on mount / step change
  useEffect(() => {
    if (!containerRef.current || nodes.length === 0) return;
    const rect = containerRef.current.getBoundingClientRect();
    const padded_w = width + canvasPadding;
    const padded_h = height + canvasPadding;
    const scaleX = rect.width / padded_w;
    const scaleY = containerHeight / padded_h;
    const fitZoom = Math.min(scaleX, scaleY, MAX_FIT_ZOOM);
    setZoom(Math.max(MIN_ZOOM, fitZoom));
    // Center the graph
    const centeredX = (rect.width - padded_w * fitZoom) / 2;
    const centeredY = (containerHeight - padded_h * fitZoom) / 2;
    setPan({ x: Math.max(0, centeredX), y: Math.max(0, centeredY) });
  }, [nodes.length, width, height, containerHeight, canvasPadding]);

  // Mouse wheel zoom — native listener to avoid passive event warning
  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const handler = (e: WheelEvent) => {
      e.preventDefault();
      const delta = e.deltaY > 0 ? 0.9 : 1.1;
      setZoom((z) => Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, z * delta)));
    };
    el.addEventListener("wheel", handler, { passive: false });
    return () => el.removeEventListener("wheel", handler);
  }, []);

  // Pan handlers
  const handleMouseDown = useCallback((e: React.MouseEvent) => {
    if (e.button !== 0) return;
    // Only pan on background click (not on nodes)
    if ((e.target as Element).closest("[data-node]")) return;
    setIsPanning(true);
    panStart.current = { x: e.clientX, y: e.clientY, panX: pan.x, panY: pan.y };
  }, [pan]);

  const handleMouseMove = useCallback((e: React.MouseEvent) => {
    if (!isPanning) return;
    const dx = e.clientX - panStart.current.x;
    const dy = e.clientY - panStart.current.y;
    setPan({ x: panStart.current.panX + dx, y: panStart.current.panY + dy });
  }, [isPanning]);

  const handleMouseUp = useCallback(() => {
    setIsPanning(false);
  }, []);

  // Zoom controls
  const zoomIn = () => setZoom((z) => Math.min(MAX_ZOOM, z * 1.2));
  const zoomOut = () => setZoom((z) => Math.max(MIN_ZOOM, z / 1.2));
  const fitView = () => {
    if (!containerRef.current) return;
    const rect = containerRef.current.getBoundingClientRect();
    const padded_w = width + canvasPadding;
    const padded_h = height + canvasPadding;
    const scaleX = rect.width / padded_w;
    const scaleY = containerHeight / padded_h;
    const fitZ = Math.min(scaleX, scaleY, MAX_FIT_ZOOM);
    setZoom(Math.max(MIN_ZOOM, fitZ));
    const centeredX = (rect.width - padded_w * fitZ) / 2;
    const centeredY = (containerHeight - padded_h * fitZ) / 2;
    setPan({ x: Math.max(0, centeredX), y: Math.max(0, centeredY) });
  };

  if (steps.length === 0) {
    return (
      <p style={{ fontSize: 13, color: "#a8a29e", margin: 0 }}>{t("component.step_graph.no_steps_yet")}</p>
    );
  }

  // Build lookup for positions
  const posMap = new Map<string, LayoutNode>();
  for (const n of nodes) posMap.set(n.step.id, n);

  // Build edges
  const edges: { from: LayoutNode; to: LayoutNode; color: string; isActive: boolean }[] = [];
  for (const n of nodes) {
    const deps = n.step.depends_on ?? [];
    for (const depId of deps) {
      const from = posMap.get(depId);
      if (from) {
        const fromIsGoal = from.step.type === "goal";
        const edgeColor = fromIsGoal ? GOAL_STYLE.arrow : (STATUS_STYLES[from.step.status] ?? STATUS_STYLES.pending).arrow;
        const isActive = (isCompletedStatus(from.step.status) || from.step.status === "running") && n.step.status === "running";
        edges.push({ from, to: n, color: edgeColor, isActive: isActive && !fromIsGoal });
      }
    }
    if (deps.length === 0 && n.col > 0) {
      const prev = nodes.find((p) => p.col === n.col - 1 && p.row === n.row) ??
                   nodes.find((p) => p.col === n.col - 1);
      if (prev) {
        const prevIsGoal = prev.step.type === "goal";
        const edgeColor = prevIsGoal ? GOAL_STYLE.arrow : (STATUS_STYLES[prev.step.status] ?? STATUS_STYLES.pending).arrow;
        const isActive = (isCompletedStatus(prev.step.status) || prev.step.status === "running") && n.step.status === "running";
        edges.push({ from: prev, to: n, color: edgeColor, isActive: isActive && !prevIsGoal });
      }
    }
  }

  const svgW = width + canvasPadding;
  const svgH = height + canvasPadding;
  const padX = canvasPadding / 2;
  const padY = canvasPadding / 2;
  const gridPatternId = isGoalVariant ? "goal-grid-dots" : "grid-dots";
  const maxNameLength = isGoalVariant ? 20 : 16;
  const nodeRadius = isGoalVariant ? 12 : 14;

  return (
    <div
      ref={containerRef}
      className={`step-graph step-graph--${variant}`}
      style={{
        position: "relative",
        width: "100%",
        height: containerHeight,
        overflow: "hidden",
        borderRadius: isGoalVariant ? 14 : 16,
        background: isGoalVariant
          ? "linear-gradient(180deg, #ffffff 0%, #fbfdff 100%)"
          : "linear-gradient(135deg, #fafaf9 0%, #f5f5f4 100%)",
        border: isGoalVariant ? "1px solid rgba(214,211,209,0.86)" : "1px solid rgba(231,229,228,0.6)",
        boxShadow: isGoalVariant ? "inset 0 1px 0 rgba(255,255,255,0.9)" : undefined,
        cursor: isPanning ? "grabbing" : "grab",
        userSelect: "none",
      }}
      onMouseDown={handleMouseDown}
      onMouseMove={handleMouseMove}
      onMouseUp={handleMouseUp}
      onMouseLeave={handleMouseUp}
    >
      {/* Grid dots background */}
      <svg
        className="step-graph-grid"
        style={{ position: "absolute", inset: 0, width: "100%", height: "100%", pointerEvents: "none" }}
      >
        <defs>
          <pattern id={gridPatternId} x="0" y="0" width={isGoalVariant ? 28 : 24} height={isGoalVariant ? 28 : 24} patternUnits="userSpaceOnUse">
            <circle className="step-graph-grid-dot" cx={isGoalVariant ? 14 : 12} cy={isGoalVariant ? 14 : 12} r="1" fill={isGoalVariant ? "rgba(168,162,158,0.18)" : "rgba(168,162,158,0.3)"} />
          </pattern>
        </defs>
        <rect width="100%" height="100%" fill={`url(#${gridPatternId})`} />
      </svg>

      {/* Zoom controls */}
      <div className="step-graph-controls" style={{
        position: "absolute", top: 12, right: 12, zIndex: 10,
        display: "flex", flexDirection: isGoalVariant ? "row" : "column", gap: 4,
        background: "rgba(255,255,255,0.9)", borderRadius: isGoalVariant ? 999 : 10,
        border: "1px solid rgba(28,25,23,0.06)", padding: 4,
        backdropFilter: "blur(8px)",
      }}>
        <CanvasButton onClick={zoomIn} title={t("component.step_graph.zoom_in")}>+</CanvasButton>
        <CanvasButton onClick={zoomOut} title={t("component.step_graph.zoom_out")}>-</CanvasButton>
        <CanvasButton onClick={fitView} title={t("component.step_graph.fit_view")}>
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2}>
            <path d="M15 3h6v6M9 21H3v-6M21 3l-7 7M3 21l7-7" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
        </CanvasButton>
      </div>

      {/* Zoom indicator */}
      <div className="step-graph-zoom-indicator" style={{
        position: "absolute", bottom: 12, left: 12, zIndex: 10,
        fontSize: 10, fontWeight: 600, color: "#a8a29e",
        background: "rgba(255,255,255,0.8)", padding: "3px 8px",
        borderRadius: isGoalVariant ? 999 : 6, border: "1px solid rgba(28,25,23,0.06)",
      }}>
        {Math.round(zoom * 100)}%
      </div>

      {/* Main SVG canvas */}
      <svg
        ref={svgRef}
        className="step-graph-svg"
        width={svgW}
        height={svgH}
        viewBox={`0 0 ${svgW} ${svgH}`}
        style={{
          display: "block",
          position: "absolute",
          left: 0,
          top: 0,
          transform: `translate(${pan.x}px, ${pan.y}px) scale(${zoom})`,
          transformOrigin: "0 0",
          transition: isPanning ? "none" : "transform 0.1s ease-out",
        }}
      >
        <defs>
          <marker id="arrow-head" viewBox="0 0 10 10" refX="10" refY="5" markerWidth="8" markerHeight="8" orient="auto-start-reverse">
            <path d="M 0 0 L 10 5 L 0 10 z" fill="#d6d3d1" />
          </marker>
          <marker id="arrow-head-active" viewBox="0 0 10 10" refX="10" refY="5" markerWidth="8" markerHeight="8" orient="auto-start-reverse">
            <path d="M 0 0 L 10 5 L 0 10 z" fill="#4f7d75" />
          </marker>
          <filter id="step-glow" x="-20%" y="-20%" width="140%" height="140%">
            <feGaussianBlur stdDeviation="4" result="blur" />
            <feFlood floodColor="#4f7d75" floodOpacity="0.3" result="color" />
            <feComposite in="color" in2="blur" operator="in" result="glow" />
            <feMerge><feMergeNode in="glow" /><feMergeNode in="SourceGraphic" /></feMerge>
          </filter>
          <filter id="edge-glow" x="-10%" y="-50%" width="120%" height="200%">
            <feGaussianBlur stdDeviation="3" result="blur" />
            <feFlood floodColor="#4f7d75" floodOpacity="0.4" result="color" />
            <feComposite in="color" in2="blur" operator="in" result="glow" />
            <feMerge><feMergeNode in="glow" /><feMergeNode in="SourceGraphic" /></feMerge>
          </filter>
          <filter id="node-shadow" x="-10%" y="-10%" width="120%" height="130%">
            <feDropShadow dx="0" dy="2" stdDeviation="4" floodColor="rgba(0,0,0,0.08)" />
          </filter>
        </defs>

        {/* Edges — base lines */}
        {edges.map((e, i) => {
          const x1 = padX + e.from.x + nodeW;
          const y1 = padY + e.from.y + nodeH / 2;
          const x2 = padX + e.to.x;
          const y2 = padY + e.to.y + nodeH / 2;
          const midX = (x1 + x2) / 2;
          const pathD = `M ${x1} ${y1} C ${midX} ${y1}, ${midX} ${y2}, ${x2} ${y2}`;
          const isPending = e.from.step.status === "pending" && e.to.step.status === "pending";
          const isCompleted = isCompletedStatus(e.from.step.status);

          return (
            <path
              key={`edge-base-${i}`}
              className={`step-graph-edge${isPending ? " step-graph-edge--pending" : ""}${isCompleted ? " step-graph-edge--completed" : ""}`}
              d={pathD}
              fill="none"
              stroke={isPending ? "#e7e5e4" : e.color}
              strokeWidth={isGoalVariant ? (isCompleted ? 1.7 : 1.35) : (isCompleted ? 2 : 1.5)}
              strokeLinecap="round"
              strokeDasharray={isPending ? "4 6" : "none"}
              markerEnd="url(#arrow-head)"
              opacity={isGoalVariant ? (isPending ? 0.25 : 0.42) : (isPending ? 0.3 : 0.5)}
            />
          );
        })}

        {/* Edges — light flow particles (continuous flow through the graph) */}
        {edges.map((e, i) => {
          const x1 = padX + e.from.x + nodeW;
          const y1 = padY + e.from.y + nodeH / 2;
          const x2 = padX + e.to.x;
          const y2 = padY + e.to.y + nodeH / 2;
          const midX = (x1 + x2) / 2;
          const pathD = `M ${x1} ${y1} C ${midX} ${y1}, ${midX} ${y2}, ${x2} ${y2}`;
          const isPending = e.from.step.status === "pending" && e.to.step.status === "pending";
          if (isPending) return null;
          if (isGoalVariant && !e.isActive) return null;

          // Stagger each edge's animation start by its topological position
          const delay = e.from.col * 1.2 + i * 0.3;
          const isActive = e.isActive;
          const isCompleted = isCompletedStatus(e.from.step.status);
          const particleColor = isActive ? "#5f928a" : isCompleted ? "#34d399" : "#a8a29e";
          const glowColor = isActive ? "#abccc4" : isCompleted ? "#6ee7b7" : "#d6d3d1";
          const speed = isActive ? 1.5 : isCompleted ? 2.5 : 3.5;

          return (
            <g key={`edge-flow-${i}`}>
              {/* Glowing trail particle */}
              <circle r={isActive ? 4 : 3} fill={particleColor} opacity={0}>
                <animateMotion dur={`${speed}s`} repeatCount="indefinite" path={pathD} begin={`${delay}s`} />
                <animate attributeName="opacity" values="0;0.9;0.9;0" dur={`${speed}s`} repeatCount="indefinite" begin={`${delay}s`} />
                <animate attributeName="r" values={isActive ? "3;5;3" : "2;3.5;2"} dur={`${speed}s`} repeatCount="indefinite" begin={`${delay}s`} />
              </circle>
              {/* Soft glow behind particle */}
              <circle r={isActive ? 10 : 7} fill={glowColor} opacity={0}>
                <animateMotion dur={`${speed}s`} repeatCount="indefinite" path={pathD} begin={`${delay}s`} />
                <animate attributeName="opacity" values="0;0.3;0.3;0" dur={`${speed}s`} repeatCount="indefinite" begin={`${delay}s`} />
              </circle>
              {/* Active edges get an extra fast particle */}
              {isActive && (
                <circle r={2.5} fill="#fff" opacity={0}>
                  <animateMotion dur={`${speed * 0.7}s`} repeatCount="indefinite" path={pathD} begin={`${delay + 0.5}s`} />
                  <animate attributeName="opacity" values="0;1;1;0" dur={`${speed * 0.7}s`} repeatCount="indefinite" begin={`${delay + 0.5}s`} />
                </circle>
              )}
            </g>
          );
        })}

        {/* Nodes */}
        {nodes.map((n) => {
          const isGoalNode = n.step.type === "goal";
          const style = isGoalNode ? GOAL_STYLE : (STATUS_STYLES[n.step.status] ?? STATUS_STYLES.pending);
          const isActive = activeStepId === n.step.id;
          const isHov = hovered === n.step.id;
          const iconPath = STEP_ICONS[n.step.type ?? "default"] ?? STEP_ICONS.default;
          const nx = padX + n.x;
          const ny = padY + n.y;

          const idx = nodes.indexOf(n);
          const enterDelay = Math.min(idx * NODE_STAGGER_SECONDS, MAX_NODE_STAGGER_SECONDS);
          return (
            <g
              key={n.step.id}
              className={`step-graph-node step-graph-node--${n.step.type ?? "default"} step-graph-node-status--${n.step.status}`}
              data-status={n.step.status}
              data-type={n.step.type ?? "default"}
              data-node={n.step.id}
              onClick={(e) => { e.stopPropagation(); onStepClick?.(n.step); }}
              onMouseEnter={() => setHovered(n.step.id)}
              onMouseLeave={() => setHovered(null)}
              style={{ cursor: "pointer" }}
              filter={isActive ? "url(#step-glow)" : isGoalVariant ? undefined : "url(#node-shadow)"}
              opacity={isGoalVariant ? 0.55 : 0.38}
            >
              {/* Staggered entrance animation */}
              <animate attributeName="opacity" from={isGoalVariant ? "0.55" : "0.38"} to="1" dur="0.32s" begin={`${enterDelay}s`} fill="freeze" />
              <animateTransform attributeName="transform" type="translate" from="0 8" to="0 0" dur="0.32s" begin={`${enterDelay}s`} fill="freeze" />

              {/* Pulse ring for running nodes */}
              {n.step.status === "running" && (
                <rect
                  x={nx - 4} y={ny - 4}
                  width={nodeW + 8} height={nodeH + 8}
                  rx={nodeRadius + 4} ry={nodeRadius + 4}
                  fill="none" stroke="#8aa9d1" strokeWidth={isGoalVariant ? 1.4 : 2}
                >
                  <animate attributeName="opacity" values={isGoalVariant ? "0.45;0;0.45" : "0.7;0;0.7"} dur="2s" repeatCount="indefinite" />
                  <animate attributeName="stroke-width" values={isGoalVariant ? "1.4;0.5;1.4" : "2;0.5;2"} dur="2s" repeatCount="indefinite" />
                </rect>
              )}

              {/* Card background */}
              <rect
                className="step-graph-node-card"
                x={nx} y={ny}
                width={nodeW} height={nodeH}
                rx={nodeRadius} ry={nodeRadius}
                fill={isHov ? "#fff" : style.bg}
                stroke={isActive ? "#4f7d75" : isHov ? style.dot : style.border}
                strokeWidth={isGoalVariant ? (isActive ? 2 : isHov ? 1.7 : 1.2) : (isActive ? 2.5 : isHov ? 2 : 1.5)}
              >
                {isHov && <animate attributeName="stroke-width" from="1.5" to="2.5" dur="0.15s" fill="freeze" />}
              </rect>

              {/* Left color accent bar */}
              <rect
                className="step-graph-node-accent"
                x={nx} y={ny + 12}
                width={isGoalVariant ? 3 : 4} height={nodeH - 24}
                rx={2}
                fill={style.dot}
              />

              {/* Type icon */}
              <svg
                x={nx + 14} y={ny + 14}
                width={18} height={18}
                viewBox="0 0 24 24"
                fill="none" stroke={style.dot} strokeWidth={1.5}
                strokeLinecap="round" strokeLinejoin="round"
              >
                <path d={iconPath} />
              </svg>

              {/* Step name */}
              <text
                className="step-graph-node-name"
                x={nx + 38} y={ny + 22}
                fontSize={11.5} fontWeight={700}
                fill="#292524" dominantBaseline="middle"
              >
                {n.step.name.length > maxNameLength ? n.step.name.slice(0, maxNameLength) + "\u2026" : n.step.name}
              </text>

              {/* Status row: dot + label */}
              <circle cx={nx + 38} cy={ny + 42} r={4} fill={style.dot}>
                {n.step.status === "running" && (
                  <animate attributeName="opacity" values="1;0.3;1" dur="1.5s" repeatCount="indefinite" />
                )}
              </circle>
              <text
                className="step-graph-node-status"
                x={nx + 48} y={ny + 42}
                fontSize={10} fontWeight={500}
                fill="#a8a29e" dominantBaseline="middle"
              >
                {displayStatus(n.step.status)}
              </text>

              {/* Completion checkmark */}
              {isCompletedStatus(n.step.status) && (
                <svg x={nx + nodeW - 24} y={ny + 8} width={16} height={16} viewBox="0 0 24 24" fill="none" stroke="#44895f" strokeWidth={2.5} strokeLinecap="round" strokeLinejoin="round">
                  <path d="M5 13l4 4L19 7" />
                </svg>
              )}

              {/* Failed X mark */}
              {(n.step.status === "failed" || n.step.status === "error") && (
                <svg x={nx + nodeW - 24} y={ny + 8} width={16} height={16} viewBox="0 0 24 24" fill="none" stroke="#d65f59" strokeWidth={2.5} strokeLinecap="round" strokeLinejoin="round">
                  <path d="M6 18L18 6M6 6l12 12" />
                </svg>
              )}

              {/* Waiting hourglass */}
              {["waiting", "waiting_human", "waiting_on_customer", "pending_approval", "blocked"].includes(n.step.status) && (
                <svg x={nx + nodeW - 24} y={ny + 8} width={16} height={16} viewBox="0 0 24 24" fill="none" stroke="#cf9b44" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round">
                  <path d="M12 6v6l4 2" />
                  <circle cx="12" cy="12" r="10" />
                </svg>
              )}

              {/* Hover tooltip — full name */}
              {isHov && n.step.name.length > maxNameLength && (
                <g>
                  <rect x={nx + 8} y={ny + nodeH + 4} width={Math.min(n.step.name.length * 6.5 + 16, nodeW + 40)} height={22} rx={6} fill="#292524" opacity={0.92} />
                  <text x={nx + 16} y={ny + nodeH + 17} fontSize={10} fontWeight={500} fill="#fff" dominantBaseline="middle">
                    {n.step.name}
                  </text>
                </g>
              )}
            </g>
          );
        })}
      </svg>
    </div>
  );
}

/* ── small button for canvas controls ─────────────────── */

function CanvasButton({ onClick, title, children }: { onClick: () => void; title: string; children: React.ReactNode }) {
  return (
    <button
      className="step-graph-control-btn"
      onClick={onClick}
      title={title}
      style={{
        width: 28, height: 28,
        display: "flex", alignItems: "center", justifyContent: "center",
        border: "none", borderRadius: 6,
        background: "transparent",
        color: "#78716c", fontSize: 16, fontWeight: 700,
        cursor: "pointer",
        transition: "background 0.15s",
      }}
      onMouseEnter={(e) => { (e.target as HTMLElement).style.background = "rgba(245,245,244,0.8)"; }}
      onMouseLeave={(e) => { (e.target as HTMLElement).style.background = "transparent"; }}
    >
      {children}
    </button>
  );
}
