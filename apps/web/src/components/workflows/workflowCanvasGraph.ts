export interface WorkflowCanvasGraphStep {
  id: string;
  type: string;
  next?: string[];
  true_next?: string[];
  false_next?: string[];
  config?: {
    cases?: { expression?: string; next?: string[] }[];
    default_next?: string[];
    [key: string]: unknown;
  };
}

export interface WorkflowCanvasRoute {
  target: string;
  kind: string;
  handle?: string;
  label?: string;
}

const COLUMN_STRIDE = 296;
const ROW_STRIDE = 118;

function targets(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.map((item) => String(item || "").trim()).filter(Boolean);
}

export function workflowCanvasRoutes(step: WorkflowCanvasGraphStep): WorkflowCanvasRoute[] {
  if (step.type === "switch") {
    const routes: WorkflowCanvasRoute[] = [];
    for (const [index, item] of (step.config?.cases || []).entries()) {
      for (const target of targets(item?.next)) {
        routes.push({
          target,
          kind: `case${index}`,
          handle: `case${index}`,
          label: item?.expression || `case ${index + 1}`,
        });
      }
    }
    for (const target of targets(step.config?.default_next)) {
      routes.push({ target, kind: "default", handle: "default", label: "default" });
    }
    return routes;
  }

  const trueTargets = targets(step.true_next);
  const falseTargets = targets(step.false_next);
  const branchTargets = new Set([...trueTargets, ...falseTargets]);
  const routes: WorkflowCanvasRoute[] = [
    ...trueTargets.map((target) => ({ target, kind: "true", handle: "true", label: "true" })),
    ...falseTargets.map((target) => ({ target, kind: "false", handle: "false", label: "false" })),
  ];
  for (const target of targets(step.next)) {
    if (!branchTargets.has(target)) routes.push({ target, kind: "default" });
  }
  // Handled failures (`config.error_next`) are real routes the runner follows.
  for (const target of targets(step.config?.error_next)) {
    routes.push({ target, kind: "error", label: "on error" });
  }
  return routes;
}

export function workflowCanvasTopologySignature(steps: WorkflowCanvasGraphStep[]): string {
  return steps.map((step) => (
    `${step.id}>${workflowCanvasRoutes(step).map((route) => `${route.kind}:${route.target}`).join(",")}`
  )).join(";");
}

export function computeWorkflowCanvasLayout(
  allSteps: WorkflowCanvasGraphStep[],
): Map<string, { x: number; y: number }> {
  const steps = allSteps.filter((step) => step.type !== "note");
  const byId = new Map(steps.map((step) => [step.id, step]));
  const successors = (step: WorkflowCanvasGraphStep) => [
    ...new Set(workflowCanvasRoutes(step).map((route) => route.target)),
  ].filter((id) => byId.has(id));
  const indegree = new Map(steps.map((step) => [step.id, 0]));
  for (const step of steps) {
    for (const target of successors(step)) {
      indegree.set(target, (indegree.get(target) || 0) + 1);
    }
  }

  const level = new Map<string, number>();
  const remainingIndegree = new Map(indegree);
  const queue = steps
    .filter((step) => (indegree.get(step.id) || 0) === 0)
    .map((step) => step.id);
  queue.forEach((id) => level.set(id, 0));
  const traversalOrder: string[] = [];
  while (queue.length) {
    const id = queue.shift()!;
    traversalOrder.push(id);
    for (const target of successors(byId.get(id)!)) {
      level.set(target, Math.max(level.get(target) ?? 0, (level.get(id) ?? 0) + 1));
      remainingIndegree.set(target, (remainingIndegree.get(target) || 0) - 1);
      if ((remainingIndegree.get(target) || 0) === 0) queue.push(target);
    }
  }

  for (const step of steps) {
    if (level.has(step.id)) continue;
    level.set(step.id, 0);
    traversalOrder.push(step.id);
  }

  const columns = new Map<number, string[]>();
  for (const id of traversalOrder) {
    const column = level.get(id) || 0;
    const ids = columns.get(column) || [];
    ids.push(id);
    columns.set(column, ids);
  }

  const positions = new Map<string, { x: number; y: number }>();
  for (const [column, ids] of columns) {
    const firstRowY = -((ids.length - 1) * ROW_STRIDE) / 2;
    ids.forEach((id, row) => positions.set(id, {
      x: column * COLUMN_STRIDE,
      y: firstRowY + row * ROW_STRIDE,
    }));
  }
  return positions;
}

// ── Hand-routed lines ────────────────────────────────────────────────────
// A line may pass through user-placed bends (waypoints). It leaves its source
// horizontally, reaches each bend vertically and turns there, and enters its
// target horizontally after turning halfway, so every bend is a real corner.

export interface CanvasPoint {
  x: number;
  y: number;
}

/** Corners of one stretch of line, excluding its start point. */
function stretchCorners(from: CanvasPoint, to: CanvasPoint, intoTarget: boolean): CanvasPoint[] {
  if (!intoTarget) return [{ x: to.x, y: from.y }, to];
  const middleX = (from.x + to.x) / 2;
  return [{ x: middleX, y: from.y }, { x: middleX, y: to.y }, to];
}

/** Corner points of the orthogonal line from source through waypoints to target. */
export function workflowEdgeCorners(
  source: CanvasPoint,
  waypoints: CanvasPoint[],
  target: CanvasPoint,
): CanvasPoint[] {
  const anchors = [source, ...waypoints, target];
  const corners: CanvasPoint[] = [source];
  for (let index = 1; index < anchors.length; index += 1) {
    corners.push(...stretchCorners(anchors[index - 1], anchors[index], index === anchors.length - 1));
  }
  // Drop repeated points and points in the middle of a straight run (a
  // reversal is kept: it is a real turn back).
  const distinct = corners.filter((point, index) => (
    index === 0 || point.x !== corners[index - 1].x || point.y !== corners[index - 1].y
  ));
  const between = (value: number, a: number, b: number) => value >= Math.min(a, b) && value <= Math.max(a, b);
  return distinct.filter((point, index) => {
    if (index === 0 || index === distinct.length - 1) return true;
    const before = distinct[index - 1];
    const after = distinct[index + 1];
    const straightVertical = before.x === point.x && point.x === after.x && between(point.y, before.y, after.y);
    const straightHorizontal = before.y === point.y && point.y === after.y && between(point.x, before.x, after.x);
    return !straightVertical && !straightHorizontal;
  });
}

/** SVG path through the corners with rounded turns. */
export function workflowEdgePathThrough(corners: CanvasPoint[], radius = 12): string {
  if (corners.length === 0) return "";
  let path = `M ${corners[0].x} ${corners[0].y}`;
  for (let index = 1; index < corners.length; index += 1) {
    const point = corners[index];
    const next = corners[index + 1];
    if (!next) {
      path += ` L ${point.x} ${point.y}`;
      break;
    }
    const before = corners[index - 1];
    const inLength = Math.hypot(point.x - before.x, point.y - before.y);
    const outLength = Math.hypot(next.x - point.x, next.y - point.y);
    const turn = Math.min(radius, inLength / 2, outLength / 2);
    const enter = {
      x: point.x - ((point.x - before.x) / (inLength || 1)) * turn,
      y: point.y - ((point.y - before.y) / (inLength || 1)) * turn,
    };
    const leave = {
      x: point.x + ((next.x - point.x) / (outLength || 1)) * turn,
      y: point.y + ((next.y - point.y) / (outLength || 1)) * turn,
    };
    path += ` L ${enter.x} ${enter.y} Q ${point.x} ${point.y} ${leave.x} ${leave.y}`;
  }
  return path;
}

/** The point halfway along the line (for its label and controls). */
export function workflowEdgeMidpoint(corners: CanvasPoint[]): CanvasPoint {
  const lengths = corners.slice(1).map((point, index) => (
    Math.hypot(point.x - corners[index].x, point.y - corners[index].y)
  ));
  let remaining = lengths.reduce((sum, length) => sum + length, 0) / 2;
  for (let index = 0; index < lengths.length; index += 1) {
    if (remaining <= lengths[index]) {
      const ratio = lengths[index] ? remaining / lengths[index] : 0;
      const from = corners[index];
      const to = corners[index + 1];
      return { x: from.x + (to.x - from.x) * ratio, y: from.y + (to.y - from.y) * ratio };
    }
    remaining -= lengths[index];
  }
  return corners[corners.length - 1] || { x: 0, y: 0 };
}

function distanceToSegment(point: CanvasPoint, from: CanvasPoint, to: CanvasPoint): number {
  const dx = to.x - from.x;
  const dy = to.y - from.y;
  const length = dx * dx + dy * dy;
  const t = length ? Math.max(0, Math.min(1, ((point.x - from.x) * dx + (point.y - from.y) * dy) / length)) : 0;
  return Math.hypot(point.x - (from.x + t * dx), point.y - (from.y + t * dy));
}

/**
 * Where a new waypoint grabbed at `point` goes in the waypoint list: after
 * the anchor whose stretch of line passes closest to the point.
 */
export function workflowEdgeWaypointInsertIndex(
  source: CanvasPoint,
  waypoints: CanvasPoint[],
  target: CanvasPoint,
  point: CanvasPoint,
): number {
  const anchors = [source, ...waypoints, target];
  let best = 0;
  let bestDistance = Number.POSITIVE_INFINITY;
  for (let index = 1; index < anchors.length; index += 1) {
    const corners = [
      anchors[index - 1],
      ...stretchCorners(anchors[index - 1], anchors[index], index === anchors.length - 1),
    ];
    for (let corner = 1; corner < corners.length; corner += 1) {
      const distance = distanceToSegment(point, corners[corner - 1], corners[corner]);
      if (distance < bestDistance) {
        bestDistance = distance;
        best = index - 1;
      }
    }
  }
  return best;
}
